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
from app import users

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
MEDIA_DIR = BASE_DIR / "media"
# 確認用に別のデータ置き場を使えるようにする（COACH_DATA_DIR）
DATA_DIR = Path(os.environ.get("COACH_DATA_DIR") or (BASE_DIR / "data"))
DB_PATH = DATA_DIR / "app.db"

MEDIA_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------- ログイン（ユーザーごとのID・パスワード。app/users.py） ----------
SESSION_COOKIE = "coach_session"
LOGIN_FREE_PREFIXES = ("/login", "/setup", "/static/")

# 入口のモード：expert（熟練者：残す・直す）／ learner（若手：見る・学ぶ）。cookie に持つ。
# 若手の役割の人は常に learner。管理者・熟練者は入口で選ぶ。
MODE_COOKIE = "coach_mode"
MODE_FREE_PREFIXES = ("/login", "/logout", "/setup", "/static/", "/mode", "/media/", "/account", "/admin", "/saved/")
# マニュアル閲覧モードでも許す書き込み（学習の記録、完成品の写真の登録、自分の動画）
LEARNER_POST_OK = ("/learn/log", "/builds", "/trials")

users.init(DB_PATH, DATA_DIR)


def current_user(request: Request):
    return users.user_from_token(request.cookies.get(SESSION_COOKIE, ""))


app = FastAPI(title="AI技能コーチ（仮）")


def current_mode(request: Request, user) -> str:
    if user is not None and user["role"] == "learner":
        return "learner"
    m = request.cookies.get(MODE_COOKIE, "")
    return m if m in ("expert", "learner") else ""


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    request.state.user = None
    request.state.mode = ""
    # まだ誰もいなければ、最初に管理者を作る画面へ
    if not path.startswith("/static/") and users.count() == 0 and not path.startswith("/setup"):
        return RedirectResponse("/setup", status_code=303)
    user = current_user(request)
    if not path.startswith(LOGIN_FREE_PREFIXES) and user is None:
        return RedirectResponse(f"/login?next={path}", status_code=303)
    if path.startswith("/admin") and (user is None or user["role"] != "admin"):
        return RedirectResponse("/?denied=1", status_code=303)
    mode = current_mode(request, user)
    if not path.startswith(MODE_FREE_PREFIXES) and not mode:
        return RedirectResponse(f"/mode?next={path}", status_code=303)
    # マニュアル閲覧モードは、学習の記録と完成品の登録以外の書き込みをさせない
    if mode == "learner" and request.method == "POST" and not path.startswith(MODE_FREE_PREFIXES) \
            and not any(path.endswith(ok) or ok in path for ok in LEARNER_POST_OK):
        return RedirectResponse("/mode?blocked=1", status_code=303)
    request.state.user = user
    request.state.mode = mode
    return await call_next(request)


app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
templates.env.globals["ROLE_LABELS"] = users.ROLE_LABELS
templates.env.globals["app_title"] = lambda: users.get_setting("app_title", "AI技能コーチ")
# CSS を直したらスマホの古いキャッシュを使わないよう、更新時刻を URL に付ける
def _static_ver(name: str) -> str:
    try:
        return str(int((BASE_DIR / "app" / "static" / name).stat().st_mtime))
    except OSError:
        return "0"
templates.env.globals["static_ver"] = _static_ver


def apply_settings() -> None:
    """管理者が画面で変えた設定を、各モジュールが読む環境変数に反映する。"""
    for key, env in (("whisper_model", "WHISPER_MODEL"), ("jpy_per_usd", "JPY_PER_USD")):
        v = users.get_setting(key)
        if v:
            os.environ[env] = v
    templates.env.globals["whisper_model"] = transcribe_mod.model_name()


apply_settings()
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
            -- マニュアル：作業1つ（表の名前は昔の「案件」のまま projects）
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                domain TEXT DEFAULT '3dp',          -- 分野プロファイル
                expert_name TEXT DEFAULT '',
                expert_level INTEGER DEFAULT 5,     -- 熟練者レベル 1〜7
                created_at TEXT NOT NULL
            );

            -- 動画：マニュアルに紐づく作業動画（しゃべりながら撮ったもの）
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

            -- 完成品の評価：測る箇所（マニュアルごと）
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
        for table in ("videos", "builds", "learn_logs"):
            if "user_id" not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT DEFAULT ''")
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
            ("title", "TEXT DEFAULT ''"),        # 動画の名前（一覧に出す）
            ("kind", "TEXT DEFAULT 'expert'"),   # expert＝熟練者の動画 / trial＝学ぶ人が自分でやった動画
            ("compare", "TEXT DEFAULT ''"),      # trial：熟練者との比較結果（JSON）
            ("compare_status", "TEXT DEFAULT ''"),  # '' / running / done / error
            ("compare_error", "TEXT DEFAULT ''"),
            ("compare_cost", "TEXT DEFAULT ''"),
        ]:
            if col not in cols:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {col} {ddl}")
        # マニュアルのフォルダー（管理者が画面で作る。1段だけ）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS folders ("
            " id TEXT PRIMARY KEY, code TEXT NOT NULL, name TEXT NOT NULL,"
            " ord INTEGER DEFAULT 0, created_at TEXT NOT NULL)"
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
        for col, ddl in [
            ("folder_id", "TEXT DEFAULT ''"),    # '' は未分類
            ("manual_no", "TEXT DEFAULT ''"),    # 例：3DP-001（作った時のフォルダー記号＋連番）
        ]:
            if col not in cols:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {ddl}")
        # 番号のないマニュアル（昔の案件）に、作った順で番号を振る
        for r in conn.execute(
            "SELECT id, folder_id FROM projects WHERE manual_no = '' ORDER BY created_at"
        ).fetchall():
            conn.execute("UPDATE projects SET manual_no = ? WHERE id = ?",
                         (next_manual_no(conn, r["folder_id"]), r["id"]))


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- マニュアル番号とフォルダー ----------

