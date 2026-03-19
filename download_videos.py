#!/usr/bin/env python
from dotenv import load_dotenv
from fasthtml.common import *
from monsterui.all import *
import asyncio
import requests
from pathlib import Path
import aiohttp
import tempfile
import os
import time
import subprocess


load_dotenv()
YOUR_API_BASE_URL = os.environ.get('YOUR_API_BASE_URL')


custom_headers = Theme.blue.headers() + [
    Link(rel="icon", href="https://api.dicebear.com/7.x/fun-emoji/svg?seed=100&size=32", type="image/png"),
    Link(rel="shortcut icon", href="https://api.dicebear.com/7.x/fun-emoji/svg?seed=100&size=32", type="image/png")
]

app, rt = fast_app(hdrs=custom_headers)
temp_files = {}
download_progress = {}

async def get_vid_info(vid):
    url = f"https://{YOUR_API_BASE_URL}/v1.1/content/query"
    
    params = {
        "query": "basic,full,translations,like,share,save,movie,tag_list,watch_count",
        "ids": vid,
        "with_desc": "true",
        "include_unlisted": "true",
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as response:
            return await response.json()


async def get_hls_duration(session, master_url):
    """Get total duration from HLS playlist to calculate progress."""
    async with session.get(master_url) as response:
        master_content = await response.text()

    # Find the highest resolution stream URL
    lines = master_content.strip().split('\n')
    stream_url = None
    for line in lines:
        if line.strip() and not line.startswith('#'):
            stream_url = line.strip()

    if not stream_url:
        return 0

    # Make stream URL absolute if it's relative
    if not stream_url.startswith('http'):
        base_url = master_url.rsplit('/', 1)[0]
        stream_url = f"{base_url}/{stream_url}"

    # Parse the stream playlist to get total duration
    async with session.get(stream_url) as response:
        playlist_content = await response.text()

    total_duration = 0
    for line in playlist_content.strip().split('\n'):
        if line.startswith('#EXTINF:'):
            duration_str = line.split(':')[1].split(',')[0]
            total_duration += float(duration_str)

    return total_duration


async def download_gj_video(vid):
    try:
        # Get video info
        vid_info = await get_vid_info(vid)
        video_data = vid_info['data']['list'][0]
        master_url = video_data['video_url']
        video_title = video_data.get('title', vid)

        # Check available resolution and get duration
        async with aiohttp.ClientSession() as session:
            async with session.get(master_url) as response:
                print(f"Status code: {response.status}")
                if response.status != 200:
                    raise Exception(f"Failed to fetch master.m3u8. Status: {response.status}")
                master_content = await response.text()
                if '1080p' in master_content:
                    resoln = '1080p'
                elif '720p' in master_content:
                    resoln = '720p'
                elif '480p' in master_content:
                    resoln = '480p'
                else:
                    resoln = '360p'

            total_duration = await get_hls_duration(session, master_url)

        # Update resolution in progress
        download_progress[vid]['resolution'] = resoln

        # Prepare output path
        safe_filename = f"{video_title}.mp4"
        file_path = os.path.join(tempfile.gettempdir(), safe_filename)

        # Remove existing file so ffmpeg doesn't prompt to overwrite
        if os.path.exists(file_path):
            os.remove(file_path)

        # Use async subprocess so we don't block the event loop
        cmd = [
            "ffmpeg",
            "-loglevel", "info",
            "-progress", "pipe:1",  # Output progress to stdout
            "-i", master_url,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            file_path
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        # Parse ffmpeg progress output to update progress
        current_time_us = 0
        total_duration_us = total_duration * 1_000_000 if total_duration > 0 else 0

        async for line in process.stdout:
            decoded = line.decode('utf-8', errors='replace').strip()
            if decoded.startswith('out_time_us='):
                try:
                    current_time_us = int(decoded.split('=')[1])
                    if total_duration_us > 0:
                        progress = min(int((current_time_us / total_duration_us) * 100), 99)
                        download_progress[vid]['progress'] = progress
                except (ValueError, IndexError):
                    pass

        return_code = await process.wait()
        if return_code != 0:
            stderr_output = await process.stderr.read()
            raise Exception(f"ffmpeg exited with code {return_code}: {stderr_output.decode('utf-8', errors='replace')[:500]}")

        # Update download progress
        download_progress[vid] = {'progress': 100, 'complete': True, 'resolution': resoln}
        temp_files[vid] = file_path
        return file_path

    except Exception as e:
        download_progress[vid] = {'error': str(e)}
        raise


@rt
def index():
    return Titled("Video Downloader", 
    Form(
        Card(
            LabelInput("Please enter video ID", id="gj_video", name="gj_video"),
            DivCentered(
                Button("Download Video", 
                cls=ButtonT.primary, 
                hx_post="/start-download", 
                hx_target="#result")
            ),
            cls="mt-5 max-w-md mx-auto"
        )
    ),
    Div(id="result", cls="max-w-md mx-auto"),  # Results will appear here
    cls=(TextT.center, 'mt-5')
)

@app.route('/start-download', methods=['POST'])
async def start_download(request):
    form_data = await request.form()
    video_id = form_data.get("gj_video", "").strip()
    
    if not video_id:
        return Alert("Please enter a video ID", cls=AlertT.warning)
    
    download_progress[video_id] = {
        'progress': 0, 
        'start_time': time.time(),  
        'downloaded_bytes': 0,
        'resolution': 'Detecting...'
    }
    asyncio.create_task(download_gj_video(video_id))
    
    # Return a progress bar that will automatically poll for updates
    return Div(
        H4(f"Downloading video...", cls="mt-3"),
        Div(id="video-info", cls=(TextPresets.muted_sm, "mt-2")),
        Progress(value=0, max=100, id="download-progress", cls="mt-3"),
        P("Starting downloading...", id="progress-text", cls=TextPresets.muted_sm),
        # This div will automatically refresh to show progress updates
        Div(id="progress-container",
            hx_get=f"/check-progress/{video_id}",
            hx_trigger="every 1s",
            hx_swap="outerHTML")
    )


@app.route('/check-progress/{vid}', methods=['GET'])
async def check_progress(request):
    vid = request.path_params["vid"]
    
    if vid not in download_progress:
        return Div("Download not found", id="progress-container")
    
    progress_data = download_progress[vid]
    progress = progress_data.get('progress', 0)
    resolution = progress_data.get('resolution', 'Unknown')

    # Calculate estimated time remaining
    time_message = ""
    if progress > 0 and progress < 100:
        start_time = progress_data.get('start_time', 0)
        downloaded_bytes = progress_data.get('downloaded_bytes', 0)
        total_bytes = progress_data.get('total_bytes', 0)
        
        if start_time > 0 and downloaded_bytes > 0 and total_bytes > 0:
            elapsed_time = time.time() - start_time
            bytes_per_second = downloaded_bytes / elapsed_time if elapsed_time > 0 else 0
            
            if bytes_per_second > 0:
                remaining_bytes = total_bytes - downloaded_bytes
                remaining_seconds = remaining_bytes / bytes_per_second
                
                if remaining_seconds < 60:
                    time_message = f" - About {int(remaining_seconds)} seconds remaining"
                elif remaining_seconds < 3600:
                    time_message = f" - About {int(remaining_seconds/60)} minutes remaining"
                else:
                    time_message = f" - About {int(remaining_seconds/3600)} hours remaining"

    if progress_data.get('complete', False):
        if progress < 100 or progress_data.get('shown_complete', False) is not True:
            download_progress[vid]['progress'] = 100
            download_progress[vid]['shown_complete'] = True
            
            return Div(
                Script("document.getElementById('download-progress').value = 100;"),
                Script("document.getElementById('progress-text').innerText = 'Download complete (100%)';"),
                # Poll one more time to show the download link
                hx_get=f"/check-progress/{vid}",
                hx_trigger="load delay:500ms",
                hx_swap="outerHTML",
                id="progress-container"
            )
        
        return DivLAligned(
            P(f"Video downloaded!"),
            A("Save", 
              href=f"/serve-video/{vid}", 
              cls=(ButtonT.primary, "mt-5", "p-3","rounded-md"),
              download=True),
            id="progress-container"
        )
    
    if progress_data.get('error', False):
        return Div(
            Alert(f"Error: {progress_data['error']}", cls=AlertT.error),
            id="progress-container"
        )
    
    return Div(
        Script(f"document.getElementById('download-progress').value = {progress};"),
        Script(f"document.getElementById('video-info').innerText = 'Resolution: {resolution}';"),
        Script(f"document.getElementById('progress-text').innerText = 'Downloaded {progress}%{time_message}';"),
        # Keep polling
        hx_get=f"/check-progress/{vid}",
        hx_trigger="every 1s",
        hx_swap="outerHTML",
        id="progress-container"
    )

@app.route("/serve-video/{vid}", methods=['GET'])
async def serve_video(request):
    vid = request.path_params["vid"]
    
    if vid not in temp_files or not os.path.exists(temp_files[vid]):
        return PlainTextResponse("Video not found or expired", status_code=404)
    
    # Get the file path
    file_path = temp_files[vid]
    
    # Create a response that will serve the file
    return FileResponse(
        path=file_path,
        filename=os.path.basename(file_path),
        media_type="video/mp4"
    )


serve()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    serve(port=port, host="0.0.0.0")