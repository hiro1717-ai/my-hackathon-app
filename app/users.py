"""ユーザー（管理者／熟練者／若手）とログイン、設定値。

- パスワードは平文で持たず、塩つきハッシュ（PBKDF2）で保存する。
- ログイン状態は「ユーザーID.署名」の cookie。署名の鍵は data/secret.txt に自動生成する（git には入れない）。
- 管理者は初回起動時の「初期設定」画面で作る（IDやパスワードをコードに書かない）。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

# 役割＝管理者か、熟練レベル（未格付・初級・中級・上級）。中級以上はマニュアル登録モードに入れる
ROLES = [("admin", "管理者"), ("advanced", "上級"), ("intermediate", "中級"), ("beginner", "初級"), ("unrated", "未格付")]
CAN_REGISTER = {"admin", "advanced", "intermediate"}
ROLE_LABELS = dict(ROLES)

_db_path: Path | None = None
_secret: bytes = b""


def init(db_path: Path, data_dir: Path) -> None:
    global _db_path, _secret
    _db_path = db_path
    key_file = data_dir / "secret.txt"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32))
    _secret = key_file.read_text().strip().encode()
    with _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                login_id TEXT UNIQUE NOT NULL,
                name TEXT DEFAULT '',
                role TEXT NOT NULL DEFAULT 'unrated',   -- admin / advanced / intermediate / beginner / unrated
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            -- 保存した手順（カード）：ユーザーごと
            CREATE TABLE IF NOT EXISTS saved_cards (
                user_id TEXT NOT NULL,
                card_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, card_id)
            );
            -- 管理者が画面から変えられる設定値
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # 昔の役割（熟練者・若手）を熟練レベルへ。熟練者→上級、若手→未格付
        conn.execute("UPDATE users SET role = 'advanced' WHERE role = 'expert'")
        conn.execute("UPDATE users SET role = 'unrated' WHERE role = 'learner'")
        # 管理者が認めた「マニュアル登録を1回」の残り
        if "create_grant" not in {r[1] for r in conn.execute("PRAGMA table_info(users)")}:
            conn.execute("ALTER TABLE users ADD COLUMN create_grant INTEGER DEFAULT 0")
        # 管理者が再設定を認めた期限（この時刻までは、IDと名前で新しいパスワードを決められる）
        ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "reset_until" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN reset_until TEXT DEFAULT ''")
        # ワンタイムパスワード（6桁。ハッシュで保存）と、間違えた回数
        if "reset_code" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN reset_code TEXT DEFAULT ''")
        if "reset_tries" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN reset_tries INTEGER DEFAULT 0")
        conn.execute("CREATE TABLE IF NOT EXISTS reset_requests (id TEXT PRIMARY KEY, token TEXT NOT NULL, name TEXT DEFAULT '',"
                     " login_hint TEXT DEFAULT '', user_id TEXT DEFAULT '', status TEXT DEFAULT 'pending',"
                     " created_at TEXT NOT NULL, decided_at TEXT DEFAULT '')")
        conn.execute("CREATE TABLE IF NOT EXISTS register_requests (id TEXT PRIMARY KEY, user_id TEXT NOT NULL,"
                     " status TEXT DEFAULT 'pending', created_at TEXT NOT NULL, decided_at TEXT DEFAULT '')")


def can_register(user) -> bool:
    """マニュアル登録モードに入れるか：管理者・中級・上級、または管理者が1回認めた人。"""
    if user is None:
        return False
    try:
        grant = user["create_grant"] or 0
    except (IndexError, KeyError):
        grant = 0
    return user["role"] in CAN_REGISTER or grant > 0


def _conn() -> sqlite3.Connection:
    assert _db_path is not None
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- パスワード ----------

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000).hex()
    return f"{salt}${h}"


def check_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000).hex()
    return hmac.compare_digest(calc, h)


# ---------- ユーザー ----------

def count() -> int:
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create(login_id: str, password: str, name: str, role: str) -> str:
    if role not in ROLE_LABELS:
        role = "unrated"
    uid = uuid.uuid4().hex[:8]
    with _conn() as conn:
        conn.execute(
            "INSERT INTO users (id, login_id, name, role, password_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, login_id.strip(), name.strip() or login_id.strip(), role, hash_password(password), _now()),
        )
    return uid


def get(uid: str) -> sqlite3.Row | None:
    with _conn() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def by_login(login_id: str) -> sqlite3.Row | None:
    with _conn() as conn:
        return conn.execute("SELECT * FROM users WHERE login_id = ?", (login_id.strip(),)).fetchone()


def all_users() -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute("SELECT * FROM users ORDER BY role, login_id").fetchall()


def update(uid: str, **fields) -> None:
    allowed = {"name", "role", "login_id"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    with _conn() as conn:
        conn.execute(f"UPDATE users SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?", (*sets.values(), uid))


def set_password(uid: str, password: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), uid))


def delete(uid: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
        conn.execute("DELETE FROM saved_cards WHERE user_id = ?", (uid,))


def authenticate(login_id: str, password: str) -> sqlite3.Row | None:
    u = by_login(login_id)
    if u and check_password(password, u["password_hash"]):
        return u
    return None


# ---------- ログイン cookie ----------

def token_for(uid: str) -> str:
    return uid + "." + hmac.new(_secret, uid.encode(), hashlib.sha256).hexdigest()


def user_from_token(token: str) -> sqlite3.Row | None:
    if not token or "." not in token:
        return None
    uid, sig = token.split(".", 1)
    if not hmac.compare_digest(sig, hmac.new(_secret, uid.encode(), hashlib.sha256).hexdigest()):
        return None
    return get(uid)


# ---------- 保存した手順 ----------

def toggle_saved(uid: str, card_id: str) -> bool:
    with _conn() as conn:
        if conn.execute("SELECT 1 FROM saved_cards WHERE user_id = ? AND card_id = ?", (uid, card_id)).fetchone():
            conn.execute("DELETE FROM saved_cards WHERE user_id = ? AND card_id = ?", (uid, card_id))
            return False
        conn.execute("INSERT INTO saved_cards (user_id, card_id, created_at) VALUES (?, ?, ?)", (uid, card_id, _now()))
        return True


def saved_ids(uid: str) -> set[str]:
    with _conn() as conn:
        return {r["card_id"] for r in conn.execute("SELECT card_id FROM saved_cards WHERE user_id = ?", (uid,))}


# ---------- 設定値 ----------

def get_setting(key: str, default: str = "") -> str:
    with _conn() as conn:
        r = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key: str, value: str) -> None:
    with _conn() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