UNFILED_CODE = "M"   # 未分類のマニュアルの番号の頭


def folder_code(conn: sqlite3.Connection, folder_id: str) -> str:
    row = conn.execute("SELECT code FROM folders WHERE id = ?", (folder_id,)).fetchone() if folder_id else None
    return row["code"] if row else UNFILED_CODE


def next_manual_no(conn: sqlite3.Connection, folder_id: str) -> str:
    """フォルダー記号＋3桁の連番。その記号で使われている一番大きい番号の次。"""
    code = folder_code(conn, folder_id)
    top = 0
    for r in conn.execute("SELECT manual_no FROM projects WHERE manual_no LIKE ?", (code + "-%",)):
        tail = r["manual_no"][len(code) + 1:]
        if tail.isdigit():
            top = max(top, int(tail))
    return f"{code}-{top + 1:03d}"


def all_folders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM folders ORDER BY ord, created_at").fetchall()


init_db()


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
            # 手で足したカードは、下書きでも残す
            conn.execute("DELETE FROM cards WHERE video_id = ? AND status = 'draft'"
                         " AND COALESCE(json_extract(data, '$.source'), '') != 'manual'", (vid,))
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

@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, error: str = ""):
    if users.count() > 0:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {"error": error, "back_url": None})


@app.post("/setup")
def setup(login_id: str = Form(...), password: str = Form(...), name: str = Form("")):
    if users.count() > 0:
        return RedirectResponse("/login", status_code=303)
    if not login_id.strip() or len(password) < 4:
        return RedirectResponse("/setup?error=1", status_code=303)
    uid = users.create(login_id, password, name, "admin")
    resp = RedirectResponse("/mode", status_code=303)
    resp.set_cookie(SESSION_COOKIE, users.token_for(uid), max_age=30 * 24 * 3600, httponly=True, samesite="lax")
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: str = ""):
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": error, "back_url": None})


@app.post("/login")
def login(login_id: str = Form(""), password: str = Form(""), next: str = Form("/")):
    u = users.authenticate(login_id, password)
    if u is None:
        return RedirectResponse(f"/login?next={next}&error=1", status_code=303)
    if not next.startswith("/") or next.startswith("/login"):
        next = "/"
    resp = RedirectResponse(next, status_code=303)
    # 30日間はログインを聞かない（毎回入力させない）
    resp.set_cookie(SESSION_COOKIE, users.token_for(u["id"]), max_age=30 * 24 * 3600, httponly=True, samesite="lax")
    resp.delete_cookie(MODE_COOKIE)
    return resp


@app.get("/mode", response_class=HTMLResponse)
def mode_page(request: Request, next: str = "/", blocked: str = ""):
    if request.state.user is not None and request.state.user["role"] == "learner":
        return RedirectResponse(next if next.startswith("/") and not next.startswith("/mode") else "/", status_code=303)
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


# ---------- アカウント（自分の記録・保存した手順・パスワード変更） ----------

@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, msg: str = ""):
    u = request.state.user
    with get_db() as conn:
        saved = conn.execute(
            """SELECT c.id, c.data, p.id project_id, p.name project_name, s.created_at
               FROM saved_cards s JOIN cards c ON c.id = s.card_id JOIN projects p ON p.id = c.project_id
               WHERE s.user_id = ? ORDER BY s.created_at DESC""", (u["id"],)).fetchall()
        saved = [{"id": r["id"], "title": (json.loads(r["data"]) or {}).get("title", ""), "project_id": r["project_id"],
                  "project_name": r["project_name"], "created_at": r["created_at"]} for r in saved]
        logs = conn.execute(
            """SELECT l.created_at, l.result, l.answer, p.name project_name, p.id project_id, c.data
               FROM learn_logs l JOIN projects p ON p.id = l.project_id LEFT JOIN cards c ON c.id = l.card_id
               WHERE l.user_id = ? ORDER BY l.created_at DESC LIMIT 50""", (u["id"],)).fetchall()
        logs = [{"created_at": r["created_at"], "result": r["result"], "answer": r["answer"], "project_name": r["project_name"],
                 "project_id": r["project_id"], "title": (json.loads(r["data"]) if r["data"] else {}).get("title", "")} for r in logs]
        trials = conn.execute(
            """SELECT v.id, v.title, v.created_at, v.compare_status, p.name project_name
               FROM videos v JOIN projects p ON p.id = v.project_id WHERE v.user_id = ? AND v.kind = 'trial' ORDER BY v.created_at DESC""",
            (u["id"],)).fetchall()
        builds = conn.execute(
            """SELECT b.id, b.created_at, p.name project_name FROM builds b JOIN projects p ON p.id = b.project_id
               WHERE b.user_id = ? ORDER BY b.created_at DESC""", (u["id"],)).fetchall()
    return templates.TemplateResponse(request, "account.html", {
        "u": u, "saved": saved, "logs": logs, "trials": trials, "builds": builds, "msg": msg, "back_url": "/"})


