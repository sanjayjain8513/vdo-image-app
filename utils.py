import os
import time
import uuid
import re
import math
from urllib.parse import urlparse, parse_qs
from werkzeug.utils import secure_filename
from flask import request, make_response
from PIL import Image
import requests
from io import BytesIO
from datetime import datetime
from typing import List, Tuple, Optional

from config import (
    UPLOAD_ROOT, OUTPUT_ROOT, ALLOWED_EXTENSIONS, MAX_FETCH_MB,
    VISITOR_LOG, PSUTIL_AVAILABLE, AUTO_RESIZE, SAFE_PIXELS,
    MAX_PIXELS, MIN_FREE_MEMORY_MB
)

try:
    import psutil
except ImportError:
    pass

def client_ip():
    """Get client IP address from request headers or remote_addr"""
    return request.headers.get('X-Forwarded-For', request.remote_addr or '0.0.0.0').split(',')[0].strip()

def log_visitor():
    """Log visitor IP and date to visitor log file"""
    ip = client_ip()
    ts = int(time.time())
    date = datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d')
    try:
        with open(VISITOR_LOG, 'a') as f:
            f.write(f"{date},{ip}\n")
    except Exception:
        pass

def cleanup_old_sessions(root, age_seconds=3600):
    """Clean up old session directories and files"""
    now = time.time()
    try:
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if os.path.isdir(path):
                try:
                    if now - os.path.getmtime(path) > age_seconds:
                        for dirpath, _, filenames in os.walk(path, topdown=False):
                            for fn in filenames:
                                try: os.remove(os.path.join(dirpath, fn))
                                except Exception: pass
                            try: os.rmdir(dirpath)
                            except Exception: pass
                except Exception:
                    pass
    except FileNotFoundError:
        pass

def get_session_id(resp=None):
    """Get or create session ID from cookies"""
    sid = request.cookies.get('session_id')
    if not sid:
        sid = uuid.uuid4().hex
        if resp is None:
            resp = make_response()
        resp.set_cookie('session_id', sid, max_age=3600, httponly=True, samesite='Lax')
    return sid, resp

