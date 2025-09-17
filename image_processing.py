import os
import tempfile
import subprocess
import math
from PIL import Image, ImageDraw, ImageFont
from typing import List, Tuple, Optional

from config import (
    ADVANCED_COMPRESS_BIN, QUALITY, PROCESS_TIMEOUT, AUTO_RESIZE, CV2_AVAILABLE
)
from utils import get_processing_strategy, calculate_safe_pixel_limit

try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:
    cv2 = None
    CV2_AVAILABLE = False

def intelligent_resize(image_path, target_pixels=None, quality_preset='balanced'):
    """Intelligently resize image based on system capacity and quality preset"""
    if target_pixels is None:
        target_pixels, _ = calculate_safe_pixel_limit()

    try:
        with Image.open(image_path) as img:
            original_width, original_height = img.size
            current_pixels = original_width * original_height

            if current_pixels <= target_pixels:
                return image_path, False, current_pixels  # No resize needed

            # Calculate new dimensions maintaining aspect ratio
            scale_factor = math.sqrt(target_pixels / current_pixels)
            new_width = max(100, int(original_width * scale_factor))
            new_height = max(100, int(original_height * scale_factor))

            # Apply quality preset adjustments
            if quality_preset == 'high':
                # Use 80% of calculated size for higher quality
                scale_factor *= 0.9
                new_width = max(100, int(original_width * scale_factor))
                new_height = max(100, int(original_height * scale_factor))
            elif quality_preset == 'fast':
                # Use smaller size for faster processing
                scale_factor *= 0.8
                new_width = max(100, int(original_width * scale_factor))
                new_height = max(100, int(original_height * scale_factor))

            # Ensure dimensions are even (some codecs prefer this)
            new_width = (new_width // 2) * 2
            new_height = (new_height // 2) * 2

            # Create resized version
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')

            resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

            # Save to temporary file
            temp_path = image_path + '_auto_resized.jpg'
            resized.save(temp_path, format='JPEG', quality=92, optimize=True)

            final_pixels = new_width * new_height
            return temp_path, True, final_pixels

    except Exception as e:
        raise ValueError(f"Failed to intelligently resize image: {str(e)}")

def resize_if_too_large(image_path, max_pixels=None):
    """Intelligently resize image based on system capacity"""
    if max_pixels is None:
        max_pixels, _ = calculate_safe_pixel_limit()

    strategy = get_processing_strategy(image_path)

    if strategy['processing_mode'] == 'reject':
        raise ValueError(strategy['reason'])
    elif strategy['processing_mode'] == 'direct':
        return image_path, strategy['reason']
    elif strategy['processing_mode'] in ('smart_resize', 'aggressive_resize'):
        try:
            quality_preset = 'balanced' if strategy['processing_mode'] == 'smart_resize' else 'fast'
            resized_path, was_resized, final_pixels = intelligent_resize(
                image_path,
                strategy['target_pixels'],
                quality_preset
            )

            if was_resized:
                reason = f"Auto-resized from {strategy['original_pixels']:,} to {final_pixels:,} pixels " \
                        f"({strategy['original_size_mb']:.1f}MB image, {strategy['available_memory_mb']:.0f}MB RAM available)"
                return resized_path, reason
            else:
                return image_path, "No resize needed"

        except Exception as e:
            raise ValueError(f"Auto-resize failed: {str(e)}")
    else:
        raise ValueError(strategy.get('reason', 'Unknown processing error'))

def to_ppm_if_needed(input_path):
    """Convert image to PPM format if needed for compression"""
    ext = os.path.splitext(input_path)[1].lower()
    if ext in ('.jpg', '.jpeg'):
        return input_path, False

    try:
        with Image.open(input_path) as im:
            if im.mode != 'RGB':
                im = im.convert('RGB')
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.ppm')
            tmp_path = tmp.name
            tmp.close()
            im.save(tmp_path, format='PPM')
            return tmp_path, True
    except Exception as e:
        raise ValueError(f"Failed to convert image to PPM format: {str(e)}")

def run_compression_command(input_path, output_path, timeout=PROCESS_TIMEOUT):
    """Run compression command with proper error handling and timeout"""
    try:
        with open(output_path, 'wb') as out_f:
            result = subprocess.run(
                [ADVANCED_COMPRESS_BIN, '-quality', str(QUALITY),
                 '-optimize', '-progressive', '-sample', '1x1', input_path],
                stdout=out_f,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=True
            )
        return None
    except subprocess.TimeoutExpired:
        return "Processing timeout - image too large or complex"
    except subprocess.CalledProcessError as e:
        if e.returncode == -9:  # SIGKILL
            return "Processing failed - image too large for available memory"
        return f"Compression failed with exit code {e.returncode}"
    except FileNotFoundError:
        return f"Compression tool '{ADVANCED_COMPRESS_BIN}' not found"
    except Exception as e:
        return f"Compression error: {str(e)}"

def compress_file(input_path, output_path):
    """Compress image file with intelligent resizing if needed"""
    temp_files_to_cleanup = []

    try:
        # Get processing strategy and auto-resize if needed
        resized_input, resize_info = resize_if_too_large(input_path)
        if resized_input != input_path:
            temp_files_to_cleanup.append(resized_input)
            input_path = resized_input

        # Get original file size
        original_size = os.path.getsize(input_path)

        # Convert to appropriate format for cjpeg
        compressor_input, is_temp = to_ppm_if_needed(input_path)
        if is_temp:
            temp_files_to_cleanup.append(compressor_input)

        # Run compression with error handling
        error = run_compression_command(compressor_input, output_path)
        if error:
            return (input_path, None, error)

        # Check if compression actually reduced file size
        compressed_size = os.path.getsize(output_path)
        if compressed_size >= original_size:
            # Compression didn't help, return original file
            try:
                os.remove(output_path)  # Remove the larger compressed file
            except Exception:
                pass

            # Copy original file to output location
            import shutil
            shutil.copy2(input_path, output_path)

            success_info = "File already optimized - returned original"
            if "Auto-resized" in resize_info:
                success_info = f"Note: {resize_info}. File already optimized - returned processed version"

            return (input_path, output_path, success_info)

        # Preserve timestamps for successful compression
        try:
            st = os.stat(input_path)
            os.utime(output_path, (st.st_atime, st.st_mtime))
        except Exception:
            pass  # Non-critical if timestamp preservation fails

        # Add resize info to success message if applicable
        success_info = None
        if "Auto-resized" in resize_info:
            success_info = f"Note: {resize_info}"

        return (input_path, output_path, success_info)

    except Exception as e:
        return (input_path, None, f"Compression failed: {str(e)}")
    finally:
        # Clean up temporary files
        for temp_file in temp_files_to_cleanup:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception:
                pass

def resize_image(input_path, output_path, mode, val1=None, val2=None, social_preset=None, social_width=None, social_height=None):
    """Resize image with specified mode and parameters"""
    temp_files_to_cleanup = []

    try:
        # Check processing strategy
        strategy = get_processing_strategy(input_path)
        if strategy['processing_mode'] == 'reject':
            return (input_path, None, strategy['reason'])

        with Image.open(input_path) as im:
            im = im.convert('RGB')
            w, h = im.size

            if mode == 'percent':
                pct = max(1, min(500, int(val1)))  # Limit to reasonable range
                nw, nh = max(1, int(w*pct/100)), max(1, int(h*pct/100))
                im = im.resize((nw, nh), Image.Resampling.LANCZOS)
            elif mode == 'fit':
                max_w, max_h = int(val1), int(val2)
                im.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
            elif mode == 'exact':
                nw, nh = int(val1), int(val2)
                im = im.resize((nw, nh), Image.Resampling.LANCZOS)
            elif mode == 'social':
                # Handle social media presets
                if social_width and social_height:
                    nw, nh = int(social_width), int(social_height)
                    im = im.resize((nw, nh), Image.Resampling.LANCZOS)
                else:
                    return (input_path, None, 'Social media dimensions not provided')
            else:
                return (input_path, None, 'Invalid resize mode')

            # Save via compressor (jpeg)
            tmp_ppm = tempfile.NamedTemporaryFile(delete=False, suffix='.ppm')
            tmp_ppm_path = tmp_ppm.name
            tmp_ppm.close()
            temp_files_to_cleanup.append(tmp_ppm_path)

            im.save(tmp_ppm_path, format='PPM')

            error = run_compression_command(tmp_ppm_path, output_path)
            if error:
                return (input_path, None, error)

            # Preserve timestamps
            try:
                st = os.stat(input_path)
                os.utime(output_path, (st.st_atime, st.st_mtime))
            except Exception:
                pass

            return (input_path, output_path, None)

    except Exception as e:
        return (input_path, None, f"Resize failed: {str(e)}")
    finally:
        # Clean up temporary files
        for temp_file in temp_files_to_cleanup:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception:
                pass

def convert_image(input_path, output_path, to_format):
    """Convert image to specified format"""
    temp_files_to_cleanup = []

    try:
        # Check processing strategy and auto-resize if needed
        resized_input, resize_info = resize_if_too_large(input_path)
        if resized_input != input_path:
            temp_files_to_cleanup.append(resized_input)
            input_path = resized_input

        to_format = to_format.lower()
        with Image.open(input_path) as im:
            im = im.convert('RGB')

            if to_format in ('jpg', 'jpeg'):
                tmp_ppm = tempfile.NamedTemporaryFile(delete=False, suffix='.ppm')
                tmp_ppm_path = tmp_ppm.name
                tmp_ppm.close()
                temp_files_to_cleanup.append(tmp_ppm_path)

                im.save(tmp_ppm_path, format='PPM')

                error = run_compression_command(tmp_ppm_path, output_path)
                if error:
                    return (input_path, None, error)

            elif to_format == 'webp':
                im.save(output_path, format='WEBP', quality=int(QUALITY), method=6)
            elif to_format == 'png':
                im.save(output_path, format='PNG', optimize=True)
            else:
                return (input_path, None, 'Unsupported target format')

            # Preserve timestamps
            try:
                st = os.stat(input_path)
                os.utime(output_path, (st.st_atime, st.st_mtime))
            except Exception:
                pass

            # Add resize info to success message if applicable
            success_info = None
            if "Auto-resized" in resize_info:
                success_info = f"Note: {resize_info}"

            return (input_path, output_path, success_info)

    except Exception as e:
        return (input_path, None, f"Format conversion failed: {str(e)}")
    finally:
        # Clean up temporary files
        for temp_file in temp_files_to_cleanup:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception:
                pass

def add_watermark(input_path, output_path, text='', image_path=None, position='bottom-right', 
                 opacity=0.7, font_size='medium', font_type='arial', font_color='#ffffff'):
    """Add text or image watermark to image"""
    temp_files_to_cleanup = []

    try:
        # Auto-resize if needed
        resized_input, resize_info = resize_if_too_large(input_path)
        if resized_input != input_path:
            temp_files_to_cleanup.append(resized_input)
            input_path = resized_input

        with Image.open(input_path) as base_img:
            base_img = base_img.convert('RGBA')

            # Create overlay image
            overlay = Image.new('RGBA', base_img.size, (255, 255, 255, 0))
            draw = ImageDraw.Draw(overlay)

            if text and text.strip():
                # Add text watermark
                try:
                    # Improved font size calculation based on image dimensions
                    min_dimension = min(base_img.size)
                    if font_size == 'small':
                        calculated_size = max(20, min_dimension // 40)
                    elif font_size == 'medium':
                        calculated_size = max(30, min_dimension // 25)
                    elif font_size == 'large':
                        calculated_size = max(48, min_dimension // 15)
                    elif font_size == 'xlarge':
                        calculated_size = max(72, min_dimension // 10)
                    else:
                        calculated_size = max(30, min_dimension // 25)

                    # Font selection
                    font_files = {
                        'arial': 'arial.ttf',
                        'times': 'times.ttf',
                        'helvetica': 'helvetica.ttf',
                        'courier': 'cour.ttf'
                    }
                    
                    font_file = font_files.get(font_type, 'arial.ttf')
                    
                    try:
                        font = ImageFont.truetype(font_file, calculated_size)
                    except:
                        # Fallback to default font with calculated size
                        try:
                            font = ImageFont.load_default()
                            # Try to create a larger default font
                            font = font.font_variant(size=calculated_size)
                        except:
                            font = ImageFont.load_default()
                except:
                    font = ImageFont.load_default()

                # Get text size
                bbox = draw.textbbox((0, 0), text, font=font)
                text_width = bbox[2] - bbox[0]
                text_height = bbox[3] - bbox[1]

                # Calculate position with responsive margin
                margin = max(20, min_dimension // 50)
                if position == 'top-left':
                    x, y = margin, margin
                elif position == 'top-right':
                    x, y = base_img.size[0] - text_width - margin, margin
                elif position == 'bottom-left':
                    x, y = margin, base_img.size[1] - text_height - margin
                elif position == 'bottom-right':
                    x, y = base_img.size[0] - text_width - margin, base_img.size[1] - text_height - margin
                else:  # center
                    x, y = (base_img.size[0] - text_width) // 2, (base_img.size[1] - text_height) // 2

                # Parse color from hex
                if font_color.startswith('#'):
                    hex_color = font_color[1:]
                    if len(hex_color) == 6:
                        r = int(hex_color[0:2], 16)
                        g = int(hex_color[2:4], 16)
                        b = int(hex_color[4:6], 16)
                    else:
                        r, g, b = 255, 255, 255  # Default to white
                else:
                    r, g, b = 255, 255, 255  # Default to white

                # Draw text with opacity
                alpha = int(255 * opacity)
                draw.text((x, y), text, font=font, fill=(r, g, b, alpha))

            elif image_path:
                # Add image watermark
                try:
                    with Image.open(image_path) as watermark_img:
                        watermark_img = watermark_img.convert('RGBA')

                        # Resize watermark to reasonable size
                        max_wm_size = min(base_img.size) // 4
                        watermark_img.thumbnail((max_wm_size, max_wm_size), Image.Resampling.LANCZOS)

                        # Apply opacity
                        if opacity < 1.0:
                            # Create alpha mask
                            alpha = watermark_img.split()[3] if watermark_img.mode == 'RGBA' else Image.new('L', watermark_img.size, 255)
                            alpha = alpha.point(lambda p: int(p * opacity))
                            watermark_img.putalpha(alpha)

                        # Calculate position
                        margin = max(20, min(base_img.size) // 50)
                        wm_w, wm_h = watermark_img.size
                        if position == 'top-left':
                            x, y = margin, margin
                        elif position == 'top-right':
                            x, y = base_img.size[0] - wm_w - margin, margin
                        elif position == 'bottom-left':
                            x, y = margin, base_img.size[1] - wm_h - margin
                        elif position == 'bottom-right':
                            x, y = base_img.size[0] - wm_w - margin, base_img.size[1] - wm_h - margin
                        else:  # center
                            x, y = (base_img.size[0] - wm_w) // 2, (base_img.size[1] - wm_h) // 2

                        overlay.paste(watermark_img, (x, y), watermark_img)

                except Exception as e:
                    return (input_path, None, f"Failed to load watermark image: {str(e)}")
            else:
                return (input_path, None, "No watermark text or image provided")

            # Combine images
            watermarked = Image.alpha_composite(base_img, overlay)
            watermarked = watermarked.convert('RGB')
            watermarked.save(output_path, format='JPEG', quality=int(QUALITY), optimize=True)

        # Add resize info to success message if applicable
        success_info = None
        if "Auto-resized" in resize_info:
            success_info = f"Note: {resize_info}"

        return (input_path, output_path, success_info)

    except Exception as e:
        return (input_path, None, f"Watermark failed: {str(e)}")
    finally:
        # Clean up temporary files
        for temp_file in temp_files_to_cleanup:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception:
                pass

def crop_image(input_path, output_path, crop_x, crop_y, crop_width, crop_height):
    """Crop image to specified dimensions"""
    temp_files_to_cleanup = []

    try:
        # Check processing strategy and auto-resize if needed
        resized_input, resize_info = resize_if_too_large(input_path)
        if resized_input != input_path:
            temp_files_to_cleanup.append(resized_input)
            input_path = resized_input

        with Image.open(input_path) as img:
            img = img.convert('RGB')

            # Validate crop parameters
            max_x = max(0, min(crop_x, img.width))
            max_y = max(0, min(crop_y, img.height))
            max_width = min(crop_width, img.width - max_x)
            max_height = min(crop_height, img.height - max_y)

            if max_width <= 0 or max_height <= 0:
                return (input_path, None, "Invalid crop dimensions")

            # Crop the image
            cropped = img.crop((max_x, max_y, max_x + max_width, max_y + max_height))

            # Save as JPEG
            cropped.save(output_path, format='JPEG', quality=int(QUALITY), optimize=True)

            # Preserve timestamps
            try:
                st = os.stat(input_path)
                os.utime(output_path, (st.st_atime, st.st_mtime))
            except Exception:
                pass

            success_info = None
            if "Auto-resized" in resize_info:
                success_info = f"Note: {resize_info}"

            return (input_path, output_path, success_info)

    except Exception as e:
        return (input_path, None, f"Crop failed: {str(e)}")
    finally:
        for temp_file in temp_files_to_cleanup:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception:
                pass

def blur_faces_and_plates(input_path, output_path, blur_strength=15):
    """Detect and blur faces and license plates using OpenCV"""
    if not CV2_AVAILABLE:
        return (input_path, None, "OpenCV not available for face/plate detection")

    temp_files_to_cleanup = []

    try:
        # Check processing strategy and auto-resize if needed
        resized_input, resize_info = resize_if_too_large(input_path)
        if resized_input != input_path:
            temp_files_to_cleanup.append(resized_input)
            input_path = resized_input

        # Read image with OpenCV
        img = cv2.imread(input_path)
        if img is None:
            return (input_path, None, "Could not read image")

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # Try to load face cascade (basic face detection)
        try:
            face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
            faces = face_cascade.detectMultiScale(gray, 1.1, 4)

            # Blur detected faces
            for (x, y, w, h) in faces:
                face_region = img[y:y+h, x:x+w]
                blurred_face = cv2.GaussianBlur(face_region, (blur_strength*2+1, blur_strength*2+1), 0)
                img[y:y+h, x:x+w] = blurred_face
        except Exception:
            pass  # Face detection failed, continue without it

        # Simple license plate detection using contours and aspect ratio
        try:
            # Create binary image for plate detection
            blur = cv2.GaussianBlur(gray, (5, 5), 0)
            edges = cv2.Canny(blur, 50, 150)

            # Find contours
            contours, _ = cv2.findContours(edges, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

            for contour in contours:
                x, y, w, h = cv2.boundingRect(contour)
                aspect_ratio = w / h
                area = cv2.contourArea(contour)

                # Filter for license plate-like rectangles
                if (2.0 < aspect_ratio < 6.0 and 1000 < area < 50000 and
                    w > 50 and h > 15 and w < img.shape[1]*0.8 and h < img.shape[0]*0.3):

                    # Blur the potential plate region
                    plate_region = img[y:y+h, x:x+w]
                    blurred_plate = cv2.GaussianBlur(plate_region, (blur_strength*2+1, blur_strength*2+1), 0)
                    img[y:y+h, x:x+w] = blurred_plate
        except Exception:
            pass  # Plate detection failed, continue without it

        # Convert back to PIL and save
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(img_rgb)
        pil_img.save(output_path, format='JPEG', quality=int(QUALITY), optimize=True)

        # Preserve timestamps
        try:
            st = os.stat(input_path)
            os.utime(output_path, (st.st_atime, st.st_mtime))
        except Exception:
            pass

        success_info = "Applied face and license plate blur detection"
        if "Auto-resized" in resize_info:
            success_info = f"Note: {resize_info}. {success_info}"

        return (input_path, output_path, success_info)

    except Exception as e:
        return (input_path, None, f"Blur detection failed: {str(e)}")
    finally:
        for temp_file in temp_files_to_cleanup:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception:
                pass

def rotate_image(input_path, output_path, angle):
    """Rotate image by specified angle"""
    temp_files_to_cleanup = []

    try:
        # Check processing strategy and auto-resize if needed
        resized_input, resize_info = resize_if_too_large(input_path)
        if resized_input != input_path:
            temp_files_to_cleanup.append(resized_input)
            input_path = resized_input

        with Image.open(input_path) as img:
            img = img.convert('RGB')

            # Rotate the image
            rotated = img.rotate(angle, expand=True, fillcolor='white')

            # Save as JPEG
            rotated.save(output_path, format='JPEG', quality=int(QUALITY), optimize=True)

            # Preserve timestamps
            try:
                st = os.stat(input_path)
                os.utime(output_path, (st.st_atime, st.st_mtime))
            except Exception:
                pass

            success_info = None
            if "Auto-resized" in resize_info:
                success_info = f"Note: {resize_info}"

            return (input_path, output_path, success_info)

    except Exception as e:
        return (input_path, None, f"Rotation failed: {str(e)}")
    finally:
        for temp_file in temp_files_to_cleanup:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception:
                pass

def merge_images(image_paths: List[str], output_path: str, layout_mode: str = 'horizontal',
                alignment: str = 'center', grid_columns: int = 3, spacing: int = 10,
                bg_color: str = '#ffffff', resize_to_fit: bool = True,
                maintain_aspect: bool = True, grid_fill: str = 'auto') -> Tuple[str, Optional[str], Optional[str]]:
    """
    Merge multiple images into one based on specified layout

    Args:
        image_paths: List of paths to images to merge
        output_path: Path where merged image will be saved
        layout_mode: 'horizontal', 'vertical', or 'grid'
        alignment: How to align images ('center', 'top', 'bottom', 'left', 'right')
        grid_columns: Number of columns for grid layout
        spacing: Spacing between images in pixels
        bg_color: Background color in hex format
        resize_to_fit: Whether to resize images to fit uniformly
        maintain_aspect: Whether to maintain aspect ratio when resizing
        grid_fill: 'auto' or 'square' for grid filling

    Returns:
        Tuple of (input_info, output_path, error_message)
    """
    temp_files_to_cleanup = []

    try:
        if len(image_paths) < 2:
            return ("", None, "At least 2 images required for merging")

        # Load and process images
        images = []
        image_info = []

        for path in image_paths:
            try:
                # Auto-resize if needed
                processed_path, resize_info = resize_if_too_large(path)
                if processed_path != path:
                    temp_files_to_cleanup.append(processed_path)

                with Image.open(processed_path) as img:
                    img = img.convert('RGB')
                    images.append(img.copy())
                    image_info.append({
                        'original_path': path,
                        'processed_path': processed_path,
                        'size': img.size,
                        'resize_info': resize_info
                    })
            except Exception as e:
                return (f"Error loading {os.path.basename(path)}", None, f"Failed to load image: {str(e)}")

        # Parse background color
        try:
            bg_color_rgb = tuple(int(bg_color.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
        except:
            bg_color_rgb = (255, 255, 255)  # Default to white

        # Calculate target size for uniform resizing
        if resize_to_fit:
            if layout_mode == 'horizontal':
                # For horizontal, use the minimum height and scale widths proportionally
                target_height = min(img.size[1] for img in images)
                resized_images = []
                for img in images:
                    if maintain_aspect:
                        ratio = target_height / img.size[1]
                        new_width = int(img.size[0] * ratio)
                        resized_images.append(img.resize((new_width, target_height), Image.Resampling.LANCZOS))
                    else:
                        # Calculate average width for uniform appearance
                        avg_width = sum(int(img.size[0] * target_height / img.size[1]) for img in images) // len(images)
                        resized_images.append(img.resize((avg_width, target_height), Image.Resampling.LANCZOS))
                images = resized_images

            elif layout_mode == 'vertical':
                # For vertical, use the minimum width and scale heights proportionally
                target_width = min(img.size[0] for img in images)
                resized_images = []
                for img in images:
                    if maintain_aspect:
                        ratio = target_width / img.size[0]
                        new_height = int(img.size[1] * ratio)
                        resized_images.append(img.resize((target_width, new_height), Image.Resampling.LANCZOS))
                    else:
                        # Calculate average height for uniform appearance
                        avg_height = sum(int(img.size[1] * target_width / img.size[0]) for img in images) // len(images)
                        resized_images.append(img.resize((target_width, avg_height), Image.Resampling.LANCZOS))
                images = resized_images

            elif layout_mode == 'grid':
                # For grid, find a common size that works well
                avg_width = sum(img.size[0] for img in images) // len(images)
                avg_height = sum(img.size[1] for img in images) // len(images)

                if maintain_aspect:
                    # Use the smaller dimension to ensure all images fit
                    target_size = min(avg_width, avg_height)
                    resized_images = []
                    for img in images:
                        img.thumbnail((target_size, target_size), Image.Resampling.LANCZOS)
                        resized_images.append(img)
                    images = resized_images
                else:
                    # Force all images to the same size
                    resized_images = []
                    for img in images:
                        resized_images.append(img.resize((avg_width, avg_height), Image.Resampling.LANCZOS))
                    images = resized_images

        # Create merged image based on layout mode
        if layout_mode == 'horizontal':
            # Calculate dimensions
            total_width = sum(img.size[0] for img in images) + spacing * (len(images) - 1)
            max_height = max(img.size[1] for img in images)

            # Create canvas
            merged = Image.new('RGB', (total_width, max_height), bg_color_rgb)

            # Paste images
            x_offset = 0
            for img in images:
                if alignment == 'top':
                    y_offset = 0
                elif alignment == 'bottom':
                    y_offset = max_height - img.size[1]
                else:  # center
                    y_offset = (max_height - img.size[1]) // 2

                merged.paste(img, (x_offset, y_offset))
                x_offset += img.size[0] + spacing

        elif layout_mode == 'vertical':
            # Calculate dimensions
            max_width = max(img.size[0] for img in images)
            total_height = sum(img.size[1] for img in images) + spacing * (len(images) - 1)

            # Create canvas
            merged = Image.new('RGB', (max_width, total_height), bg_color_rgb)

            # Paste images
            y_offset = 0
            for img in images:
                if alignment == 'left':
                    x_offset = 0
                elif alignment == 'right':
                    x_offset = max_width - img.size[0]
                else:  # center
                    x_offset = (max_width - img.size[0]) // 2

                merged.paste(img, (x_offset, y_offset))
                y_offset += img.size[1] + spacing

        elif layout_mode == 'grid':
            # Calculate grid dimensions
            total_images = len(images)
            cols = min(grid_columns, total_images)

            if grid_fill == 'square':
                rows = cols  # Force square grid
                # Pad with blank spaces if needed
                while len(images) < rows * cols:
                    blank_img = Image.new('RGB', images[0].size, bg_color_rgb)
                    images.append(blank_img)
            else:
                rows = math.ceil(total_images / cols)

            # Calculate cell dimensions
            if images:
                cell_width = max(img.size[0] for img in images)
                cell_height = max(img.size[1] for img in images)
            else:
                return ("", None, "No images to process")

            # Calculate canvas dimensions
            canvas_width = cols * cell_width + (cols - 1) * spacing
            canvas_height = rows * cell_height + (rows - 1) * spacing

            # Create canvas
            merged = Image.new('RGB', (canvas_width, canvas_height), bg_color_rgb)

            # Paste images
            for i, img in enumerate(images):
                if i >= rows * cols:
                    break

                row = i // cols
                col = i % cols

                x = col * (cell_width + spacing)
                y = row * (cell_height + spacing)

                # Center image in cell if alignment is center
                if alignment == 'center':
                    x += (cell_width - img.size[0]) // 2
                    y += (cell_height - img.size[1]) // 2

                merged.paste(img, (x, y))

        else:
            return ("", None, f"Unsupported layout mode: {layout_mode}")

        # Save merged image
        merged.save(output_path, format='JPEG', quality=int(QUALITY), optimize=True)

        # Create summary info
        input_info = f"{len(image_info)} images merged"
        success_info = f"Successfully merged {len(image_info)} images using {layout_mode} layout"

        # Add resize info if any images were auto-resized
        auto_resized = [info for info in image_info if "Auto-resized" in info['resize_info']]
        if auto_resized:
            success_info += f" (Note: {len(auto_resized)} images were auto-resized for processing)"

        return (input_info, output_path, success_info)

    except Exception as e:
        return ("", None, f"Merge failed: {str(e)}")
    finally:
        # Clean up temporary files
        for temp_file in temp_files_to_cleanup:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception:
                pass