@app.post("/account/password")
def account_password(request: Request, current: str = Form(""), new: str = Form("")):
    u = request.state.user
    if not users.check_password(current, u["password_hash"]):
        return RedirectResponse("/account?msg=wrong", status_code=303)
    if len(new) < 4:
        return RedirectResponse("/account?msg=short", status_code=303)
    users.set_password(u["id"], new)
    return RedirectResponse("/account?msg=ok", status_code=303)


@app.post("/account/name")
def account_name(request: Request, name: str = Form("")):
    users.update(request.state.user["id"], name=name.strip() or request.state.user["login_id"])
    return RedirectResponse("/account?msg=ok", status_code=303)


@app.post("/saved/{card_id}")
def toggle_saved(request: Request, card_id: str):
    return JSONResponse({"saved": users.toggle_saved(request.state.user["id"], card_id)})


# ---------- 管理者（ユーザーの配布・設定） ----------

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, msg: str = ""):
    with get_db() as conn:
        folders = all_folders(conn)
        counts = {r["folder_id"]: r["n"] for r in conn.execute(
            "SELECT folder_id, COUNT(*) AS n FROM projects GROUP BY folder_id")}
    return templates.TemplateResponse(request, "admin.html", {
        "all_users": users.all_users(), "ROLES": users.ROLES, "msg": msg, "back_url": "/",
        "folders": folders, "folder_counts": counts,
        "settings": {"app_title": users.get_setting("app_title", "AI技能コーチ"),
                     "whisper_model": transcribe_mod.model_name(),
                     "jpy_per_usd": os.environ.get("JPY_PER_USD", "155")},
    })


@app.post("/admin/users")
def admin_add_user(login_id: str = Form(...), password: str = Form(...), name: str = Form(""), role: str = Form("learner")):
    if not login_id.strip() or len(password) < 4:
        return RedirectResponse("/admin?msg=invalid", status_code=303)
    if users.by_login(login_id):
        return RedirectResponse("/admin?msg=dup", status_code=303)
    users.create(login_id, password, name, role)
    return RedirectResponse("/admin?msg=added", status_code=303)


@app.post("/admin/users/{uid}")
def admin_edit_user(request: Request, uid: str, action: str = Form(...), role: str = Form(""), password: str = Form(""), name: str = Form("")):
    me = request.state.user["id"]
    if action == "delete":
        if uid == me:
            return RedirectResponse("/admin?msg=self", status_code=303)
        users.delete(uid)
    elif action == "role":
        if uid == me and role != "admin":
            return RedirectResponse("/admin?msg=self", status_code=303)
        users.update(uid, role=role)
    elif action == "password":
        if len(password) < 4:
            return RedirectResponse("/admin?msg=invalid", status_code=303)
        users.set_password(uid, password)
    elif action == "name":
        users.update(uid, name=name)
    return RedirectResponse("/admin?msg=ok", status_code=303)


@app.get("/admin/videos", response_class=HTMLResponse)
def admin_videos(request: Request, q: str = "", kind: str = "", folder: str = "", msg: str = ""):
    """動画編集モード：分類 → マニュアル → 動画の順に並べ、名前・分類を直せる。各画面に入って直すこともできる。"""
    with get_db() as conn:
        manuals = conn.execute("SELECT id, name, manual_no, folder_id FROM projects ORDER BY manual_no").fetchall()
        rows = conn.execute("SELECT * FROM videos ORDER BY created_at").fetchall()
        folders = all_folders(conn)
        names = {u["id"]: u["name"] for u in users.all_users()}
    fids = {f["id"] for f in folders}
    videos = [video_card(v) for v in rows]
    for v in videos:
        v["who"] = names.get(v.get("user_id") or "", "")
    words = q.lower().split()
    def hit(v: dict, m: sqlite3.Row) -> bool:
        if kind and v["kind"] != kind:
            return False
        text = " ".join([v["label"], v["original_name"] or "", v["who"], m["name"], m["manual_no"]]).lower()
        return all(w in text for w in words)
    groups = []  # [{folder, manuals: [{manual, videos}]}]
    for fo in list(folders) + [None]:
        fid = fo["id"] if fo else ""
        if folder and folder != (fid or "none"):
            continue
        ms = []
        for m in manuals:
            mf = m["folder_id"] if m["folder_id"] in fids else ""
            if mf != fid:
                continue
            vs = [v for v in videos if v["project_id"] == m["id"] and hit(v, m)]
            if vs or (not words and not kind):
                ms.append({"manual": m, "videos": vs})
        if ms:
            groups.append({"folder": fo, "manuals": ms})
    return templates.TemplateResponse(request, "admin_videos.html", {
        "groups": groups, "folders": folders, "total": len(videos),
        "hits": sum(len(m["videos"]) for g in groups for m in g["manuals"]),
        "f": {"q": q, "kind": kind, "folder": folder}, "msg": msg, "back_url": "/admin",
    })


