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

app = FastAPI(title="AI技能コーチ（仮）")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


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
