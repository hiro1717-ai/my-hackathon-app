"""AI技能コーチ（仮）のサーバー本体。

起動:  uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
スマホからは同じ Wi-Fi で http://<MacのIPアドレス>:8000 を開く。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import analyze as analyze_mod
from app import frames as frames_mod
from app import profiles
from app import transcribe as transcribe_mod

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
templates.env.globals["whisper_model"] = transcribe_mod.model_name()
templates.env.globals["whisper_models"] = transcribe_mod.MODELS


def fmt_time(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


templates.env.filters["mmss"] = fmt_time


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
                status TEXT DEFAULT 'uploaded',     -- uploaded / transcribing / transcribed / error
                duration REAL DEFAULT 0,
                segments TEXT DEFAULT '[]',         -- 時刻つきの文章（JSON）
                frames TEXT DEFAULT '[]',           -- 切り出した静止画（JSON）
                error TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            );

            -- 判断カード：分かれ目1個＝1枚。data は20項目のJSON
            CREATE TABLE IF NOT EXISTS cards (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                video_id TEXT NOT NULL,
                ord INTEGER DEFAULT 0,
                status TEXT DEFAULT 'draft',        -- draft / approved / rejected
                data TEXT DEFAULT '{}',
                answers TEXT DEFAULT '{}',          -- 質問への熟練者の答え {質問: 答え}
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- 教育モードの記録：新人がどの分かれ目で何と答え、どう振り返ったか
            CREATE TABLE IF NOT EXISTS learn_logs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                card_id TEXT NOT NULL,
                learner TEXT DEFAULT '',
                answer TEXT DEFAULT '',
                result TEXT DEFAULT '',             -- ok / ng
                created_at TEXT NOT NULL
            );
            """
        )
        # 古いDBに列を足す（すでにあれば何もしない）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
        for col, ddl in [
            ("duration", "REAL DEFAULT 0"),
            ("segments", "TEXT DEFAULT '[]'"),
            ("frames", "TEXT DEFAULT '[]'"),
            ("error", "TEXT DEFAULT ''"),
            ("model", "TEXT DEFAULT ''"),        # 文字起こしに使ったモデル名
            ("elapsed", "REAL DEFAULT 0"),       # 文字起こしにかかった秒数
            ("analysis_status", "TEXT DEFAULT ''"),  # '' / running / done / error
            ("analysis_error", "TEXT DEFAULT ''"),
            ("summary", "TEXT DEFAULT ''"),      # AIがまとめた「この動画でやっている作業」
            ("notes", "TEXT DEFAULT ''"),        # AIから熟練者への確認事項
        ]:
            if col not in cols:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {col} {ddl}")


init_db()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def set_video(vid: str, **fields) -> None:
    keys = ", ".join(f"{k} = ?" for k in fields)
    with get_db() as conn:
        conn.execute(f"UPDATE videos SET {keys} WHERE id = ?", (*fields.values(), vid))


# ---------- 裏で動く処理：文字起こし → 静止画 ----------

def process_video(vid: str, model: str = "") -> None:
    with get_db() as conn:
        v = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
    if not v:
        return
    video_path = MEDIA_DIR / v["filename"]
    model = model or transcribe_mod.model_name()
    with get_db() as conn:
        proj = conn.execute("SELECT domain FROM projects WHERE id = ?", (v["project_id"],)).fetchone()
    domain = (proj["domain"] if proj else "") or "3dp"
    try:
        set_video(vid, status="transcribing", error="", model=model)
        duration = frames_mod.video_duration(video_path)
        t0 = time.time()
        segments = transcribe_mod.transcribe(video_path, model, domain)
        set_video(vid, duration=duration, elapsed=round(time.time() - t0, 1),
                  segments=json.dumps(segments, ensure_ascii=False))

        times = frames_mod.pick_times(segments, duration)
        frame_dir = video_path.parent / f"{vid}_frames"
        frames = frames_mod.extract_frames(video_path, frame_dir, times)
        frames = [{"time": f["time"], "url": f"/media/{v['project_id']}/{vid}_frames/{f['file']}"} for f in frames]
        set_video(vid, frames=json.dumps(frames), status="transcribed")
    except Exception:
        set_video(vid, status="error", error=traceback.format_exc()[-2000:])
        return
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        process_analysis(vid)


