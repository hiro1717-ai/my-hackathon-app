"""AI技能コーチ（仮）のサーバー本体。

起動:  uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
スマホからは同じ Wi-Fi で http://<MacのIPアドレス>:8000 を開く。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

import hashlib
import hmac

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
MEDIA_DIR = BASE_DIR / "media"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "app.db"

MEDIA_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

# ---------- ログイン（共通パスワード1つ。.env の APP_PASSWORD） ----------
# 空なら「ログインなし」で動く（開発用）。現場で使うときは必ず設定する。
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
SESSION_COOKIE = "coach_session"
LOGIN_FREE_PREFIXES = ("/login", "/static/")


def session_token() -> str:
    """パスワードから作る署名。これが cookie に入っていればログイン済みとみなす。"""
    return hmac.new(b"ai-skill-coach-v1", APP_PASSWORD.encode(), hashlib.sha256).hexdigest()


def is_logged_in(request: Request) -> bool:
    if not APP_PASSWORD:
        return True
    token = request.cookies.get(SESSION_COOKIE, "")
    return hmac.compare_digest(token, session_token())


app = FastAPI(title="AI技能コーチ（仮）")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if not path.startswith(LOGIN_FREE_PREFIXES) and not is_logged_in(request):
        return RedirectResponse(f"/login?next={path}", status_code=303)
    return await call_next(request)


app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
templates.env.globals["login_enabled"] = bool(APP_PASSWORD)


# ---------- データベース（SQLite。1ファイル） ----------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            -- 案件：試作部品1点
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                domain TEXT DEFAULT '3dp',          -- 分野プロファイル
                expert_name TEXT DEFAULT '',
                expert_level INTEGER DEFAULT 5,     -- 熟練者レベル 1〜7
                created_at TEXT NOT NULL
            );

            -- 動画：案件に紐づく作業動画（しゃべりながら撮ったもの）
            CREATE TABLE IF NOT EXISTS videos (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                original_name TEXT DEFAULT '',
                status TEXT DEFAULT 'uploaded',     -- uploaded / transcribing / transcribed / analyzed
                created_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            );
            """
        )


init_db()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- 画面 ----------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: str = ""):
    return templates.TemplateResponse(
        request, "login.html", {"next": next, "error": error}
    )


@app.post("/login")
def login(password: str = Form(""), next: str = Form("/")):
    if not APP_PASSWORD or not hmac.compare_digest(password, APP_PASSWORD):
        return RedirectResponse(f"/login?next={next}&error=1", status_code=303)
    resp = RedirectResponse(next or "/", status_code=303)
    # 30日間はログインを聞かない（ガラケー世代向け：毎回入力させない）
    resp.set_cookie(SESSION_COOKIE, session_token(), max_age=30 * 24 * 3600, httponly=True, samesite="lax")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with get_db() as conn:
        projects = conn.execute(
            "SELECT * FROM projects ORDER BY created_at DESC"
        ).fetchall()
    return templates.TemplateResponse(
        request, "index.html", {"projects": projects}
    )


@app.post("/projects")
def create_project(
    name: str = Form(...),
    description: str = Form(""),
    expert_name: str = Form(""),
    expert_level: int = Form(5),
):
    pid = uuid.uuid4().hex[:8]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, description, expert_name, expert_level, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (pid, name, description, expert_name, expert_level, now()),
        )
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@app.post("/projects/{pid}/delete")
def delete_project(pid: str):
    with get_db() as conn:
        conn.execute("DELETE FROM videos WHERE project_id = ?", (pid,))
        conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
    shutil.rmtree(MEDIA_DIR / pid, ignore_errors=True)
    return RedirectResponse("/", status_code=303)


@app.get("/projects/{pid}", response_class=HTMLResponse)
def project_page(request: Request, pid: str):
    with get_db() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        videos = conn.execute(
            "SELECT * FROM videos WHERE project_id = ? ORDER BY created_at DESC", (pid,)
        ).fetchall()
    return templates.TemplateResponse(
        request, "project.html", {"project": project, "videos": videos}
    )


@app.post("/projects/{pid}/videos")
async def upload_video(pid: str, file: UploadFile = File(...)):
    vid = uuid.uuid4().hex[:8]
    ext = Path(file.filename or "video.mp4").suffix or ".mp4"
    dest_dir = MEDIA_DIR / pid
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{vid}{ext}"
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO videos (id, project_id, filename, original_name, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (vid, pid, f"{pid}/{vid}{ext}", file.filename or "", now()),
        )
    return RedirectResponse(f"/projects/{pid}", status_code=303)