def allowed_file(filename):
    """Check if file has allowed extension"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def ensure_dirs(session_id):
    """Ensure upload and output directories exist for session"""
    up = os.path.join(UPLOAD_ROOT, session_id)
    out = os.path.join(OUTPUT_ROOT, session_id)
    os.makedirs(up, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    return up, out

def safe_save_upload(file_storage, dest_dir):
    """Safely save uploaded file with unique name"""
    orig_name = secure_filename(file_storage.filename)
    name_no_ext, ext = os.path.splitext(orig_name)
    short = uuid.uuid4().hex[:4]
    stored_name = f"{name_no_ext}_{short}{ext.lower()}"
    path = os.path.join(dest_dir, stored_name)
    file_storage.save(path)
    return orig_name, path

def get_system_memory_info():
    """Get current system memory information"""
    if not PSUTIL_AVAILABLE or not AUTO_RESIZE:
        # Fallback if psutil not available or auto-resize disabled
        return {
            'total_mb': 1024,  # Assume 1GB if we can't detect
            'available_mb': 512,
            'used_percent': 50
        }

    try:
        memory = psutil.virtual_memory()
        return {
            'total_mb': memory.total / (1024 * 1024),
            'available_mb': memory.available / (1024 * 1024),
            'used_percent': memory.percent
        }
    except Exception:
        # Fallback if psutil fails
        return {
            'total_mb': 1024,  # Assume 1GB if we can't detect
            'available_mb': 512,
            'used_percent': 50
        }

def calculate_safe_pixel_limit():
    """Calculate safe pixel limit based on available system memory"""
    memory_info = get_system_memory_info()
    available_mb = memory_info['available_mb']

    # Reserve memory for system and other processes
    processing_memory_mb = max(100, available_mb - MIN_FREE_MEMORY_MB)

    # Rough calculation: 1 pixel needs ~6-8 bytes during processing (RGB + overhead)
    # Being conservative with 10 bytes per pixel
    bytes_per_pixel = 10
    safe_pixels = int((processing_memory_mb * 1024 * 1024) / bytes_per_pixel)

    # Apply reasonable bounds
    safe_pixels = max(1000000, min(safe_pixels, SAFE_PIXELS))  # Between 1M and SAFE_PIXELS

    return safe_pixels, memory_info

def get_processing_strategy(image_path):
    """Determine the best processing strategy based on image size and system resources"""
    try:
        with Image.open(image_path) as img:
            width, height = img.size
            pixels = width * height
            file_size_mb = os.path.getsize(image_path) / (1024 * 1024)

        safe_limit, memory_info = calculate_safe_pixel_limit()

        strategy = {
            'original_pixels': pixels,
            'original_size_mb': file_size_mb,
            'safe_limit': safe_limit,
            'available_memory_mb': memory_info['available_mb'],
            'needs_resize': pixels > safe_limit,
            'processing_mode': 'direct'
        }

        # Determine processing mode
        if pixels > MAX_PIXELS:
            strategy['processing_mode'] = 'reject'
            strategy['reason'] = f'Image too large ({pixels:,} pixels). Maximum allowed: {MAX_PIXELS:,} pixels.'
        elif pixels > safe_limit * 4:  # Very large image
            strategy['processing_mode'] = 'aggressive_resize'
            strategy['target_pixels'] = safe_limit // 2  # More aggressive resize
            strategy['reason'] = f'Very large image - will resize to {strategy["target_pixels"]:,} pixels for processing.'
        elif pixels > safe_limit:  # Large image
            strategy['processing_mode'] = 'smart_resize'
            strategy['target_pixels'] = safe_limit
            strategy['reason'] = f'Large image - will resize to {strategy["target_pixels"]:,} pixels to fit available memory.'
        else:
            strategy['processing_mode'] = 'direct'
            strategy['reason'] = 'Image size is within system capacity - processing directly.'

        return strategy

    except Exception as e:
        return {
            'processing_mode': 'error',
            'reason': f'Cannot analyze image: {str(e)}'
        }

def fetch_remote_image(url, dest_dir):
    """Download image from direct URL or Google Drive public link (no auth).
       Enforces MAX_FETCH_MB and image/* content-type."""
    def drive_id_from(u):
        m = re.search(r'/d/([a-zA-Z0-9_-]+)', u)
        if m: return m.group(1)
        qs = parse_qs(urlparse(u).query)
        if 'id' in qs: return qs['id'][0]
        return None

    headers = {'User-Agent': 'Mozilla/5.0'}
    netloc = urlparse(url).netloc
    if 'drive.google.com' in netloc:
        file_id = drive_id_from(url)
        if not file_id:
            raise ValueError("Unsupported Google Drive link format.")
        dl_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        r = requests.get(dl_url, headers=headers, stream=True, timeout=15)
    else:
        r = requests.get(url, headers=headers, stream=True, timeout=15)

    r.raise_for_status()
    content_type = r.headers.get('Content-Type', '')
    if 'image' not in content_type:
        raise ValueError("URL does not point to an image.")

    size = 0
    data = BytesIO()
    for chunk in r.iter_content(chunk_size=8192):
        if not chunk: break
        size += len(chunk)
        if size > MAX_FETCH_MB * 1024 * 1024:
            raise ValueError(f"Remote image exceeds {MAX_FETCH_MB} MB limit.")
        data.write(chunk)
    data.seek(0)

    # Guess extension
    ext = '.jpg'
    if 'png' in content_type: ext = '.png'
    elif 'webp' in content_type: ext = '.webp'
    elif 'jpeg' in content_type or 'jpg' in content_type: ext = '.jpg'
    name_no_ext = 'remote_' + uuid.uuid4().hex[:6]
    stored_name = f"{name_no_ext}{ext}"
    path = os.path.join(dest_dir, stored_name)
    with open(path, 'wb') as f:
        f.write(data.read())
    return stored_name, path

def handle_source_inputs(session_id, upload_field='images', remote_url_field='remote_url', drive_url_field='drive_url'):
    """Handle file uploads and remote URLs"""
    from config import MAX_FILES, MAX_FILE_MB

    up_dir, _ = ensure_dirs(session_id)
    files = request.files.getlist(upload_field)
    selected = []

    # Server-side validation: file count
    total_selected = len([f for f in files if f and f.filename])
    remote_url = request.form.get(remote_url_field, '').strip()
    drive_url = request.form.get(drive_url_field, '').strip()

    if remote_url: total_selected += 1
    if drive_url: total_selected += 1

    if total_selected == 0:
        return [], "No files or URLs provided."
    if total_selected > MAX_FILES:
        return [], f"Max {MAX_FILES} files per request."

    # Save uploads
    for fs in files:
        if not fs or not fs.filename: continue
        if not allowed_file(fs.filename):
            return [], "Unsupported file type. Allowed: jpg, jpeg, png, webp."
        fs.seek(0, os.SEEK_END)
        size = fs.tell()
        fs.seek(0)
        if size > MAX_FILE_MB * 1024 * 1024:
            return [], f"File {fs.filename} exceeds {MAX_FILE_MB} MB limit."

        orig_name, path = safe_save_upload(fs, up_dir)
        selected.append((orig_name, path))

    # Remote URL - Use fetch_remote_image
    if remote_url:
        try:
            name, path = fetch_remote_image(remote_url, up_dir)
            selected.append((name, path))
        except Exception as e:
            return [], f"Remote URL error: {str(e)}"

    # Drive URL
    if drive_url:
        try:
            name, path = fetch_remote_image(drive_url, up_dir)
            selected.append((name, path))
        except Exception as e:
            return [], f"Drive URL error: {str(e)}"

    return selected, None

def handle_multiple_remote_sources(session_id, remote_urls_field='remote_urls', drive_urls_field='drive_urls'):
    """Handle multiple remote URLs for merge functionality"""
    from config import MAX_FILES, MAX_FILE_MB

    up_dir, _ = ensure_dirs(session_id)
    selected = []

    # Handle multiple remote URLs (newline separated)
    remote_urls = request.form.get(remote_urls_field, '').strip()
    if remote_urls:
        urls = [url.strip() for url in remote_urls.split('\n') if url.strip()]
        for url in urls:
            try:
                name, path = fetch_remote_image(url, up_dir)
                selected.append((name, path))
            except Exception as e:
                return [], f"Remote URL error ({url}): {str(e)}"

    # Handle multiple Google Drive URLs (newline separated)
    drive_urls = request.form.get(drive_urls_field, '').strip()
    if drive_urls:
        urls = [url.strip() for url in drive_urls.split('\n') if url.strip()]
        for url in urls:
            try:
                name, path = fetch_remote_image(url, up_dir)
                selected.append((name, path))
            except Exception as e:
                return [], f"Drive URL error ({url}): {str(e)}"

    return selected, None

def validate_social_media_preset(preset_name: str) -> Optional[Tuple[int, int]]:
    """Validate and return dimensions for social media presets"""
    presets = {
        # Instagram
        'instagram-square': (1080, 1080),
        'instagram-portrait': (1080, 1350),
        'instagram-story': (1080, 1920),
        'instagram-landscape': (1080, 566),
        
        # Facebook
        'facebook-post': (1200, 630),
        'facebook-cover': (820, 312),
        'facebook-story': (1080, 1920),
        'facebook-profile': (170, 170),
        
        # Twitter/X
        'twitter-post': (1200, 675),
        'twitter-header': (1500, 500),
        'twitter-profile': (400, 400),
        
        # LinkedIn
        'linkedin-post': (1200, 627),
        'linkedin-cover': (1584, 396),
        'linkedin-profile': (400, 400),
        
        # YouTube
        'youtube-thumbnail': (1280, 720),
        'youtube-banner': (2560, 1440),
        'youtube-profile': (800, 800),
        
        # Pinterest
        'pinterest-pin': (1000, 1500),
        'pinterest-board': (222, 150),
        
        # TikTok
        'tiktok-video': (1080, 1920),
        'tiktok-profile': (200, 200),
        
        # WhatsApp
        'whatsapp-status': (1080, 1920),
        'whatsapp-profile': (640, 640),
        
        # Web & Print
        'web-banner': (728, 90),
        'web-rectangle': (300, 250),
        'print-4x6': (1800, 1200),
        'print-5x7': (2100, 1500),
        'print-8x10': (3000, 2400),
    }
    
    return presets.get(preset_name)

def get_merge_layout_info(layout_mode: str, image_count: int, grid_columns: int = 3) -> dict:
    """Get information about merge layout for UI feedback"""
    if layout_mode == 'horizontal':
        return {
            'description': f'Images will be arranged side by side in a single row',
            'expected_dimensions': f'{image_count} images × 1 row',
            'aspect_ratio': 'Wide (landscape)'
        }
    elif layout_mode == 'vertical':
        return {
            'description': f'Images will be stacked vertically in a single column',
            'expected_dimensions': f'1 column × {image_count} images',
            'aspect_ratio': 'Tall (portrait)'
        }
    elif layout_mode == 'grid':
        rows = math.ceil(image_count / grid_columns)
        actual_cols = min(grid_columns, image_count)
        return {
            'description': f'Images will be arranged in a {actual_cols}×{rows} grid',
            'expected_dimensions': f'{actual_cols} columns × {rows} rows',
            'aspect_ratio': 'Square' if actual_cols == rows else ('Wide' if actual_cols > rows else 'Tall')
        }
    else:
        return {
            'description': 'Unknown layout mode',
            'expected_dimensions': 'Unknown',
            'aspect_ratio': 'Unknown'
        }

def estimate_merged_file_size(image_paths: List[str], layout_mode: str) -> str:
    """Estimate the file size of merged image"""
    try:
        total_pixels = 0
        for path in image_paths:
            with Image.open(path) as img:
                total_pixels += img.size[0] * img.size[1]
        
        # Rough estimation: merged image will have similar total pixel count
        # but compressed, so estimate 3 bytes per pixel for JPEG
        estimated_bytes = total_pixels * 3
        
        if estimated_bytes < 1024 * 1024:
            return f"{estimated_bytes / 1024:.0f} KB"
        else:
            return f"{estimated_bytes / (1024 * 1024):.1f} MB"
    except:
        return "Unknown"

def clean_filename(filename: str) -> str:
    """Clean filename for safe storage and display"""
    # Remove or replace unsafe characters
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    # Limit length
    if len(filename) > 100:
        name, ext = os.path.splitext(filename)
        filename = name[:90] + ext
    return filename

def get_image_metadata(image_path: str) -> dict:
    """Extract basic metadata from image"""
    try:
        with Image.open(image_path) as img:
            return {
                'width': img.size[0],
                'height': img.size[1],
                'mode': img.mode,
                'format': img.format,
                'file_size': os.path.getsize(image_path),
                'megapixels': round((img.size[0] * img.size[1]) / 1000000, 1)
            }
    except Exception as e:
        return {
            'error': str(e),
            'file_size': os.path.getsize(image_path) if os.path.exists(image_path) else 0
        }