def _back_to_videos(q: str, kind: str, folder: str, msg: str, anchor: str) -> RedirectResponse:
    from urllib.parse import urlencode
    return RedirectResponse(f"/admin/videos?{urlencode({'q': q, 'kind': kind, 'folder': folder, 'msg': msg})}#{anchor}",
                            status_code=303)


@app.post("/admin/videos/{vid}/title")
def admin_video_title(vid: str, title: str = Form(""), q: str = Form(""), kind: str = Form(""), folder: str = Form("")):
    set_video(vid, title=title.strip())
    return _back_to_videos(q, kind, folder, "ok", f"v-{vid}")


@app.post("/admin/manuals/{pid}/folder")
def admin_manual_folder(pid: str, folder_id: str = Form(""), new_folder_name: str = Form(""), new_folder_code: str = Form(""),
                        q: str = Form(""), kind: str = Form(""), folder: str = Form("")):
    """動画編集モードから、マニュアルの分類を変える（新しい分類も作れる）。番号は変えない。"""
    with get_db() as conn:
        folder_id = pick_folder(conn, folder_id, new_folder_name, new_folder_code)
        conn.execute("UPDATE projects SET folder_id = ? WHERE id = ?", (folder_id, pid))
    return _back_to_videos(q, kind, folder, "folder", f"m-{pid}")


def clean_code(code: str) -> str:
    """フォルダー記号：英数字だけ、大文字、8文字まで。"""
    return "".join(c for c in code.upper() if c.isascii() and c.isalnum())[:8]


def make_folder(conn, name: str, code: str = "") -> str:
    """分類を作ってidを返す。記号が空なら C1, C2… を自動で付ける。重なれば後ろに数字を足す。"""
    base = clean_code(code) or "C"
    taken = {r[0] for r in conn.execute("SELECT code FROM folders").fetchall()} | {UNFILED_CODE}
    code, n = (base, 1) if code and base not in taken else (base, 0)
    while code in taken:
        n += 1
        code = f"{base}{n}"
    fid = uuid.uuid4().hex[:8]
    top = conn.execute("SELECT COALESCE(MAX(ord), 0) FROM folders").fetchone()[0]
    conn.execute("INSERT INTO folders (id, code, name, ord, created_at) VALUES (?, ?, ?, ?, ?)",
                 (fid, code[:8], name.strip(), top + 1, now()))
    return fid


def pick_folder(conn, folder_id: str, new_name: str, new_code: str) -> str:
    """分類の選択。「＋新しい分類」を選んで名前があれば、その場で作る。"""
    if folder_id == "__new":
        return make_folder(conn, new_name, new_code) if new_name.strip() else ""
    return folder_id


@app.post("/admin/folders")
def admin_add_folder(code: str = Form(""), name: str = Form("")):
    code = clean_code(code)
    with get_db() as conn:
        if not code or not name.strip():
            return RedirectResponse("/admin?msg=folder_invalid#folders", status_code=303)
        if code == UNFILED_CODE or conn.execute("SELECT 1 FROM folders WHERE code = ?", (code,)).fetchone():
            return RedirectResponse("/admin?msg=folder_dup#folders", status_code=303)
        top = conn.execute("SELECT COALESCE(MAX(ord), 0) FROM folders").fetchone()[0]
        conn.execute("INSERT INTO folders (id, code, name, ord, created_at) VALUES (?, ?, ?, ?, ?)",
                     (uuid.uuid4().hex[:8], code, name.strip(), top + 1, now()))
    return RedirectResponse("/admin?msg=ok#folders", status_code=303)


