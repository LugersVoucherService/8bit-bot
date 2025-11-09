
import secrets
import string
import shutil
import gc
import asyncio
import aiofiles
from pathlib import Path
from typing import Optional
import httpx

try:
    from config import WEB_SERVER_URL_PRIMARY, WEB_SERVER_URL_FALLBACK, WEB_SERVER_SECRET
except ImportError:
    from .config import WEB_SERVER_URL_PRIMARY, WEB_SERVER_URL_FALLBACK, WEB_SERVER_SECRET

_current_server_url = None

def generate_model_id() -> str:
    """Generate a unique 12-character model ID"""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(12))

async def upload_gltf_to_server(gltf_path: str, model_id: str, build_filename: Optional[str] = None, build_size: Optional[int] = None, build_hash: Optional[str] = None) -> Optional[str]:
    """
    Upload GLTF file to the web server (memory-efficient for Railway)
    Returns the viewer URL if successful, None otherwise
    """
    try:
        import os
        # Read file asynchronously in chunks to avoid loading entire file into memory
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
        
        async with httpx.AsyncClient(timeout=60.0) as client:
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
    except Exception as e:
        print(f"Error uploading GLTF: {e}")
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

