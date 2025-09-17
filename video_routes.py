import os
import uuid
import subprocess
import json
import threading
import shutil
import time
import zipfile
from datetime import datetime, timedelta
from flask import render_template, render_template_string, request, jsonify, send_file, session
from werkzeug.utils import secure_filename
from concurrent.futures import ProcessPoolExecutor, as_completed
from auth_routes import login_required, admin_required
from config import (
    VIDEO_ALLOWED_EXTENSIONS, MAX_VIDEO_MB, MAX_VIDEO_FILES,
    VIDEO_UPLOAD_ROOT, VIDEO_OUTPUT_ROOT, VIDEO_MAX_WORKERS
)

# Global job storage
compression_jobs = {}

def allowed_video_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in VIDEO_ALLOWED_EXTENSIONS

def get_video_info(video_path):
    cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', video_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    except subprocess.CalledProcessError:
        return None

def extract_original_timestamps(video_path):
    """Extract original video creation timestamp from metadata - working version from app_latest.py"""
    timestamps = {'modified': None, 'accessed': None, 'source': 'filesystem'}
    
    try:
        # Try to get creation time from video metadata
        cmd = ['ffprobe', '-v', 'quiet', '-select_streams', 'v:0', 
               '-show_entries', 'format_tags=creation_time,date', 
               '-of', 'csv=p=0', video_path]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip() and result.stdout.strip() != 'N/A':
            creation_time_str = result.stdout.strip()
            if 'T' in creation_time_str:
                try:
                    # Handle ISO format timestamps from video metadata
                    dt = datetime.fromisoformat(creation_time_str.replace('Z', '+00:00'))
                    timestamps['modified'] = dt.timestamp()
                    timestamps['accessed'] = dt.timestamp()
                    timestamps['source'] = 'video_metadata'
                    print(f"DEBUG: Video metadata timestamp: {dt}")
                    return timestamps
                except Exception as parse_error:
                    print(f"DEBUG: Failed to parse video timestamp: {parse_error}")
    except Exception as ffprobe_error:
        print(f"DEBUG: ffprobe failed: {ffprobe_error}")
    
    # Fallback to file system timestamps
    try:
        stat_info = os.stat(video_path)
        timestamps['modified'] = stat_info.st_mtime
        timestamps['accessed'] = stat_info.st_atime
        timestamps['source'] = 'filesystem'
        print(f"DEBUG: Using filesystem timestamp: {datetime.fromtimestamp(timestamps['modified'])}")
    except Exception as stat_error:
        print(f"DEBUG: Failed to get file stats: {stat_error}")
    
    return timestamps

