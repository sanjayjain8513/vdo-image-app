#!/usr/bin/env python3
"""
Setup script to create and prepare persistent data directories
"""
import os
import json
from datetime import datetime

def setup_data_directories():
    """Create and setup persistent data directories"""

    # Create main data directory
    data_dir = 'data'
    os.makedirs(data_dir, exist_ok=True)
    print(f"✓ Created data directory: {data_dir}")

    # Create users.json if it doesn't exist
    users_file = os.path.join(data_dir, 'users.json')
    if not os.path.exists(users_file):
        default_users = {}
        with open(users_file, 'w') as f:
            json.dump(default_users, f, indent=2)
        print(f"✓ Created users file: {users_file}")
    else:
        print(f"✓ Users file already exists: {users_file}")

    # Create video_jobs.json if it doesn't exist
    jobs_file = os.path.join(data_dir, 'video_jobs.json')
    if not os.path.exists(jobs_file):
        default_jobs = {}
        with open(jobs_file, 'w') as f:
            json.dump(default_jobs, f, indent=2)
        print(f"✓ Created video jobs file: {jobs_file}")
    else:
        print(f"✓ Video jobs file already exists: {jobs_file}")

    # Create visitor log file
    visitor_log = os.path.join(data_dir, 'visitors.log')
    if not os.path.exists(visitor_log):
        with open(visitor_log, 'w') as f:
            f.write("")  # Create empty file
        print(f"✓ Created visitor log: {visitor_log}")
    else:
        print(f"✓ Visitor log already exists: {visitor_log}")

    # Set proper permissions
    try:
        os.chmod(data_dir, 0o755)
        os.chmod(users_file, 0o644)
        os.chmod(jobs_file, 0o644)
        os.chmod(visitor_log, 0o644)
        print("✓ Set proper file permissions")
    except Exception as e:
        print(f"⚠ Warning: Could not set permissions: {e}")

    print(f"\n✅ Data directory setup complete!")
    print(f"   Users: {users_file}")
    print(f"   Jobs: {jobs_file}")
    print(f"   Logs: {visitor_log}")

if __name__ == "__main__":
    setup_data_directories()