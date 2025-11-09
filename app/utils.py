
import secrets
import string
import shutil
import gc
import asyncio
import aiofiles
from pathlib import Path
from typing import Optional
import httpx
import base64
import re

try:
    from config import (
        WEB_SERVER_URL_PRIMARY, WEB_SERVER_URL_FALLBACK, WEB_SERVER_SECRET,
        R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME,
        R2_ENDPOINT_URL, R2_PUBLIC_URL
    )
except ImportError:
    from .config import (
        WEB_SERVER_URL_PRIMARY, WEB_SERVER_URL_FALLBACK, WEB_SERVER_SECRET,
        R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME,
        R2_ENDPOINT_URL, R2_PUBLIC_URL
    )

_current_server_url = None

def generate_model_id() -> str:
    """Generate a unique 12-character model ID"""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(12))

async def upload_gltf_to_server(gltf_path: str, model_id: str, build_filename: Optional[str] = None, build_size: Optional[int] = None, build_hash: Optional[str] = None) -> Optional[str]:
    """
    Upload GLTF file to the web server (memory-efficient for Railway)
    Returns the viewer URL if successful, None otherwise
    For files > 2MB, uploads directly to R2 (bypasses web server file size limits)
    """
    try:
        import os
        # Read file asynchronously in chunks to avoid loading entire file into memory
        file_size = os.path.getsize(gltf_path)
        
        # For files > 2MB, upload directly to R2 (bypasses PythonAnywhere 2MB limit and Vercel 4.5MB limit)
        # This allows files up to 100MB on free tier
        if file_size > 2 * 1024 * 1024:  # 2MB
            print(f"File size ({file_size / 1024 / 1024:.1f}MB) exceeds web server limits, uploading directly to R2")
            # Upload directly to R2
            r2_url = await upload_gltf_direct_to_r2(gltf_path, model_id)
            if not r2_url:
                print("Direct R2 upload failed, falling back to web server upload")
                # Fall back to web server upload (will try Vercel)
                return await _upload_via_web_server(gltf_path, model_id, build_filename, build_size, build_hash)
            
            # Register model with backend using R2 URL
            viewer_url = await register_model_with_r2_url(
                model_id, r2_url, build_filename, build_size, build_hash
            )
            return viewer_url
        else:
            # For files <= 2MB, use web server upload (faster for small files)
            return await _upload_via_web_server(gltf_path, model_id, build_filename, build_size, build_hash)
    except Exception as e:
        print(f"Error uploading GLTF: {e}")
        import traceback
        traceback.print_exc()
        return None

async def _upload_via_web_server(gltf_path: str, model_id: str, build_filename: Optional[str] = None, build_size: Optional[int] = None, build_hash: Optional[str] = None) -> Optional[str]:
    """
    Upload GLTF file via web server (for files <= 2MB)
    Returns the viewer URL if successful, None otherwise
    """
    try:
        import os
        file_size = os.path.getsize(gltf_path)
        
        # Use async file reading for better concurrency
        if file_size < 10 * 1024 * 1024:
            # For small files (<10MB), read directly
            gltf_data = await read_file_async(Path(gltf_path))
        else:
            # For larger files, read in chunks asynchronously
            gltf_data = b''
            async with aiofiles.open(gltf_path, 'rb') as f:
                while True:
                    chunk = await f.read(1024 * 1024)  # 1MB chunks
                    if not chunk:
                        break
                    gltf_data += chunk
        
        files = {
            'gltf': (f"{model_id}.gltf", gltf_data, 'model/gltf+json')
        }
        
        data = {
            'model_id': model_id,
            'expires_in': 600  # 10 minutes
        }
        
        # Add build file metadata for caching (using SHA-1 hash)
        if build_filename and build_size:
            data['build_filename'] = build_filename
            data['build_size'] = str(build_size)
        if build_hash:
            data['build_hash'] = build_hash
        
        headers = {
            'X-API-Secret': WEB_SERVER_SECRET
        }
        
        # Get active server URL (with fallback)
        server_url = await get_active_server_url()
        
        # Increase timeout for larger files
        timeout = 120.0 if file_size > 10 * 1024 * 1024 else 60.0
        
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                response = await client.post(
                    f"{server_url}/api/upload",
                    files=files,
                    data=data,
                    headers=headers
                )
                response.raise_for_status()
                result = response.json()
                # Clear data from memory
                del gltf_data
                
                viewer_url = result.get('url')
                
                # Preview will be generated client-side by viewer.js
                # No server-side generation needed
                
                return viewer_url
            except httpx.HTTPStatusError as e:
                # If we get a 413 (Payload Too Large) or 502/503 (Web Server Unavailable)
                # try fallback
                if e.response.status_code in (413, 502, 503):
                    print(f"Primary server rejected upload (status {e.response.status_code}), trying Vercel fallback")
                    server_url = WEB_SERVER_URL_FALLBACK
                    # Retry with fallback
                    response = await client.post(
                        f"{server_url}/api/upload",
                        files=files,
                        data=data,
                        headers=headers
                    )
                    response.raise_for_status()
                    result = response.json()
                    del gltf_data
                    return result.get('url')
                else:
                    raise
    except Exception as e:
        print(f"Error uploading GLTF: {e}")
        import traceback
        traceback.print_exc()
        return None