def create_zip_with_timestamps(file_path, original_timestamps):
    """Create a ZIP file containing the video with preserved timestamps - from app_latest.py"""
    try:
        # Create zip filename
        base_path = os.path.splitext(file_path)[0]
        zip_path = base_path + '.zip'
        
        # Ensure the video file has correct timestamps before adding to ZIP
        if original_timestamps.get('modified'):
            os.utime(file_path, (
                original_timestamps.get('accessed', original_timestamps['modified']), 
                original_timestamps['modified']
            ))
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
            # Get the original filename without path
            filename = os.path.basename(file_path)
            
            # Add file to ZIP
            zf.write(file_path, filename)
            
            # Get file info and set timestamp
            zip_info = zf.getinfo(filename)
            if original_timestamps.get('modified'):
                # Convert timestamp to ZIP format (year, month, day, hour, minute, second)
                dt = datetime.fromtimestamp(original_timestamps['modified'])
                zip_info.date_time = (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
        
        # Set ZIP file timestamps to match original
        if original_timestamps.get('modified'):
            os.utime(zip_path, (original_timestamps.get('accessed', original_timestamps['modified']), original_timestamps['modified']))
        
        return zip_path
    except Exception as e:
        print(f"Failed to create ZIP: {e}")
        return None

def get_compression_settings(level, video_info):
    """Get compression settings based on level and video analysis - from app_latest.py"""
    if not video_info or 'streams' not in video_info:
        return get_default_settings(level)
    
    video_stream = None
    for stream in video_info['streams']:
        if stream['codec_type'] == 'video':
            video_stream = stream
            break
    
    if not video_stream:
        return get_default_settings(level)
    
    width = int(video_stream.get('width', 1920))
    height = int(video_stream.get('height', 1080))
    current_bitrate = int(video_stream.get('bit_rate', 0)) if video_stream.get('bit_rate') else 0
    
    # Calculate pixels for resolution-based settings
    pixels = width * height
    
    settings = {}
    
    if level == 'lossless':
        settings = {
            'crf': 18 if pixels >= 1920*1080 else 16,
            'preset': 'slow',
            'use_bitrate': False,
            'fps_limit': None
        }
    
    elif level == 'high_quality':
        settings = {
            'crf': 20 if pixels >= 1920*1080 else 18,
            'preset': 'medium',
            'use_bitrate': False,
            'fps_limit': None
        }
    
    elif level == 'balanced':
        if pixels >= 3840 * 2160:
            settings = {'bitrate': '12000k', 'maxrate': '18000k', 'bufsize': '24000k', 'crf': 22}
        elif pixels >= 2560 * 1440:
            settings = {'bitrate': '6000k', 'maxrate': '9000k', 'bufsize': '12000k', 'crf': 21}
        elif pixels >= 1920 * 1080:
            settings = {'bitrate': '4000k', 'maxrate': '6000k', 'bufsize': '8000k', 'crf': 20}
        elif pixels >= 1280 * 720:
            settings = {'bitrate': '2000k', 'maxrate': '3000k', 'bufsize': '4000k', 'crf': 19}
        else:
            settings = {'bitrate': '1000k', 'maxrate': '1500k', 'bufsize': '2000k', 'crf': 18}
        
        settings.update({
            'preset': 'medium',
            'use_bitrate': True,
            'fps_limit': None
        })
    
    elif level == 'youtube':
        if pixels >= 3840 * 2160:
            settings = {'bitrate': '8000k', 'maxrate': '12000k', 'bufsize': '16000k', 'crf': 24}
        elif pixels >= 2560 * 1440:
            settings = {'bitrate': '4000k', 'maxrate': '6000k', 'bufsize': '8000k', 'crf': 22}
        elif pixels >= 1920 * 1080:
            settings = {'bitrate': '2000k', 'maxrate': '3000k', 'bufsize': '4000k', 'crf': 21}
        elif pixels >= 1280 * 720:
            settings = {'bitrate': '1000k', 'maxrate': '1500k', 'bufsize': '2000k', 'crf': 20}
        else:
            settings = {'bitrate': '500k', 'maxrate': '750k', 'bufsize': '1000k', 'crf': 19}
        
        settings.update({
            'preset': 'slow',
            'use_bitrate': True,
            'fps_limit': 30
        })
    
    elif level == 'aggressive':
        if pixels >= 3840 * 2160:
            settings = {'bitrate': '6000k', 'maxrate': '9000k', 'bufsize': '12000k', 'crf': 26}
        elif pixels >= 2560 * 1440:
            settings = {'bitrate': '3000k', 'maxrate': '4500k', 'bufsize': '6000k', 'crf': 24}
        elif pixels >= 1920 * 1080:
            settings = {'bitrate': '1500k', 'maxrate': '2250k', 'bufsize': '3000k', 'crf': 23}
        elif pixels >= 1280 * 720:
            settings = {'bitrate': '800k', 'maxrate': '1200k', 'bufsize': '1600k', 'crf': 22}
        else:
            settings = {'bitrate': '400k', 'maxrate': '600k', 'bufsize': '800k', 'crf': 21}
        
        settings.update({
            'preset': 'slow',
            'use_bitrate': True,
            'fps_limit': 30
        })
    
    elif level == 'maximum':
        if pixels >= 3840 * 2160:
            settings = {'bitrate': '4000k', 'maxrate': '6000k', 'bufsize': '8000k', 'crf': 28}
        elif pixels >= 2560 * 1440:
            settings = {'bitrate': '2000k', 'maxrate': '3000k', 'bufsize': '4000k', 'crf': 26}
        elif pixels >= 1920 * 1080:
            settings = {'bitrate': '1000k', 'maxrate': '1500k', 'bufsize': '2000k', 'crf': 25}
        elif pixels >= 1280 * 720:
            settings = {'bitrate': '600k', 'maxrate': '900k', 'bufsize': '1200k', 'crf': 24}
        else:
            settings = {'bitrate': '300k', 'maxrate': '450k', 'bufsize': '600k', 'crf': 23}
        
        settings.update({
            'preset': 'slow',
            'use_bitrate': True,
            'fps_limit': 30
        })
    
    return settings

def get_default_settings(level):
    """Default settings when video analysis fails - from app_latest.py"""
    defaults = {
        'lossless': {'crf': 18, 'preset': 'slow', 'use_bitrate': False, 'fps_limit': None},
        'high_quality': {'crf': 20, 'preset': 'medium', 'use_bitrate': False, 'fps_limit': None},
        'balanced': {'bitrate': '4000k', 'maxrate': '6000k', 'bufsize': '8000k', 'crf': 20, 'preset': 'medium', 'use_bitrate': True, 'fps_limit': None},
        'youtube': {'bitrate': '2000k', 'maxrate': '3000k', 'bufsize': '4000k', 'crf': 21, 'preset': 'slow', 'use_bitrate': True, 'fps_limit': 30},
        'aggressive': {'bitrate': '1500k', 'maxrate': '2250k', 'bufsize': '3000k', 'crf': 23, 'preset': 'slow', 'use_bitrate': True, 'fps_limit': 30},
        'maximum': {'bitrate': '1000k', 'maxrate': '1500k', 'bufsize': '2000k', 'crf': 25, 'preset': 'slow', 'use_bitrate': True, 'fps_limit': 30}
    }
    return defaults.get(level, defaults['balanced'])

def should_skip_compression(video_info, original_size, level):
    """Determine if compression should be skipped for lossless/high_quality levels - from app_latest.py"""
    if level not in ['lossless', 'high_quality']:
        return False, "Processing with compression"
    
    if not video_info or 'streams' not in video_info:
        return False, "Unable to analyze - proceeding with compression"
    
    video_stream = None
    for stream in video_info['streams']:
        if stream['codec_type'] == 'video':
            video_stream = stream
            break
    
    if not video_stream:
        return False, "No video stream found"
    
    current_codec = video_stream.get('codec_name', '').lower()
    current_bitrate = int(video_stream.get('bit_rate', 0)) if video_stream.get('bit_rate') else 0
    
    width = int(video_stream.get('width', 1920))
    height = int(video_stream.get('height', 1080))
    pixels = width * height
    
    if pixels <= 640 * 480:
        reasonable_bitrate = 1000000
    elif pixels <= 1280 * 720:
        reasonable_bitrate = 2500000
    elif pixels <= 1920 * 1080:
        reasonable_bitrate = 5000000
    elif pixels <= 3840 * 2160:
        reasonable_bitrate = 15000000
    else:
        reasonable_bitrate = 25000000
    
    if level == 'lossless' and current_codec in ['h264', 'x264', 'avc1'] and current_bitrate > 0:
        if current_bitrate <= reasonable_bitrate * 1.1:
            return True, f"Already efficiently compressed ({current_bitrate//1000}kbps)"
    
    return False, f"Will compress to optimize quality/size ratio"

def compress_video(input_path, output_path, job_id, file_index, compression_settings, original_filename):
    """Video compression with timestamp preservation - from app_latest.py"""
    try:
        if not os.path.exists(input_path):
            raise Exception(f"Input file does not exist: {input_path}")
        
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        compression_jobs[job_id]['files'][file_index]['status'] = 'analyzing'
        compression_jobs[job_id]['files'][file_index]['progress'] = 5
        compression_jobs[job_id]['files'][file_index]['output_path'] = output_path
        
        # Extract original timestamps - this is the key step
        original_timestamps = extract_original_timestamps(input_path)
        
        video_info = get_video_info(input_path)
        original_size = os.path.getsize(input_path)
        compression_jobs[job_id]['files'][file_index]['original_size'] = original_size
        
        should_skip, analysis_msg = should_skip_compression(video_info, original_size, compression_settings['level'])
        compression_jobs[job_id]['files'][file_index]['analysis'] = analysis_msg
        
        if should_skip:
            compression_jobs[job_id]['files'][file_index]['status'] = 'skipped'
            compression_jobs[job_id]['files'][file_index]['progress'] = 100
            
            shutil.copy2(input_path, output_path)
            
            # Apply original timestamps to output file
            if original_timestamps.get('modified'):
                try:
                    os.utime(output_path, (
                        original_timestamps.get('accessed', original_timestamps['modified']), 
                        original_timestamps['modified']
                    ))
                    print(f"DEBUG: Applied timestamps to skipped file: {datetime.fromtimestamp(original_timestamps['modified'])}")
                except Exception as e:
                    print(f"DEBUG: Failed to apply timestamps to skipped file: {e}")
            
            zip_path = create_zip_with_timestamps(output_path, original_timestamps)
            if zip_path:
                compression_jobs[job_id]['files'][file_index]['zip_path'] = zip_path
                compression_jobs[job_id]['files'][file_index]['zip_filename'] = os.path.basename(zip_path)
            
            compression_jobs[job_id]['files'][file_index].update({
                'compressed_size': original_size,
                'compression_ratio': 0,
                'completed_at': datetime.now().isoformat(),
                'message': 'Compression skipped - file already optimally compressed'
            })
            return
        
        compression_jobs[job_id]['files'][file_index]['status'] = 'processing'
        compression_jobs[job_id]['files'][file_index]['progress'] = 10
        
        duration = None
        if video_info and 'format' in video_info:
            duration = float(video_info['format'].get('duration', 0))
        
        settings = get_compression_settings(compression_settings['level'], video_info)
        
        cmd = ['ffmpeg', '-i', input_path, '-y']
        
        try:
            test_result = subprocess.run(['ffmpeg', '-f', 'lavfi', '-i', 'testsrc=duration=1:size=320x240:rate=1', '-t', '1', '-f', 'null', '-'], 
                                       capture_output=True, stderr=subprocess.DEVNULL, timeout=5)
            if test_result.returncode == 0:
                cmd.extend(['-hwaccel', 'auto'])
        except:
            pass
        
        if compression_settings['codec'] == 'h264':
            cmd.extend([
                '-c:v', 'libx264',
                '-preset', settings['preset'],
                '-profile:v', 'high',
                '-level:v', '4.1'
            ])
            
            if settings.get('use_bitrate'):
                cmd.extend([
                    '-b:v', settings['bitrate'],
                    '-maxrate', settings['maxrate'],
                    '-bufsize', settings['bufsize']
                ])
                if settings.get('crf'):
                    cmd.extend(['-crf', str(settings['crf'])])
            else:
                cmd.extend(['-crf', str(settings['crf'])])
            
            if settings['preset'] not in ['ultrafast', 'superfast']:
                cmd.extend(['-tune', 'film'])
            
            cmd.extend(['-movflags', '+faststart'])
                
        elif compression_settings['codec'] == 'h265':
            cmd.extend([
                '-c:v', 'libx265',
                '-preset', settings['preset'],
                '-profile:v', 'main'
            ])
            
            if settings.get('use_bitrate'):
                cmd.extend([
                    '-b:v', settings['bitrate'],
                    '-maxrate', settings['maxrate'],
                    '-bufsize', settings['bufsize']
                ])
                if settings.get('crf'):
                    cmd.extend(['-crf', str(settings['crf'])])
            else:
                cmd.extend(['-crf', str(settings['crf'])])
            
            cmd.extend(['-movflags', '+faststart'])
            
        elif compression_settings['codec'] == 'vp9':
            cmd.extend([
                '-c:v', 'libvpx-vp9',
                '-row-mt', '1'
            ])
            
            if settings.get('use_bitrate'):
                cmd.extend([
                    '-b:v', settings['bitrate'],
                    '-maxrate', settings['maxrate'],
                    '-bufsize', settings['bufsize']
                ])
            else:
                cmd.extend(['-crf', str(settings.get('crf', 20)), '-b:v', '0'])
        
        if settings.get('fps_limit'):
            cmd.extend(['-r', str(settings['fps_limit'])])
        
        if video_info and 'streams' in video_info:
            for stream in video_info['streams']:
                if stream['codec_type'] == 'video':
                    height = int(stream.get('height', 1080))
                    if height > 1080 and compression_settings['level'] in ['youtube', 'aggressive', 'maximum']:
                        cmd.extend(['-vf', 'scale=-2:1080'])
                    break
        
        if compression_settings['level'] in ['lossless', 'high_quality']:
            cmd.extend(['-c:a', 'aac', '-b:a', '128k'])
        else:
            cmd.extend(['-c:a', 'aac', '-b:a', '96k', '-ac', '2'])
        
        cmd.extend([
            '-threads', str(min(os.cpu_count() or 4, 8)),
            '-err_detect', 'ignore_err'
        ])
        
        cmd.append(output_path)
        
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        
        for line in iter(process.stdout.readline, ''):
            if line.strip() == '':
                continue
                
            if 'Duration:' in line and duration is None:
                try:
                    duration_str = line.split('Duration: ')[1].split(',')[0]
                    h, m, s = duration_str.split(':')
                    duration = int(h) * 3600 + int(m) * 60 + float(s)
                except Exception:
                    pass
            
            if 'time=' in line and duration and duration > 0:
                try:
                    time_str = line.split('time=')[1].split()[0]
                    h, m, s = time_str.split(':')
                    current_time = int(h) * 3600 + int(m) * 60 + float(s)
                    progress = min(90, int(10 + (current_time / duration) * 80))
                    compression_jobs[job_id]['files'][file_index]['progress'] = progress
                except:
                    pass
        
        return_code = process.wait()
        
        if return_code == 0 and os.path.exists(output_path):
            time.sleep(1.0)
            compressed_size = os.path.getsize(output_path)
            
            if compressed_size >= original_size and compression_settings['level'] not in ['lossless', 'high_quality']:
                try:
                    os.remove(output_path)
                    time.sleep(0.5)
                except Exception:
                    pass
                
                shutil.copy2(input_path, output_path)
                compressed_size = original_size
                compression_ratio = 0
                message = "Original file kept - compression didn't reduce size"
            else:
                compression_ratio = ((original_size - compressed_size) / original_size) * 100
                message = "Compression completed successfully"
            
            # Apply original timestamps to compressed file
            if original_timestamps.get('modified'):
                try:
                    os.utime(output_path, (
                        original_timestamps.get('accessed', original_timestamps['modified']), 
                        original_timestamps['modified']
                    ))
                    print(f"DEBUG: Applied timestamps to compressed file: {datetime.fromtimestamp(original_timestamps['modified'])}")
                except Exception as e:
                    print(f"DEBUG: Failed to apply timestamps to compressed file: {e}")
            
            zip_path = create_zip_with_timestamps(output_path, original_timestamps)
            if zip_path:
                compression_jobs[job_id]['files'][file_index]['zip_path'] = zip_path
                compression_jobs[job_id]['files'][file_index]['zip_filename'] = os.path.basename(zip_path)
            
            compression_jobs[job_id]['files'][file_index].update({
                'status': 'completed',
                'progress': 100,
                'compressed_size': compressed_size,
                'compression_ratio': round(compression_ratio, 2),
                'completed_at': datetime.now().isoformat(),
                'message': message
            })
        else:
            error_msg = f"FFmpeg process failed with return code {return_code}"
            compression_jobs[job_id]['files'][file_index].update({
                'status': 'failed',
                'error': error_msg,
                'completed_at': datetime.now().isoformat()
            })
            
    except Exception as e:
        compression_jobs[job_id]['files'][file_index].update({
            'status': 'failed',
            'error': f"Processing error: {str(e)}",
            'completed_at': datetime.now().isoformat()
        })

def process_batch(job_id, compression_settings):
    """Process video batch - from app_latest.py"""
    try:
        if job_id not in compression_jobs:
            return
            
        job = compression_jobs[job_id]
        
        for i, file_info in enumerate(job['files']):
            if file_info['status'] == 'queued':
                try:
                    compress_video(file_info['input_path'], file_info['output_path'], job_id, i, compression_settings, file_info['filename'])
                except Exception as e:
                    compression_jobs[job_id]['files'][i].update({
                        'status': 'failed',
                        'error': f"Processing failed: {str(e)}",
                        'completed_at': datetime.now().isoformat()
                    })
        
        all_files = job['files']
        completed_files = [f for f in all_files if f['status'] in ['completed', 'skipped']]
        failed_files = [f for f in all_files if f['status'] == 'failed']
        
        if len(completed_files) == len(all_files):
            job['status'] = 'completed'
        elif len(failed_files) > 0:
            job['status'] = 'partially_completed' if len(completed_files) > 0 else 'failed'
        
        job['completed_at'] = datetime.now().isoformat()
        
    except Exception as e:
        if job_id in compression_jobs:
            compression_jobs[job_id]['status'] = 'failed'
            compression_jobs[job_id]['error'] = f"Batch processing failed: {str(e)}"

def register_video_routes(app, limiter):
    """Register video compression routes"""
    
    @app.route('/video')
    def video_page():
        return render_template('video.html')
    
    @app.route('/video/upload', methods=['POST'])
    @login_required
    @limiter.limit('5 per minute')
    def upload_video_files():
        if 'files[]' not in request.files and 'file' not in request.files:
            return jsonify({'error': 'No files selected'}), 400
        
        files = request.files.getlist('files[]') if 'files[]' in request.files else [request.files['file']]
        
        if not files or all(f.filename == '' for f in files):
            return jsonify({'error': 'No files selected'}), 400
        
        if len(files) > MAX_VIDEO_FILES:
            return jsonify({'error': f'Too many files. Maximum {MAX_VIDEO_FILES} files allowed'}), 400
        
        valid_files = []
        for file in files:
            if file.filename != '' and allowed_video_file(file.filename):
                valid_files.append(file)
        
        if not valid_files:
            return jsonify({'error': 'No valid video files found'}), 400
        
        job_id = str(uuid.uuid4())
        
        compression_settings = {
            'codec': request.form.get('codec', 'h264'),
            'level': request.form.get('level', 'balanced')
        }
        
        job_files = []
        
        for i, file in enumerate(valid_files):
            filename = secure_filename(file.filename)
            file_extension = filename.rsplit('.', 1)[1].lower()
            
            input_path = os.path.join(VIDEO_UPLOAD_ROOT, f"{job_id}_{i}.{file_extension}")
            file.save(input_path)
            
            if not os.path.exists(input_path):
                return jsonify({'error': f'Failed to save file {filename}'}), 500
            
            # Create simpler output filename - just add _compressed
            name_without_ext = filename.rsplit('.', 1)[0]
            if len(name_without_ext) > 50:  # Truncate very long names
                name_without_ext = name_without_ext[:47] + "..."
            output_filename = f"{name_without_ext}_compressed.{file_extension}"
            output_path = os.path.join(VIDEO_OUTPUT_ROOT, f"{job_id}_{i}_{output_filename}")
            download_url = f"/video/download/{job_id}/{i}"
            
            file_info = {
                'filename': filename,
                'input_path': input_path,
                'output_path': output_path,
                'output_filename': output_filename,
                'download_url': download_url,
                'status': 'queued',
                'progress': 0,
                'analysis': 'Waiting to start...',
                'original_size': 0,
                'compressed_size': 0,
                'compression_ratio': 0,
                'message': '',
                'error': '',
                'completed_at': ''
            }
            
            job_files.append(file_info)
        
        compression_jobs[job_id] = {
            'id': job_id,
            'files': job_files,
            'status': 'processing',
            'created_at': datetime.now().isoformat(),
            'settings': compression_settings,
            'download_urls': [f['download_url'] for f in job_files],
            'user': session['user']
        }
        
        thread = threading.Thread(target=process_batch, args=(job_id, compression_settings))
        thread.daemon = True
        thread.start()
        
        return jsonify({
            'job_id': job_id,
            'message': f'Successfully uploaded {len(valid_files)} files. Processing started.',
            'file_count': len(valid_files),
            'download_urls': compression_jobs[job_id]['download_urls']
        })
    
    @app.route('/video/status/<job_id>')
    @login_required
    def get_video_status(job_id):
        if job_id not in compression_jobs:
            return jsonify({'error': 'Job not found'}), 404
        
        job = compression_jobs[job_id]
        if job.get('user') != session['user'] and session.get('role') != 'admin':
            return jsonify({'error': 'Access denied'}), 403
        
        import copy
        job_copy = copy.deepcopy(job)
        
        for file_info in job_copy['files']:
            file_info.pop('input_path', None)
            file_info.pop('output_path', None)
        
        return jsonify(job_copy)
    
    @app.route('/video/download/<job_id>/<int:file_index>')
    @login_required
    def download_video_file(job_id, file_index):
        if job_id not in compression_jobs:
            return render_template_string("""
                <!DOCTYPE html>
                <html lang="en">
                <head>
                    <meta charset="UTF-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1.0">
                    <title>Job Not Found - Video Compressor</title>
                    <style>
                        * { margin: 0; padding: 0; box-sizing: border-box; }
                        body { 
                            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
                            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
                            min-height: 100vh; 
                            display: flex; 
                            align-items: center; 
                            justify-content: center; 
                            padding: 20px; 
                        }
                        .error-container { 
                            background: white; 
                            border-radius: 15px; 
                            box-shadow: 0 10px 30px rgba(0,0,0,0.3); 
                            padding: 40px; 
                            text-align: center; 
                            max-width: 500px; 
                            width: 100%; 
                        }
                        .error-icon { 
                            font-size: 4em; 
                            color: #dc3545; 
                            margin-bottom: 20px; 
                        }
                        .error-title { 
                            color: #333; 
                            font-size: 2em; 
                            margin-bottom: 15px; 
                        }
                        .error-message { 
                            color: #666; 
                            font-size: 1.1em; 
                            margin-bottom: 30px; 
                            line-height: 1.5; 
                        }
                        .back-button { 
                            background: linear-gradient(135deg, #007bff, #0056b3); 
                            color: white; 
                            border: none; 
                            padding: 15px 30px; 
                            border-radius: 25px; 
                            cursor: pointer; 
                            font-size: 16px; 
                            text-decoration: none; 
                            display: inline-block; 
                            transition: all 0.3s ease; 
                        }
                        .back-button:hover { 
                            transform: translateY(-2px); 
                            box-shadow: 0 5px 15px rgba(0,123,255,0.4); 
                        }
                    </style>
                </head>
                <body>
                    <div class="error-container">
                        <div class="error-icon">✖</div>
                        <h1 class="error-title">Job Not Found</h1>
                        <p class="error-message">The compression job you're looking for doesn't exist or has been removed.</p>
                        <a href="/video" class="back-button">Return to Video Compressor</a>
                    </div>
                </body>
                </html>
            """), 404
        
        job = compression_jobs[job_id]
        
        if job.get('user') != session['user'] and session.get('role') != 'admin':
            return render_template_string("""
                <!DOCTYPE html>
                <html lang="en">
                <head>
                    <meta charset="UTF-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1.0">
                    <title>Access Denied - Video Compressor</title>
                    <style>
                        * { margin: 0; padding: 0; box-sizing: border-box; }
                        body { 
                            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
                            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
                            min-height: 100vh; 
                            display: flex; 
                            align-items: center; 
                            justify-content: center; 
                            padding: 20px; 
                        }
                        .error-container { 
                            background: white; 
                            border-radius: 15px; 
                            box-shadow: 0 10px 30px rgba(0,0,0,0.3); 
                            padding: 40px; 
                            text-align: center; 
                            max-width: 500px; 
                            width: 100%; 
                        }
                        .error-icon { 
                            font-size: 4em; 
                            color: #ffc107; 
                            margin-bottom: 20px; 
                        }
                        .error-title { 
                            color: #333; 
                            font-size: 2em; 
                            margin-bottom: 15px; 
                        }
                        .error-message { 
                            color: #666; 
                            font-size: 1.1em; 
                            margin-bottom: 30px; 
                            line-height: 1.5; 
                        }
                        .back-button { 
                            background: linear-gradient(135deg, #007bff, #0056b3); 
                            color: white; 
                            border: none; 
                            padding: 15px 30px; 
                            border-radius: 25px; 
                            cursor: pointer; 
                            font-size: 16px; 
                            text-decoration: none; 
                            display: inline-block; 
                            transition: all 0.3s ease; 
                        }
                        .back-button:hover { 
                            transform: translateY(-2px); 
                            box-shadow: 0 5px 15px rgba(0,123,255,0.4); 
                        }
                    </style>
                </head>
                <body>
                    <div class="error-container">
                        <div class="error-icon">⚠</div>
                        <h1 class="error-title">Access Denied</h1>
                        <p class="error-message">You don't have permission to access this file. You can only download files from your own compression jobs.</p>
                        <a href="/video" class="back-button">Return to Video Compressor</a>
                    </div>
                </body>
                </html>
            """), 403
        
        if file_index >= len(job['files']):
            return render_template_string("""
                <!DOCTYPE html>
                <html lang="en">
                <head>
                    <meta charset="UTF-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1.0">
                    <title>File Not Found - Video Compressor</title>
                    <style>
                        * { margin: 0; padding: 0; box-sizing: border-box; }
                        body { 
                            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
                            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
                            min-height: 100vh; 
                            display: flex; 
                            align-items: center; 
                            justify-content: center; 
                            padding: 20px; 
                        }
                        .error-container { 
                            background: white; 
                            border-radius: 15px; 
                            box-shadow: 0 10px 30px rgba(0,0,0,0.3); 
                            padding: 40px; 
                            text-align: center; 
                            max-width: 500px; 
                            width: 100%; 
                        }
                        .error-icon { 
                            font-size: 4em; 
                            color: #dc3545; 
                            margin-bottom: 20px; 
                        }
                        .error-title { 
                            color: #333; 
                            font-size: 2em; 
                            margin-bottom: 15px; 
                        }
                        .error-message { 
                            color: #666; 
                            font-size: 1.1em; 
                            margin-bottom: 30px; 
                            line-height: 1.5; 
                        }
                        .back-button { 
                            background: linear-gradient(135deg, #007bff, #0056b3); 
                            color: white; 
                            border: none; 
                            padding: 15px 30px; 
                            border-radius: 25px; 
                            cursor: pointer; 
                            font-size: 16px; 
                            text-decoration: none; 
                            display: inline-block; 
                            transition: all 0.3s ease; 
                        }
                        .back-button:hover { 
                            transform: translateY(-2px); 
                            box-shadow: 0 5px 15px rgba(0,123,255,0.4); 
                        }
                    </style>
                </head>
                <body>
                    <div class="error-container">
                        <div class="error-icon">✖</div>
                        <h1 class="error-title">File Not Found</h1>
                        <p class="error-message">The requested file doesn't exist in this compression job.</p>
                        <a href="/video" class="back-button">Return to Video Compressor</a>
                    </div>
                </body>
                </html>
            """), 404
        
        file_info = job['files'][file_index]
        
        if file_info['status'] not in ['completed', 'skipped']:
            return render_template_string(f"""
                <!DOCTYPE html>
                <html lang="en">
                <head>
                    <meta charset="UTF-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1.0">
                    <title>File Not Ready - Video Compressor</title>
                    <style>
                        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
                        body {{ 
                            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
                            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
                            min-height: 100vh; 
                            display: flex; 
                            align-items: center; 
                            justify-content: center; 
                            padding: 20px; 
                        }}
                        .error-container {{ 
                            background: white; 
                            border-radius: 15px; 
                            box-shadow: 0 10px 30px rgba(0,0,0,0.3); 
                            padding: 40px; 
                            text-align: center; 
                            max-width: 500px; 
                            width: 100%; 
                        }}
                        .error-icon {{ 
                            font-size: 4em; 
                            color: #17a2b8; 
                            margin-bottom: 20px; 
                        }}
                        .error-title {{ 
                            color: #333; 
                            font-size: 2em; 
                            margin-bottom: 15px; 
                        }}
                        .error-message {{ 
                            color: #666; 
                            font-size: 1.1em; 
                            margin-bottom: 20px; 
                            line-height: 1.5; 
                        }}
                        .status-info {{ 
                            background: #e9ecef; 
                            padding: 15px; 
                            border-radius: 8px; 
                            margin-bottom: 30px; 
                            color: #495057; 
                        }}
                        .back-button {{ 
                            background: linear-gradient(135deg, #007bff, #0056b3); 
                            color: white; 
                            border: none; 
                            padding: 15px 30px; 
                            border-radius: 25px; 
                            cursor: pointer; 
                            font-size: 16px; 
                            text-decoration: none; 
                            display: inline-block; 
                            transition: all 0.3s ease; 
                            margin: 5px; 
                        }}
                        .back-button:hover {{ 
                            transform: translateY(-2px); 
                            box-shadow: 0 5px 15px rgba(0,123,255,0.4); 
                        }}
                        .refresh-button {{ 
                            background: linear-gradient(135deg, #28a745, #20c997); 
                        }}
                        .refresh-button:hover {{ 
                            box-shadow: 0 5px 15px rgba(40,167,69,0.4); 
                        }}
                    </style>
                </head>
                <body>
                    <div class="error-container">
                        <div class="error-icon">⏳</div>
                        <h1 class="error-title">File Not Ready</h1>
                        <p class="error-message">This file is still being processed and is not ready for download yet.</p>
                        <div class="status-info">
                            <strong>Current Status:</strong> {file_info["status"].replace('_', ' ').title()}<br>
                            <strong>File:</strong> {file_info["filename"]}
                        </div>
                        <a href="/video" class="back-button">Return to Video Compressor</a>
                        <a href="javascript:location.reload()" class="back-button refresh-button">Refresh Page</a>
                    </div>
                </body>
                </html>
            """), 400

        # Try to serve ZIP file first (with preserved timestamps)
        if 'zip_path' in file_info and os.path.exists(file_info['zip_path']):
            zip_filename = file_info.get('zip_filename', f"compressed_{file_info['filename']}.zip")
            
            try:
                return send_file(
                    file_info['zip_path'],
                    as_attachment=True,
                    download_name=zip_filename,
                    mimetype='application/zip'
                )
            except Exception as e:
                return jsonify({'error': f'Failed to serve ZIP file: {str(e)}'}), 500
        
        # Fallback to raw video file if ZIP doesn't exist
        elif 'output_path' in file_info and os.path.exists(file_info['output_path']):
            output_filename = file_info.get('output_filename', f"compressed_{file_info['filename']}")
            
            try:
                return send_file(
                    file_info['output_path'],
                    as_attachment=True,
                    download_name=output_filename,
                    mimetype='video/mp4'
                )
            except Exception as e:
                return jsonify({'error': f'Failed to serve video file: {str(e)}'}), 500
        
        else:
            return jsonify({'error': 'File no longer exists on server'}), 404
    
    @app.route('/video/cleanup/<job_id>', methods=['DELETE'])
    @login_required
    def cleanup_video_job(job_id):
        if job_id not in compression_jobs:
            return jsonify({'error': 'Job not found'}), 404
        
        job = compression_jobs[job_id]
        if job.get('user') != session['user'] and session.get('role') != 'admin':
            return jsonify({'error': 'Access denied'}), 403
        
        errors = []
        for file_info in job['files']:
            try:
                if os.path.exists(file_info['input_path']):
                    os.remove(file_info['input_path'])
                if 'output_path' in file_info and os.path.exists(file_info['output_path']):
                    os.remove(file_info['output_path'])
                if 'zip_path' in file_info and os.path.exists(file_info['zip_path']):
                    os.remove(file_info['zip_path'])
            except Exception as e:
                errors.append(f"Failed to delete files for {file_info['filename']}: {str(e)}")
        
        del compression_jobs[job_id]
        
        if errors:
            return jsonify({'message': 'Job cleaned up with some errors', 'errors': errors}), 207
        
        return jsonify({'message': 'Job cleaned up successfully'})