def process_analysis(vid: str) -> None:
    """判断カードの下書き（Claude API）。既存の下書きは置き換える（承認済みは残す）。"""
    with get_db() as conn:
        v = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (v["project_id"],)).fetchone()
    try:
        set_video(vid, analysis_status="running", analysis_error="")
        out = analyze_mod.draft_cards(dict(project), json.loads(v["segments"] or "[]"),
                                      json.loads(v["frames"] or "[]"), MEDIA_DIR)
        with get_db() as conn:
            conn.execute("DELETE FROM cards WHERE video_id = ? AND status = 'draft'", (vid,))
            base = conn.execute("SELECT COALESCE(MAX(ord), 0) FROM cards WHERE video_id = ?", (vid,)).fetchone()[0]
            for i, c in enumerate(out["cards"], 1):
                conn.execute(
                    "INSERT INTO cards (id, project_id, video_id, ord, status, data, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, 'draft', ?, ?, ?)",
                    (uuid.uuid4().hex[:8], v["project_id"], vid, base + i,
                     json.dumps(c, ensure_ascii=False), now(), now()),
                )
        set_video(vid, analysis_status="done", summary=out.get("summary", ""), notes=out.get("notes_for_expert", ""))
    except Exception:
        set_video(vid, analysis_status="error", analysis_error=traceback.format_exc()[-2000:])


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
        request, "index.html", {"projects": projects, "back_url": None}
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
        request, "project.html", {"project": project, "videos": videos, "back_url": "/"}
    )


@app.post("/projects/{pid}/videos")
async def upload_video(pid: str, background: BackgroundTasks, file: UploadFile = File(...)):
    vid = uuid.uuid4().hex[:8]
    ext = Path(file.filename or "video.mp4").suffix.lower() or ".mp4"
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
    # 送信したらすぐ画面を返し、文字起こしは裏で進める
    background.add_task(process_video, vid)
    return RedirectResponse(f"/videos/{vid}", status_code=303)


@app.get("/videos/{vid}", response_class=HTMLResponse)
def video_page(request: Request, vid: str):
    with get_db() as conn:
        video = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (video["project_id"],)).fetchone()
        cards = conn.execute("SELECT * FROM cards WHERE video_id = ? ORDER BY ord", (vid,)).fetchall()
    segments = json.loads(video["segments"] or "[]")
    frames = json.loads(video["frames"] or "[]")
    cards = [card_view(c) for c in cards]
    return templates.TemplateResponse(
        request, "video.html",
        {"video": video, "project": project, "segments": segments, "frames": frames, "cards": cards,
         "has_key": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
         "back_url": f"/projects/{project['id']}"},
    )


@app.get("/videos/{vid}/status")
def video_status(vid: str):
    """処理中の画面が数秒ごとに状態を確認するための小さな窓口。"""
    with get_db() as conn:
        video = conn.execute("SELECT status, analysis_status FROM videos WHERE id = ?", (vid,)).fetchone()
    return JSONResponse({"status": video["status"] if video else "missing",
                         "analysis": video["analysis_status"] if video else ""})


@app.post("/videos/{vid}/retry")
def video_retry(vid: str, background: BackgroundTasks, model: str = Form("")):
    allowed = {m for m, _ in transcribe_mod.MODELS}
    background.add_task(process_video, vid, model if model in allowed else "")
    return RedirectResponse(f"/videos/{vid}", status_code=303)


@app.post("/videos/{vid}/analyze")
def video_analyze(vid: str, background: BackgroundTasks):
    background.add_task(process_analysis, vid)
    return RedirectResponse(f"/videos/{vid}", status_code=303)


