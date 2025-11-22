import discord
from discord import app_commands
from discord.ext import commands
from pathlib import Path
import shutil
import gc
import sys
import os
import asyncio
import math
import platform
import psutil
import time
import random
from datetime import datetime, timedelta
import httpx
import re
import requests
import base64
from urllib.parse import urlparse, parse_qs, unquote
from io import BytesIO


app_dir = os.path.dirname(os.path.abspath(__file__))
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)

from config import (
    DISCORD_BOT_TOKEN,
    WEB_SERVER_URL_PRIMARY,
    WEB_SERVER_URL_FALLBACK,
    WEB_SERVER_SECRET,
    TEMP_DIR,
    MAX_BUILD_FILE_SIZE
)

# Guild and User Restrictions
ALLOWED_GUILD_ID = 1434376307734745092
OWNER_ID = 1149910630678134916
STAFF_ROLE_ID = 1436606654924984370  # 8Bit Staff
DEV_ROLE_ID = 1434956294984437942  # Developer role
COOLDOWN_EXEMPT_ROLE_ID = 1436840291079557270  # Role immune to cooldowns

from utils import (
    generate_model_id,
    upload_gltf_to_server,
    check_web_server_health,
    cleanup_temp_files,
    force_garbage_collection,
    get_usage_stats,
    get_cached_builds,
    delete_model_from_backend,
    get_active_server_url
)
from renderer import GLTFRenderer

for item in TEMP_DIR.glob("*"):
    cleanup_temp_files(item)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="*", intents=intents)
tree = bot.tree  # Use the bot's built-in tree

_commands_registered = False
_bot_start_time = None

def generate_preview(model_id: str, gltf_url: str):
    try:
        flowkit_url = f"https://www.flowkit.app/s/demo/r/rh:-45,rv:15,s:512/u/{gltf_url}"
        print(f"[Flowkit] Fetching {flowkit_url}")
        resp = requests.get(flowkit_url, timeout=60)
        resp.raise_for_status()

        if resp.headers.get("Content-Type", "").startswith("image/"):
            img_data = resp.content
        else:
            match = re.search(r"data:image/png;base64,([A-Za-z0-9+/=]+)", resp.text)
            if not match:
                print("No base64 image in Flowkit response.")
                return None
            img_data = base64.b64decode(match.group(1))

        files = {"preview": (f"{model_id}_preview.png", img_data, "image/png")}
        data = {"model_id": model_id}
        headers = {"X-API-Secret": API_KEY}
        r = requests.post(f"{API_BASE_URL}/api/upload-preview", files=files, data=data, headers=headers)

        if r.ok:
            url = r.json().get("preview_url")
            print(f"✅ Uploaded preview to R2: {url}")
            return url
        else:
            print(f"Upload failed: {r.status_code} {r.text}")
            return None

    except Exception as e:
        print(f"❌ Flowkit generation failed: {e}")
        return None

def has_member_access(user) -> bool:
    """Check if user has member-level access (owner or staff role)"""
    if user.id == OWNER_ID:
        return True
    # Check if user is a Member (has roles) and is in the correct guild
    if isinstance(user, discord.Member):
        if user.guild and user.guild.id == ALLOWED_GUILD_ID:
            return any(role.id == STAFF_ROLE_ID for role in user.roles)
    return False

def has_dev_access(user) -> bool:
    """Check if user has developer-level access (owner or dev role)"""
    if user.id == OWNER_ID:
        return True
    # Check if user is a Member (has roles) and is in the correct guild
    if isinstance(user, discord.Member):
        if user.guild and user.guild.id == ALLOWED_GUILD_ID:
            return any(role.id == DEV_ROLE_ID for role in user.roles)
    return False

def is_cooldown_exempt(user) -> bool:
    """Check if user is exempt from cooldowns"""
    if user.id == OWNER_ID:
        return True
    # Check if user has the cooldown exempt role
    if isinstance(user, discord.Member):
        if user.guild and user.guild.id == ALLOWED_GUILD_ID:
            return any(role.id == COOLDOWN_EXEMPT_ROLE_ID for role in user.roles)
    return False

# Store cooldowns per command
_cooldown_storage = {}

def cooldown_with_exemption(rate: int, per: float, key=None):
    """
    Custom cooldown check that exempts certain users
    """
    # Create a unique identifier for this cooldown
    cooldown_id = f"{rate}_{per}_{id(key) if key else 'default'}"
    
    async def predicate(interaction: discord.Interaction):
        # Check if user is exempt
        if is_cooldown_exempt(interaction.user):
            return True  # Exempt users bypass cooldown
        
        # Get or create cooldown for this command
        if cooldown_id not in _cooldown_storage:
            _cooldown_storage[cooldown_id] = app_commands.Cooldown(rate, per)
        
        check = _cooldown_storage[cooldown_id]
        
        # Apply normal cooldown check
        if key is None:
            check_key = (interaction.guild_id, interaction.user.id)
        else:
            check_key = key(interaction)
        
        retry_after = check.update_rate_limit(check_key)
        if retry_after:
            raise app_commands.CommandOnCooldown(check, retry_after)
        return True
    
    return app_commands.check(predicate)

