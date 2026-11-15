"""Local-owner pairing and bounded viewer sessions, separate from ingestion credentials."""
import argparse
from contextlib import contextmanager
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import time
from .storage import prepare_private_database
from .paths import default_data_dir

COOKIE = 'kindle_access'
SESSION_SECONDS = 90 * 24 * 3600
MAX_DEVICES = 8


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class AccessStore:
    def __init__(self, path):
        self.path = prepare_private_database(path)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS viewers (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
                    browser TEXT NOT NULL, ip TEXT NOT NULL, created_at REAL NOT NULL,
                    last_seen REAL NOT NULL, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS pairing (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), code TEXT NOT NULL,
                    expires_at REAL NOT NULL, attempts INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS denied (
                    ip TEXT PRIMARY KEY, last_seen REAL NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def prune(db, now):
        db.execute('DELETE FROM viewers WHERE expires_at<=?', (now,))
        db.execute('DELETE FROM pairing WHERE expires_at<=?', (now,))
        db.execute('DELETE FROM denied WHERE last_seen<?', (now-86400,))

    def state(self):
        now = time.time()
        with self.connect() as db:
            self.prune(db, now)
            devices = [dict(r) for r in db.execute('''SELECT id,name,browser,ip,created_at,last_seen,expires_at
                FROM viewers ORDER BY last_seen DESC''')]
            pairing = db.execute('SELECT code,expires_at FROM pairing').fetchone()
            denied = [dict(r) for r in db.execute('SELECT ip,last_seen FROM denied ORDER BY last_seen DESC LIMIT 20')]
        return {'devices': devices, 'pairing': dict(pairing) if pairing else None, 'denied': denied}

    def create_pairing(self):
        now = time.time()
        code = ''.join(secrets.choice('23456789ABCDEFGHJKLMNPQRSTUVWXYZ') for _ in range(10))
        with self.connect() as db:
            self.prune(db, now)
            if db.execute('SELECT COUNT(*) FROM viewers').fetchone()[0] >= MAX_DEVICES:
                raise ValueError('最多授权 8 台设备，请先移除不再使用的设备。')
            db.execute('INSERT OR REPLACE INTO pairing VALUES (1,?,?,0)', (code, now+300))
        return self.state()

    def pair(self, code, name, ip, user_agent):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40 or not all(c.isprintable() for c in name):
            raise ValueError('设备名称应为 1–40 个可见字符。')
        if not isinstance(code, str) or len(code) > 32:
            raise ValueError('配对码无效或已过期。')
        code = code.replace(' ', '').replace('-', '').upper()
        now = time.time()
        token = secrets.token_urlsafe(32)
        browser = ('Kindle' if any(x in user_agent.lower() for x in ('kindle', 'silk')) else
                   'Edge' if 'Edg/' in user_agent else 'Chrome' if 'Chrome/' in user_agent else
                   'Safari' if 'Safari/' in user_agent else '浏览器')
        accepted = False
        with self.connect() as db:
            self.prune(db, now)
            pairing = db.execute('SELECT * FROM pairing').fetchone()
            if pairing:
                if hmac.compare_digest(code.encode(), pairing['code'].encode()):
                    if db.execute('SELECT COUNT(*) FROM viewers').fetchone()[0] < MAX_DEVICES:
                        db.execute('INSERT INTO viewers VALUES (?,?,?,?,?,?,?,?)',
                            (secrets.token_hex(12), name.strip(), digest(token), browser, ip, now, now, now+SESSION_SECONDS))
                        accepted = True
                    db.execute('DELETE FROM pairing')
                else:
                    db.execute('UPDATE pairing SET attempts=attempts+1')
                    db.execute('DELETE FROM pairing WHERE attempts>=10')
        if not accepted:
            self.record_denied(ip)
            raise ValueError('配对码无效或已过期，请在 Mac 上重新生成。')
        return token

    def authorize(self, token, ip):
        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            return False
        now = time.time()
        with self.connect() as db:
            row = db.execute('SELECT id,last_seen,ip FROM viewers WHERE token_hash=? AND expires_at>?',
                             (digest(token), now)).fetchone()
            if not row:
                return False
            if now-row['last_seen'] >= 30 or row['ip'] != ip:
                db.execute('UPDATE viewers SET last_seen=?,ip=? WHERE id=?', (now, ip, row['id']))
        return True

    def record_denied(self, ip):
        now = time.time()
        with self.connect() as db:
            db.execute('DELETE FROM denied WHERE last_seen<?', (now-86400,))
            row = db.execute('SELECT last_seen FROM denied WHERE ip=?', (ip,)).fetchone()
            if not row or now-row[0] >= 60:
                db.execute('INSERT OR REPLACE INTO denied VALUES (?,?)', (ip, now))
            db.execute('DELETE FROM denied WHERE ip NOT IN (SELECT ip FROM denied ORDER BY last_seen DESC LIMIT 20)')

    def manage(self, action, device_id=None):
        if action == 'create':
            return self.create_pairing()
        with self.connect() as db:
            if action == 'revoke':
                db.execute('DELETE FROM viewers WHERE id=?', (device_id,))
            elif action == 'reset':
                db.execute('DELETE FROM viewers')
                db.execute('DELETE FROM pairing')
                db.execute('DELETE FROM denied')
            elif action == 'cancel':
                db.execute('DELETE FROM pairing')
        return self.state()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['status', 'create', 'cancel', 'revoke', 'reset'])
    parser.add_argument('device_id', nargs='?')
    parser.add_argument('--data-dir', type=Path, default=default_data_dir())
    args = parser.parse_args()
    try:
        access = AccessStore(args.data_dir/'access.sqlite3')
        print(json.dumps(access.manage(args.action, args.device_id), ensure_ascii=False))
    except ValueError as exc:
        print(json.dumps({'error':str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
    except (OSError, sqlite3.Error):
        print(json.dumps({'error': '访问管理操作未完成，请检查设备数量或稍后重试。'}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