# Preview generation is now handled client-side by viewer.js
# No server-side preview generation needed

async def check_web_server_health(server_url: Optional[str] = None) -> bool:
    """Check if the web server is available"""
    url = server_url or WEB_SERVER_URL_PRIMARY
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{url}/health")
            return response.status_code == 200
    except:
        return False

async def get_active_server_url() -> str:
    """Get the active server URL (primary if available, fallback otherwise)"""
    global _current_server_url
    
    # If we have a cached active URL, check if it's still working
    if _current_server_url:
        if await check_web_server_health(_current_server_url):
            return _current_server_url
        else:
            # Cached URL is down, clear it
            _current_server_url = None
    
    # Try primary server first
    if await check_web_server_health(WEB_SERVER_URL_PRIMARY):
        _current_server_url = WEB_SERVER_URL_PRIMARY
        return WEB_SERVER_URL_PRIMARY
    
    # Primary failed, try fallback
    if await check_web_server_health(WEB_SERVER_URL_FALLBACK):
        _current_server_url = WEB_SERVER_URL_FALLBACK
        return WEB_SERVER_URL_FALLBACK
    
    # Both failed, return primary anyway (will fail gracefully)
    return WEB_SERVER_URL_PRIMARY

async def write_file_async(file_path: Path, content: bytes):
    """Write file asynchronously to avoid blocking"""
    async with aiofiles.open(file_path, 'wb') as f:
        await f.write(content)

async def read_file_async(file_path: Path) -> bytes:
    """Read file asynchronously to avoid blocking"""
    async with aiofiles.open(file_path, 'rb') as f:
        return await f.read()

def calculate_memory_usage(build_file_size: int) -> int:
    """
    Estimate memory usage for rendering a build file
    Returns estimated memory in bytes
    """
    # Base memory overhead: ~10MB
    base_memory = 10 * 1024 * 1024
    
    # Build file in memory: file_size
    build_memory = build_file_size
    
    # GLTF file is typically 2-5x larger than build file
    gltf_memory = build_file_size * 3
    
    # Renderer overhead: ~5MB
    renderer_memory = 5 * 1024 * 1024
    
    # Total estimate
    total = base_memory + build_memory + gltf_memory + renderer_memory
    
    return total

def cleanup_temp_files(path: Path):
    """Clean up temporary files and directories"""
    try:
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except:
        pass

def force_garbage_collection():
    """Force garbage collection to free memory (Railway optimization)"""
    gc.collect()

async def get_usage_stats() -> dict:
    """Get R2 usage stats from backend (no R2 API call - from local tracker)"""
    server_url = await get_active_server_url()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{server_url}/health")
            if response.status_code == 200:
                data = response.json()
                return data.get('r2_usage', {})
    except:
        pass
    return {}

async def get_cached_builds() -> Optional[dict]:
    """Get all cached builds from backend"""
    server_url = await get_active_server_url()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{server_url}/api/builds",
                headers={
                    'X-API-Secret': WEB_SERVER_SECRET
                }
            )
            if response.status_code == 200:
                return response.json()
    except Exception as e:
        print(f"Error getting cached builds: {e}")
    return None