@tree.command(name="render", description="Render a build file to 3D and get a temporary viewer link", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(
    build_file="Build file (.Build or .build) to render (optional if using index)",
    index="Index of cached build from /builds (optional if uploading file)"
)
@cooldown_with_exemption(1, 30.0, key=lambda i: (i.guild_id, i.user.id))  # 30 second cooldown per user (exempt role bypasses)
async def render_command(
    interaction: discord.Interaction,
    build_file: discord.Attachment = None,
    index: int = None
):
    if not has_member_access(interaction.user):
        embed = discord.Embed(
            title="Access Denied",
            description="You don't have permission to use this command.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    # Send immediate response to show the bot is working
    preparing_embed = discord.Embed(
        title="Rendering Your Build",
        description="Rendering your build, this may take a while.",
        color=0x5865F2
    )
    await interaction.response.send_message(embed=preparing_embed)
    
    # Get the message we just sent so we can edit it later
    preparing_message = await interaction.original_response()
    
    # If index is provided, render from cache
    if index is not None:
        try:
            builds_data = await get_cached_builds()
            if not builds_data:
                embed = discord.Embed(
                    title="Error",
                    description="Unable to retrieve cached builds. Make sure the backend server is running.",
                    color=0xED4245
                )
                await preparing_message.edit(embed=embed)
                return
            
            builds = builds_data.get('builds', [])
            total_builds = len(builds)
            
            if total_builds == 0:
                embed = discord.Embed(
                    title="No Cached Builds",
                    description="No cached builds found. Please upload a build file instead.",
                    color=0xED4245
                )
                await preparing_message.edit(embed=embed)
                return
            
            # Convert 1-based index to 0-based
            if index < 1 or index > total_builds:
                embed = discord.Embed(
                    title="Invalid Index",
                    description=f"Index must be between 1 and {total_builds}. Use `/builds` to see available builds.",
                    color=0xED4245
                )
                await preparing_message.edit(embed=embed)
                return
            
            # Get the build at the specified index (1-based to 0-based conversion)
            build = builds[index - 1]
            model_id = build.get('id', 'Unknown')
            
            if not model_id or model_id == 'Unknown':
                embed = discord.Embed(
                    title="Error",
                    description="Invalid build data. The cached build may be corrupted.",
                    color=0xED4245
                )
                await preparing_message.edit(embed=embed)
                return
            
            # Get viewer URL from model_id
            server_url = await get_active_server_url()
            viewer_url = f"{server_url}/model?model_id={model_id}"
            
            usage_stats = await get_usage_stats()
            expiry_timestamp = int((datetime.now() + timedelta(minutes=10)).timestamp())
            
            filename = build.get('filename', 'Unknown')
            embed = discord.Embed(
                title="Build Rendered",
                description=f"**Viewer:** [Open 3D Model]({viewer_url})\n\n**Build:** {filename}\n**Model ID:** `{model_id}`\n\nExpires <t:{expiry_timestamp}:R>",
                color=0x5865F2,
                timestamp=datetime.now()
            )
            
            storage_pct = usage_stats.get('storage_percent', 0)
            a_class_pct = usage_stats.get('a_class_percent', 0)
            b_class_pct = usage_stats.get('b_class_percent', 0)
            embed.set_footer(
                text=f"Storage: {storage_pct:.1f}% | A-class: {a_class_pct:.2f}% | B-class: {b_class_pct:.2f}%"
            )
            
            await preparing_message.edit(embed=embed)
            return
            
        except Exception as e:
            embed = discord.Embed(
                title="Error",
                description=f"An error occurred while loading cached build: {str(e)}",
                color=0xED4245
            )
            await preparing_message.edit(embed=embed)
            return
    
    # If no index and no file, show error
    if build_file is None:
        embed = discord.Embed(
            title="Missing Input",
            description="Please provide either a build file attachment or an index number from `/builds`.",
            color=0xED4245
        )
        await preparing_message.edit(embed=embed)
        return
    
    # Original file upload logic
    if not build_file.filename.lower().endswith(('.build', '.Build')):
        embed = discord.Embed(
            title="Invalid File",
            description="Please upload a .Build or .build file.",
            color=0xED4245
        )
        await preparing_message.edit(embed=embed)
        return
    
    if build_file.size > MAX_BUILD_FILE_SIZE:
        embed = discord.Embed(
            title="File Too Large",
            description=f"File size ({build_file.size / 1024 / 1024:.1f}MB) exceeds limit ({MAX_BUILD_FILE_SIZE / 1024 / 1024:.0f}MB).\nPlease use a smaller build file.",
            color=0xED4245
        )
        await preparing_message.edit(embed=embed)
        return
    
    try:
        from utils import check_build_cache, write_file_async, calculate_memory_usage, calculate_build_hash
        import psutil

        # Read build file content once and calculate SHA-1 hash
        build_content = await build_file.read()
        build_hash = calculate_build_hash(build_content)
        print(f"[Cache] Build hash: {build_hash[:16]}... for {build_file.filename}")
        
        # Check cache first using SHA-1 hash
        cached = await check_build_cache(build_hash)

        if cached:
            model_id = cached['model_id']
            # Get viewer URL directly from cache (gltf_url) - no API call needed!
            gltf_url = cached.get('gltf_url', cached.get('url'))
            # Construct viewer URL from model_id (gltf_url is stored but we use model_id for viewer)
            server_url = await get_active_server_url()
            viewer_url = f"{server_url}/model?model_id={model_id}"
            print(f"Cache hit: {build_file.filename} ({build_file.size} bytes) -> {model_id} (reused, no render, no R2 API call)")
        else:
            # Check memory before processing
            memory = psutil.virtual_memory()
            estimated_memory = calculate_memory_usage(build_file.size)

            # If memory is too high, wait a bit and check cache again (another user might have uploaded)
            if memory.percent > 85:
                await asyncio.sleep(0.5)  # Brief wait for concurrent uploads
                cached = await check_build_cache(build_hash)
                if cached:
                    model_id = cached['model_id']
                    server_url = await get_active_server_url()
                    viewer_url = f"{server_url}/model?model_id={model_id}"
                    print(f"Cache hit after wait: {build_file.filename} ({build_file.size} bytes) -> {model_id}")
                else:
                    # Still no cache, proceed with render
                    build_path = TEMP_DIR / f"temp_{build_file.filename}"
                    await write_file_async(build_path, build_content)

                    model_id = generate_model_id()
                    renderer = GLTFRenderer(str(build_path))
                    renderer.parse_build_file()

                    if len(renderer.positions) == 0:
                        embed = discord.Embed(
                            title="Render Error",
                            description="No blocks found in build file.",
                            color=0xED4245
                        )
                        await preparing_message.edit(embed=embed)
                        cleanup_temp_files(build_path)
                        return

                    gltf_dir = TEMP_DIR / model_id
                    gltf_dir.mkdir(exist_ok=True)
                    gltf_path = gltf_dir / f"{model_id}.gltf"

                    center, max_size = renderer.export_to_gltf(str(gltf_path))

                    html_content = renderer.create_viewer_html(
                        f"{model_id}.gltf",
                        center,
                        max_size,
                        port=8000
                    )
                    html_path = gltf_dir / "index.html"
                    await write_file_async(html_path, html_content.encode('utf-8'))

                    # Check cache one more time before upload (catch concurrent duplicates)
                    cached = await check_build_cache(build_hash)
                    if cached:
                        model_id = cached['model_id']
                        server_url = await get_active_server_url()
                        viewer_url = f"{server_url}/model?model_id={model_id}"
                        print(f"Cache hit before upload: {build_file.filename} ({build_file.size} bytes) -> {model_id} (skipped R2 upload)")
                        cleanup_temp_files(build_path)
                        cleanup_temp_files(gltf_dir)
                    else: #1
                        # Upload (low-memory path)
                        viewer_url = await upload_gltf_to_server(
                            str(gltf_path),
                            model_id,
                            build_filename=build_file.filename,
                            build_size=build_file.size,
                            build_hash=build_hash
                        )
                        cleanup_temp_files(build_path)
                        cleanup_temp_files(gltf_dir)
            else:
                # Memory is fine, proceed normally
                build_path = TEMP_DIR / f"temp_{build_file.filename}"
                await write_file_async(build_path, build_content)

                model_id = generate_model_id()
                renderer = GLTFRenderer(str(build_path))
                renderer.parse_build_file()

                if len(renderer.positions) == 0:
                    embed = discord.Embed(
                        title="Render Error",
                        description="No blocks found in build file.",
                        color=0xED4245
                    )
                    await preparing_message.edit(embed=embed)
                    cleanup_temp_files(build_path)
                    return

                gltf_dir = TEMP_DIR / model_id
                gltf_dir.mkdir(exist_ok=True)
                gltf_path = gltf_dir / f"{model_id}.gltf"

                center, max_size = renderer.export_to_gltf(str(gltf_path))

                html_content = renderer.create_viewer_html(
                    f"{model_id}.gltf",
                    center,
                    max_size,
                    port=8000
                )
                html_path = gltf_dir / "index.html"
                await write_file_async(html_path, html_content.encode('utf-8'))

                # Check cache one more time before upload (catch concurrent duplicates)
                cached = await check_build_cache(build_hash)
                if cached:
                    model_id = cached['model_id']
                    server_url = await get_active_server_url()
                    viewer_url = f"{server_url}/model?model_id={model_id}"
                    print(f"Cache hit before upload: {build_file.filename} ({build_file.size} bytes) -> {model_id} (skipped R2 upload)")
                    cleanup_temp_files(build_path)
                    cleanup_temp_files(gltf_dir)
                else: #2
                    # Upload (low-memory path)
                    viewer_url = await upload_gltf_to_server(
                        str(gltf_path),
                        model_id,
                        build_filename=build_file.filename,
                        build_size=build_file.size,
                        build_hash=build_hash
                    )
                    cleanup_temp_files(build_path)
                    cleanup_temp_files(gltf_dir)
        if not viewer_url:
            server_available = await check_web_server_health()
            if server_available:
                server_url = await get_active_server_url()
                viewer_url = f"{server_url}/model?model_id={model_id}"
            else:
                embed = discord.Embed(
                    title="Web Server Unavailable",
                    description="The web server is currently unavailable. Please try again later.",
                    color=0xED4245
                )
                await preparing_message.edit(embed=embed)
                cleanup_temp_files(build_path)
                cleanup_temp_files(gltf_dir)
                force_garbage_collection()
                return

        usage_stats = await get_usage_stats()
        expiry_timestamp = int((datetime.now() + timedelta(minutes=10)).timestamp())
        
        # Get preview URL if available (from cache)
        preview_url = None
        if cached:
            preview_url = cached.get('preview_url')
        
        # Create embed with viewer link first (preview will be added async)
        embed = discord.Embed(
            title="Build Rendered",
            description=f"**Viewer:** [Open 3D Model]({viewer_url})\n\nExpires <t:{expiry_timestamp}:R>",
            color=0x5865F2,
            timestamp=datetime.now()
        )
        
        # Add preview image if available from cache
        storage_pct = usage_stats.get('storage_percent', 0)
        a_class_pct = usage_stats.get('a_class_percent', 0)
        b_class_pct = usage_stats.get('b_class_percent', 0)
        
        if preview_url:
            embed.set_image(url=preview_url)
            embed.set_footer(
                text=f"Storage: {storage_pct:.1f}% | A-class: {a_class_pct:.2f}% | B-class: {b_class_pct:.2f}%"
            )
        else:
            embed.set_footer(
                text=f"Preview loading... | Storage: {storage_pct:.1f}% | A-class: {a_class_pct:.2f}% | B-class: {b_class_pct:.2f}%"
            )

        # Send embed with model link first
        await preparing_message.edit(embed=embed)
        
        # Generate preview asynchronously if not in cache
        if not preview_url:
            async def generate_and_update_preview():
                try:
                    # Construct R2 URL for the GLTF file
                    from config import R2_PUBLIC_URL
                    gltf_url = f"{R2_PUBLIC_URL}/{model_id}.gltf"
                    
                    # Check if preview is ready from Flowkit (Flowkit caches renders, so this is fast)
                    from utils import check_preview_ready, generate_preview_with_flowkit
                    
                    # Wait a bit for Flowkit to process if needed, then check
                    max_attempts = 10
                    attempt = 0
                    preview_ready = False
                    
                    while attempt < max_attempts and not preview_ready:
                        await asyncio.sleep(1)  # Wait 1 second between checks
                        preview_ready = await check_preview_ready(gltf_url)
                        attempt += 1
                    
                    if preview_ready:
                        # Generate and upload preview (Flowkit will use its cache if available)
                        print(f"[Bot] Generating preview for {model_id}...")
                        generated_preview_url = await generate_preview_with_flowkit(model_id, gltf_url)
                        
                        if generated_preview_url:
                            # Update cache with preview URL via API
                            try:
                                server_url = await get_active_server_url()
                                async with httpx.AsyncClient(timeout=10.0) as client:
                                    response = await client.post(
                                        f"{server_url}/api/generate-preview",
                                        json={
                                            'model_id': model_id,
                                            'gltf_url': gltf_url
                                        },
                                        headers={
                                            'X-API-Secret': WEB_SERVER_SECRET,
                                            'Content-Type': 'application/json'
                                        }
                                    )
                            except Exception as e:
                                print(f"[Bot] Error updating cache with preview: {e}")
                            
                            print(f"[Bot] Preview generated for {model_id}: {generated_preview_url}")
                            
                            # Update embed with preview
                            new_embed = discord.Embed(
                                title="Build Rendered",
                                description=f"**Viewer:** [Open 3D Model]({viewer_url})\n\nExpires <t:{expiry_timestamp}:R>",
                                color=0x5865F2,
                                timestamp=datetime.now()
                            )
                            new_embed.set_image(url=generated_preview_url)
                            new_embed.set_footer(
                                text=f"Storage: {storage_pct:.1f}% | A-class: {a_class_pct:.2f}% | B-class: {b_class_pct:.2f}%"
                            )
                            await preparing_message.edit(embed=new_embed)
                        else:
                            # Preview generation failed, update footer
                            new_embed = discord.Embed(
                                title="Build Rendered",
                                description=f"**Viewer:** [Open 3D Model]({viewer_url})\n\nExpires <t:{expiry_timestamp}:R>",
                                color=0x5865F2,
                                timestamp=datetime.now()
                            )
                            new_embed.set_footer(
                                text=f"Preview unavailable | Storage: {storage_pct:.1f}% | A-class: {a_class_pct:.2f}% | B-class: {b_class_pct:.2f}%"
                            )
                            await preparing_message.edit(embed=new_embed)
                    else:
                        # Preview not ready after max attempts
                        new_embed = discord.Embed(
                            title="Build Rendered",
                            description=f"**Viewer:** [Open 3D Model]({viewer_url})\n\nExpires <t:{expiry_timestamp}:R>",
                            color=0x5865F2,
                            timestamp=datetime.now()
                        )
                        new_embed.set_footer(
                            text=f"Preview unavailable | Storage: {storage_pct:.1f}% | A-class: {a_class_pct:.2f}% | B-class: {b_class_pct:.2f}%"
                        )
                        await preparing_message.edit(embed=new_embed)
                except Exception as e:
                    print(f"[Bot] Error in async preview generation: {e}")
                    import traceback
                    traceback.print_exc()
            
            # Start async preview generation
            asyncio.create_task(generate_and_update_preview())

        force_garbage_collection()
        
    except Exception as e:
        embed = discord.Embed(
            title="Render Error",
            description=f"An error occurred while rendering: {str(e)}",
            color=0xED4245
        )
        await preparing_message.edit(embed=embed)
        print(f"Render error: {e}")
        import traceback
        traceback.print_exc()
        force_garbage_collection()

# Cooldown error handler for prefix commands
@bot.event
async def on_command_error(ctx, error):
    """Handle command errors including cooldowns"""
    if isinstance(error, commands.CommandOnCooldown):
        # Check if user is exempt from cooldowns
        if is_cooldown_exempt(ctx.author):
            # User is exempt, retry the command
            ctx.command.reset_cooldown(ctx)
            await ctx.reinvoke()
            return
        
        retry_after = error.retry_after
        embed = discord.Embed(
            title="⏱️ Cooldown Active",
            description=f"Please wait **{retry_after:.1f} seconds** before using this command again.",
            color=0xFFA500,
            timestamp=datetime.now()
        )
        await ctx.send(embed=embed)
    else:
        # Let other errors propagate
        raise error

@tree.command(name="usage", description="View R2 storage usage statistics (Devs only)", guild=discord.Object(id=ALLOWED_GUILD_ID))
async def usage_command(interaction: discord.Interaction):
    if not has_dev_access(interaction.user):
        embed = discord.Embed(
            title="Access Denied",
            description="You don't have permission to use this command.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    usage_stats = await get_usage_stats()
    
    if not usage_stats:
        embed = discord.Embed(
            title="Usage Statistics",
            description="Unable to retrieve usage statistics. Make sure the backend server is running.",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    storage_gb = usage_stats.get('storage_gb', 0)
    storage_pct = usage_stats.get('storage_percent', 0)
    a_class_calls = usage_stats.get('a_class_calls', 0)
    a_class_pct = usage_stats.get('a_class_percent', 0)
    b_class_calls = usage_stats.get('b_class_calls', 0)
    b_class_pct = usage_stats.get('b_class_percent', 0)
    month = usage_stats.get('month', 'Unknown')
    
    storage_bar = "█" * int(storage_pct / 5) + "░" * (20 - int(storage_pct / 5))
    a_class_bar = "█" * int(a_class_pct / 5) + "░" * (20 - int(a_class_pct / 5))
    b_class_bar = "█" * int(b_class_pct / 5) + "░" * (20 - int(b_class_pct / 5))
    
    embed = discord.Embed(
        title="R2 Usage Statistics",
        description=f"**Period:** {month}\n\n**Storage:** {storage_gb:.2f} GB / 9.8 GB ({storage_pct:.1f}%)\n`{storage_bar}`\n\n**A-Class Operations:** {a_class_calls:,} / 900,000 ({a_class_pct:.2f}%)\n`{a_class_bar}`\n\n**B-Class Operations:** {b_class_calls:,} / 9,800,000 ({b_class_pct:.2f}%)\n`{b_class_bar}`",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    if storage_pct > 80 or a_class_pct > 80 or b_class_pct > 80:
        embed.color = 0xFFA500  # Orange
        if storage_pct > 90 or a_class_pct > 90 or b_class_pct > 90:
            embed.color = 0xED4245  # Red
    
    await interaction.followup.send(embed=embed, ephemeral=True)

@tree.command(name="builds", description="View cached builds (Devs only)", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(
    page="Page number to view (default: 1)"
)
async def builds_command(interaction: discord.Interaction, page: int = 1):
    """View cached builds with pagination"""
    if not has_dev_access(interaction.user):
        embed = discord.Embed(
            title="Access Denied",
            description="You don't have permission to use this command.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    builds_data = await get_cached_builds()
    
    if not builds_data or not builds_data.get('success'):
        embed = discord.Embed(
            title="Error",
            description="Unable to retrieve cached builds. Make sure the backend server is running.",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    builds = builds_data.get('builds', [])
    total_builds = len(builds)
    
    if total_builds == 0:
        embed = discord.Embed(
            title="Cached Builds",
            description="No cached builds found.",
            color=0x5865F2
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    items_per_page = 10
    total_pages = math.ceil(total_builds / items_per_page)
    
    if page < 1:
        page = 1
    elif page > total_pages:
        page = total_pages
    
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_builds = builds[start_idx:end_idx]
    
    build_list = []
    for i, build in enumerate(page_builds, start=start_idx + 1):
        filename = build.get('filename', 'Unknown')
        size = build.get('size', 0)
        model_id = build.get('id', 'Unknown')
        created_at = build.get('created_at', '')
        
        if size < 1024:
            size_str = f"{size} B"
        elif size < 1024 * 1024:
            size_str = f"{size / 1024:.1f} KB"
        else:
            size_str = f"{size / (1024 * 1024):.1f} MB"
        
        try:
            if created_at:
                dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                created_str = dt.strftime('%Y-%m-%d %H:%M')
            else:
                created_str = "Unknown"
        except:
            created_str = "Unknown"
        
        if len(filename) > 30:
            filename = filename[:27] + "..."
        
        build_list.append(f"`{i}.` **{filename}**\n   ID: `{model_id}` | Size: {size_str} | Created: {created_str}")
    
    embed = discord.Embed(
        title="Cached Builds",
        description="\n\n".join(build_list) if build_list else "No builds on this page.",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    embed.set_footer(text=f"Page {page} of {total_pages} | Total: {total_builds} builds")
    
    if total_pages > 1:
        nav_info = []
        if page > 1:
            nav_info.append(f"Use `/builds page:{page-1}` for previous page")
        if page < total_pages:
            nav_info.append(f"Use `/builds page:{page+1}` for next page")
        if nav_info:
            embed.add_field(name="Navigation", value="\n".join(nav_info), inline=False)
    
    await interaction.followup.send(embed=embed, ephemeral=True)

@tree.command(name="list-duplicates", description="List builds with same file size (Devs only)", guild=discord.Object(id=ALLOWED_GUILD_ID))
async def list_duplicates_command(interaction: discord.Interaction):
    """List builds with duplicate file sizes"""
    if not has_dev_access(interaction.user):
        embed = discord.Embed(
            title="Access Denied",
            description="You don't have permission to use this command.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    # Get cached builds
    builds_data = await get_cached_builds()
    
    if not builds_data or not builds_data.get('success'):
        embed = discord.Embed(
            title="Error",
            description="Unable to retrieve cached builds. Make sure the backend server is running.",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    builds = builds_data.get('builds', [])
    
    size_groups = {}
    for build in builds:
        size = build.get('size', 0)
        if size not in size_groups:
            size_groups[size] = []
        size_groups[size].append(build)
    
    duplicates = {size: builds_list for size, builds_list in size_groups.items() if len(builds_list) > 1}
    
    if not duplicates:
        embed = discord.Embed(
            title="Duplicate Builds",
            description="No duplicate builds found (same file size).",
            color=0x5865F2,
            timestamp=datetime.now()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    # Format duplicate list
    duplicate_list = []
    for size, builds_list in sorted(duplicates.items(), key=lambda x: len(x[1]), reverse=True):
        # Format file size
        if size < 1024:
            size_str = f"{size} B"
        elif size < 1024 * 1024:
            size_str = f"{size / 1024:.1f} KB"
        else:
            size_str = f"{size / (1024 * 1024):.1f} MB"
        
        build_names = []
        for build in builds_list:
            filename = build.get('filename', 'Unknown')
            model_id = build.get('id', 'Unknown')
            if len(filename) > 25:
                filename = filename[:22] + "..."
            build_names.append(f"`{model_id}` - {filename}")
        
        duplicate_list.append(f"**Size: {size_str}** ({len(builds_list)} builds)\n" + "\n".join(build_names))
    
    # Create embed
    embed = discord.Embed(
        title="Duplicate Builds",
        description="\n\n".join(duplicate_list[:10]) if duplicate_list else "No duplicates found.",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    embed.set_footer(text=f"Found {len(duplicates)} duplicate size groups")
    
    await interaction.followup.send(embed=embed, ephemeral=True)

@tree.command(name="delete", description="Delete a model from storage and cache (Devs only)", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(
    model_id="Model ID to delete"
)
async def delete_command(interaction: discord.Interaction, model_id: str):
    """Delete a model from R2 storage and API cache"""
    # Check if user is allowed
    if not has_dev_access(interaction.user):
        embed = discord.Embed(
            title="Access Denied",
            description="You don't have permission to use this command.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    # Delete model from backend
    success = await delete_model_from_backend(model_id)
    
    if success:
        embed = discord.Embed(
            title="Model Deleted",
            description=f"Model `{model_id}` has been deleted from R2 storage and API cache.",
            color=0x57F287,
            timestamp=datetime.now()
        )
    else:
        embed = discord.Embed(
            title="Delete Failed",
            description=f"Failed to delete model `{model_id}`. It may not exist or the backend server is unavailable.",
            color=0xED4245,
            timestamp=datetime.now()
        )
    
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="check-permissions", description="Check who has specific permissions (Owner only)", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(
    permission_type="Type of permission to check (member or dev)"
)
@app_commands.choices(permission_type=[
    app_commands.Choice(name="Member", value="member"),
    app_commands.Choice(name="Developer", value="dev")
])
async def check_permissions_command(interaction: discord.Interaction, permission_type: str):
    """Check who has member or developer permissions"""
    if interaction.user.id != OWNER_ID:
        embed = discord.Embed(
            title="Access Denied",
            description="Only the bot owner can use this command.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    guild = bot.get_guild(ALLOWED_GUILD_ID)
    if not guild:
        embed = discord.Embed(
            title="Error",
            description="Could not find the guild.",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    if permission_type == "member":
        role_id = STAFF_ROLE_ID
        role_name = "Member (Staff)"
        access_func = has_member_access
    else:
        role_id = DEV_ROLE_ID
        role_name = "Developer"
        access_func = has_dev_access
    
    role = guild.get_role(role_id)
    if not role:
        embed = discord.Embed(
            title="Error",
            description=f"Could not find the {role_name} role.",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    # Get all members with this role
    members_with_role = [member for member in guild.members if role in member.roles]
    
    # Also check owner
    owner = guild.get_member(OWNER_ID)
    owner_has_access = owner and access_func(owner)
    
    # Build list
    user_list = []
    if owner_has_access:
        user_list.append(f"👑 **{owner.display_name}** ({owner.mention}) - Owner")
    
    for member in sorted(members_with_role, key=lambda m: m.display_name.lower()):
        if member.id != OWNER_ID:  # Don't duplicate owner
            user_list.append(f"• **{member.display_name}** ({member.mention})")
    
    if not user_list:
        embed = discord.Embed(
            title=f"{role_name} Permissions",
            description=f"No users have {role_name.lower()} permissions.",
            color=0x5865F2
        )
    else:
        user_text = "\n".join(user_list[:50])  # Limit to 50 users
        if len(user_list) > 50:
            user_text += f"\n\n... and {len(user_list) - 50} more"
        
        embed = discord.Embed(
            title=f"{role_name} Permissions",
            description=f"Users with {role_name.lower()} permissions:\n\n{user_text}",
            color=0x5865F2,
            timestamp=datetime.now()
        )
        embed.set_footer(text=f"Total: {len(user_list)} user(s)")
    
    await interaction.followup.send(embed=embed, ephemeral=True)

@tree.command(name="grant-access", description="Grant elevated access to a user (Owner only)", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(
    user="User to grant access to",
    access_level="Level of access to grant"
)
@app_commands.choices(access_level=[
    app_commands.Choice(name="Member (Staff)", value="member"),
    app_commands.Choice(name="Developer", value="dev")
])
async def grant_access_command(interaction: discord.Interaction, user: discord.Member, access_level: str):
    """Grant member or developer access to a user"""
    if interaction.user.id != OWNER_ID:
        embed = discord.Embed(
            title="Access Denied",
            description="Only the bot owner can use this command.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    guild = bot.get_guild(ALLOWED_GUILD_ID)
    if not guild:
        embed = discord.Embed(
            title="Error",
            description="Could not find the guild.",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    if access_level == "member":
        role_id = STAFF_ROLE_ID
        role_name = "Member (Staff)"
    else:
        role_id = DEV_ROLE_ID
        role_name = "Developer"
    
    role = guild.get_role(role_id)
    if not role:
        embed = discord.Embed(
            title="Error",
            description=f"Could not find the {role_name} role.",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    # Check if bot has permission to manage roles
    if not guild.me.guild_permissions.manage_roles:
        embed = discord.Embed(
            title="Error",
            description="Bot does not have permission to manage roles.",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    # Check if role is higher than bot's highest role
    if role >= guild.me.top_role:
        embed = discord.Embed(
            title="Error",
            description=f"The {role_name} role is higher than the bot's highest role. Please move the bot's role above this role.",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    # Check if user already has the role
    if role in user.roles:
        embed = discord.Embed(
            title="Already Has Access",
            description=f"{user.mention} already has {role_name.lower()} permissions.",
            color=0xFFA500
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    try:
        await user.add_roles(role, reason=f"Granted {role_name} access by {interaction.user}")
        embed = discord.Embed(
            title="Access Granted",
            description=f"Successfully granted {role_name.lower()} permissions to {user.mention}.",
            color=0x57F287,
            timestamp=datetime.now()
        )
        embed.set_footer(text=f"Granted by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except discord.Forbidden:
        embed = discord.Embed(
            title="Error",
            description=f"Bot does not have permission to add roles to {user.mention}.",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        embed = discord.Embed(
            title="Error",
            description=f"Failed to grant access: {str(e)}",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

@tree.command(name="revoke-access", description="Revoke elevated access from a user (Owner only)", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(
    user="User to revoke access from",
    access_level="Level of access to revoke"
)
@app_commands.choices(access_level=[
    app_commands.Choice(name="Member (Staff)", value="member"),
    app_commands.Choice(name="Developer", value="dev")
])
async def revoke_access_command(interaction: discord.Interaction, user: discord.Member, access_level: str):
    """Revoke member or developer access from a user"""
    if interaction.user.id != OWNER_ID:
        embed = discord.Embed(
            title="Access Denied",
            description="Only the bot owner can use this command.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    guild = bot.get_guild(ALLOWED_GUILD_ID)
    if not guild:
        embed = discord.Embed(
            title="Error",
            description="Could not find the guild.",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    if access_level == "member":
        role_id = STAFF_ROLE_ID
        role_name = "Member (Staff)"
    else:
        role_id = DEV_ROLE_ID
        role_name = "Developer"
    
    role = guild.get_role(role_id)
    if not role:
        embed = discord.Embed(
            title="Error",
            description=f"Could not find the {role_name} role.",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    # Check if bot has permission to manage roles
    if not guild.me.guild_permissions.manage_roles:
        embed = discord.Embed(
            title="Error",
            description="Bot does not have permission to manage roles.",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    # Check if role is higher than bot's highest role
    if role >= guild.me.top_role:
        embed = discord.Embed(
            title="Error",
            description=f"The {role_name} role is higher than the bot's highest role. Please move the bot's role above this role.",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    # Check if user has the role
    if role not in user.roles:
        embed = discord.Embed(
            title="No Access",
            description=f"{user.mention} does not have {role_name.lower()} permissions.",
            color=0xFFA500
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    # Prevent revoking from owner
    if user.id == OWNER_ID:
        embed = discord.Embed(
            title="Error",
            description="Cannot revoke access from the bot owner.",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    try:
        await user.remove_roles(role, reason=f"Revoked {role_name} access by {interaction.user}")
        embed = discord.Embed(
            title="Access Revoked",
            description=f"Successfully revoked {role_name.lower()} permissions from {user.mention}.",
            color=0x57F287,
            timestamp=datetime.now()
        )
        embed.set_footer(text=f"Revoked by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except discord.Forbidden:
        embed = discord.Embed(
            title="Error",
            description=f"Bot does not have permission to remove roles from {user.mention}.",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        embed = discord.Embed(
            title="Error",
            description=f"Failed to revoke access: {str(e)}",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

# Prefix commands with '*' (mirror slash commands)
@bot.command(name="render", aliases=["r"])
@commands.cooldown(1, 30.0, commands.BucketType.user)  # 30 second cooldown per user
async def render_prefix(ctx, index: int = None):
    """Prefix version of /render command - supports file upload or index"""
    if ctx.guild.id != ALLOWED_GUILD_ID:
        return
    if not has_member_access(ctx.author):
        embed = discord.Embed(
            title="Access Denied",
            description="You don't have permission to use this command.",
            color=0xED4245
        )
        await ctx.send(embed=embed)
        return
    
    # Send immediate response to show the bot is working
    preparing_embed = discord.Embed(
        title="Rendering Your Build",
        description="Rendering your build, this may take a while.",
        color=0x5865F2
    )
    preparing_message = await ctx.send(embed=preparing_embed)
    
    # If index is provided, render from cache
    if index is not None:
        try:
            builds_data = await get_cached_builds()
            if not builds_data:
                embed = discord.Embed(
                    title="Error",
                    description="Unable to retrieve cached builds. Make sure the backend server is running.",
                    color=0xED4245
                )
                await preparing_message.edit(embed=embed)
                return
            
            builds = builds_data.get('builds', [])
            total_builds = len(builds)
            
            if total_builds == 0:
                embed = discord.Embed(
                    title="No Cached Builds",
                    description="No cached builds found. Please upload a build file instead.",
                    color=0xED4245
                )
                await preparing_message.edit(embed=embed)
                return
            
            # Convert 1-based index to 0-based
            if index < 1 or index > total_builds:
                embed = discord.Embed(
                    title="Invalid Index",
                    description=f"Index must be between 1 and {total_builds}. Use `*builds` to see available builds.",
                    color=0xED4245
                )
                await preparing_message.edit(embed=embed)
                return
            
            # Get the build at the specified index (1-based to 0-based conversion)
            build = builds[index - 1]
            model_id = build.get('id', 'Unknown')
            
            if not model_id or model_id == 'Unknown':
                embed = discord.Embed(
                    title="Error",
                    description="Invalid build data. The cached build may be corrupted.",
                    color=0xED4245
                )
                await preparing_message.edit(embed=embed)
                return
            
            # Get viewer URL from model_id
            server_url = await get_active_server_url()
            viewer_url = f"{server_url}/model?model_id={model_id}"
            
            usage_stats = await get_usage_stats()
            expiry_timestamp = int((datetime.now() + timedelta(minutes=10)).timestamp())
            
            filename = build.get('filename', 'Unknown')
            embed = discord.Embed(
                title="Build Rendered",
                description=f"**Viewer:** [Open 3D Model]({viewer_url})\n\n**Build:** {filename}\n**Model ID:** `{model_id}`\n\nExpires <t:{expiry_timestamp}:R>",
                color=0x5865F2,
                timestamp=datetime.now()
            )
            
            storage_pct = usage_stats.get('storage_percent', 0)
            a_class_pct = usage_stats.get('a_class_percent', 0)
            b_class_pct = usage_stats.get('b_class_percent', 0)
            embed.set_footer(
                text=f"Storage: {storage_pct:.1f}% | A-class: {a_class_pct:.2f}% | B-class: {b_class_pct:.2f}%"
            )
            
            await preparing_message.edit(embed=embed)
            return
            
        except Exception as e:
            embed = discord.Embed(
                title="Error",
                description=f"An error occurred while loading cached build: {str(e)}",
                color=0xED4245
            )
            await preparing_message.edit(embed=embed)
            return
    
    # If no index, check for file attachment
    if not ctx.message.attachments:
        embed = discord.Embed(
            title="Missing Input",
            description="Please provide either a build file attachment or an index number (e.g., `*render 5`). Use `*builds` to see available builds.",
            color=0xED4245
        )
        await preparing_message.edit(embed=embed)
        return
    
    build_file = ctx.message.attachments[0]
    
    if not build_file.filename.lower().endswith(('.build', '.Build')):
        embed = discord.Embed(
            title="Invalid File",
            description="Please upload a .Build or .build file.",
            color=0xED4245
        )
        await ctx.send(embed=embed)
        return
    
    if build_file.size > MAX_BUILD_FILE_SIZE:
        embed = discord.Embed(
            title="File Too Large",
            description=f"File size ({build_file.size / 1024 / 1024:.1f}MB) exceeds limit ({MAX_BUILD_FILE_SIZE / 1024 / 1024:.0f}MB).",
            color=0xED4245
        )
        await ctx.send(embed=embed)
        return
    
    try:
        from utils import check_build_cache, write_file_async, calculate_memory_usage, calculate_build_hash, get_active_server_url
        import psutil

        # Read build file content once and calculate SHA-1 hash
        build_content = await build_file.read()
        build_hash = calculate_build_hash(build_content)
        
        # Check cache first using SHA-1 hash
        cached = await check_build_cache(build_hash)

        if cached:
            model_id = cached['model_id']
            # Get viewer URL directly from cache (gltf_url) - no API call needed!
            gltf_url = cached.get('gltf_url', cached.get('url'))
            # Construct viewer URL from model_id (gltf_url is stored but we use model_id for viewer)
            server_url = await get_active_server_url()
            viewer_url = f"{server_url}/model?model_id={model_id}"
            print(f"Cache hit: {build_file.filename} ({build_file.size} bytes) -> {model_id} (reused, no render, no R2 API call)")
        else:
            # Check memory before processing
            memory = psutil.virtual_memory()
            estimated_memory = calculate_memory_usage(build_file.size)

            # If memory is too high, wait a bit and check cache again (another user might have uploaded)
            if memory.percent > 85:
                await asyncio.sleep(0.5)  # Brief wait for concurrent uploads
                cached = await check_build_cache(build_hash)
                if cached:
                    model_id = cached['model_id']
                    server_url = await get_active_server_url()
                    viewer_url = f"{server_url}/model?model_id={model_id}"
                    print(f"Cache hit after wait: {build_file.filename} ({build_file.size} bytes) -> {model_id}")
                else:
                    # Still no cache, proceed with render
                    build_path = TEMP_DIR / f"temp_{build_file.filename}"
                    await write_file_async(build_path, build_content)

                    model_id = generate_model_id()
                    renderer = GLTFRenderer(str(build_path))
                    renderer.parse_build_file()

                    if len(renderer.positions) == 0:
                        embed = discord.Embed(
                            title="Render Error",
                            description="No blocks found in build file.",
                            color=0xED4245
                        )
                        await preparing_message.edit(embed=embed)
                        cleanup_temp_files(build_path)
                        return

                    gltf_dir = TEMP_DIR / model_id
                    gltf_dir.mkdir(exist_ok=True)
                    gltf_path = gltf_dir / f"{model_id}.gltf"

                    center, max_size = renderer.export_to_gltf(str(gltf_path))

                    html_content = renderer.create_viewer_html(
                        f"{model_id}.gltf",
                        center,
                        max_size,
                        port=8000
                    )
                    html_path = gltf_dir / "index.html"
                    await write_file_async(html_path, html_content.encode('utf-8'))

                    # Check cache one more time before upload (catch concurrent duplicates)
                    cached = await check_build_cache(build_hash)
                    if cached:
                        model_id = cached['model_id']
                        server_url = await get_active_server_url()
                        viewer_url = f"{server_url}/model?model_id={model_id}"
                        print(f"Cache hit before upload: {build_file.filename} ({build_file.size} bytes) -> {model_id} (skipped R2 upload)")
                        cleanup_temp_files(build_path)
                        cleanup_temp_files(gltf_dir)
                    else: #3
                        viewer_url = await upload_gltf_to_server(
                            str(gltf_path),
                            model_id,
                            build_filename=build_file.filename,
                            build_size=build_file.size,
                            build_hash=build_hash
                        )
                        cleanup_temp_files(build_path)
                        cleanup_temp_files(gltf_dir)
            else:
                # Memory is fine, proceed normally
                build_path = TEMP_DIR / f"temp_{build_file.filename}"
                await write_file_async(build_path, build_content)

                model_id = generate_model_id()
                renderer = GLTFRenderer(str(build_path))
                renderer.parse_build_file()

                if len(renderer.positions) == 0:
                    embed = discord.Embed(
                        title="Render Error",
                        description="No blocks found in build file.",
                        color=0xED4245
                    )
                    await preparing_message.edit(embed=embed)
                    cleanup_temp_files(build_path)
                    return

                gltf_dir = TEMP_DIR / model_id
                gltf_dir.mkdir(exist_ok=True)
                gltf_path = gltf_dir / f"{model_id}.gltf"

                center, max_size = renderer.export_to_gltf(str(gltf_path))

                html_content = renderer.create_viewer_html(
                    f"{model_id}.gltf",
                    center,
                    max_size,
                    port=8000
                )
                html_path = gltf_dir / "index.html"
                await write_file_async(html_path, html_content.encode('utf-8'))

                # Check cache one more time before upload (catch concurrent duplicates)
                cached = await check_build_cache(build_hash)
                if cached:
                    model_id = cached['model_id']
                    server_url = await get_active_server_url()
                    viewer_url = f"{server_url}/model?model_id={model_id}"
                    print(f"Cache hit before upload: {build_file.filename} ({build_file.size} bytes) -> {model_id} (skipped R2 upload)")
                    cleanup_temp_files(build_path)
                    cleanup_temp_files(gltf_dir)
                else: #4
                    viewer_url = await upload_gltf_to_server(
                        str(gltf_path),
                        model_id,
                        build_filename=build_file.filename,
                        build_size=build_file.size,
                        build_hash=build_hash
                    )
                    cleanup_temp_files(build_path)
                    cleanup_temp_files(gltf_dir)

        if not viewer_url:
            embed = discord.Embed(
                title="Web Server Unavailable",
                description="The web server is currently unavailable. Please try again later.",
                color=0xED4245
            )
            await preparing_message.edit(embed=embed)
            return
        
        usage_stats = await get_usage_stats()
        expiry_timestamp = int((datetime.now() + timedelta(minutes=10)).timestamp())
        
        # Get preview URL if available (from cache)
        preview_url = None
        if cached:
            preview_url = cached.get('preview_url')
        
        # Create embed with viewer link first (preview will be added async)
        embed = discord.Embed(
            title="Build Rendered",
            description=f"**Viewer:** [Open 3D Model]({viewer_url})\n\nExpires <t:{expiry_timestamp}:R>",
            color=0x5865F2,
            timestamp=datetime.now()
        )
        
        # Add preview image if available from cache
        storage_pct = usage_stats.get('storage_percent', 0)
        a_class_pct = usage_stats.get('a_class_percent', 0)
        b_class_pct = usage_stats.get('b_class_percent', 0)
        
        if preview_url:
            embed.set_image(url=preview_url)
            embed.set_footer(
                text=f"Storage: {storage_pct:.1f}% | A-class: {a_class_pct:.2f}% | B-class: {b_class_pct:.2f}%"
            )
        else:
            embed.set_footer(
                text=f"Preview loading... | Storage: {storage_pct:.1f}% | A-class: {a_class_pct:.2f}% | B-class: {b_class_pct:.2f}%"
            )

        # Send embed with model link first
        await preparing_message.edit(embed=embed)
        
        # Generate preview asynchronously if not in cache
        if not preview_url:
            async def generate_and_update_preview():
                try:
                    # Construct R2 URL for the GLTF file
                    from config import R2_PUBLIC_URL
                    gltf_url = f"{R2_PUBLIC_URL}/{model_id}.gltf"
                    
                    # Check if preview is ready from Flowkit (Flowkit caches renders, so this is fast)
                    from utils import check_preview_ready, generate_preview_with_flowkit
                    
                    # Wait a bit for Flowkit to process if needed, then check
                    max_attempts = 10
                    attempt = 0
                    preview_ready = False
                    
                    while attempt < max_attempts and not preview_ready:
                        await asyncio.sleep(1)  # Wait 1 second between checks
                        preview_ready = await check_preview_ready(gltf_url)
                        attempt += 1
                    
                    if preview_ready:
                        # Generate and upload preview (Flowkit will use its cache if available)
                        print(f"[Bot] Generating preview for {model_id}...")
                        generated_preview_url = await generate_preview_with_flowkit(model_id, gltf_url)
                        
                        if generated_preview_url:
                            # Update cache with preview URL via API
                            try:
                                server_url = await get_active_server_url()
                                async with httpx.AsyncClient(timeout=10.0) as client:
                                    response = await client.post(
                                        f"{server_url}/api/generate-preview",
                                        json={
                                            'model_id': model_id,
                                            'gltf_url': gltf_url
                                        },
                                        headers={
                                            'X-API-Secret': WEB_SERVER_SECRET,
                                            'Content-Type': 'application/json'
                                        }
                                    )
                            except Exception as e:
                                print(f"[Bot] Error updating cache with preview: {e}")
                            
                            print(f"[Bot] Preview generated for {model_id}: {generated_preview_url}")
                            
                            # Update embed with preview
                            new_embed = discord.Embed(
                                title="Build Rendered",
                                description=f"**Viewer:** [Open 3D Model]({viewer_url})\n\nExpires <t:{expiry_timestamp}:R>",
                                color=0x5865F2,
                                timestamp=datetime.now()
                            )
                            new_embed.set_image(url=generated_preview_url)
                            new_embed.set_footer(
                                text=f"Storage: {storage_pct:.1f}% | A-class: {a_class_pct:.2f}% | B-class: {b_class_pct:.2f}%"
                            )
                            await preparing_message.edit(embed=new_embed)
                        else:
                            # Preview generation failed, update footer
                            new_embed = discord.Embed(
                                title="Build Rendered",
                                description=f"**Viewer:** [Open 3D Model]({viewer_url})\n\nExpires <t:{expiry_timestamp}:R>",
                                color=0x5865F2,
                                timestamp=datetime.now()
                            )
                            new_embed.set_footer(
                                text=f"Preview unavailable | Storage: {storage_pct:.1f}% | A-class: {a_class_pct:.2f}% | B-class: {b_class_pct:.2f}%"
                            )
                            await preparing_message.edit(embed=new_embed)
                    else:
                        # Preview not ready after max attempts
                        new_embed = discord.Embed(
                            title="Build Rendered",
                            description=f"**Viewer:** [Open 3D Model]({viewer_url})\n\nExpires <t:{expiry_timestamp}:R>",
                            color=0x5865F2,
                            timestamp=datetime.now()
                        )
                        new_embed.set_footer(
                            text=f"Preview unavailable | Storage: {storage_pct:.1f}% | A-class: {a_class_pct:.2f}% | B-class: {b_class_pct:.2f}%"
                        )
                        await preparing_message.edit(embed=new_embed)
                except Exception as e:
                    print(f"[Bot] Error in async preview generation: {e}")
                    import traceback
                    traceback.print_exc()
            
            # Start async preview generation
            asyncio.create_task(generate_and_update_preview())
        
        force_garbage_collection()
        
    except Exception as e:
        embed = discord.Embed(
            title="Render Error",
            description=f"An error occurred while rendering: {str(e)}",
            color=0xED4245
        )
        try:
            await preparing_message.edit(embed=embed)
        except:
            await ctx.send(embed=embed)
        print(f"Render error: {e}")
        import traceback
        traceback.print_exc()
        force_garbage_collection()

@bot.command(name="usage", aliases=["u"])
async def usage_prefix(ctx):
    """Prefix version of /usage command"""
    if ctx.guild.id != ALLOWED_GUILD_ID or not has_dev_access(ctx.author):
        return
    
    await ctx.typing()
    usage_stats = await get_usage_stats()
    
    if not usage_stats:
        embed = discord.Embed(
            title="Error",
            description="Unable to retrieve usage statistics.",
            color=0xED4245
        )
        await ctx.send(embed=embed)
        return
    
    storage_gb = usage_stats.get('storage_gb', 0)
    storage_pct = usage_stats.get('storage_percent', 0)
    a_class_calls = usage_stats.get('a_class_calls', 0)
    a_class_pct = usage_stats.get('a_class_percent', 0)
    b_class_calls = usage_stats.get('b_class_calls', 0)
    b_class_pct = usage_stats.get('b_class_percent', 0)
    
    storage_bar = "█" * int(storage_pct / 5) + "░" * (20 - int(storage_pct / 5))
    a_class_bar = "█" * int(a_class_pct / 5) + "░" * (20 - int(a_class_pct / 5))
    b_class_bar = "█" * int(b_class_pct / 5) + "░" * (20 - int(b_class_pct / 5))
    
    embed = discord.Embed(
        title="R2 Usage Statistics",
        description=f"**Storage:** {storage_gb:.2f} GB / 9.8 GB ({storage_pct:.1f}%)\n`{storage_bar}`\n\n**A-Class Operations:** {a_class_calls:,} / 900,000 ({a_class_pct:.2f}%)\n`{a_class_bar}`\n\n**B-Class Operations:** {b_class_calls:,} / 9,800,000 ({b_class_pct:.2f}%)\n`{b_class_bar}`",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    if storage_pct > 80 or a_class_pct > 80 or b_class_pct > 80:
        embed.color = 0xFFA500
        if storage_pct > 90 or a_class_pct > 90 or b_class_pct > 90:
            embed.color = 0xED4245
    
    await ctx.send(embed=embed)

@bot.command(name="builds", aliases=["b"])
async def builds_prefix(ctx, page: int = 1):
    """Prefix version of /builds command"""
    if ctx.guild.id != ALLOWED_GUILD_ID or not has_dev_access(ctx.author):
        return
    
    await ctx.typing()
    builds_data = await get_cached_builds()
    
    if not builds_data or not builds_data.get('success'):
        embed = discord.Embed(
            title="Error",
            description="Unable to retrieve cached builds.",
            color=0xED4245
        )
        await ctx.send(embed=embed)
        return
    
    builds = builds_data.get('builds', [])
    total_builds = len(builds)
    
    if total_builds == 0:
        embed = discord.Embed(
            title="No Cached Builds",
            description="No cached builds found.",
            color=0x5865F2
        )
        await ctx.send(embed=embed)
        return
    
    items_per_page = 10
    total_pages = math.ceil(total_builds / items_per_page)
    
    if page < 1:
        page = 1
    elif page > total_pages:
        page = total_pages
    
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_builds = builds[start_idx:end_idx]
    
    build_list = []
    for i, build in enumerate(page_builds, start=start_idx + 1):
        filename = build.get('filename', 'Unknown')
        size = build.get('size', 0)
        model_id = build.get('id', 'Unknown')
        created_at = build.get('created_at', '')
        
        if size < 1024:
            size_str = f"{size} B"
        elif size < 1024 * 1024:
            size_str = f"{size / 1024:.1f} KB"
        else:
            size_str = f"{size / (1024 * 1024):.1f} MB"
        
        try:
            if created_at:
                dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                created_str = dt.strftime('%Y-%m-%d %H:%M')
            else:
                created_str = "Unknown"
        except:
            created_str = "Unknown"
        
        if len(filename) > 30:
            filename = filename[:27] + "..."
        
        build_list.append(f"`{i}.` **{filename}**\n   ID: `{model_id}` | Size: {size_str} | Created: {created_str}")
    
    embed = discord.Embed(
        title="Cached Builds",
        description="\n\n".join(build_list) if build_list else "No builds on this page.",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    embed.set_footer(text=f"Page {page} of {total_pages} | Total: {total_builds} builds")
    
    if total_pages > 1:
        nav_info = []
        if page > 1:
            nav_info.append(f"Use `*builds {page-1}` for previous page")
        if page < total_pages:
            nav_info.append(f"Use `*builds {page+1}` for next page")
        if nav_info:
            embed.add_field(name="Navigation", value="\n".join(nav_info), inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name="list-duplicates", aliases=["ld", "duplicates"])
async def list_duplicates_prefix(ctx):
    """Prefix version of /list-duplicates command"""
    if ctx.guild.id != ALLOWED_GUILD_ID or not has_dev_access(ctx.author):
        return
    
    await ctx.typing()
    builds_data = await get_cached_builds()
    
    if not builds_data or not builds_data.get('success'):
        await ctx.send("Unable to retrieve cached builds.")
        return
    
    builds = builds_data.get('builds', [])
    
    size_groups = {}
    for build in builds:
        size = build.get('size', 0)
        if size not in size_groups:
            size_groups[size] = []
        size_groups[size].append(build)
    
    duplicates = {size: builds_list for size, builds_list in size_groups.items() if len(builds_list) > 1}
    
    if not duplicates:
        embed = discord.Embed(
            title="No Duplicates",
            description="No duplicate builds found (same file size).",
            color=0x5865F2
        )
        await ctx.send(embed=embed)
        return
    
    duplicate_list = []
    for size, builds_list in sorted(duplicates.items(), key=lambda x: len(x[1]), reverse=True):
        if size < 1024:
            size_str = f"{size} B"
        elif size < 1024 * 1024:
            size_str = f"{size / 1024:.1f} KB"
        else:
            size_str = f"{size / (1024 * 1024):.1f} MB"
        
        build_names = []
        for build in builds_list:
            filename = build.get('filename', 'Unknown')
            model_id = build.get('id', 'Unknown')
            if len(filename) > 25:
                filename = filename[:22] + "..."
            build_names.append(f"`{model_id}` - {filename}")
        
        duplicate_list.append(f"**Size: {size_str}** ({len(builds_list)} builds)\n" + "\n".join(build_names))
    
    embed = discord.Embed(
        title="Duplicate Builds",
        description="\n\n".join(duplicate_list[:10]) if duplicate_list else "No duplicates found.",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    embed.set_footer(text=f"Found {len(duplicates)} duplicate size groups")
    await ctx.send(embed=embed)

@bot.command(name="delete", aliases=["del"])
async def delete_prefix(ctx, model_id: str = None):
    """Prefix version of /delete command"""
    if ctx.guild.id != ALLOWED_GUILD_ID or not has_dev_access(ctx.author):
        return
    
    if not model_id:
        embed = discord.Embed(
            title="Missing Model ID",
            description="Please provide a model ID: `*delete <model_id>`",
            color=0xED4245
        )
        await ctx.send(embed=embed)
        return
    
    await ctx.typing()
    success = await delete_model_from_backend(model_id)
    
    if success:
        embed = discord.Embed(
            title="Model Deleted",
            description=f"Model `{model_id}` has been deleted from R2 storage and API cache.",
            color=0x57F287,
            timestamp=datetime.now()
        )
    else:
        embed = discord.Embed(
            title="Delete Failed",
            description=f"Failed to delete model `{model_id}`. It may not exist or the backend server is unavailable.",
            color=0xED4245,
            timestamp=datetime.now()
        )
    
    await ctx.send(embed=embed)

@bot.command(name="uptime", aliases=["ut", "up"])
@commands.cooldown(3, 10.0, commands.BucketType.user)  # 3 uses per 10 seconds
async def uptime_prefix(ctx):
    """Prefix version of /uptime command"""
    if ctx.guild.id != ALLOWED_GUILD_ID or not has_member_access(ctx.author):
        return
    
    await ctx.typing()
    
    if _bot_start_time is None:
        uptime_seconds = 0
    else:
        uptime_seconds = int(time.time() - _bot_start_time)
    
    days = uptime_seconds // 86400
    hours = (uptime_seconds % 86400) // 3600
    minutes = (uptime_seconds % 3600) // 60
    seconds = uptime_seconds % 60
    
    uptime_str = f"{days}d {hours}h {minutes}m {seconds}s"
    if days == 0:
        uptime_str = f"{hours}h {minutes}m {seconds}s"
    if days == 0 and hours == 0:
        uptime_str = f"{minutes}m {seconds}s"
    if days == 0 and hours == 0 and minutes == 0:
        uptime_str = f"{seconds}s"
    
    embed = discord.Embed(
        title="Bot Uptime",
        description=f"**Uptime:** {uptime_str}\n**Started:** <t:{int(_bot_start_time)}:R>" if _bot_start_time else "Uptime tracking not available",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    await ctx.send(embed=embed)

@bot.command(name="credits", aliases=["credit", "about"])
async def credits_prefix(ctx):
    """Prefix version of /credits command"""
    if ctx.guild.id != ALLOWED_GUILD_ID:
        return
    
    await ctx.typing()
    
    # Try to get the creator user
    creator_id = 1149910630678134916
    creator_mention = f"<@{creator_id}>"
    try:
        creator_user = await bot.fetch_user(creator_id)
        creator_mention = creator_user.mention
        creator_name = creator_user.display_name
    except:
        creator_name = "_zenix"
    
    embed = discord.Embed(
        title="8Bit | Credits",
        description="**Thank you for using 8Bit.**",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    embed.add_field(
        name="Bot Developer",
        value=f"**{creator_name}**\n`{creator_id}`\n{creator_mention}",
        inline=True
    )
    
    embed.add_field(
        name="API Developer",
        value=f"**{creator_name}**\n`{creator_id}`\n{creator_mention}",
        inline=True
    )
    
    embed.add_field(
        name="\u200b",
        value="\u200b",
        inline=True
    )
    
    embed.add_field(
        name="🛠️ Technologies & Services",
        value="• **Flowkit** - 3D Model Preview Generation\n• **PythonAnywhere** - Backend Hosting\n• **Railway** - Bot Hosting\n• **R2 Cloud Storage** - File Storage\n• **Discord.py** - Bot Framework\n• **Three.js** - 3D Viewer",
        inline=False
    )
    
    embed.set_footer(
        text="8Bit | Renderer",
        icon_url=str(bot.user.avatar.url) if bot.user and bot.user.avatar else None
    )
    
    await ctx.send(embed=embed)

@bot.command(name="systeminfo", aliases=["si", "sys", "info"])
async def systeminfo_prefix(ctx):
    """Prefix version of /systeminfo command"""
    if ctx.guild.id != ALLOWED_GUILD_ID or not has_dev_access(ctx.author):
        return
    
    await ctx.typing()
    
    # Detect hosting platform
    hosting_platform = "Unknown"
    if os.environ.get('RAILWAY_ENVIRONMENT'):
        hosting_platform = "Railway"
    elif os.environ.get('HEROKU_APP_NAME'):
        hosting_platform = "Heroku"
    elif os.environ.get('VERCEL'):
        hosting_platform = "Vercel"
    elif os.path.exists('/.dockerenv'):
        hosting_platform = "Docker"
    else:
        hosting_platform = "Local"
    
    # System info
    cpu_percent = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    memory_percent = memory.percent
    memory_used_gb = memory.used / (1024**3)
    memory_total_gb = memory.total / (1024**3)
    
    disk = psutil.disk_usage('/')
    disk_percent = disk.percent
    disk_used_gb = disk.used / (1024**3)
    disk_total_gb = disk.total / (1024**3)
    
    # Python info
    python_version = platform.python_version()
    platform_info = platform.platform()
    
    # Bot info
    guild_count = len(bot.guilds)
    user_count = len(bot.users)
    
    embed = discord.Embed(
        title="System Information",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    embed.add_field(
        name="Hosting Platform",
        value=f"**Platform:** {hosting_platform}\n**Environment:** {os.environ.get('RAILWAY_ENVIRONMENT', 'Production' if hosting_platform != 'Local' else 'Development')}",
        inline=False
    )
    
    embed.add_field(
        name="System Resources",
        value=f"**CPU Usage:** {cpu_percent:.1f}%\n**Memory:** {memory_percent:.1f}% ({memory_used_gb:.2f} GB / {memory_total_gb:.2f} GB)\n**Disk:** {disk_percent:.1f}% ({disk_used_gb:.2f} GB / {disk_total_gb:.2f} GB)",
        inline=False
    )
    
    embed.add_field(
        name="Bot Statistics",
        value=f"**Guilds:** {guild_count}\n**Users:** {user_count}\n**Python:** {python_version}",
        inline=False
    )
    
    embed.add_field(
        name="System",
        value=f"**Platform:** {platform_info}",
        inline=False
    )
    
    await ctx.send(embed=embed)

@bot.command(name="image2link", aliases=["i2l", "img2link", "image"])
@commands.cooldown(5, 10.0, commands.BucketType.user)  # 5 uses per 10 seconds
async def image2link_prefix(ctx):
    """Prefix version of /image2link command - supports attachments only"""
    if ctx.guild.id != ALLOWED_GUILD_ID or not has_member_access(ctx.author):
        return
    
    await ctx.typing()
    
    cdn_url = None
    
    # Check if image attachment is provided
    if ctx.message.attachments:
        image = ctx.message.attachments[0]
        if image.content_type and image.content_type.startswith('image/'):
            cdn_url = image.url
        else:
            embed = discord.Embed(
                title="Invalid File",
                description="The provided file is not an image.",
                color=0xED4245
            )
            await ctx.send(embed=embed)
            return
    else:
        embed = discord.Embed(
            title="No Image Provided",
            description="Please attach an image file.",
            color=0xED4245
        )
        await ctx.send(embed=embed)
        return
    
    if not cdn_url:
        embed = discord.Embed(
            title="Error",
            description="Failed to get Discord CDN link.",
            color=0xED4245
        )
        await ctx.send(embed=embed)
        return
    
    embed = discord.Embed(
        title="Image CDN Link",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    description_parts = []
    description_parts.append("**Discord CDN Link:**")
    description_parts.append(f"[Click here]({cdn_url})")
    description_parts.append(f"```{cdn_url}```")
    
    embed.description = "\n".join(description_parts)
    embed.set_image(url=cdn_url)
    embed.set_footer(text="Discord CDN link (permanent)")
    
    await ctx.send(embed=embed)

@tree.command(name="random", description="Generate a random number", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(
    min_value="Minimum value (default: 1)",
    max_value="Maximum value (default: 100)"
)
@cooldown_with_exemption(3, 5.0, key=lambda i: (i.guild_id, i.user.id))  # 3 uses per 5 seconds (exempt role bypasses)
async def random_command(interaction: discord.Interaction, min_value: int = 1, max_value: int = 100):
    """Generate a random number"""
    if not has_member_access(interaction.user):
        embed = discord.Embed(
            title="Access Denied",
            description="You don't have permission to use this command.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if min_value > max_value:
        embed = discord.Embed(
            title="Invalid Range",
            description="Minimum value must be less than or equal to maximum value.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if max_value - min_value > 1000000:
        embed = discord.Embed(
            title="Range Too Large",
            description="Range cannot exceed 1,000,000.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    result = random.randint(min_value, max_value)
    
    embed = discord.Embed(
        title="Random Number",
        description=f"**Result:** `{result}`\n**Range:** {min_value} - {max_value}",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    await interaction.response.send_message(embed=embed)

@tree.command(name="flip", description="Flip a coin", guild=discord.Object(id=ALLOWED_GUILD_ID))
@cooldown_with_exemption(5, 3.0, key=lambda i: (i.guild_id, i.user.id))  # 5 uses per 3 seconds (exempt role bypasses)
async def flip_command(interaction: discord.Interaction):
    """Flip a coin"""
    if not has_member_access(interaction.user):
        embed = discord.Embed(
            title="Access Denied",
            description="You don't have permission to use this command.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    result = random.choice(["Heads", "Tails"])
    emoji = "🪙" if result == "Heads" else "🪙"
    
    embed = discord.Embed(
        title="Coin Flip",
        description=f"**Result:** {result} {emoji}",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    await interaction.response.send_message(embed=embed)

@tree.command(name="dice", description="Roll dice", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(
    sides="Number of sides (default: 6)",
    count="Number of dice to roll (default: 1, max: 10)"
)
@cooldown_with_exemption(5, 3.0, key=lambda i: (i.guild_id, i.user.id))  # 5 uses per 3 seconds (exempt role bypasses)
async def dice_command(interaction: discord.Interaction, sides: int = 6, count: int = 1):
    """Roll dice"""
    if not has_member_access(interaction.user):
        embed = discord.Embed(
            title="Access Denied",
            description="You don't have permission to use this command.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if sides < 2 or sides > 100:
        embed = discord.Embed(
            title="Invalid Sides",
            description="Number of sides must be between 2 and 100.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if count < 1 or count > 10:
        embed = discord.Embed(
            title="Invalid Count",
            description="Number of dice must be between 1 and 10.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    results = [random.randint(1, sides) for _ in range(count)]
    total = sum(results)
    
    results_str = ", ".join([str(r) for r in results])
    if count > 1:
        description = f"**Rolls:** {results_str}\n**Total:** {total}"
    else:
        description = f"**Result:** {results[0]}"
    
    embed = discord.Embed(
        title="Dice Roll",
        description=description,
        color=0x5865F2,
        timestamp=datetime.now()
    )
    embed.set_footer(text=f"{count} d{sides}")
    
    await interaction.response.send_message(embed=embed)

@tree.command(name="choose", description="Choose randomly from options", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(
    options="Options separated by commas (e.g., apple, banana, orange)"
)
@cooldown_with_exemption(5, 3.0, key=lambda i: (i.guild_id, i.user.id))  # 5 uses per 3 seconds (exempt role bypasses)
async def choose_command(interaction: discord.Interaction, options: str):
    """Choose randomly from options"""
    if not has_member_access(interaction.user):
        embed = discord.Embed(
            title="Access Denied",
            description="You don't have permission to use this command.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    choices = [opt.strip() for opt in options.split(",") if opt.strip()]
    
    if len(choices) < 2:
        embed = discord.Embed(
            title="Not Enough Options",
            description="Please provide at least 2 options separated by commas.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if len(choices) > 20:
        embed = discord.Embed(
            title="Too Many Options",
            description="Maximum 20 options allowed.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    chosen = random.choice(choices)
    
    embed = discord.Embed(
        title="Random Choice",
        description=f"**Chosen:** {chosen}\n\n**Options:** {', '.join(choices)}",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    await interaction.response.send_message(embed=embed)

@bot.command(name="random", aliases=["rand", "rng"])
@commands.cooldown(3, 5.0, commands.BucketType.user)  # 3 uses per 5 seconds
async def random_prefix(ctx, min_value: int = 1, max_value: int = 100):
    """Prefix version of /random command"""
    if ctx.guild.id != ALLOWED_GUILD_ID or not has_member_access(ctx.author):
        return
    
    await ctx.typing()
    
    if min_value > max_value:
        embed = discord.Embed(
            title="Invalid Range",
            description="Minimum value must be less than or equal to maximum value.",
            color=0xED4245
        )
        await ctx.send(embed=embed)
        return
    
    if max_value - min_value > 1000000:
        embed = discord.Embed(
            title="Range Too Large",
            description="Range cannot exceed 1,000,000.",
            color=0xED4245
        )
        await ctx.send(embed=embed)
        return
    
    result = random.randint(min_value, max_value)
    
    embed = discord.Embed(
        title="Random Number",
        description=f"**Result:** `{result}`\n**Range:** {min_value} - {max_value}",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    await ctx.send(embed=embed)

@bot.command(name="flip", aliases=["coin", "coinflip"])
@commands.cooldown(5, 3.0, commands.BucketType.user)  # 5 uses per 3 seconds
async def flip_prefix(ctx):
    """Prefix version of /flip command"""
    if ctx.guild.id != ALLOWED_GUILD_ID or not has_member_access(ctx.author):
        return
    
    result = random.choice(["Heads", "Tails"])
    emoji = "🪙"
    
    embed = discord.Embed(
        title="Coin Flip",
        description=f"**Result:** {result} {emoji}",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    await ctx.send(embed=embed)

@bot.command(name="dice", aliases=["d", "roll"])
@commands.cooldown(5, 3.0, commands.BucketType.user)  # 5 uses per 3 seconds
async def dice_prefix(ctx, sides: int = 6, count: int = 1):
    """Prefix version of /dice command"""
    if ctx.guild.id != ALLOWED_GUILD_ID or not has_member_access(ctx.author):
        return
    
    await ctx.typing()
    
    if sides < 2 or sides > 100:
        embed = discord.Embed(
            title="Invalid Sides",
            description="Number of sides must be between 2 and 100.",
            color=0xED4245
        )
        await ctx.send(embed=embed)
        return
    
    if count < 1 or count > 10:
        embed = discord.Embed(
            title="Invalid Count",
            description="Number of dice must be between 1 and 10.",
            color=0xED4245
        )
        await ctx.send(embed=embed)
        return
    
    results = [random.randint(1, sides) for _ in range(count)]
    total = sum(results)
    
    results_str = ", ".join([str(r) for r in results])
    if count > 1:
        description = f"**Rolls:** {results_str}\n**Total:** {total}"
    else:
        description = f"**Result:** {results[0]}"
    
    embed = discord.Embed(
        title="Dice Roll",
        description=description,
        color=0x5865F2,
        timestamp=datetime.now()
    )
    embed.set_footer(text=f"{count} d{sides}")
    
    await ctx.send(embed=embed)

@bot.command(name="choose", aliases=["pick", "select"])
@commands.cooldown(5, 3.0, commands.BucketType.user)  # 5 uses per 3 seconds
async def choose_prefix(ctx, *, options: str):
    """Prefix version of /choose command"""
    if ctx.guild.id != ALLOWED_GUILD_ID or not has_member_access(ctx.author):
        return
    
    await ctx.typing()
    
    choices = [opt.strip() for opt in options.split(",") if opt.strip()]
    
    if len(choices) < 2:
        embed = discord.Embed(
            title="Not Enough Options",
            description="Please provide at least 2 options separated by commas.",
            color=0xED4245
        )
        await ctx.send(embed=embed)
        return
    
    if len(choices) > 20:
        embed = discord.Embed(
            title="Too Many Options",
            description="Maximum 20 options allowed.",
            color=0xED4245
        )
        await ctx.send(embed=embed)
        return
    
    chosen = random.choice(choices)
    
    embed = discord.Embed(
        title="Random Choice",
        description=f"**Chosen:** {chosen}\n\n**Options:** {', '.join(choices)}",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    await ctx.send(embed=embed)

@bot.event
async def on_message(message: discord.Message):
    """Handle messages, including bot mentions for help"""
    # Check if bot is mentioned
    if bot.user in message.mentions and not message.author.bot:
        if message.guild and message.guild.id == ALLOWED_GUILD_ID:
            embed = discord.Embed(
                title="8Bit | Renderer - Commands",
                description="Available commands for 8Bit Bot",
                color=0x5865F2,
                timestamp=datetime.now()
            )
            
            # Combined commands with aliases and cooldowns
            commands_list = [
                ("`/render`", "Render a build file to 3D", "MEMBER", ["*r", "*render"], "30s"),
                ("`/usage`", "View R2 storage usage statistics", "DEVELOPER", ["*u", "*usage"], None),
                ("`/builds`", "View cached builds with pagination", "DEVELOPER", ["*b", "*builds"], None),
                ("`/list-duplicates`", "List builds with same file size", "DEVELOPER", ["*ld", "*list-duplicates"], None),
                ("`/delete <model_id>`", "Delete a model from storage and cache", "DEVELOPER", ["*del", "*delete"], None),
                ("`/uptime`", "View bot uptime", "MEMBER", ["*ut", "*uptime"], "10s (3x)"),
                ("`/credits`", "View bot credits and information", "MEMBER", ["*credits", "*credit", "*about"], None),
                ("`/systeminfo`", "View bot system information", "DEVELOPER", ["*si", "*systeminfo"], None),
                ("`/image2link`", "Convert image to Discord CDN link", "MEMBER", ["*i2l", "*image2link"], "10s (5x)"),
                ("`/random [min] [max]`", "Generate a random number", "MEMBER", ["*random", "*rand", "*rng"], "5s (3x)"),
                ("`/flip`", "Flip a coin", "MEMBER", ["*flip", "*coin", "*coinflip"], "3s (5x)"),
                ("`/dice [sides] [count]`", "Roll dice", "MEMBER", ["*d", "*dice", "*roll"], "3s (5x)"),
                ("`/choose <options>`", "Choose randomly from options", "MEMBER", ["*choose", "*pick", "*select"], "3s (5x)"),
            ]
            
            # Separate commands by access level
            member_commands = [cmd for cmd in commands_list if cmd[2] == "MEMBER"]
            developer_commands = [cmd for cmd in commands_list if cmd[2] == "DEVELOPER"]
            
            # Format commands into a string, splitting if needed
            def format_commands(commands_list, max_length=1024):
                """Format commands into a string, splitting if needed"""
                parts = []
                current_part = []
                current_length = 0
                
                for i, cmd_data in enumerate(commands_list):
                    cmd, desc, level, aliases, cooldown = cmd_data
                    alias_str = " • ".join([f"`{alias}`" for alias in aliases])
                    # Clean format: command - description (Aliases: ...) [Cooldown: ...]
                    cooldown_text = f" [Cooldown: {cooldown}]" if cooldown else ""
                    # Add divider between commands (except for the first one)
                    divider = "────────────────────────────────────────\n" if i > 0 else ""
                    # Use proper markdown formatting without nested italics
                    command_text = f"{divider}{cmd} - {desc}\nAliases: {alias_str}{cooldown_text}\n\n"
                    command_length = len(command_text)
                    
                    # If adding this command would exceed max_length, start a new part
                    if current_length + command_length > max_length and current_part:
                        parts.append("".join(current_part).rstrip())
                        current_part = []
                        current_length = 0
                    
                    current_part.append(command_text)
                    current_length += command_length
                
                # Add the last part if it exists
                if current_part:
                    parts.append("".join(current_part).rstrip())
                
                return parts
            
            # Format member commands
            member_parts = format_commands(member_commands)
            for i, part in enumerate(member_parts):
                field_name = "👤 Member Commands" if i == 0 else "👤 Member Commands (cont.)"
                embed.add_field(name=field_name, value=part, inline=False)
            
            # Format developer commands
            developer_parts = format_commands(developer_commands)
            for i, part in enumerate(developer_parts):
                field_name = "🔧 Developer Commands" if i == 0 else "🔧 Developer Commands (cont.)"
                embed.add_field(name=field_name, value=part, inline=False)
            
            embed.set_footer(text="Mention @8Bit to see this help message")
            
            await message.channel.send(embed=embed)
    
    # Process commands normally
    await bot.process_commands(message)

@tree.command(name="uptime", description="View bot uptime", guild=discord.Object(id=ALLOWED_GUILD_ID))
@cooldown_with_exemption(3, 10.0, key=lambda i: (i.guild_id, i.user.id))  # 3 uses per 10 seconds (exempt role bypasses)
async def uptime_command(interaction: discord.Interaction):
    """View bot uptime"""
    if not has_member_access(interaction.user):
        embed = discord.Embed(
            title="Access Denied",
            description="You don't have permission to use this command.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    if _bot_start_time is None:
        uptime_seconds = 0
    else:
        uptime_seconds = int(time.time() - _bot_start_time)
    
    days = uptime_seconds // 86400
    hours = (uptime_seconds % 86400) // 3600
    minutes = (uptime_seconds % 3600) // 60
    seconds = uptime_seconds % 60
    
    uptime_str = f"{days}d {hours}h {minutes}m {seconds}s"
    if days == 0:
        uptime_str = f"{hours}h {minutes}m {seconds}s"
    if days == 0 and hours == 0:
        uptime_str = f"{minutes}m {seconds}s"
    if days == 0 and hours == 0 and minutes == 0:
        uptime_str = f"{seconds}s"
    
    embed = discord.Embed(
        title="Bot Uptime",
        description=f"**Uptime:** {uptime_str}\n**Started:** <t:{int(_bot_start_time)}:R>" if _bot_start_time else "Uptime tracking not available",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    await interaction.followup.send(embed=embed, ephemeral=True)

@tree.command(name="credits", description="View bot credits and information", guild=discord.Object(id=ALLOWED_GUILD_ID))
async def credits_command(interaction: discord.Interaction):
    """View bot credits and information"""
    await interaction.response.defer(ephemeral=False)
    
    # Try to get the creator user
    creator_id = 1149910630678134916
    creator_mention = f"<@{creator_id}>"
    try:
        creator_user = await bot.fetch_user(creator_id)
        creator_mention = creator_user.mention
        creator_name = creator_user.display_name
    except:
        creator_name = "_zenix"
    
    embed = discord.Embed(
        title="8Bit | Renderer - Credits",
        description="**Thank you for using 8Bit Renderer!**\n\nThis bot was created with ❤️ by the 8Bit team.",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    embed.add_field(
        name="👤 Bot Creator",
        value=f"**{creator_name}**\n`{creator_id}`\n{creator_mention}",
        inline=True
    )
    
    embed.add_field(
        name="🔧 API Creator",
        value=f"**{creator_name}**\n`{creator_id}`\n{creator_mention}",
        inline=True
    )
    
    embed.add_field(
        name="\u200b",
        value="\u200b",
        inline=True
    )
    
    embed.add_field(
        name="🛠️ Technologies & Services",
        value="• **Flowkit** - 3D Model Preview Generation\n• **PythonAnywhere** - Backend Hosting\n• **Railway** - Bot Hosting\n• **R2 Cloud Storage** - File Storage\n• **Discord.py** - Bot Framework\n• **Three.js** - 3D Viewer",
        inline=False
    )
    
    embed.set_footer(
        text="8Bit | Renderer",
        icon_url=str(bot.user.avatar.url) if bot.user and bot.user.avatar else None
    )
    
    await interaction.followup.send(embed=embed)

@tree.command(name="systeminfo", description="View bot system information", guild=discord.Object(id=ALLOWED_GUILD_ID))
async def systeminfo_command(interaction: discord.Interaction):
    """View bot system information"""
    if not has_dev_access(interaction.user):
        embed = discord.Embed(
            title="Access Denied",
            description="You don't have permission to use this command.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    # Detect hosting platform
    hosting_platform = "Unknown"
    if os.environ.get('RAILWAY_ENVIRONMENT'):
        hosting_platform = "Railway"
    elif os.environ.get('HEROKU_APP_NAME'):
        hosting_platform = "Heroku"
    elif os.environ.get('VERCEL'):
        hosting_platform = "Vercel"
    elif os.path.exists('/.dockerenv'):
        hosting_platform = "Docker"
    else:
        hosting_platform = "Local"
    
    # System info
    cpu_percent = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    memory_percent = memory.percent
    memory_used_gb = memory.used / (1024**3)
    memory_total_gb = memory.total / (1024**3)
    
    disk = psutil.disk_usage('/')
    disk_percent = disk.percent
    disk_used_gb = disk.used / (1024**3)
    disk_total_gb = disk.total / (1024**3)
    
    # Python info
    python_version = platform.python_version()
    platform_info = platform.platform()
    
    # Bot info
    guild_count = len(bot.guilds)
    user_count = len(bot.users)
    
    embed = discord.Embed(
        title="System Information",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    embed.add_field(
        name="Hosting Platform",
        value=f"**Platform:** {hosting_platform}\n**Environment:** {os.environ.get('RAILWAY_ENVIRONMENT', 'Production' if hosting_platform != 'Local' else 'Development')}",
        inline=False
    )
    
    embed.add_field(
        name="System Resources",
        value=f"**CPU Usage:** {cpu_percent:.1f}%\n**Memory:** {memory_percent:.1f}% ({memory_used_gb:.2f} GB / {memory_total_gb:.2f} GB)\n**Disk:** {disk_percent:.1f}% ({disk_used_gb:.2f} GB / {disk_total_gb:.2f} GB)",
        inline=False
    )
    
    embed.add_field(
        name="Bot Statistics",
        value=f"**Guilds:** {guild_count}\n**Users:** {user_count}\n**Python:** {python_version}",
        inline=False
    )
    
    embed.add_field(
        name="System",
        value=f"**Platform:** {platform_info}",
        inline=False
    )
    
    await interaction.followup.send(embed=embed, ephemeral=True)

async def extract_image_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)
        
        if 'google.com/url' in url:
            if 'url' in query_params:
                actual_url = unquote(query_params['url'][0])
                return actual_url
        
        if 'gstatic.com' in url or 'googleusercontent.com' in url:
            return url
        
        if 'url=' in url:
            for param_name in ['url', 'image', 'src', 'link']:
                if param_name in query_params:
                    potential_url = unquote(query_params[param_name][0])
                    if potential_url.startswith(('http://', 'https://')):
                        return potential_url
        
        return url
    except:
        return url

async def download_image_from_url(url: str) -> tuple[BytesIO, str]:
    try:
        actual_url = await extract_image_url(url)
        
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(actual_url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': 'https://www.google.com/'
            })
            response.raise_for_status()
            
            if len(response.content) == 0:
                raise ValueError("Downloaded image is empty")
            
            content = response.content
            is_image = False
            content_type = response.headers.get('content-type', '').split(';')[0].strip()
            
            if content[:4] == b'\x89PNG':
                is_image = True
                content_type = 'image/png'
            elif content[:2] == b'\xff\xd8':
                is_image = True
                content_type = 'image/jpeg'
            elif content[:6] == b'GIF89a' or content[:6] == b'GIF87a':
                is_image = True
                content_type = 'image/gif'
            elif content[:4] == b'RIFF' and content[8:12] == b'WEBP':
                is_image = True
                content_type = 'image/webp'
            elif content_type.startswith('image/'):
                is_image = True
            else:
                if content_type.startswith('image/'):
                    is_image = True
                else:
                    raise ValueError("URL does not point to a valid image")
            
            if not is_image:
                raise ValueError("URL does not point to a valid image")
            
            image_data = BytesIO(content)
            image_data.seek(0)  # Reset to beginning
            
            return image_data, content_type
    except httpx.HTTPError as e:
        raise ValueError(f"HTTP error downloading image: {str(e)}")
    except Exception as e:
        raise ValueError(f"Failed to download image: {str(e)}")

@tree.command(name="image2link", description="Convert image to Discord CDN link", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(
    image="Image attachment to convert"
)
@cooldown_with_exemption(5, 10.0, key=lambda i: (i.guild_id, i.user.id))  # 5 uses per 10 seconds (exempt role bypasses)
async def image2link_command(interaction: discord.Interaction, image: discord.Attachment = None):
    """Convert image to Discord CDN link"""
    if not has_member_access(interaction.user):
        embed = discord.Embed(
            title="Access Denied",
            description="You don't have permission to use this command.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    await interaction.response.defer()
    
    cdn_url = None
    
    # Check if image attachment is provided
    if image:
        if not image.content_type or not image.content_type.startswith('image/'):
            embed = discord.Embed(
                title="Invalid File",
                description="The provided file is not an image.",
                color=0xED4245
            )
            await interaction.followup.send(embed=embed)
            return
        cdn_url = image.url
    else:
        # Check if there's an image in the message
        if interaction.message and interaction.message.attachments:
            image = interaction.message.attachments[0]
            if image.content_type and image.content_type.startswith('image/'):
                cdn_url = image.url
            else:
                embed = discord.Embed(
                    title="Invalid File",
                    description="The provided file is not an image.",
                    color=0xED4245
                )
                await interaction.followup.send(embed=embed)
                return
        else:
            embed = discord.Embed(
                title="No Image Provided",
                description="Please attach an image file.",
                color=0xED4245
            )
            await interaction.followup.send(embed=embed)
            return
    
    if not cdn_url:
        embed = discord.Embed(
            title="Error",
            description="Failed to get Discord CDN link.",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed)
        return
    
    embed = discord.Embed(
        title="Image Link",
        color=0x5865F2,
        timestamp=datetime.now()
    )
    
    description_parts = []
    description_parts.append(f"[Click here]({cdn_url})")
    description_parts.append(f"```{cdn_url}```")
    
    embed.description = "\n".join(description_parts)
    embed.set_image(url=cdn_url)
    embed.set_footer(text="Discord CDN link (permanent)")
    
    await interaction.followup.send(embed=embed)

@render_command.error
@random_command.error
@flip_command.error
@dice_command.error
@choose_command.error
@uptime_command.error
@image2link_command.error
async def cooldown_error_handler(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Handle cooldown errors for slash commands"""
    if isinstance(error, app_commands.CommandOnCooldown):
        # Check if user is exempt from cooldowns
        if is_cooldown_exempt(interaction.user):
            # User is exempt, bypass cooldown by not raising error
            # The command will execute normally
            return
        
        retry_after = error.retry_after
        embed = discord.Embed(
            title="⏱️ Cooldown Active",
            description=f"Please wait **{retry_after:.1f} seconds** before using this command again.",
            color=0xFFA500,
            timestamp=datetime.now()
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        raise error

@bot.event
async def on_ready():
    """Called when bot is ready"""
    global _bot_start_time
    _bot_start_time = time.time()
    
    print(f"{bot.user} has connected to Discord!")
    print(f"Bot ID: {bot.user.id}")
    print(f"Guilds: {len(bot.guilds)}")
    
    # Sync slash commands to specific guild
    try:
        guild = discord.Object(id=ALLOWED_GUILD_ID)
        
        # Wait a bit for Discord to be ready
        await asyncio.sleep(2)
        
        # CRITICAL: Check and clear GLOBAL commands first (this is likely the issue!)
        print("Checking for global commands (these cause duplicates)...")
        try:
            global_commands = await tree.fetch_commands(guild=None)
            if global_commands:
                print(f"⚠ Found {len(global_commands)} GLOBAL command(s) - DELETING to prevent duplicates!")
                for cmd in global_commands:
                    print(f"  - /{cmd.name} (ID: {cmd.id}) - DELETING")
                    try:
                        # Delete individual global command
                        await bot.http.delete_global_command(bot.application_id, cmd.id)
                    except Exception as del_error:
                        print(f"    Could not delete {cmd.name}: {del_error}")
                print("✓ Cleared all global commands")
            else:
                print("✓ No global commands found")
        except Exception as global_error:
            print(f"Could not check/clear global commands: {global_error}")
        
        # Get existing commands from Discord guild to check for duplicates
        print("Checking existing guild commands...")
        try:
            existing_commands = await tree.fetch_commands(guild=guild)
            if existing_commands:
                print(f"Found {len(existing_commands)} existing guild command(s):")
                for cmd in existing_commands:
                    print(f"  - /{cmd.name} (ID: {cmd.id})")
        except Exception as fetch_error:
            print(f"Could not fetch existing commands: {fetch_error}")
        
        # Count commands in tree before sync
        tree_commands = tree.get_commands(guild=guild)
        print(f"Commands in tree: {len(tree_commands)}")
        for cmd in tree_commands:
            print(f"  - /{cmd.name}")
        
        # Sync commands to guild ONLY (this will replace existing ones)
        print("Syncing commands to guild...")
        synced = await tree.sync(guild=guild)
        print(f"Synced {len(synced)} slash command(s) to guild {ALLOWED_GUILD_ID}!")
        for cmd in synced:
            print(f"  - /{cmd.name}")
        
        # Verify commands are registered
        if len(synced) == 0:
            print("ERROR: No commands synced! This indicates a serious sync issue.")
        else:
            print(f"✓ Successfully registered {len(synced)} command(s)!")
            
        # Double-check for duplicates (both guild and global)
        await asyncio.sleep(2)
        try:
            final_guild_commands = await tree.fetch_commands(guild=guild)
            final_global_commands = await tree.fetch_commands(guild=None)
            
            guild_names = [cmd.name for cmd in final_guild_commands]
            global_names = [cmd.name for cmd in final_global_commands]
            
            # Check for duplicates within guild
            duplicates = [name for name in guild_names if guild_names.count(name) > 1]
            if duplicates:
                print(f"⚠ WARNING: Found duplicate guild commands: {set(duplicates)}")
            
            # Check for conflicts between global and guild (THIS CAUSES DUPLICATES IN DISCORD UI!)
            conflicts = set(guild_names) & set(global_names)
            if conflicts:
                print(f"⚠ CRITICAL: Commands exist in BOTH global and guild: {conflicts}")
                print("  This causes duplicates in Discord! Force deleting global versions...")
                for cmd_name in conflicts:
                    try:
                        # Find and delete the global command
                        for global_cmd in final_global_commands:
                            if global_cmd.name == cmd_name:
                                await bot.http.delete_global_command(bot.application_id, global_cmd.id)
                                print(f"    Deleted global /{cmd_name}")
                    except Exception as del_error:
                        print(f"    Could not delete global {cmd_name}: {del_error}")
                print("✓ Cleared conflicting global commands")
            
            if not duplicates and not conflicts:
                print("✓ No duplicate commands detected!")
        except Exception as check_error:
            print(f"Could not verify commands: {check_error}")
            
    except Exception as e:
        print(f"Error syncing commands: {e}")
        import traceback
        traceback.print_exc()

@tree.command(name="clearnopreviewcache", description="Clear cached builds with no preview URL (Devs only)", guild=discord.Object(id=ALLOWED_GUILD_ID))
async def clear_no_preview_cache_command(interaction: discord.Interaction):
    """Clear cached builds that have no preview URL"""
    if not has_dev_access(interaction.user):
        embed = discord.Embed(
            title="Access Denied",
            description="You don't have permission to use this command.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        server_url = await get_active_server_url()
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{server_url}/api/clear-cache",
                headers={
                    'X-API-Secret': WEB_SERVER_SECRET
                }
            )
            
        if response.status_code == 200:
            data = response.json()
            count = data.get('count', 0)
            
            embed = discord.Embed(
                title="Cache Cleared",
                description=f"Successfully cleared **{count}** cached builds that had no preview URL.",
                color=0x57F287
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            embed = discord.Embed(
                title="Error",
                description=f"Failed to clear cache. Status: {response.status_code}\nResponse: {response.text}",
                color=0xED4245
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            
    except Exception as e:
        embed = discord.Embed(
            title="Error",
            description=f"An error occurred: {str(e)}",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        await interaction.followup.send(embed=embed, ephemeral=True)

@tree.command(name="checkcache", description="Check cache statistics (Devs only)", guild=discord.Object(id=ALLOWED_GUILD_ID))
async def check_cache_command(interaction: discord.Interaction):
    """Check cache statistics"""
    if not has_dev_access(interaction.user):
        embed = discord.Embed(
            title="Access Denied",
            description="You don't have permission to use this command.",
            color=0xED4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        server_url = await get_active_server_url()
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{server_url}/api/cache-stats",
                headers={
                    'X-API-Secret': WEB_SERVER_SECRET
                }
            )
            
        if response.status_code == 200:
            data = response.json()
            stats = data.get('stats', {})
            
            total_builds = stats.get('total_builds', 0)
            builds_without_preview = stats.get('builds_without_preview', 0)
            total_size = stats.get('total_size_bytes', 0)
            
            # Format size
            if total_size < 1024:
                size_str = f"{total_size} B"
            elif total_size < 1024 * 1024:
                size_str = f"{total_size / 1024:.1f} KB"
            else:
                size_str = f"{total_size / (1024 * 1024):.1f} MB"
            
            embed = discord.Embed(
                title="Cache Statistics",
                description=f"**Total Cached Builds:** {total_builds}\n**Builds Without Preview:** {builds_without_preview}\n**Total Cache Size:** {size_str}",
                color=0x5865F2,
                timestamp=datetime.now()
            )
            
            if builds_without_preview > 0:
                embed.add_field(
                    name="Cleanup Available", 
                    value=f"You can clear {builds_without_preview} builds using `/clearnopreviewcache`",
                    inline=False
                )
                
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            embed = discord.Embed(
                title="Error",
                description=f"Failed to get cache stats. Status: {response.status_code}\nResponse: {response.text}",
                color=0xED4245
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            
    except Exception as e:
        embed = discord.Embed(
            title="Error",
            description=f"An error occurred: {str(e)}",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

def main():
    if not DISCORD_BOT_TOKEN:
        exit(1)
    
    bot.run(DISCORD_BOT_TOKEN)

if __name__ == "__main__":
    main()

