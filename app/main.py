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

# 入口のモード：expert（熟練者：残す・直す）／ learner（若手：見る・学ぶ）。cookie に持つ
MODE_COOKIE = "coach_mode"
MODE_FREE_PREFIXES = ("/login", "/logout", "/static/", "/mode", "/media/")
# 若手モードでも許す書き込み（学習の記録、完成品の写真の登録）
LEARNER_POST_OK = ("/learn/log", "/builds")


def session_token() -> str:
    """パスワードから作る署名。これが cookie に入っていればログイン済みとみなす。"""
    return hmac.new(b"ai-skill-coach-v1", APP_PASSWORD.encode(), hashlib.sha256).hexdigest()


def is_logged_in(request: Request) -> bool:
    if not APP_PASSWORD:
        return True
    token = request.cookies.get(SESSION_COOKIE, "")
    return hmac.compare_digest(token, session_token())


app = FastAPI(title="AI技能コーチ（仮）")


def current_mode(request: Request) -> str:
    m = request.cookies.get(MODE_COOKIE, "")
    return m if m in ("expert", "learner") else ""


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if not path.startswith(LOGIN_FREE_PREFIXES) and not is_logged_in(request):
        return RedirectResponse(f"/login?next={path}", status_code=303)
    mode = current_mode(request)
    if not path.startswith(MODE_FREE_PREFIXES) and not mode:
        return RedirectResponse(f"/mode?next={path}", status_code=303)
    # 若手モードは、学習の記録と完成品の登録以外の書き込みをさせない
    if mode == "learner" and request.method == "POST" and not path.startswith(MODE_FREE_PREFIXES) \
            and not any(path.endswith(ok) or ok in path for ok in LEARNER_POST_OK):
        return RedirectResponse("/mode?blocked=1", status_code=303)
    request.state.mode = mode
    return await call_next(request)


app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
templates.env.globals["login_enabled"] = bool(APP_PASSWORD)


@app.middleware("http")
async def expose_mode(request: Request, call_next):
    # テンプレートで {{ request.state.mode }} を使えるように（ログイン前は空）
    if not hasattr(request.state, "mode"):
        request.state.mode = current_mode(request)
    return await call_next(request)
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

            -- 完成品の評価：測る箇所（案件ごと）
            CREATE TABLE IF NOT EXISTS measures (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                name TEXT NOT NULL,                 -- 例：フタの外径
                target REAL DEFAULT 0,              -- 目標値
                tol REAL DEFAULT 0,                 -- 許容範囲（±）
                unit TEXT DEFAULT 'mm',
                created_at TEXT NOT NULL
            );

            -- 完成品の記録：熟練者の基準（reference）と学ぶ人の完成品（trial）
            CREATE TABLE IF NOT EXISTS builds (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                kind TEXT DEFAULT 'trial',          -- reference / trial
                maker TEXT DEFAULT '',
                photos TEXT DEFAULT '{}',           -- {面: 保存パス}
                dims TEXT DEFAULT '{}',             -- {測る箇所ID: 実測値}
                result TEXT DEFAULT '{}',           -- AIの比較結果
                eval_status TEXT DEFAULT '',        -- '' / done / error
                created_at TEXT NOT NULL
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
            ("analysis_cost", "TEXT DEFAULT ''"),  # 前回の実費（JSON：モデル・トークン・円）
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
    # 判断カードの下書き（Claude API・有料）は自動では走らせない。画面で料金を確かめて「はい」を押したときだけ。


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
        set_video(vid, analysis_status="done", summary=out.get("summary", ""), notes=out.get("notes_for_expert", ""),
                  analysis_cost=json.dumps(out.get("_usage", {})))
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


@app.get("/mode", response_class=HTMLResponse)
def mode_page(request: Request, next: str = "/", blocked: str = ""):
    return templates.TemplateResponse(request, "mode.html", {"next": next, "blocked": blocked, "back_url": None})


@app.post("/mode")
def set_mode(mode: str = Form(...), next: str = Form("/")):
    if mode not in ("expert", "learner"):
        mode = "learner"
    if not next.startswith("/") or next.startswith("/mode"):
        next = "/"
    resp = RedirectResponse(next, status_code=303)
    resp.set_cookie(MODE_COOKIE, mode, max_age=30 * 24 * 3600, samesite="lax")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    resp.delete_cookie(MODE_COOKIE)
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
         "last_cost": json.loads(video["analysis_cost"] or "{}"),
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


@app.get("/videos/{vid}/analyze", response_class=HTMLResponse)
def video_analyze_confirm(request: Request, vid: str):
    """有料処理の前の確認画面：料金の予想を円で出し、「はい」を押したときだけ走らせる。"""
    with get_db() as conn:
        video = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
        n_approved = conn.execute("SELECT COUNT(*) FROM cards WHERE video_id = ? AND status = 'approved'", (vid,)).fetchone()[0]
    est = analyze_mod.estimate(json.loads(video["segments"] or "[]"), json.loads(video["frames"] or "[]"))
    last = json.loads(video["analysis_cost"] or "{}")
    return templates.TemplateResponse(
        request, "confirm.html",
        {"video": video, "est": est, "last": last, "n_approved": n_approved,
         "has_key": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
         "rate": analyze_mod.jpy_rate(), "back_url": f"/videos/{vid}"},
    )


@app.post("/videos/{vid}/analyze")
def video_analyze(vid: str, background: BackgroundTasks, confirm: str = Form("")):
    if confirm != "yes":
        return RedirectResponse(f"/videos/{vid}/analyze", status_code=303)
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
         "merged": request.query_params.get("merged"), "merge_error": request.query_params.get("merge_error"),
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


@app.get("/cards/{cid}/merge", response_class=HTMLResponse)
def card_merge_confirm(request: Request, cid: str):
    """答えを欄に書き戻す前の確認（有料・軽いモデル）。"""
    with get_db() as conn:
        card = card_view(conn.execute("SELECT * FROM cards WHERE id = ?", (cid,)).fetchone())
    est = analyze_mod.estimate_merge(card["data"], card["answers"])
    return templates.TemplateResponse(
        request, "confirm_merge.html",
        {"card": card, "est": est, "rate": analyze_mod.jpy_rate(),
         "has_key": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()), "back_url": f"/cards/{cid}"},
    )