@app.post("/admin/folders/{fid}")
def admin_edit_folder(fid: str, action: str = Form(...), name: str = Form(""), code: str = Form("")):
    with get_db() as conn:
        folders = all_folders(conn)
        ids = [f["id"] for f in folders]
        if fid not in ids:
            return RedirectResponse("/admin#folders", status_code=303)
        if action == "rename":
            code = clean_code(code)
            if not code or not name.strip():
                return RedirectResponse("/admin?msg=folder_invalid#folders", status_code=303)
            if code == UNFILED_CODE or conn.execute("SELECT 1 FROM folders WHERE code = ? AND id != ?", (code, fid)).fetchone():
                return RedirectResponse("/admin?msg=folder_dup#folders", status_code=303)
            # 記号を変えても、もう振った番号は変えない（新しく作る分から新しい記号）
            conn.execute("UPDATE folders SET name = ?, code = ? WHERE id = ?", (name.strip(), code, fid))
        elif action in ("up", "down"):
            i = ids.index(fid)
            j = i - 1 if action == "up" else i + 1
            if 0 <= j < len(ids):
                ids[i], ids[j] = ids[j], ids[i]
                for n, x in enumerate(ids):
                    conn.execute("UPDATE folders SET ord = ? WHERE id = ?", (n, x))
        elif action == "delete":
            # 中のマニュアルは消さず、未分類へ移す
            conn.execute("UPDATE projects SET folder_id = '' WHERE folder_id = ?", (fid,))
            conn.execute("DELETE FROM folders WHERE id = ?", (fid,))
    return RedirectResponse("/admin?msg=ok#folders", status_code=303)


