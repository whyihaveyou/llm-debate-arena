import os
import sqlite3
import hashlib
import secrets
import logging
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "debate.db"

log = logging.getLogger(__name__)

# --- Encryption helpers (Fernet) ---
ENCRYPTION_KEY = os.environ.get("DEBATE_ENCRYPTION_KEY", "")
_fernet = None
if ENCRYPTION_KEY:
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(ENCRYPTION_KEY.encode())
    except Exception as e:
        log.warning("Failed to initialize encryption: %s", e)

if not _fernet:
    log.warning("DEBATE_ENCRYPTION_KEY not set — API keys stored in plaintext")


def encrypt_key(plaintext: str) -> str:
    if not _fernet or not plaintext:
        return plaintext
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_key(ciphertext: str) -> str:
    if not _fernet or not ciphertext:
        return ciphertext
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except Exception:
        return ciphertext  # backward compat: not-yet-encrypted key


# --- Database ---

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS user_models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            api_key TEXT NOT NULL,
            model TEXT NOT NULL,
            auth_type TEXT DEFAULT 'bearer',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            month TEXT NOT NULL,
            debate_count INTEGER DEFAULT 0,
            UNIQUE(user_id, month),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)
    try:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def _hash_pw(password: str, salt: str | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return h, salt


def create_user(username: str, password: str) -> bool:
    conn = get_db()
    try:
        h, salt = _hash_pw(password)
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
            (username, h, salt, datetime.now().isoformat()),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def verify_user(username: str, password: str) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if not row:
        return None
    h, _ = _hash_pw(password, row["salt"])
    if h != row["password_hash"]:
        return None
    return dict(row)


def create_session(user_id: int) -> str:
    token = secrets.token_hex(32)
    conn = get_db()
    conn.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
                 (token, user_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return token


def verify_session(token: str) -> dict | None:
    if not token or len(token) < 10:
        return None
    conn = get_db()
    row = conn.execute(
        "SELECT s.token, s.user_id, u.username, u.is_admin FROM sessions s JOIN users u ON s.user_id = u.id WHERE s.token = ?",
        (token,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_session(token: str):
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()


def get_user_models(user_id: int) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, base_url, model, auth_type, created_at FROM user_models WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_user_model(user_id: int, name: str, base_url: str, api_key: str, model: str, auth_type: str = "bearer") -> int:
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO user_models (user_id, name, base_url, api_key, model, auth_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, name, base_url, encrypt_key(api_key), model, auth_type, datetime.now().isoformat()),
    )
    conn.commit()
    mid = cursor.lastrowid
    conn.close()
    return mid


def get_user_model(user_id: int, model_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM user_models WHERE id = ? AND user_id = ?", (model_id, user_id)).fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    result["api_key"] = decrypt_key(result["api_key"])
    return result


def update_user_model(model_id: int, user_id: int, **kwargs) -> bool:
    conn = get_db()
    existing = conn.execute("SELECT id FROM user_models WHERE id = ? AND user_id = ?", (model_id, user_id)).fetchone()
    if not existing:
        conn.close()
        return False
    if "api_key" in kwargs:
        kwargs["api_key"] = encrypt_key(kwargs["api_key"])
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [model_id, user_id]
    conn.execute(f"UPDATE user_models SET {sets} WHERE id = ? AND user_id = ?", vals)
    conn.commit()
    conn.close()
    return True


def delete_user_model(model_id: int, user_id: int):
    conn = get_db()
    conn.execute("DELETE FROM user_models WHERE id = ? AND user_id = ?", (model_id, user_id))
    conn.commit()
    conn.close()


# --- Admin ---

def get_user_by_id(user_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT id, username, is_admin, created_at FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_admin(user_id: int, is_admin: bool):
    conn = get_db()
    conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id))
    conn.commit()
    conn.close()


def get_all_users() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT id, username, is_admin, created_at FROM users ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Quota ---

def _current_month() -> str:
    return datetime.now().strftime("%Y-%m")


def get_quota_usage(user_id: int) -> int:
    conn = get_db()
    row = conn.execute(
        "SELECT debate_count FROM usage WHERE user_id = ? AND month = ?",
        (user_id, _current_month())
    ).fetchone()
    conn.close()
    return row["debate_count"] if row else 0


def increment_quota_usage(user_id: int):
    conn = get_db()
    conn.execute(
        "INSERT INTO usage (user_id, month, debate_count) VALUES (?, ?, 1) "
        "ON CONFLICT(user_id, month) SET debate_count = debate_count + 1",
        (user_id, _current_month())
    )
    conn.commit()
    conn.close()


def get_all_usage_stats() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT u.username, u.is_admin, g.month, g.debate_count "
        "FROM usage g JOIN users u ON g.user_id = u.id "
        "ORDER BY u.username, g.month DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