# ---------- 判断カード ----------

# 画面に出す項目（番号は spec.md 9節）。(キー, 見出し, 種類)
CARD_FIELDS = [
    ("title", "1 手順名", "line"),
    ("stage", "1 段階", "line"),
    ("watch", "2 注目点（何を見る）", "text"),
    ("boundary", "3 正常／異常の境界", "text"),
    ("decision", "4 判断（どうする）", "text"),
    ("degree", "5 加減（どのくらい）", "text"),
    ("reason", "6 理由（なぜ）", "text"),
    ("stop_condition", "7 止める条件／次へ移る条件", "text"),
    ("novice_result", "8 素人がやるとどうなる", "text"),
    ("tools", "10 必要な工具・物", "list"),
    ("judgment_type", "11 判断の種類", "judgment"),
    ("judgment_note", "11 補足（一般／ローカル、数値化の候補）", "text"),
    ("safety", "12 安全に関わるか", "bool"),
    ("improvement", "13 さらに良くするには（AI・確度の高いものだけ）", "text"),
    ("improvement_basis", "13 根拠", "text"),
    ("prerequisites", "18 この手順の前に必要なこと", "text"),
    ("order_matters", "19 順番の重要度", "order"),
    ("order_note", "19 順番を変えると何が起きるか", "text"),
    ("ripple", "20 波及先（後の手順にどう効くか）", "text"),
    ("is_branch", "分かれ目マーカー", "bool"),
    ("branch_reason", "分かれ目と判断した理由", "text"),
    ("evidence", "根拠にした熟練者の言葉", "text"),
]
JUDGMENT_LABELS = {"A": "A 知識", "B": "B 数値", "C": "C 体感", "D": "D 状況"}
ORDER_LABELS = {"high": "重要（順番を変えるとやり直し）", "mid": "ある程度", "low": "前後してよい"}
STATUS_LABELS = {"draft": "下書き", "approved": "承認済み", "rejected": "不採用"}
templates.env.globals.update(CARD_FIELDS=CARD_FIELDS, JUDGMENT_LABELS=JUDGMENT_LABELS,
                             ORDER_LABELS=ORDER_LABELS, STATUS_LABELS=STATUS_LABELS)


def card_view(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["data"] = json.loads(row["data"] or "{}")
    d["answers"] = json.loads(row["answers"] or "{}")
    return d


@app.get("/cards/{cid}", response_class=HTMLResponse)
def card_page(request: Request, cid: str):
    with get_db() as conn:
        card = conn.execute("SELECT * FROM cards WHERE id = ?", (cid,)).fetchone()
        video = conn.execute("SELECT * FROM videos WHERE id = ?", (card["video_id"],)).fetchone()
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (card["project_id"],)).fetchone()
    return templates.TemplateResponse(
        request, "card.html",
        {"card": card_view(card), "video": video, "project": project,
         "back_url": f"/videos/{video['id']}#cards"},
    )


