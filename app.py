from flask import Flask
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

# Rate limiting
limiter = Limiter(get_remote_address, app=app, default_limits=['20 per minute'])

@app.errorhandler(413)
def too_large(e):
    return 'Payload too large.', 413

@app.before_request
def before():
    log_visitor()
    cleanup_old_sessions('uploads', 3600)
    cleanup_old_sessions('outputs', 3600)
    cleanup_old_sessions('video_uploads', 3600)
    cleanup_old_sessions('video_outputs', 3600)

# Register all routes
register_routes(app, limiter)        # Your existing image routes
register_auth_routes(app, limiter)   # New authentication routes
register_video_routes(app, limiter)  # New video routes

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)