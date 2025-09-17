import os
import json
import hashlib
import secrets
from datetime import datetime, timedelta
from flask import render_template, request, jsonify, session, redirect, url_for, flash
from functools import wraps

# Use the correct path for users file
USERS_FILE = 'users.json'

def load_users():
    """Load users from JSON file"""
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, 'r') as f:
                users_data = json.load(f)
                print(f"DEBUG: Loaded users from {USERS_FILE}: {list(users_data.keys())}")
                return users_data
        else:
            # Create default admin user if no users file exists
            print("DEBUG: Creating new users file with default admin")
            default_users = {
                'admin': {
                    'password_hash': hash_password('admin123'),
                    'role': 'admin',
                    'created_at': datetime.now().isoformat(),
                    'last_login': None
                }
            }
            save_users(default_users)
            return default_users
    except Exception as e:
        print(f"Error loading users: {e}")
        return {}

def save_users(users):
    """Save users to JSON file"""
    try:
        with open(USERS_FILE, 'w') as f:
            json.dump(users, f, indent=2)
        print(f"DEBUG: Saved users to {USERS_FILE}")
    except Exception as e:
        print(f"Error saving users: {e}")

def hash_password(password):
    """Hash password using SHA-256 with salt"""
    salt = secrets.token_hex(16)
    password_hash = hashlib.sha256((password + salt).encode()).hexdigest()
    hashed = f"{salt}:{password_hash}"
    print(f"DEBUG: Hashed password for verification")
    return hashed

def verify_password(password, stored_hash):
    """Verify password against stored hash"""
    try:
        if ':' not in stored_hash:
            # Handle old format or plain text passwords (for migration)
            print("DEBUG: Old format hash detected, checking plain text")
            return password == stored_hash
        
        salt, password_hash = stored_hash.split(':', 1)
        computed_hash = hashlib.sha256((password + salt).encode()).hexdigest()
        is_valid = computed_hash == password_hash
        print(f"DEBUG: Password verification result: {is_valid}")
        return is_valid
    except Exception as e:
        print(f"DEBUG: Password verification error: {e}")
        return False

def authenticate_user(username, password):
    """Authenticate user credentials"""
    print(f"DEBUG: Attempting to authenticate user: {username}")
    users = load_users()
    
    if username in users:
        print(f"DEBUG: User {username} found in database")
        stored_hash = users[username]['password_hash']
        print(f"DEBUG: Stored hash format: {stored_hash[:20]}...")
        
        if verify_password(password, stored_hash):
            print(f"DEBUG: Password verification successful for {username}")
            # Update last login
            users[username]['last_login'] = datetime.now().isoformat()
            save_users(users)
            return users[username]
        else:
            print(f"DEBUG: Password verification failed for {username}")
    else:
        print(f"DEBUG: User {username} not found in database")
    
    return None

def create_user(username, password, role='user'):
    """Create a new user"""
    users = load_users()
    if username in users:
        return False, "User already exists"
    
    users[username] = {
        'password_hash': hash_password(password),
        'role': role,
        'created_at': datetime.now().isoformat(),
        'last_login': None
    }
    
    save_users(users)
    return True, "User created successfully"

def delete_user(username):
    """Delete a user"""
    users = load_users()
    if username not in users:
        return False, "User not found"
    
    if username == 'admin':
        return False, "Cannot delete admin user"
    
    del users[username]
    save_users(users)
    return True, "User deleted successfully"

def update_user_password(username, new_password):
    """Update user password"""
    users = load_users()
    if username not in users:
        return False, "User not found"
    
    users[username]['password_hash'] = hash_password(new_password)
    save_users(users)
    return True, "Password updated successfully"

# Authentication decorators
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            if request.is_json:
                return jsonify({'error': 'Authentication required', 'redirect': '/login'}), 401
            else:
                flash('Please login to access this feature', 'error')
                return redirect(url_for('login'))
        
        if 'login_time' in session:
            login_time = datetime.fromisoformat(session['login_time'])
            if datetime.now() - login_time > timedelta(seconds=3600):  # 1 hour timeout
                session.clear()
                if request.is_json:
                    return jsonify({'error': 'Session expired', 'redirect': '/login'}), 401
                else:
                    flash('Session expired. Please login again.', 'error')
                    return redirect(url_for('login'))
        
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session or session.get('role') != 'admin':
            if request.is_json:
                return jsonify({'error': 'Admin access required'}), 403
            else:
                flash('Admin access required', 'error')
                return redirect(url_for('home'))
        return f(*args, **kwargs)
    return decorated_function