@app.post("/cards/{cid}")
async def card_save(request: Request, cid: str):
    """承認画面の保存：各欄の修正、質問への答え、状態（承認／下書き／不採用）。"""
    form = await request.form()
    with get_db() as conn:
        card = conn.execute("SELECT * FROM cards WHERE id = ?", (cid,)).fetchone()
    data = json.loads(card["data"] or "{}")
    answers = json.loads(card["answers"] or "{}")
    full_form = "title" in form  # 承認画面からの送信（欄が全部ある）ときだけ欄を書き換える
    for key, _label, kind in CARD_FIELDS:
        if not full_form:
            break
        if kind == "bool":
            data[key] = form.get(key) == "on"
        elif kind == "list":
            data[key] = [t.strip() for t in str(form.get(key, "")).replace("、", ",").split(",") if t.strip()]
        elif key in form:
            data[key] = str(form.get(key, "")).strip()
    for i, q in enumerate(data.get("questions", [])):
        a = str(form.get(f"answer_{i}", "")).strip()
        if a:
            answers[q] = a
    action = form.get("action", "save")
    status = {"approve": "approved", "reject": "rejected", "draft": "draft"}.get(action, card["status"])
    with get_db() as conn:
        conn.execute("UPDATE cards SET data = ?, answers = ?, status = ?, updated_at = ? WHERE id = ?",
                     (json.dumps(data, ensure_ascii=False), json.dumps(answers, ensure_ascii=False), status, now(), cid))
    if action == "approve":
        # 次の下書きへ進む（1枚ずつ「合ってる／直す」）
        with get_db() as conn:
            nxt = conn.execute(
                "SELECT id FROM cards WHERE video_id = ? AND status = 'draft' AND ord > ? ORDER BY ord LIMIT 1",
                (card["video_id"], card["ord"])).fetchone()
        if nxt:
            return RedirectResponse(f"/cards/{nxt['id']}", status_code=303)
        return RedirectResponse(f"/videos/{card['video_id']}#cards", status_code=303)
    return RedirectResponse(f"/cards/{cid}", status_code=303)


# ---------- 参照モード／教育モード ----------

def project_cards(pid: str, approved_only: bool = True) -> tuple[dict, list[dict], list[dict]]:
    with get_db() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        videos = conn.execute("SELECT * FROM videos WHERE project_id = ? ORDER BY created_at", (pid,)).fetchall()
        q = "SELECT c.* FROM cards c JOIN videos v ON v.id = c.video_id WHERE c.project_id = ?"
        if approved_only:
            q += " AND c.status = 'approved'"
        cards = conn.execute(q + " ORDER BY v.created_at, c.ord", (pid,)).fetchall()
    vmap = {v["id"]: dict(v) for v in videos}
    out = []
    for c in cards:
        d = card_view(c)
        d["video"] = vmap.get(c["video_id"], {})
        out.append(d)
    return dict(project), out, [dict(v) for v in videos]


@app.get("/projects/{pid}/guide", response_class=HTMLResponse)
def guide_page(request: Request, pid: str, all: int = 0):
    """参照モード：動画で見れる手順書。承認済みカードを順に。配分ビューつき。"""
    project, cards, videos = project_cards(pid, approved_only=not all)
    total = sum(max(0.0, float(c["data"].get("time_end", 0)) - float(c["data"].get("time_start", 0))) for c in cards) or 1.0
    for c in cards:
        c["share"] = round(100 * max(0.0, float(c["data"].get("time_end", 0)) - float(c["data"].get("time_start", 0))) / total)
    return templates.TemplateResponse(
        request, "guide.html",
        {"project": project, "cards": cards, "videos": videos, "show_all": bool(all),
         "back_url": f"/projects/{pid}"},
    )


@app.get("/projects/{pid}/learn", response_class=HTMLResponse)
def learn_page(request: Request, pid: str):
    """教育モード：分かれ目で動画が止まり、「次にどうする？なぜ？」と問う。"""
    project, cards, videos = project_cards(pid, approved_only=True)
    stops = [c for c in cards if c["data"].get("is_branch")] or cards
    return templates.TemplateResponse(
        request, "learn.html",
        {"project": project, "cards": cards, "stops": stops, "videos": videos,
         "back_url": f"/projects/{pid}"},
    )


@app.post("/projects/{pid}/learn/log")
async def learn_log(pid: str, request: Request):
    form = await request.form()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO learn_logs (id, project_id, card_id, learner, answer, result, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex[:8], pid, str(form.get("card_id", "")), str(form.get("learner", "")),
             str(form.get("answer", "")), str(form.get("result", "")), now()),
        )
    return JSONResponse({"ok": True})


@app.get("/projects/{pid}/learn/summary")
def learn_summary(pid: str):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT card_id, result, COUNT(*) n FROM learn_logs WHERE project_id = ? GROUP BY card_id, result", (pid,)
        ).fetchall()
    return JSONResponse([dict(r) for r in rows])