def calculate_build_hash(build_content: bytes) -> str:
    """Calculate SHA-1 hash of build file content for deterministic caching"""
    import hashlib
    return hashlib.sha1(build_content).hexdigest()

async def check_build_cache(build_hash: str) -> Optional[dict]:
    """Check if a build file is cached using SHA-1 hash (avoids re-rendering)"""
    server_url = await get_active_server_url()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{server_url}/api/check-cache",
                json={
                    'hash': build_hash
                },
                headers={
                    'X-API-Secret': WEB_SERVER_SECRET,
                    'Content-Type': 'application/json'
                }
            )
            if response.status_code == 200:
                data = response.json()
                if data.get('cached'):
                    return data
    except Exception as e:
        print(f"Error checking cache: {e}")
    return None

async def delete_model_from_backend(model_id: str) -> bool:
    """Delete a model from backend (R2 and cache)"""
    server_url = await get_active_server_url()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{server_url}/api/delete",
                json={'model_id': model_id},
                headers={
                    'X-API-Secret': WEB_SERVER_SECRET,
                    'Content-Type': 'application/json'
                }
            )
            if response.status_code == 200:
                return True
    except Exception as e:
        print(f"Error deleting model: {e}")
    return False

async def upload_gltf_direct_to_r2(gltf_path: str, model_id: str) -> Optional[str]:
    """
    Upload GLTF file directly to R2 from the bot (bypasses web server file size limits)
    Returns the public R2 URL if successful, None otherwise
    Supports files up to 100MB (R2 free tier limit is much higher)
    """
    try:
        import boto3
        from botocore.exceptions import ClientError
        import os
        
        file_size = os.path.getsize(gltf_path)
        print(f"Uploading {model_id}.gltf directly to R2 (size: {file_size / 1024 / 1024:.1f}MB)")
        
        # Run boto3 operations in executor to avoid blocking
        loop = asyncio.get_event_loop()
        
        def _upload_to_r2():
            # Create S3 client for R2
            s3_client = boto3.client(
                's3',
                endpoint_url=R2_ENDPOINT_URL,
                aws_access_key_id=R2_ACCESS_KEY_ID,
                aws_secret_access_key=R2_SECRET_ACCESS_KEY,
                region_name='auto'
            )
            
            key = f"{model_id}.gltf"
            
            # Upload file using streaming to avoid loading entire file into memory
            with open(gltf_path, 'rb') as file_obj:
                s3_client.upload_fileobj(
                    file_obj,
                    R2_BUCKET_NAME,
                    key,
                    ExtraArgs={
                        'ContentType': 'model/gltf+json',
                        'CacheControl': 'public, max-age=31536000'  # 1 year cache
                    }
                )
            
            return f"{R2_PUBLIC_URL}/{key}"
        
        # Run upload in executor
        public_url = await loop.run_in_executor(None, _upload_to_r2)
        print(f"Successfully uploaded {model_id}.gltf directly to R2: {public_url}")
        return public_url
        
    except ClientError as e:
        print(f"R2 direct upload error: {e}")
        return None
    except Exception as e:
        print(f"Unexpected error uploading directly to R2: {e}")
        import traceback
        traceback.print_exc()
        return None

async def register_model_with_r2_url(model_id: str, r2_url: str, build_filename: Optional[str] = None, build_size: Optional[int] = None, build_hash: Optional[str] = None, preview_url: Optional[str] = None) -> Optional[str]:
    """
    Register a model with the backend using an R2 URL (file already uploaded to R2)
    Returns the viewer URL if successful, None otherwise
    """
    try:
        server_url = await get_active_server_url()
        
        data = {
            'model_id': model_id,
            'r2_url': r2_url,
            'expires_in': 600  # 10 minutes
        }
        
        # Add build file metadata for caching
        if build_filename and build_size:
            data['build_filename'] = build_filename
            data['build_size'] = str(build_size)
        if build_hash:
            data['build_hash'] = build_hash
        if preview_url:
            data['preview_url'] = preview_url
        
        headers = {
            'X-API-Secret': WEB_SERVER_SECRET
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{server_url}/api/register",
                json=data,
                headers=headers
            )
            response.raise_for_status()
            result = response.json()
            
            viewer_url = result.get('url')
            return viewer_url
            
    except Exception as e:
        print(f"Error registering model with R2 URL: {e}")
        import traceback
        traceback.print_exc()
        return None

