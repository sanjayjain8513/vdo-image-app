import os
import uuid
import time
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, as_completed
from flask import render_template, send_from_directory, abort, jsonify, request, make_response, url_for

from config import (
    MAX_FILE_MB, MAX_FILES, CV2_AVAILABLE, OUTPUT_ROOT, ADMIN_TOKEN,
    VISITOR_LOG, MAX_PIXELS, PSUTIL_AVAILABLE, PIL_EXTENDED, MAX_WORKERS
)
from utils import (
    get_session_id, ensure_dirs, handle_source_inputs, safe_save_upload,
    allowed_file, get_system_memory_info, calculate_safe_pixel_limit, fetch_remote_image
)
from image_processing import (
    compress_file, resize_image, convert_image, add_watermark,
    crop_image, blur_faces_and_plates, rotate_image, merge_images
)

def register_routes(app, limiter):
    """Register all Flask routes with the app"""

    @app.get('/')
    def home():
        resp = make_response(render_template('index.html', config_max_mb=MAX_FILE_MB, max_files=MAX_FILES,cv2_available=CV2_AVAILABLE))
        sid, resp = get_session_id(resp)
        ensure_dirs(sid)
        return resp


    @app.get('/compress')
    def compress_page():
        resp = make_response(render_template('compress.html', config_max_mb=MAX_FILE_MB, max_files=MAX_FILES))
        sid, resp = get_session_id(resp)
        ensure_dirs(sid)
        return resp

    @app.post('/compress')
    @limiter.limit('15 per minute')
    def compress_route():
        sid, _ = get_session_id()
        _, out_dir = ensure_dirs(sid)
        selected, err = handle_source_inputs(sid)
        if err:
            return render_template('compress.html', error=err, config_max_mb=MAX_FILE_MB, max_files=MAX_FILES), 400

        results, downloads = [], []
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {}
            for original_name, original_path in selected:
                base, _ = os.path.splitext(original_name)
                out_name = f"{base}_compressed.jpg"
                out_path = os.path.join(out_dir, out_name)
                if os.path.exists(out_path):
                    out_name = f"{base}_compressed_{uuid.uuid4().hex[:4]}.jpg"
                    out_path = os.path.join(out_dir, out_name)
                futures[ex.submit(compress_file, original_path, out_path)] = (original_name, out_name)

            for fut in as_completed(futures):
                original_name, out_name = futures[fut]
                try:
                    inp, outp, info = fut.result()
                    if info and not outp:  # Error case
                        results.append({'name': original_name, 'error': info})
                    else:
                        original_size = os.path.getsize(inp)/1024
                        compressed_size = os.path.getsize(outp)/1024

                        # Calculate reduction, ensuring it's never negative
                        if original_size > 0:
                            reduction = max(0, ((original_size - compressed_size)/original_size*100))
                        else:
                            reduction = 0

                        dl = url_for('download_file', session_id=sid, filename=out_name)

                        result_data = {
                            'name': original_name,
                            'download': dl,
                            'original_size': f"{original_size:.1f} KB",
                            'compressed_size': f"{compressed_size:.1f} KB",
                            'reduction': f"{reduction:.1f}%"
                        }

                        # Add processing info if available, or indicate no compression achieved
                        if info:
                            result_data['info'] = info
                        elif reduction == 0:
                            result_data['info'] = "File already optimized - no further compression possible"

                        results.append(result_data)
                        downloads.append(dl)
                except Exception as ex_err:
                    results.append({'name': original_name, 'error': f"Processing failed: {str(ex_err)}"})

        return render_template('compress.html', results=results, all_downloads=downloads, config_max_mb=MAX_FILE_MB, max_files=MAX_FILES)

    @app.get('/merge')
    def merge_page():
        resp = make_response(render_template('merge.html', config_max_mb=MAX_FILE_MB, max_files=MAX_FILES))
        sid, resp = get_session_id(resp)
        ensure_dirs(sid)
        return resp

    @app.post('/merge')
    @limiter.limit('10 per minute')  # Lower limit for merge operations
    def merge_route():
        sid, _ = get_session_id()
        up_dir, out_dir = ensure_dirs(sid)

        # Handle multiple input sources for merge
        files = request.files.getlist('images')
        selected = []

        # Process uploaded files
        for fs in files:
            if not fs or not fs.filename: continue
            if not allowed_file(fs.filename):
                return render_template('merge.html', error="Unsupported file type. Allowed: jpg, jpeg, png, webp.",
                                     config_max_mb=MAX_FILE_MB, max_files=MAX_FILES), 400
            fs.seek(0, os.SEEK_END)
            size = fs.tell()
            fs.seek(0)
            if size > MAX_FILE_MB * 1024 * 1024:
                return render_template('merge.html', error=f"File {fs.filename} exceeds {MAX_FILE_MB} MB limit.",
                                     config_max_mb=MAX_FILE_MB, max_files=MAX_FILES), 400

            orig_name, path = safe_save_upload(fs, up_dir)
            selected.append((orig_name, path))

        # Handle remote URLs (multiple URLs separated by newlines)
        remote_urls = request.form.get('remote_urls', '').strip()
        if remote_urls:
            for url in remote_urls.split('\n'):
                url = url.strip()
                if url:
                    try:
                        name, path = fetch_remote_image(url, up_dir)
                        selected.append((name, path))
                    except Exception as e:
                        return render_template('merge.html', error=f"Remote URL error: {str(e)}",
                                             config_max_mb=MAX_FILE_MB, max_files=MAX_FILES), 400

        # Handle Google Drive URLs (multiple URLs separated by newlines)
        drive_urls = request.form.get('drive_urls', '').strip()
        if drive_urls:
            for url in drive_urls.split('\n'):
                url = url.strip()
                if url:
                    try:
                        name, path = fetch_remote_image(url, up_dir)
                        selected.append((name, path))
                    except Exception as e:
                        return render_template('merge.html', error=f"Drive URL error: {str(e)}",
                                             config_max_mb=MAX_FILE_MB, max_files=MAX_FILES), 400

        if len(selected) < 2:
            return render_template('merge.html', error="At least 2 images required for merging.",
                                 config_max_mb=MAX_FILE_MB, max_files=MAX_FILES), 400

        if len(selected) > MAX_FILES:
            return render_template('merge.html', error=f"Maximum {MAX_FILES} files allowed.",
                                 config_max_mb=MAX_FILE_MB, max_files=MAX_FILES), 400

        # Get merge parameters
        layout_mode = request.form.get('layout_mode', 'horizontal')
        alignment = request.form.get('alignment', 'center')
        grid_columns = int(request.form.get('grid_columns', '3'))
        spacing = int(request.form.get('spacing', '10'))
        bg_color = request.form.get('bg_color_hex', '#ffffff')
        resize_to_fit = bool(request.form.get('resize_to_fit'))
        maintain_aspect = bool(request.form.get('maintain_aspect'))
        grid_fill = request.form.get('grid_fill', 'auto')

        # Create output filename
        timestamp = int(time.time())
        out_name = f"merged_{layout_mode}_{timestamp}.jpg"
        out_path = os.path.join(out_dir, out_name)

        try:
            # Extract image paths
            image_paths = [path for _, path in selected]

            # Perform merge
            input_info, output_path, info = merge_images(
                image_paths=image_paths,
                output_path=out_path,
                layout_mode=layout_mode,
                alignment=alignment,
                grid_columns=grid_columns,
                spacing=spacing,
                bg_color=bg_color,
                resize_to_fit=resize_to_fit,
                maintain_aspect=maintain_aspect,
                grid_fill=grid_fill
            )

            if not output_path:  # Error case
                return render_template('merge.html', error=info, config_max_mb=MAX_FILE_MB, max_files=MAX_FILES), 400

            # Get result info
            file_size = os.path.getsize(output_path) / 1024

            # Get merged image dimensions
            try:
                from PIL import Image
                with Image.open(output_path) as img:
                    dimensions = f"{img.size[0]}×{img.size[1]}"
            except:
                dimensions = "Unknown"

            dl = url_for('download_file', session_id=sid, filename=out_name)

            results = [{
                'name': out_name,
                'download': dl,
                'images_count': len(selected),
                'dimensions': dimensions,
                'file_size': f"{file_size:.1f} KB",
                'info': info
            }]

            downloads = [dl]

            return render_template('merge.html', results=results, all_downloads=downloads,
                                 config_max_mb=MAX_FILE_MB, max_files=MAX_FILES)

        except Exception as e:
            return render_template('merge.html', error=f"Merge failed: {str(e)}",
                                 config_max_mb=MAX_FILE_MB, max_files=MAX_FILES), 400



    @app.get('/watermark')
    def watermark_page():
        resp = make_response(render_template('watermark.html', config_max_mb=MAX_FILE_MB, max_files=MAX_FILES))
        sid, resp = get_session_id(resp)
        ensure_dirs(sid)
        return resp

    @app.post('/watermark')
    @limiter.limit('15 per minute')
    def watermark_route():
        sid, _ = get_session_id()
        up_dir, out_dir = ensure_dirs(sid)
    
        selected, err = handle_source_inputs(sid, 'images', 'remote_url', 'drive_url')
        if err:
            return render_template('watermark.html', error=err, config_max_mb=MAX_FILE_MB, max_files=MAX_FILES), 400
    
    # Get watermark parameters
        watermark_text = request.form.get('watermark_text', '').strip()
        watermark_position = request.form.get('position', 'bottom-right')
        opacity = float(request.form.get('opacity', '0.7'))
        font_size = request.form.get('font_size', 'medium')  # Changed to string
        font_type = request.form.get('font_type', 'arial')   # New parameter
        font_color = request.form.get('font_color', '#ffffff')  # Changed default to hex
        
        # Handle watermark image upload
        watermark_image_path = None
        watermark_file = request.files.get('watermark_image')
        if watermark_file and watermark_file.filename and allowed_file(watermark_file.filename):
            _, watermark_image_path = safe_save_upload(watermark_file, up_dir)
        
        if not watermark_text and not watermark_image_path:
            return render_template('watermark.html', error='Please provide either text or image watermark',
                                 config_max_mb=MAX_FILE_MB, max_files=MAX_FILES), 400
    
        results, downloads = [], []
    
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {}
            for original_name, original_path in selected:
                base, _ = os.path.splitext(original_name)
                out_name = f"{base}_watermarked.jpg"
                out_path = os.path.join(out_dir, out_name)
                
                if os.path.exists(out_path):
                    out_name = f"{base}_watermarked_{uuid.uuid4().hex[:4]}.jpg"
                    out_path = os.path.join(out_dir, out_name)
                
                # Updated function call with new parameters
                futures[ex.submit(add_watermark, original_path, out_path, watermark_text,
                                 watermark_image_path, watermark_position, opacity, 
                                 font_size, font_type, font_color)] = (original_name, out_name)
            
            for fut in as_completed(futures):
                original_name, out_name = futures[fut]
                try:
                    inp, outp, info = fut.result()
                    if info and not outp:
                        results.append({'name': original_name, 'error': info})
                    else:
                        orig_size = os.path.getsize(inp)/1024
                        new_size = os.path.getsize(outp)/1024
                        dl = url_for('download_file', session_id=sid, filename=out_name)
                        result_data = {
                            'name': original_name,
                            'download': dl,
                            'original_size': f"{orig_size:.1f} KB",
                            'new_size': f"{new_size:.1f} KB"
                        }
                        if info:
                            result_data['info'] = info
                        results.append(result_data)
                        downloads.append(dl)
                except Exception as ex_err:
                    results.append({'name': original_name, 'error': f"Processing failed: {str(ex_err)}"})
        
        return render_template('watermark.html', results=results, all_downloads=downloads,
                             config_max_mb=MAX_FILE_MB, max_files=MAX_FILES)
    
    @app.get('/crop')
    def crop_page():
        resp = make_response(render_template('crop.html', config_max_mb=MAX_FILE_MB, max_files=MAX_FILES))
        sid, resp = get_session_id(resp)
        ensure_dirs(sid)
        return resp

    @app.post('/crop')
    @limiter.limit('15 per minute')
    def crop_route():
        sid, _ = get_session_id()
        _, out_dir = ensure_dirs(sid)
        selected, err = handle_source_inputs(sid, 'images', 'remote_url', 'drive_url')
        if err:
            return render_template('crop.html', error=err, config_max_mb=MAX_FILE_MB, max_files=MAX_FILES), 400

        try:
            crop_x = int(request.form.get('crop_x', '0'))
            crop_y = int(request.form.get('crop_y', '0'))
            crop_width = int(request.form.get('crop_width', '100'))
            crop_height = int(request.form.get('crop_height', '100'))
        except ValueError:
            return render_template('crop.html', error='Invalid crop dimensions',
                                 config_max_mb=MAX_FILE_MB, max_files=MAX_FILES), 400

        results, downloads = [], []
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {}
            for original_name, original_path in selected:
                base, _ = os.path.splitext(original_name)
                out_name = f"{base}_cropped.jpg"
                out_path = os.path.join(out_dir, out_name)
                if os.path.exists(out_path):
                    out_name = f"{base}_cropped_{uuid.uuid4().hex[:4]}.jpg"
                    out_path = os.path.join(out_dir, out_name)

                futures[ex.submit(crop_image, original_path, out_path, crop_x, crop_y, crop_width, crop_height)] = (original_name, out_name)

            for fut in as_completed(futures):
                original_name, out_name = futures[fut]
                try:
                    inp, outp, info = fut.result()
                    if info and not outp:
                        results.append({'name': original_name, 'error': info})
                    else:
                        orig_size = os.path.getsize(inp)/1024
                        new_size = os.path.getsize(outp)/1024
                        dl = url_for('download_file', session_id=sid, filename=out_name)

                        result_data = {
                            'name': original_name,
                            'download': dl,
                            'original_size': f"{orig_size:.1f} KB",
                            'new_size': f"{new_size:.1f} KB"
                        }

                        if info:
                            result_data['info'] = info

                        results.append(result_data)
                        downloads.append(dl)
                except Exception as ex_err:
                    results.append({'name': original_name, 'error': f"Processing failed: {str(ex_err)}"})

        return render_template('crop.html', results=results, all_downloads=downloads,
                             config_max_mb=MAX_FILE_MB, max_files=MAX_FILES)

    @app.get('/rotate')
    def rotate_page():
        resp = make_response(render_template('rotate.html', config_max_mb=MAX_FILE_MB, max_files=MAX_FILES))
        sid, resp = get_session_id(resp)
        ensure_dirs(sid)
        return resp

    @app.post('/rotate')
    @limiter.limit('15 per minute')
    def rotate_route():
        sid, _ = get_session_id()
        _, out_dir = ensure_dirs(sid)
        selected, err = handle_source_inputs(sid, 'images', 'remote_url', 'drive_url')
        if err:
            return render_template('rotate.html', error=err, config_max_mb=MAX_FILE_MB, max_files=MAX_FILES), 400

        try:
            angle = float(request.form.get('angle', '0'))
            angle = angle % 360  # Normalize angle
        except ValueError:
            return render_template('rotate.html', error='Invalid rotation angle',
                                 config_max_mb=MAX_FILE_MB, max_files=MAX_FILES), 400

        results, downloads = [], []
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {}
            for original_name, original_path in selected:
                base, _ = os.path.splitext(original_name)
                out_name = f"{base}_rotated.jpg"
                out_path = os.path.join(out_dir, out_name)
                if os.path.exists(out_path):
                    out_name = f"{base}_rotated_{uuid.uuid4().hex[:4]}.jpg"
                    out_path = os.path.join(out_dir, out_name)

                futures[ex.submit(rotate_image, original_path, out_path, angle)] = (original_name, out_name)

            for fut in as_completed(futures):
                original_name, out_name = futures[fut]
                try:
                    inp, outp, info = fut.result()
                    if info and not outp:
                        results.append({'name': original_name, 'error': info})
                    else:
                        orig_size = os.path.getsize(inp)/1024
                        new_size = os.path.getsize(outp)/1024
                        dl = url_for('download_file', session_id=sid, filename=out_name)

                        result_data = {
                            'name': original_name,
                            'download': dl,
                            'original_size': f"{orig_size:.1f} KB",
                            'new_size': f"{new_size:.1f} KB"
                        }

                        if info:
                            result_data['info'] = info

                        results.append(result_data)
                        downloads.append(dl)
                except Exception as ex_err:
                    results.append({'name': original_name, 'error': f"Processing failed: {str(ex_err)}"})

        return render_template('rotate.html', results=results, all_downloads=downloads,
                             config_max_mb=MAX_FILE_MB, max_files=MAX_FILES)

    @app.get('/blur')
    def blur_page():
        resp = make_response(render_template('blur.html', config_max_mb=MAX_FILE_MB, max_files=MAX_FILES,
                                           cv2_available=CV2_AVAILABLE))
        sid, resp = get_session_id(resp)
        ensure_dirs(sid)
        return resp

    @app.post('/blur')
    @limiter.limit('10 per minute')  # Lower limit for resource-intensive operation
    def blur_route():
        # Early check for OpenCV availability
        if not CV2_AVAILABLE:
            return render_template('blur.html',
                                 error='OpenCV is not available. Face and license plate detection requires OpenCV to be installed.',
                                 config_max_mb=MAX_FILE_MB, max_files=MAX_FILES,
                                 cv2_available=CV2_AVAILABLE), 400

        sid, _ = get_session_id()
        _, out_dir = ensure_dirs(sid)
        selected, err = handle_source_inputs(sid, 'images', 'remote_url', 'drive_url')
        if err:
            return render_template('blur.html', error=err, config_max_mb=MAX_FILE_MB, max_files=MAX_FILES,
                                 cv2_available=CV2_AVAILABLE), 400

        blur_strength = int(request.form.get('blur_strength', '15'))
        blur_strength = max(5, min(50, blur_strength))  # Limit range

        results, downloads = [], []
        with ProcessPoolExecutor(max_workers=max(1, MAX_WORKERS//2)) as ex:  # Use fewer workers for intensive operation
            futures = {}
            for original_name, original_path in selected:
                base, _ = os.path.splitext(original_name)
                out_name = f"{base}_blurred.jpg"
                out_path = os.path.join(out_dir, out_name)
                if os.path.exists(out_path):
                    out_name = f"{base}_blurred_{uuid.uuid4().hex[:4]}.jpg"
                    out_path = os.path.join(out_dir, out_name)

                futures[ex.submit(blur_faces_and_plates, original_path, out_path, blur_strength)] = (original_name, out_name)

            for fut in as_completed(futures):
                original_name, out_name = futures[fut]
                try:
                    inp, outp, info = fut.result()
                    if info and not outp:
                        results.append({'name': original_name, 'error': info})
                    else:
                        orig_size = os.path.getsize(inp)/1024
                        new_size = os.path.getsize(outp)/1024
                        dl = url_for('download_file', session_id=sid, filename=out_name)

                        result_data = {
                            'name': original_name,
                            'download': dl,
                            'original_size': f"{orig_size:.1f} KB",
                            'new_size': f"{new_size:.1f} KB"
                        }

                        if info:
                            result_data['info'] = info

                        results.append(result_data)
                        downloads.append(dl)
                except Exception as ex_err:
                    results.append({'name': original_name, 'error': f"Processing failed: {str(ex_err)}"})

        return render_template('blur.html', results=results, all_downloads=downloads,
                             config_max_mb=MAX_FILE_MB, max_files=MAX_FILES, cv2_available=CV2_AVAILABLE)

    @app.get('/convert')
    def convert_page():
        resp = make_response(render_template('convert.html', config_max_mb=MAX_FILE_MB, max_files=MAX_FILES))
        sid, resp = get_session_id(resp)
        ensure_dirs(sid)
        return resp

    @app.post('/convert')
    @limiter.limit('15 per minute')
    def convert_route():
        sid, _ = get_session_id()
        _, out_dir = ensure_dirs(sid)
        selected, err = handle_source_inputs(sid, 'images', 'remote_url', 'drive_url')
        if err:
            return render_template('convert.html', error=err, config_max_mb=MAX_FILE_MB, max_files=MAX_FILES), 400

        target = request.form.get('target_format', 'jpg').lower()
        if target not in ('jpg', 'jpeg', 'png', 'webp'):
            return render_template('convert.html', error='Unsupported target format.', config_max_mb=MAX_FILE_MB, max_files=MAX_FILES), 400

        results, downloads = [], []
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {}
            for original_name, original_path in selected:
                base, _ = os.path.splitext(original_name)
                ext = '.jpg' if target in ('jpg','jpeg') else ('.png' if target=='png' else '.webp')
                out_name = f"{base}_converted{ext}"
                out_path = os.path.join(out_dir, out_name)
                if os.path.exists(out_path):
                    out_name = f"{base}_converted_{uuid.uuid4().hex[:4]}{ext}"
                    out_path = os.path.join(out_dir, out_name)
                futures[ex.submit(convert_image, original_path, out_path, target)] = (original_name, out_name)

            for fut in as_completed(futures):
                original_name, out_name = futures[fut]
                try:
                    inp, outp, info = fut.result()
                    if info and not outp:  # Error case
                        results.append({'name': original_name, 'error': info})
                    else:
                        orig_size = os.path.getsize(inp)/1024
                        new_size = os.path.getsize(outp)/1024
                        dl = url_for('download_file', session_id=sid, filename=out_name)

                        result_data = {
                            'name': original_name,
                            'download': dl,
                            'original_size': f"{orig_size:.1f} KB",
                            'new_size': f"{new_size:.1f} KB"
                        }

                        # Add processing info if available
                        if info:
                            result_data['info'] = info

                        results.append(result_data)
                        downloads.append(dl)
                except Exception as ex_err:
                    results.append({'name': original_name, 'error': f"Processing failed: {str(ex_err)}"})

        return render_template('convert.html', results=results, all_downloads=downloads, config_max_mb=MAX_FILE_MB, max_files=MAX_FILES)

    @app.get('/batch')
    def batch_page():
        resp = make_response(render_template('batch.html', config_max_mb=MAX_FILE_MB, max_files=MAX_FILES,
                                           cv2_available=CV2_AVAILABLE))
        sid, resp = get_session_id(resp)
        ensure_dirs(sid)
        return resp

    @app.post('/batch')
    @limiter.limit('10 per minute')  # Lower limit for batch operations
    def batch_route():
        sid, _ = get_session_id()
        up_dir, out_dir = ensure_dirs(sid)
        selected, err = handle_source_inputs(sid, 'images', 'remote_url', 'drive_url')
        if err:
            return render_template('batch.html', error=err, config_max_mb=MAX_FILE_MB, max_files=MAX_FILES,
                                 cv2_available=CV2_AVAILABLE), 400

        # Get batch operation parameters
        operations = []

        # Check which operations are enabled
        if request.form.get('enable_resize'):
            resize_mode = request.form.get('resize_mode', 'percent')
            val1 = request.form.get('resize_val1', '50')
            val2 = request.form.get('resize_val2', '50')
            operations.append(('resize', {'mode': resize_mode, 'val1': val1, 'val2': val2}))

        if request.form.get('enable_crop'):
            crop_x = int(request.form.get('crop_x', '0'))
            crop_y = int(request.form.get('crop_y', '0'))
            crop_width = int(request.form.get('crop_width', '100'))
            crop_height = int(request.form.get('crop_height', '100'))
            operations.append(('crop', {'x': crop_x, 'y': crop_y, 'width': crop_width, 'height': crop_height}))

        if request.form.get('enable_rotate'):
            angle = float(request.form.get('rotate_angle', '0'))
            operations.append(('rotate', {'angle': angle}))

        if request.form.get('enable_watermark'):
            watermark_text = request.form.get('watermark_text', '').strip()
            watermark_position = request.form.get('watermark_position', 'bottom-right')
            opacity = float(request.form.get('watermark_opacity', '0.7'))

            # Handle watermark image upload
            watermark_image_path = None
            watermark_file = request.files.get('watermark_image')
            if watermark_file and watermark_file.filename and allowed_file(watermark_file.filename):
                _, watermark_image_path = safe_save_upload(watermark_file, up_dir)

            if watermark_text or watermark_image_path:
                operations.append(('watermark', {
                    'text': watermark_text,
                    'image_path': watermark_image_path,
                    'position': watermark_position,
                    'opacity': opacity
                }))

        if request.form.get('enable_blur') and CV2_AVAILABLE:
            blur_strength = int(request.form.get('blur_strength', '15'))
            operations.append(('blur', {'strength': blur_strength}))

        if request.form.get('enable_compress'):
            operations.append(('compress', {}))

        if not operations:
            return render_template('batch.html', error='Please select at least one operation',
                                 config_max_mb=MAX_FILE_MB, max_files=MAX_FILES, cv2_available=CV2_AVAILABLE), 400

        def process_batch(input_path, output_path, operations):
            """Apply multiple operations in sequence"""
            current_path = input_path
            temp_files = []

            try:
                for i, (op_type, params) in enumerate(operations):
                    is_last = (i == len(operations) - 1)
                    next_path = output_path if is_last else f"{input_path}_temp_{i}.jpg"

                    if op_type == 'resize':
                        result = resize_image(current_path, next_path, params['mode'], params['val1'], params['val2'])
                    elif op_type == 'crop':
                        result = crop_image(current_path, next_path, params['x'], params['y'], params['width'], params['height'])
                    elif op_type == 'rotate':
                        result = rotate_image(current_path, next_path, params['angle'])
                    elif op_type == 'watermark':
                        result = add_watermark(current_path, next_path, params['text'], params['image_path'],
                                             params['position'], params['opacity'])
                    elif op_type == 'blur':
                        result = blur_faces_and_plates(current_path, next_path, params['strength'])
                    elif op_type == 'compress':
                        result = compress_file(current_path, next_path)
                    else:
                        continue

                    inp, outp, info = result
                    if not outp:  # Operation failed
                        return result

                    # Clean up previous temp file
                    if current_path != input_path and current_path in temp_files:
                        try:
                            os.remove(current_path)
                            temp_files.remove(current_path)
                        except Exception:
                            pass

                    if not is_last:
                        temp_files.append(next_path)
                    current_path = next_path

                return (input_path, output_path, f"Applied {len(operations)} operations successfully")

            except Exception as e:
                return (input_path, None, f"Batch processing failed: {str(e)}")
            finally:
                # Clean up any remaining temp files
                for temp_file in temp_files:
                    try:
                        if os.path.exists(temp_file):
                            os.remove(temp_file)
                    except Exception:
                        pass

        results, downloads = [], []
        with ProcessPoolExecutor(max_workers=max(1, MAX_WORKERS//2)) as ex:  # Use fewer workers for batch operations
            futures = {}
            for original_name, original_path in selected:
                base, _ = os.path.splitext(original_name)
                out_name = f"{base}_processed.jpg"
                out_path = os.path.join(out_dir, out_name)
                if os.path.exists(out_path):
                    out_name = f"{base}_processed_{uuid.uuid4().hex[:4]}.jpg"
                    out_path = os.path.join(out_dir, out_name)

                futures[ex.submit(process_batch, original_path, out_path, operations)] = (original_name, out_name)

            for fut in as_completed(futures):
                original_name, out_name = futures[fut]
                try:
                    inp, outp, info = fut.result()
                    if info and not outp:
                        results.append({'name': original_name, 'error': info})
                    else:
                        orig_size = os.path.getsize(inp)/1024
                        new_size = os.path.getsize(outp)/1024
                        dl = url_for('download_file', session_id=sid, filename=out_name)

                        result_data = {
                            'name': original_name,
                            'download': dl,
                            'original_size': f"{orig_size:.1f} KB",
                            'new_size': f"{new_size:.1f} KB"
                        }

                        if info:
                            result_data['info'] = info

                        results.append(result_data)
                        downloads.append(dl)
                except Exception as ex_err:
                    results.append({'name': original_name, 'error': f"Processing failed: {str(ex_err)}"})

        return render_template('batch.html', results=results, all_downloads=downloads,
                             config_max_mb=MAX_FILE_MB, max_files=MAX_FILES, cv2_available=CV2_AVAILABLE)

    @app.get('/download/<session_id>/<filename>')
    def download_file(session_id, filename):
        safe_fn = os.path.basename(filename)
        path = os.path.join(OUTPUT_ROOT, session_id, safe_fn)
        if not os.path.isfile(path):
            abort(404)
        return send_from_directory(os.path.join(OUTPUT_ROOT, session_id), safe_fn, as_attachment=True)

    @app.route('/contact')
    def contact():
        return render_template('contact.html')

    @app.get('/health')
    def health():
        memory_info = get_system_memory_info()
        safe_limit, _ = calculate_safe_pixel_limit()

        return jsonify({
            'status': 'ok',
            'time': int(time.time()),
            'memory': {
                'total_mb': round(memory_info['total_mb'], 1),
                'available_mb': round(memory_info['available_mb'], 1),
                'used_percent': round(memory_info['used_percent'], 1)
            },
            'processing': {
                'max_pixels': MAX_PIXELS,
                'current_safe_limit': safe_limit,
                'auto_resize_enabled': True,  # From config.AUTO_RESIZE
                'psutil_available': PSUTIL_AVAILABLE
            },
            'features': {
                'compression': True,
                'resize': True,
                'merge': True,
                'convert': True,
                'watermark': True,
                'crop': True,
                'rotate': True,
                'blur_detection': CV2_AVAILABLE,
                'opencv_available': CV2_AVAILABLE,
                'pil_extended': PIL_EXTENDED
            }
        })

    @app.get('/system-status')
    def system_status():
        """Detailed system status for monitoring"""
        memory_info = get_system_memory_info()
        safe_limit, _ = calculate_safe_pixel_limit()

        # Example image size calculations
        example_sizes = []
        for pixels in [1000000, 5000000, 25000000, 50000000, 100000000, 150000000]:
            side = int(pixels ** 0.5)
            will_resize = pixels > safe_limit
            example_sizes.append({
                'pixels': f"{pixels:,}",
                'approx_size': f"{side}x{side}",
                'will_auto_resize': will_resize,
                'target_pixels': f"{safe_limit:,}" if will_resize else "No resize needed"
            })

        return jsonify({
            'system': {
                'memory': memory_info,
                'cpu_count': multiprocessing.cpu_count(),
                'max_workers': MAX_WORKERS,
                'psutil_available': PSUTIL_AVAILABLE
            },
            'processing_limits': {
                'max_pixels_allowed': MAX_PIXELS,
                'current_safe_limit': safe_limit,
                'auto_resize_enabled': True,  # From config.AUTO_RESIZE
                'min_free_memory_mb': 200  # From config.MIN_FREE_MEMORY_MB
            },
            'example_processing': example_sizes
        })

    @app.get('/robots.txt')
    def robots():
        return send_from_directory('static', 'robots.txt')

    @app.get('/blog')
    def blog():
        return render_template('blog.html')

    @app.get('/admin/stats')
    def stats():
        token = request.args.get('token', '')
        if token != ADMIN_TOKEN:
            abort(403)
        daily = {}
        try:
            with open(VISITOR_LOG, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    date, ip = line.split(',')
                    daily.setdefault(date, set()).add(ip)
        except FileNotFoundError:
            pass
        data = {d: len(s) for d, s in sorted(daily.items())}
        return jsonify(data).get('/')
    def home():
        resp = make_response(render_template('index.html', config_max_mb=MAX_FILE_MB, max_files=MAX_FILES,
                                           cv2_available=CV2_AVAILABLE))
        sid, resp = get_session_id(resp)
        ensure_dirs(sid)
        return resp

    @app.get('/features')
    def features():
        """Feature overview page"""
        features = {
            'compress': {
                'title': 'Image Compression',
                'description': 'Reduce image file size while maintaining quality using advanced JPEG compression',
                'icon': '🗜️'
            },
            'resize': {
                'title': 'Image Resize',
                'description': 'Change image dimensions by percentage, fit to size, exact dimensions, or social media presets',
                'icon': '📏'
            },
            'merge': {
                'title': 'Merge Images',
                'description': 'Combine multiple images horizontally, vertically, or in grid layouts',
                'icon': '🔗'
            },
            'convert': {
                'title': 'Format Conversion',
                'description': 'Convert between JPG, PNG, and WebP image formats',
                'icon': '🔄'
            },
            'watermark': {
                'title': 'Add Watermarks',
                'description': 'Add text or image watermarks to protect your images',
                'icon': '🏷️'
            },
            'crop': {
                'title': 'Crop Images',
                'description': 'Extract specific portions of your images with precise cropping',
                'icon': '✂️'
            },
            'rotate': {
                'title': 'Rotate Images',
                'description': 'Rotate images by any angle with automatic background filling',
                'icon': '🔄'
            },
            'blur': {
                'title': 'Privacy Blur',
                'description': 'Automatically detect and blur faces and license plates for privacy' +
                              (' (OpenCV Available)' if CV2_AVAILABLE else ' (Requires OpenCV)'),
                'icon': '🔒',
                'available': CV2_AVAILABLE
            }
        }

        return render_template('features.html', features=features, cv2_available=CV2_AVAILABLE)

    @app.get('/resize')
    def resize_page():
        resp = make_response(render_template('resize.html', config_max_mb=MAX_FILE_MB, max_files=MAX_FILES))
        sid, resp = get_session_id(resp)
        ensure_dirs(sid)
        return resp

    @app.route('/favicon.ico')
    def favicon():
        return send_from_directory(
        os.path.join(app.root_path, 'static', 'images'),
        'favicon.ico',
        mimetype='image/vnd.microsoft.icon'
        )


    @app.post('/resize')
    @limiter.limit('15 per minute')
    def resize_route():
        sid, _ = get_session_id()
        _, out_dir = ensure_dirs(sid)
        selected, err = handle_source_inputs(sid, 'images', 'remote_url', 'drive_url')
        if err:
            return render_template('resize.html', error=err, config_max_mb=MAX_FILE_MB, max_files=MAX_FILES), 400

        mode = request.form.get('resize_mode', 'percent')
        val1 = request.form.get('val1', '50')
        val2 = request.form.get('val2', '50')

        # Handle social media presets
        social_preset = request.form.get('social_preset', '')
        social_width = request.form.get('social_width', '')
        social_height = request.form.get('social_height', '')

        results, downloads = [], []
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {}
            for original_name, original_path in selected:
                base, _ = os.path.splitext(original_name)
                out_name = f"{base}_resized.jpg"
                out_path = os.path.join(out_dir, out_name)
                if os.path.exists(out_path):
                    out_name = f"{base}_resized_{uuid.uuid4().hex[:4]}.jpg"
                    out_path = os.path.join(out_dir, out_name)
                futures[ex.submit(resize_image, original_path, out_path, mode, val1, val2, social_preset, social_width, social_height)] = (original_name, out_name)

            for fut in as_completed(futures):
                original_name, out_name = futures[fut]
                try:
                    inp, outp, info = fut.result()
                    if info and not outp:  # Error case
                        results.append({'name': original_name, 'error': info})
                    else:
                        orig_size = os.path.getsize(inp)/1024
                        new_size = os.path.getsize(outp)/1024
                        dl = url_for('download_file', session_id=sid, filename=out_name)

                        result_data = {
                            'name': original_name,
                            'download': dl,
                            'original_size': f"{orig_size:.1f} KB",
                            'new_size': f"{new_size:.1f} KB"
                        }

                        # Add processing info if available
                        if info:
                            result_data['info'] = info

                        results.append(result_data)
                        downloads.append(dl)
                except Exception as ex_err:
                    results.append({'name': original_name, 'error': f"Processing failed: {str(ex_err)}"})

        return render_template('resize.html', results=results, all_downloads=downloads, config_max_mb=MAX_FILE_MB, max_files=MAX_FILES)
