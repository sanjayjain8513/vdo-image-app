# Deployment Guide

## Pre-Deployment Setup

### 1. Create Data Directory on Host
```bash
# Create data directory on the host (outside container)
mkdir -p ./data
chmod 755 ./data

# This will persist user accounts, video jobs, and visitor logs
```

### 2. Files that will be Persisted
- `./data/users.json` - User accounts and admin settings
- `./data/video_jobs.json` - Video compression job tracking
- `./data/visitors.log` - Visitor analytics

## Deployment Steps

### Option 1: Using Docker Compose (Recommended)
```bash
# 1. Stop existing containers
docker-compose down

# 2. Build new image with optimizations
docker-compose build --no-cache

# 3. Start with persistent storage
docker-compose up -d

# 4. Verify data persistence
docker exec -it <container_name> ls -la /app/data/
```

### Option 2: Using Docker Run
```bash
# Stop old container
docker stop <old_container_name>
docker rm <old_container_name>

# Build new image
docker build -t resizeimages-app .

# Run with persistent data mounting
docker run -d \
  --name resizeimages-new \
  --restart unless-stopped \
  -p 5000:5000 \
  -v ./uploads:/app/uploads \
  -v ./outputs:/app/outputs \
  -v ./video_uploads:/app/video_uploads \
  -v ./video_outputs:/app/video_outputs \
  -v ./data:/app/data \
  --cpus="2.0" \
  --memory="3.5g" \
  resizeimages-app
```

## Nginx Configuration Update

```bash
# Copy updated nginx config
sudo cp nginx-updated.conf /etc/nginx/sites-available/resizeimages.co.in

# Test configuration
sudo nginx -t

# Reload nginx if test passes
sudo systemctl reload nginx
```

## Verify Deployment

### 1. Check Container Health
```bash
docker ps
docker logs <container_name>
```

### 2. Test Key Features
- Admin panel: https://resizeimages.co.in/admin
- Video compression: https://resizeimages.co.in/video
- Image compression: https://resizeimages.co.in/compress
- Health check: https://resizeimages.co.in/health

### 3. Verify Persistence
```bash
# Check data files exist
ls -la ./data/

# Should show:
# users.json
# video_jobs.json
# visitors.log
```

## Benefits After Deployment

✅ **User accounts persist** across container restarts
✅ **Video jobs survive** app restarts
✅ **Download URLs work** consistently
✅ **Delete operations** always work
✅ **Visitor analytics** maintained
✅ **Admin settings** preserved
✅ **Optimized performance** for 2 vCPU/4GB
✅ **Cloudflare integration** working

## Troubleshooting

### Container Won't Start
```bash
# Check logs
docker logs <container_name>

# Verify data directory permissions
ls -la ./data/
chmod 755 ./data
```

### Admin Panel Issues
```bash
# Check nginx configuration
sudo nginx -t

# Verify admin routes in logs
docker logs <container_name> | grep admin
```

### Video Jobs Issues
```bash
# Check video jobs file
cat ./data/video_jobs.json

# Monitor job processing
docker logs -f <container_name>
```