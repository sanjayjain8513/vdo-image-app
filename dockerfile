FROM python:3.11-slim

# Build deps + mozjpeg from source so `cjpeg` exists
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
 && git clone https://github.com/mozilla/mozjpeg.git /tmp/mozjpeg \
 && cd /tmp/mozjpeg && cmake . && make && make install \
 && ln -sf /opt/mozjpeg/bin/cjpeg /usr/local/bin/cjpeg \
 && ln -sf /opt/mozjpeg/bin/jpegtran /usr/local/bin/jpegtran \
 && rm -rf /var/lib/apt/lists/* /tmp/mozjpeg

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install opencv-python-headless numpy

COPY . .

# Tuning (can override at run)
ENV QUALITY=85
ENV MAX_FILE_MB=25
ENV SECRET_KEY=change-me

EXPOSE 5000
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "app:app"]

