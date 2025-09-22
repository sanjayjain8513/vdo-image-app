from flask import Flask, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from config import MAX_FILE_MB, MAX_FILES, SECRET_KEY
from utils import log_visitor, cleanup_old_sessions
from routes import register_routes
from auth_routes import register_auth_routes
from video_routes import register_video_routes

# Create Flask app
app = Flask(__name__)
app.secret_key = SECRET_KEY  # Add session support
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_MB * 1024 * 1024 * MAX_FILES

# Cloudflare Real IP detection function
def get_real_ip():
    """Get real visitor IP from Cloudflare headers or fallback to default"""
    # Priority order: CF-Connecting-IP > X-Forwarded-For > remote_addr
    if request.headers.get('CF-Connecting-IP'):
        return request.headers.get('CF-Connecting-IP')
    elif request.headers.get('X-Forwarded-For'):
        # X-Forwarded-For can have multiple IPs, get the first one
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    else:
        return request.remote_addr

# Rate limiting with Cloudflare support
limiter = Limiter(get_real_ip, app=app, default_limits=['20 per minute'])

@app.errorhandler(413)
def too_large(e):
    return 'Payload too large.', 413

@app.before_request
def before():
    # Set real IP for downstream components (like logging)
    real_ip = get_real_ip()
    request.environ['REMOTE_ADDR'] = real_ip

    # Log visitor with real IP
    log_visitor()

    # Cleanup old files
    cleanup_old_sessions('uploads', 3600)
    cleanup_old_sessions('outputs', 3600)
    cleanup_old_sessions('video_uploads', 3600)
    cleanup_old_sessions('video_outputs', 3600)

# Health check endpoint for monitoring/load balancers
@app.route('/health')
def health_check():
    """Simple health check for monitoring and load balancers"""
    return {'status': 'healthy', 'message': 'Service is running'}, 200

# Cloudflare-specific debug endpoint (remove in production if desired)
@app.route('/debug/headers')
def debug_headers():
    """Debug endpoint to check Cloudflare headers"""
    return {
        'real_ip': get_real_ip(),
        'cf_connecting_ip': request.headers.get('CF-Connecting-IP'),
        'x_forwarded_for': request.headers.get('X-Forwarded-For'),
        'remote_addr': request.remote_addr,
        'cf_ray': request.headers.get('CF-Ray'),
        'cf_visitor': request.headers.get('CF-Visitor')
    }

# Register all routes
register_routes(app, limiter)        # Your existing image routes
register_auth_routes(app, limiter)   # New authentication routes
register_video_routes(app, limiter)  # New video routes

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)