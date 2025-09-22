import os
import multiprocessing
import secrets

# ---------- Configuration ----------
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}
MAX_FILE_MB = int(os.getenv('MAX_FILE_MB', '100'))
MAX_FILES = int(os.getenv('MAX_FILES', '10'))
MAX_FETCH_MB = int(os.getenv('MAX_FETCH_MB', '100'))  # remote download size limit (URL/Drive)
# Persistent data directory - MUST be defined first
DATA_DIR = os.getenv('DATA_DIR', 'data')
os.makedirs(DATA_DIR, exist_ok=True)

UPLOAD_ROOT = 'uploads'
OUTPUT_ROOT = 'outputs'
# Use any advanced compressor binary compatible with cjpeg arguments (e.g., mozjpeg's cjpeg)
ADVANCED_COMPRESS_BIN = os.getenv('ADVANCED_COMPRESS_BIN', 'cjpeg')
QUALITY = os.getenv('QUALITY', '85')  # fixed to avoid confusing users
ADMIN_TOKEN = os.getenv('ADMIN_TOKEN', 'changeme')
VISITOR_LOG = os.getenv('VISITOR_LOG', os.path.join(DATA_DIR, 'visitors.log'))  # CSV: date,ip

# Processing limits - optimized for 2 vCPU/4GB system
MAX_PIXELS = int(os.getenv('MAX_PIXELS', '150000000'))  # Maximum pixels allowed (150M)
SAFE_PIXELS = int(os.getenv('SAFE_PIXELS', '50000000'))  # Safe processing limit - doubled for 4GB RAM
PROCESS_TIMEOUT = int(os.getenv('PROCESS_TIMEOUT', '300'))  # 5 minutes for large images
MAX_WORKERS = int(os.getenv('MAX_WORKERS', '2'))  # 2 workers for 2 vCPU cores
AUTO_RESIZE = os.getenv('AUTO_RESIZE', 'true').lower() == 'true'  # Enable intelligent resizing
MIN_FREE_MEMORY_MB = int(os.getenv('MIN_FREE_MEMORY_MB', '500'))  # Higher threshold for 4GB system

VIDEO_ALLOWED_EXTENSIONS = {'mp4', 'avi', 'mov', 'mkv', 'wmv', 'flv', 'webm'}
MAX_VIDEO_MB = int(os.getenv('MAX_VIDEO_MB', '1000'))  # 1GB default
MAX_VIDEO_FILES = int(os.getenv('MAX_VIDEO_FILES', '10'))
VIDEO_UPLOAD_ROOT = 'video_uploads'
VIDEO_OUTPUT_ROOT = 'video_outputs'

# Authentication settings
SECRET_KEY = os.getenv('SECRET_KEY', secrets.token_hex(32))
USERS_FILE = os.path.join(DATA_DIR, 'users.json')
SESSION_TIMEOUT = 3600  # 1 hour

# Video processing limits
VIDEO_MAX_WORKERS = int(os.getenv('VIDEO_MAX_WORKERS', '2'))
VIDEO_PROCESS_TIMEOUT = int(os.getenv('VIDEO_PROCESS_TIMEOUT', '3600'))  # 1 hour

# Create video directories
os.makedirs(VIDEO_UPLOAD_ROOT, exist_ok=True)
os.makedirs(VIDEO_OUTPUT_ROOT, exist_ok=True)

# Try to import optional libraries
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = True

try:
    from PIL import ImageDraw, ImageFont, ImageFilter
    PIL_EXTENDED = True
except ImportError:
    PIL_EXTENDED = False

# Ensure directories exist
os.makedirs(UPLOAD_ROOT, exist_ok=True)
os.makedirs(OUTPUT_ROOT, exist_ok=True)
