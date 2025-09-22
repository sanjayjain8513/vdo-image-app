FROM python:3.11-slim

# Build deps + mozjpeg from source so `cjpeg` exists + FFmpeg for video processing
RUN apt-get update && apt-get install -y \
    build-essential \
    libjpeg-dev \
    libpng-dev \
    libwebp-dev \
    zlib1g-dev \
    cmake \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    libgthread-2.0-0 \
    git \
    ffmpeg \
    && git clone https://github.com/mozilla/mozjpeg.git /tmp/mozjpeg \
    && cd /tmp/mozjpeg && cmake . && make && make install \
    && ln -sf /opt/mozjpeg/bin/cjpeg /usr/local/bin/cjpeg \
    && ln -sf /opt/mozjpeg/bin/jpegtran /usr/local/bin/jpegtran \
    && rm -rf /var/lib/apt/lists/* /tmp/mozjpeg

# Verify FFmpeg installation
RUN ffmpeg -version && ffprobe -version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install opencv-python-headless numpy

COPY . .

# Create necessary directories for video processing and data persistence
RUN mkdir -p uploads/videos compressed/videos data

# Setup persistent data directories and files
RUN python setup_data_dirs.py

# Performance tuning for 2 vCPU/4GB setup
ENV QUALITY=85
ENV MAX_FILE_MB=100
ENV SECRET_KEY=change-me

# Video-specific environment variables
ENV MAX_VIDEO_MB=1000
ENV MAX_VIDEO_FILES=10

# Resource optimization for 2 vCPU/4GB
ENV MAX_WORKERS=2
ENV VIDEO_MAX_WORKERS=2
ENV SAFE_PIXELS=50000000
ENV MIN_FREE_MEMORY_MB=500

# Threading for image processing libraries
ENV MAGICK_THREAD_LIMIT=2
ENV OMP_NUM_THREADS=2
ENV OPENCV_NUM_THREADS=2

EXPOSE 5000
CMD ["gunicorn", "--workers=2", "--worker-class=gthread", "--threads=2", "--worker-connections=1000", "--max-requests=1000", "--bind=0.0.0.0:5000", "--timeout=300", "--keep-alive=2", "app:app"]