async def generate_preview_with_flowkit(model_id: str, gltf_url: str) -> Optional[str]:
    """
    Generate preview using Flowkit API and upload to R2
    Returns preview URL if successful, None otherwise
    Based on test_cframe.py logic
    """
    try:
        # Flowkit snapshot endpoint with parameters:
        #   rh = horizontal rotation (-45)
        #   rv = vertical rotation (15)
        #   s  = size (512)
        #   sh = shadows (false = disable)
        #   bg = background color in hex (000000 = black)
        flowkit_url = f"https://www.flowkit.app/s/demo/r/rh:145,rv:30,s:512/u/{gltf_url}"
        
        print(f"[Preview] Generating preview for {model_id} using Flowkit: {flowkit_url}")
        
        # Fetch preview from Flowkit
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(flowkit_url)
            response.raise_for_status()
            
            # Extract image data
            img_data = None
            content_type = response.headers.get("Content-Type", "")
            if content_type.startswith("image/"):
                img_data = response.content
            else:
                text = response.text
                if "data:image/png;base64," in text:
                    b64data = re.search(r"data:image/png;base64,([A-Za-z0-9+/=]+)", text).group(1)
                    img_data = base64.b64decode(b64data)
                else:
                    raise ValueError("Unexpected response type from Flowkit")
            
            if not img_data:
                raise ValueError("Failed to extract image data from Flowkit response")
            
            print(f"[Preview] Preview generated, size: {len(img_data)} bytes")
            
            # Upload preview to R2
            preview_url = await upload_preview_to_r2(model_id, img_data)
            return preview_url
            
    except httpx.RequestError as e:
        print(f"[Preview] Flowkit request error for {model_id}: {e}")
        return None
    except Exception as e:
        print(f"[Preview] Error generating preview for {model_id}: {e}")
        import traceback
        traceback.print_exc()
        return None

async def upload_preview_to_r2(model_id: str, img_data: bytes) -> Optional[str]:
    """
    Upload preview image to R2
    Returns preview URL if successful, None otherwise
    """
    try:
        import boto3
        from botocore.exceptions import ClientError
        from io import BytesIO
        
        print(f"Uploading preview for {model_id} to R2 (size: {len(img_data)} bytes)")
        
        # Run boto3 operations in executor to avoid blocking
        loop = asyncio.get_event_loop()
        
        def _upload_preview():
            # Create S3 client for R2
            s3_client = boto3.client(
                's3',
                endpoint_url=R2_ENDPOINT_URL,
                aws_access_key_id=R2_ACCESS_KEY_ID,
                aws_secret_access_key=R2_SECRET_ACCESS_KEY,
                region_name='auto'
            )
            
            preview_key = f"{model_id}_preview.png"
            
            # Upload preview using BytesIO
            img_buffer = BytesIO(img_data)
            s3_client.upload_fileobj(
                img_buffer,
                R2_BUCKET_NAME,
                preview_key,
                ExtraArgs={
                    'ContentType': 'image/png',
                    'CacheControl': 'public, max-age=31536000'  # 1 year cache
                }
            )
            
            return f"{R2_PUBLIC_URL}/{preview_key}"
        
        # Run upload in executor
        preview_url = await loop.run_in_executor(None, _upload_preview)
        print(f"Successfully uploaded preview for {model_id} to R2: {preview_url}")
        return preview_url
        
    except ClientError as e:
        print(f"R2 preview upload error: {e}")
        return None
    except Exception as e:
        print(f"Unexpected error uploading preview to R2: {e}")
        import traceback
        traceback.print_exc()
        return None

async def check_preview_ready(gltf_url: str) -> bool:
    """
    Check if Flowkit preview is ready by attempting to fetch it
    Returns True if preview is ready, False otherwise
    """
    try:
        flowkit_url = f"https://www.flowkit.app/s/demo/r/rh:-45,rv:15,s:512,sh:false,bg:000000/u/{gltf_url}"
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(flowkit_url)
            if response.status_code == 200:
                content_type = response.headers.get("Content-Type", "")
                if content_type.startswith("image/"):
                    return True
                text = response.text
                if "data:image/png;base64," in text:
                    return True
        return False
    except:
        return False