def register_auth_routes(app, limiter):
    """Register authentication routes"""
    
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'GET':
            return render_template('login.html')
        
        # Handle both form data and JSON
        if request.is_json:
            data = request.get_json()
            username = data.get('username', '').strip()
            password = data.get('password', '')
        else:
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
        
        print(f"DEBUG: Login attempt for username: '{username}'")
        
        if not username or not password:
            error_msg = 'Username and password are required'
            if request.is_json:
                return jsonify({'error': error_msg}), 400
            else:
                flash(error_msg, 'error')
                return render_template('login.html')
        
        user = authenticate_user(username, password)
        if user:
            session['user'] = username
            session['role'] = user['role']
            session['login_time'] = datetime.now().isoformat()
            
            print(f"DEBUG: Login successful for {username} with role {user['role']}")
            
            if request.is_json:
                return jsonify({'success': True, 'role': user['role']})
            else:
                flash('Login successful', 'success')
                return redirect(url_for('home'))
        else:
            error_msg = 'Invalid username or password'
            print(f"DEBUG: Login failed for {username}")
            
            if request.is_json:
                return jsonify({'error': error_msg}), 401
            else:
                flash(error_msg, 'error')
                return render_template('login.html')

    @app.route('/logout', methods=['POST'])
    def logout():
        session.clear()
        return jsonify({'success': True})

    @app.route('/check-auth')
    def check_auth():
        if 'user' in session:
            if 'login_time' in session:
                login_time = datetime.fromisoformat(session['login_time'])
                if datetime.now() - login_time > timedelta(seconds=3600):
                    session.clear()
                    return jsonify({'authenticated': False, 'reason': 'Session expired'})
            
            return jsonify({
                'authenticated': True,
                'user': session['user'],
                'role': session.get('role', 'user')
            })
        return jsonify({'authenticated': False})

    @app.route('/admin')
    @admin_required
    def admin_panel():
        users = load_users()
        user_list = []
        for username, user_data in users.items():
            user_list.append({
                'username': username,
                'role': user_data['role'],
                'created_at': user_data['created_at'],
                'last_login': user_data.get('last_login', 'Never')
            })
        return render_template('admin.html', users=user_list)

    @app.route('/admin/create-user', methods=['POST'])
    @admin_required
    def admin_create_user():
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        role = request.form.get('role', 'user')
        
        if not username or not password:
            return jsonify({'error': 'Username and password are required'}), 400
        
        if role not in ['user', 'admin']:
            return jsonify({'error': 'Invalid role'}), 400
        
        success, message = create_user(username, password, role)
        if success:
            return jsonify({'success': True, 'message': message})
        else:
            return jsonify({'error': message}), 400

    @app.route('/admin/delete-user', methods=['POST'])
    @admin_required
    def admin_delete_user():
        username = request.form.get('username', '').strip()
        
        if not username:
            return jsonify({'error': 'Username is required'}), 400
        
        success, message = delete_user(username)
        if success:
            return jsonify({'success': True, 'message': message})
        else:
            return jsonify({'error': message}), 400

    @app.route('/admin/update-password', methods=['POST'])
    @admin_required
    def admin_update_password():
        username = request.form.get('username', '').strip()
        new_password = request.form.get('new_password', '')
        
        if not username or not new_password:
            return jsonify({'error': 'Username and new password are required'}), 400
        
        success, message = update_user_password(username, new_password)
        if success:
            return jsonify({'success': True, 'message': message})
        else:
            return jsonify({'error': message}), 400

# Debug function to check users file
def debug_users_file():
    """Debug function to check users file status"""
    print(f"DEBUG: Checking users file at {USERS_FILE}")
    print(f"DEBUG: File exists: {os.path.exists(USERS_FILE)}")
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, 'r') as f:
                content = f.read()
                print(f"DEBUG: File content preview: {content[:200]}...")
        except Exception as e:
            print(f"DEBUG: Error reading file: {e}")

# Call debug function when module loads
debug_users_file()