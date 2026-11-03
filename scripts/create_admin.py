#!/usr/bin/env python3
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.masterdeorca import database
from pwdlib import PasswordHash


def main():
    email = (os.environ.get('ADMIN_EMAIL') or 'admin@example.com').strip().lower()
    password = os.environ.get('ADMIN_PASSWORD') or 'admin@123'
    if not email or not password:
        print('Email and password required', file=sys.stderr)
        return 1
    ph = PasswordHash.recommended()
    phash = ph.hash(password)
    now = datetime.now(timezone.utc).isoformat()
    
    db = database.connect()
    try:
        # Check existing admin
        try:
            existing = db.execute("SELECT email FROM admins WHERE email=?", (email,)).fetchone()
            if existing:
                print('Admin already exists')
                return 0
        except Exception:
            pass
        # Create in admins table
        try:
            db.execute("INSERT INTO admins(email,password_hash,access_mode) VALUES(?,?,?)", (email, phash, 'edit'))
        except Exception as e:
            try:
                db.execute("INSERT INTO admins(email,password_hash) VALUES(?,?)", (email, phash))
            except Exception as e2:
                print(f'Failed to create admin: {e2}', file=sys.stderr)
                return 1
        # Create/update in users table for login
        try:
            u = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if u:
                db.execute("UPDATE users SET name='Administrator', role='admin', status='active', hashed_password=? WHERE email=?", (phash, email))
            else:
                db.execute("INSERT INTO users(email,name,hashed_password,role,status,created_at) VALUES(?,?,?,?,?,?)",
                           (email, 'Administrator', phash, 'admin', 'active', now))
        except Exception as e:
            print(f'Warning: users table update failed: {e}', file=sys.stderr)
        
        # Verify
        try:
            v = db.execute("SELECT email FROM admins WHERE email=?", (email,)).fetchone()
            if v:
                print('Admin created and verified')
                return 0
        except Exception:
            pass
        print('Admin created')
        return 0
    finally:
        try:
            db.close()
        except Exception:
            pass


if __name__ == '__main__':
    raise SystemExit(main())