@app.post("/cards/{cid}/merge")
def card_merge(cid: str, confirm: str = Form("")):
    if confirm != "yes":
        return RedirectResponse(f"/cards/{cid}/merge", status_code=303)
    with get_db() as conn:
        card = card_view(conn.execute("SELECT * FROM cards WHERE id = ?", (cid,)).fetchone())
    try:
        out = analyze_mod.merge_answers(card["data"], card["answers"])
    except Exception as e:
        return RedirectResponse(f"/cards/{cid}?merge_error=1", status_code=303)
    data = card["data"]
    for k in analyze_mod.MERGE_FIELDS:
        if k in out:
            data[k] = out[k]
    data["merge_note"] = out.get("note", "")
    data["merge_cost"] = out.get("_usage", {})
    # 書き戻した質問は「答え済み」として質問一覧から外し、答えは残す
    data["answered_questions"] = list(card["answers"].keys())
    data["questions"] = [q for q in data.get("questions", []) if q not in card["answers"]]
    with get_db() as conn:
        conn.execute("UPDATE cards SET data = ?, updated_at = ? WHERE id = ?",
                     (json.dumps(data, ensure_ascii=False), now(), cid))
    return RedirectResponse(f"/cards/{cid}?merged=1", status_code=303)


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


@app.get("/projects/{pid}/short", response_class=HTMLResponse)
def short_page(request: Request, pid: str):
    """Z世代向け：ショート動画風の参照モード（縦全画面、上スワイプで次の手順）。"""
    project, cards, videos = project_cards(pid, approved_only=True)
    return templates.TemplateResponse(request, "short.html", {"project": project, "cards": cards, "videos": videos})


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


# ---------- 完成品の評価 ----------

templates.env.globals["FACES"] = analyze_mod.FACES