@app.post("/admin/settings")
def admin_settings(app_title: str = Form("AI技能コーチ"), whisper_model: str = Form("small"), jpy_per_usd: str = Form("155")):
    users.set_setting("app_title", app_title.strip() or "AI技能コーチ")
    if whisper_model in {m for m, _ in transcribe_mod.MODELS}:
        users.set_setting("whisper_model", whisper_model)
    try:
        users.set_setting("jpy_per_usd", str(float(jpy_per_usd)))
    except ValueError:
        pass
    apply_settings()
    return RedirectResponse("/admin?msg=ok", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", folder: str = "", expert: str = "", level: str = "", approved: str = ""):
    """マニュアル一覧。検索（題名・ゴール・番号・熟練者）と絞り込み、フォルダーごとに並べる。"""
    with get_db() as conn:
        folders = all_folders(conn)
        manuals = [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY manual_no").fetchall()]
        approved_n = {r["project_id"]: r["n"] for r in conn.execute(
            "SELECT project_id, COUNT(*) AS n FROM cards WHERE status = 'approved' GROUP BY project_id")}
    folder_ids = {f["id"] for f in folders}
    for m in manuals:
        m["approved"] = approved_n.get(m["id"], 0)
        if m["folder_id"] not in folder_ids:   # 消えたフォルダーにいたものは未分類
            m["folder_id"] = ""
    experts = sorted({m["expert_name"] for m in manuals if m["expert_name"]})

    words = q.split()
    def hit(m: dict) -> bool:
        text = " ".join([m["manual_no"], m["name"], m["description"] or "", m["expert_name"] or ""]).lower()
        if any(w.lower() not in text for w in words):
            return False
        if folder and m["folder_id"] != ("" if folder == "none" else folder):
            return False
        if expert and m["expert_name"] != expert:
            return False
        if level.isdigit() and m["expert_level"] < int(level):
            return False
        if approved == "yes" and not m["approved"]:
            return False
        if approved == "no" and m["approved"]:
            return False
        return True
    shown = [m for m in manuals if hit(m)]

    # フォルダーの並び順でまとめる。未分類は最後
    groups = [{"folder": f, "manuals": [m for m in shown if m["folder_id"] == f["id"]]} for f in folders]
    groups.append({"folder": None, "manuals": [m for m in shown if not m["folder_id"]]})
    groups = [g for g in groups if g["manuals"]]
    return templates.TemplateResponse(request, "index.html", {
        "groups": groups, "folders": folders, "experts": experts, "total": len(manuals), "hits": len(shown),
        "f": {"q": q, "folder": folder, "expert": expert, "level": level, "approved": approved},
        "filtered": any([q, folder, expert, level, approved]), "back_url": None,
    })


@app.post("/projects")
def create_project(
    name: str = Form(...),
    description: str = Form(""),
    expert_name: str = Form(""),
    expert_level: int = Form(5),
    folder_id: str = Form(""),
    new_folder_name: str = Form(""),
    new_folder_code: str = Form(""),
):
    pid = uuid.uuid4().hex[:8]
    with get_db() as conn:
        folder_id = pick_folder(conn, folder_id, new_folder_name, new_folder_code)
        conn.execute(
            "INSERT INTO projects (id, name, description, expert_name, expert_level, folder_id, manual_no, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, name, description, expert_name, expert_level, folder_id,
             next_manual_no(conn, folder_id), now()),
        )
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@app.post("/projects/{pid}/edit")
async def edit_project(request: Request, pid: str, name: str = Form(...), description: str = Form(""),
                       expert_name: str = Form(""), expert_level: int = Form(5)):
    """マニュアルの名前・作業名（動画の名前）・ゴール・熟練者を直す（マニュアル登録モード）。番号とフォルダーは別の欄で。"""
    if not name.strip():
        return RedirectResponse(f"/projects/{pid}", status_code=303)
    form = await request.form()
    with get_db() as conn:
        conn.execute(
            "UPDATE projects SET name = ?, description = ?, expert_name = ?, expert_level = ? WHERE id = ?",
            (name.strip(), description, expert_name.strip(), min(max(expert_level, 1), 7), pid),
        )
        # 作業名＝このマニュアルの中の各動画の名前
        for key, val in form.items():
            if key.startswith("vtitle_"):
                conn.execute("UPDATE videos SET title = ? WHERE id = ? AND project_id = ?",
                             (str(val).strip(), key[len("vtitle_"):], pid))
    return RedirectResponse(f"/projects/{pid}?msg=ok", status_code=303)


@app.post("/projects/{pid}/folder")
def move_project(pid: str, folder_id: str = Form(""), new_folder_name: str = Form(""), new_folder_code: str = Form("")):
    """分類を変える（新しい分類もここで作れる）。番号は変えない。"""
    with get_db() as conn:
        folder_id = pick_folder(conn, folder_id, new_folder_name, new_folder_code)
        conn.execute("UPDATE projects SET folder_id = ? WHERE id = ?", (folder_id, pid))
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@app.post("/admin/manuals/{pid}/number")
def renumber_project(pid: str, manual_no: str = Form("")):
    """マニュアル番号を管理者が直す。ほかと重なる番号は受け付けない。"""
    no = manual_no.strip().upper()
    with get_db() as conn:
        if not no or conn.execute("SELECT 1 FROM projects WHERE manual_no = ? AND id != ?", (no, pid)).fetchone():
            return RedirectResponse(f"/projects/{pid}?msg=dupno", status_code=303)
        conn.execute("UPDATE projects SET manual_no = ? WHERE id = ?", (no, pid))
    return RedirectResponse(f"/projects/{pid}?msg=ok", status_code=303)


@app.post("/projects/{pid}/delete")
def delete_project(pid: str):
    with get_db() as conn:
        conn.execute("DELETE FROM videos WHERE project_id = ?", (pid,))
        conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
    shutil.rmtree(MEDIA_DIR / pid, ignore_errors=True)
    return RedirectResponse("/", status_code=303)


@app.get("/projects/{pid}", response_class=HTMLResponse)
def project_page(request: Request, pid: str, msg: str = ""):
    with get_db() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        folders = all_folders(conn)
        rows = conn.execute(
            "SELECT * FROM videos WHERE project_id = ? ORDER BY created_at DESC", (pid,)
        ).fetchall()
        # 動画ごとの最初のカードの題名（動画に名前がないときの表示に使う）
        first_titles = {}
        for r in conn.execute(
            "SELECT video_id, data FROM cards WHERE project_id = ? AND status != 'rejected' ORDER BY ord DESC", (pid,)
        ).fetchall():
            first_titles[r["video_id"]] = json.loads(r["data"] or "{}").get("title", "")
    videos = [video_card(v, first_titles.get(v["id"], "")) for v in rows]
    return templates.TemplateResponse(
        request, "project.html",
        {"project": project, "videos": [v for v in videos if v["kind"] != "trial"],
         "trials": [v for v in videos if v["kind"] == "trial"], "back_url": "/",
         "folders": folders, "folder": next((f for f in folders if f["id"] == project["folder_id"]), None),
         "msg": msg}
    )


def video_card(v: sqlite3.Row, first_title: str = "") -> dict:
    """一覧用：サムネイル（真ん中あたりの静止画。先頭は真っ黒なことが多い）と名前を付ける。"""
    d = dict(v)
    frames = json.loads(v["frames"] or "[]")
    d["thumb"] = frames[len(frames) // 3]["url"] if frames else ""
    d["label"] = v["title"] or (v["summary"] or "")[:40] or first_title or v["original_name"] or "動画"
    return d


@app.post("/projects/{pid}/videos")
async def upload_video(request: Request, pid: str, background: BackgroundTasks, file: UploadFile = File(...)):
    return await save_upload(pid, background, file, "expert", user_id=request.state.user["id"])


@app.post("/projects/{pid}/trials")
async def upload_trial(request: Request, pid: str, background: BackgroundTasks, file: UploadFile = File(...), title: str = Form("")):
    """学ぶ人が自分でやった動画。閲覧モードでも送れる。"""
    return await save_upload(pid, background, file, "trial", title, user_id=request.state.user["id"])


async def save_upload(pid: str, background: BackgroundTasks, file: UploadFile, kind: str, title: str = "", user_id: str = ""):
    vid = uuid.uuid4().hex[:8]
    ext = Path(file.filename or "video.mp4").suffix.lower() or ".mp4"
    dest_dir = MEDIA_DIR / pid
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{vid}{ext}"
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO videos (id, project_id, filename, original_name, created_at, kind, title, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (vid, pid, f"{pid}/{vid}{ext}", file.filename or "", now(), kind, title.strip(), user_id),
        )
    # 送信したらすぐ画面を返し、文字起こしは裏で進める
    background.add_task(process_video, vid)
    return RedirectResponse(f"/trials/{vid}" if kind == "trial" else f"/videos/{vid}", status_code=303)


def parse_mmss(text: str) -> float:
    """「1:05」や「65」を秒にする。読めなければ0。"""
    try:
        if ":" in text:
            m, s = text.split(":", 1)
            return int(m) * 60 + float(s)
        return float(text)
    except ValueError:
        return 0.0


KIND_FLAGS = {  # 手で足すカードの種類 → カードの印
    "knack": {"is_branch": True, "safety": False},   # カンコツ（分かれ目）
    "safety": {"is_branch": False, "safety": True},  # 安全の説明
    "step": {"is_branch": False, "safety": False},   # ふつうの手順
}


@app.post("/videos/{vid}/cards")
def add_card(vid: str, title: str = Form(...), kind: str = Form("step"), start: str = Form("0"), end: str = Form("0")):
    """熟練者が場面を区切ってカードを足す（カンコツ・安全の説明など）。
    その時間に重なるカードは前後に分ける（真ん中に入れたら、前と「続き」の2枚になる）。"""
    t0 = max(parse_mmss(start), 0.0)
    t1 = parse_mmss(end)
    if t1 <= t0:
        return RedirectResponse(f"/videos/{vid}?msg=range#add-card", status_code=303)
    data = {"title": title.strip(), "time_start": t0, "time_end": t1, "judgment_type": "A", "source": "manual",
            **KIND_FLAGS.get(kind, KIND_FLAGS["step"])}
    cid = uuid.uuid4().hex[:8]
    with get_db() as conn:
        v = conn.execute("SELECT project_id FROM videos WHERE id = ?", (vid,)).fetchone()
        rows = conn.execute("SELECT * FROM cards WHERE video_id = ? AND status != 'rejected' ORDER BY ord", (vid,)).fetchall()
        # 丸ごと覆われるカードがあれば、消してしまわないよう止める
        for r in rows:
            d = json.loads(r["data"] or "{}")
            s0, s1 = d.get("time_start", 0), d.get("time_end", 0)
            if t0 <= s0 and s1 <= t1 and s1 > s0:
                return RedirectResponse(f"/videos/{vid}?msg=cover#add-card", status_code=303)
        for r in rows:
            d = json.loads(r["data"] or "{}")
            s0, s1 = d.get("time_start", 0), d.get("time_end", 0)
            if s1 <= t0 or s0 >= t1:
                continue  # 重ならない
            if s0 < t0 and s1 > t1:  # 真ん中に入る → 前はこのカード、後ろは「続き」のカード
                rest = dict(d, time_start=t1, time_end=s1, title=(d.get("title", "") + "（続き）"))
                conn.execute(
                    "INSERT INTO cards (id, project_id, video_id, ord, status, data, answers, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (uuid.uuid4().hex[:8], r["project_id"], vid, r["ord"], r["status"],
                     json.dumps(rest, ensure_ascii=False), r["answers"], now(), now()))
                d["time_end"] = t0
            elif s0 < t0:  # 後ろが重なる → 終わりを削る
                d["time_end"] = t0
            else:          # 前が重なる → 始めを削る
                d["time_start"] = t1
            conn.execute("UPDATE cards SET data = ?, updated_at = ? WHERE id = ?",
                         (json.dumps(d, ensure_ascii=False), now(), r["id"]))
        conn.execute(
            "INSERT INTO cards (id, project_id, video_id, ord, status, data, created_at, updated_at)"
            " VALUES (?, ?, ?, 0, 'draft', ?, ?, ?)",
            (cid, v["project_id"], vid, json.dumps(data, ensure_ascii=False), now(), now()))
        # 並びを時刻の順に振り直す
        allc = conn.execute("SELECT id, ord, data FROM cards WHERE video_id = ?", (vid,)).fetchall()
        allc = sorted(allc, key=lambda r: (json.loads(r["data"] or "{}").get("time_start", 0), r["ord"]))
        for i, r in enumerate(allc, 1):
            conn.execute("UPDATE cards SET ord = ? WHERE id = ?", (i, r["id"]))
    return RedirectResponse(f"/cards/{cid}", status_code=303)


@app.post("/videos/{vid}/title")
def video_title(vid: str, title: str = Form("")):
    set_video(vid, title=title.strip())
    return RedirectResponse(f"/videos/{vid}", status_code=303)


# ---------- 学ぶ人の動画：熟練者との違いを見る ----------

@app.get("/trials/{vid}", response_class=HTMLResponse)
def trial_page(request: Request, vid: str):
    with get_db() as conn:
        video = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
    project, cards, _ = project_cards(video["project_id"])
    segments = json.loads(video["segments"] or "[]")
    compare = json.loads(video["compare"] or "{}")
    # 比較結果の各行に、対応するカード（題名で突き合わせ）を付ける
    by_title = {c["data"].get("title", ""): c for c in cards}
    for it in compare.get("items", []):
        it["card"] = by_title.get(it.get("card_title", ""))
    return templates.TemplateResponse(
        request, "trial.html",
        {"video": video, "project": project, "cards": cards, "segments": segments, "compare": compare,
         "last_cost": json.loads(video["compare_cost"] or "{}"),
         "back_url": f"/projects/{project['id']}"},
    )


@app.get("/trials/{vid}/status")
def trial_status(vid: str):
    with get_db() as conn:
        video = conn.execute("SELECT status, compare_status FROM videos WHERE id = ?", (vid,)).fetchone()
    return JSONResponse({"status": video["status"] if video else "missing",
                         "compare": video["compare_status"] if video else ""})


@app.post("/trials/{vid}/retry")
def trial_retry(vid: str, background: BackgroundTasks):
    background.add_task(process_video, vid)
    return RedirectResponse(f"/trials/{vid}", status_code=303)


@app.post("/trials/{vid}/delete")
def trial_delete(vid: str):
    with get_db() as conn:
        v = conn.execute("SELECT * FROM videos WHERE id = ? AND kind = 'trial'", (vid,)).fetchone()
        if not v:
            return RedirectResponse("/", status_code=303)
        conn.execute("DELETE FROM videos WHERE id = ?", (vid,))
    (MEDIA_DIR / v["filename"]).unlink(missing_ok=True)
    shutil.rmtree(MEDIA_DIR / v["project_id"] / f"{vid}_frames", ignore_errors=True)
    return RedirectResponse(f"/projects/{v['project_id']}", status_code=303)


@app.get("/trials/{vid}/compare", response_class=HTMLResponse)
def trial_compare_confirm(request: Request, vid: str):
    with get_db() as conn:
        video = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
    _, cards, _ = project_cards(video["project_id"])
    est = analyze_mod.estimate_compare([c["data"] for c in cards], json.loads(video["segments"] or "[]"),
                                       json.loads(video["frames"] or "[]"))
    return templates.TemplateResponse(
        request, "confirm_compare.html",
        {"video": video, "est": est, "last": json.loads(video["compare_cost"] or "{}"), "n_cards": len(cards),
         "has_key": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
         "rate": analyze_mod.jpy_rate(), "back_url": f"/trials/{vid}"},
    )


@app.post("/trials/{vid}/compare")
def trial_compare(vid: str, background: BackgroundTasks, confirm: str = Form("")):
    if confirm != "yes":
        return RedirectResponse(f"/trials/{vid}/compare", status_code=303)
    background.add_task(process_compare, vid)
    return RedirectResponse(f"/trials/{vid}", status_code=303)


@app.get("/videos/{vid}", response_class=HTMLResponse)
def video_page(request: Request, vid: str, msg: str = "", s: str = "", e: str = ""):
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
         "msg": msg, "pre_s": s, "pre_e": e,
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


def process_compare(vid: str) -> None:
    """学ぶ人の動画を、熟練者の承認済みカードと比べる（Claude API・有料。確認画面の「はい」の後だけ）。"""
    with get_db() as conn:
        v = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (v["project_id"],)).fetchone()
    _, cards, _ = project_cards(v["project_id"])
    try:
        set_video(vid, compare_status="running", compare_error="")
        out = analyze_mod.compare_trial(dict(project), [c["data"] for c in cards],
                                        json.loads(v["segments"] or "[]"), json.loads(v["frames"] or "[]"), MEDIA_DIR)
        usage = out.pop("_usage", {})
        set_video(vid, compare=json.dumps(out, ensure_ascii=False), compare_status="done",
                  compare_cost=json.dumps(usage), summary=out.get("summary", ""))
    except Exception:
        set_video(vid, compare_status="error", compare_error=traceback.format_exc()[-2000:])


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
        secs = max(0.0, float(c["data"].get("time_end", 0)) - float(c["data"].get("time_start", 0)))
        c["secs"] = round(secs)
        c["share"] = round(100 * secs / total)
    return templates.TemplateResponse(
        request, "guide.html",
        {"project": project, "cards": cards, "videos": videos, "show_all": bool(all),
         "back_url": f"/projects/{pid}"},
    )


@app.get("/projects/{pid}/short", response_class=HTMLResponse)
def short_page(request: Request, pid: str):
    """Z世代向け：ショート動画風の参照モード（縦全画面、上スワイプで次の手順）。"""
    project, cards, videos = project_cards(pid, approved_only=True)
    return templates.TemplateResponse(request, "short.html", {"project": project, "cards": cards, "videos": videos,
                                                              "saved_ids": users.saved_ids(request.state.user["id"])})


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
            "INSERT INTO learn_logs (id, project_id, card_id, learner, answer, result, created_at, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex[:8], pid, str(form.get("card_id", "")), str(form.get("learner", "")) or request.state.user["name"],
             str(form.get("answer", "")), str(form.get("result", "")), now(), request.state.user["id"]),
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
        conn.execute("INSERT INTO builds (id, project_id, kind, maker, photos, dims, created_at, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (bid, pid, kind, str(form.get("maker", "")).strip() or request.state.user["name"], json.dumps(photos, ensure_ascii=False), json.dumps(dims), now(), request.state.user["id"]))
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