def build_view(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["photos"] = json.loads(row["photos"] or "{}")
    d["dims"] = json.loads(row["dims"] or "{}")
    d["result"] = json.loads(row["result"] or "{}")
    return d


def dim_check(measures: list[sqlite3.Row], dims: dict) -> list[dict]:
    """寸法を許容範囲と照らす（ローカル計算、無料）。"""
    out = []
    for m in measures:
        v = dims.get(m["id"])
        if v in (None, ""):
            out.append({"name": m["name"], "target": m["target"], "tol": m["tol"], "unit": m["unit"], "value": None, "ok": None, "diff": None})
            continue
        v = float(v)
        diff = v - float(m["target"])
        out.append({"name": m["name"], "target": m["target"], "tol": m["tol"], "unit": m["unit"], "value": v,
                    "ok": abs(diff) <= float(m["tol"]), "diff": round(diff, 2)})
    return out


@app.get("/projects/{pid}/eval", response_class=HTMLResponse)
def eval_page(request: Request, pid: str):
    with get_db() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        measures = conn.execute("SELECT * FROM measures WHERE project_id = ? ORDER BY created_at", (pid,)).fetchall()
        builds = [build_view(b) for b in conn.execute("SELECT * FROM builds WHERE project_id = ? ORDER BY created_at DESC", (pid,)).fetchall()]
    reference = next((b for b in builds if b["kind"] == "reference"), None)
    trials = [b for b in builds if b["kind"] == "trial"]
    for t in trials:
        t["checks"] = dim_check(measures, t["dims"])
    return templates.TemplateResponse(
        request, "eval.html",
        {"project": project, "measures": measures, "reference": reference, "trials": trials, "back_url": f"/projects/{pid}"},
    )


@app.post("/projects/{pid}/measures")
def add_measure(pid: str, name: str = Form(...), target: float = Form(0), tol: float = Form(0), unit: str = Form("mm")):
    with get_db() as conn:
        conn.execute("INSERT INTO measures (id, project_id, name, target, tol, unit, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (uuid.uuid4().hex[:8], pid, name.strip(), target, tol, unit.strip() or "mm", now()))
    return RedirectResponse(f"/projects/{pid}/eval", status_code=303)


@app.post("/projects/{pid}/measures/{mid}/delete")
def delete_measure(pid: str, mid: str):
    with get_db() as conn:
        conn.execute("DELETE FROM measures WHERE id = ? AND project_id = ?", (mid, pid))
    return RedirectResponse(f"/projects/{pid}/eval", status_code=303)


@app.post("/projects/{pid}/builds")
async def add_build(pid: str, request: Request):
    """完成品の写真（最大3面）と寸法を保存。kind=reference なら熟練者の基準。"""
    form = await request.form()
    bid = uuid.uuid4().hex[:8]
    kind = "reference" if form.get("kind") == "reference" else "trial"
    photos: dict[str, str] = {}
    tmp_dir = MEDIA_DIR / pid / "builds"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for i, face in enumerate(analyze_mod.FACES):
        f = form.get(f"photo_{i}")
        if f is None or not getattr(f, "filename", ""):
            continue
        raw = tmp_dir / f"{bid}_{i}_raw"
        with open(raw, "wb") as out:
            shutil.copyfileobj(f.file, out)
        dst = tmp_dir / f"{bid}_{i}.jpg"
        try:
            frames_mod.shrink_image(raw, dst)
        finally:
            raw.unlink(missing_ok=True)
        photos[face] = f"{pid}/builds/{bid}_{i}.jpg"
    dims = {k[4:]: str(v) for k, v in form.items() if k.startswith("dim_") and str(v).strip()}
    with get_db() as conn:
        if kind == "reference":
            conn.execute("DELETE FROM builds WHERE project_id = ? AND kind = 'reference'", (pid,))
        conn.execute("INSERT INTO builds (id, project_id, kind, maker, photos, dims, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (bid, pid, kind, str(form.get("maker", "")).strip(), json.dumps(photos, ensure_ascii=False), json.dumps(dims), now()))
    if kind == "reference":
        return RedirectResponse(f"/projects/{pid}/eval", status_code=303)
    return RedirectResponse(f"/builds/{bid}", status_code=303)


@app.get("/builds/{bid}", response_class=HTMLResponse)
def build_page(request: Request, bid: str):
    with get_db() as conn:
        b = build_view(conn.execute("SELECT * FROM builds WHERE id = ?", (bid,)).fetchone())
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (b["project_id"],)).fetchone()
        measures = conn.execute("SELECT * FROM measures WHERE project_id = ? ORDER BY created_at", (b["project_id"],)).fetchall()
        ref = conn.execute("SELECT * FROM builds WHERE project_id = ? AND kind = 'reference'", (b["project_id"],)).fetchone()
    ref = build_view(ref) if ref else None
    pairs = [f for f in analyze_mod.FACES if ref and f in ref["photos"] and f in b["photos"]]
    est = analyze_mod.estimate_eval(len(pairs))
    return templates.TemplateResponse(
        request, "build.html",
        {"build": b, "project": project, "ref": ref, "pairs": pairs, "checks": dim_check(measures, b["dims"]),
         "est": est, "rate": analyze_mod.jpy_rate(),
         "has_key": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
         "back_url": f"/projects/{b['project_id']}/eval"},
    )


@app.post("/builds/{bid}/evaluate")
def build_evaluate(bid: str, confirm: str = Form("")):
    if confirm != "yes":
        return RedirectResponse(f"/builds/{bid}", status_code=303)
    with get_db() as conn:
        b = build_view(conn.execute("SELECT * FROM builds WHERE id = ?", (bid,)).fetchone())
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (b["project_id"],)).fetchone()
        ref = conn.execute("SELECT * FROM builds WHERE project_id = ? AND kind = 'reference'", (b["project_id"],)).fetchone()
    if not ref:
        return RedirectResponse(f"/builds/{bid}", status_code=303)
    try:
        out = analyze_mod.evaluate_build(dict(project), build_view(ref)["photos"], b["photos"], MEDIA_DIR)
        status = "done"
    except Exception:
        out = {"error": traceback.format_exc()[-1500:]}
        status = "error"
    with get_db() as conn:
        conn.execute("UPDATE builds SET result = ?, eval_status = ? WHERE id = ?", (json.dumps(out, ensure_ascii=False), status, bid))
    return RedirectResponse(f"/builds/{bid}", status_code=303)
