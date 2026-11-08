"""WAKAREME AI のサーバー本体。

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
LEARNER_POST_OK = ("/learn/log", "/builds", "/trials", "/pin", "/register-request")

users.init(DB_PATH, DATA_DIR)


def current_user(request: Request):
    return users.user_from_token(request.cookies.get(SESSION_COOKIE, ""))


app = FastAPI(title="WAKAREME AI")


def current_mode(request: Request, user) -> str:
    if user is not None and not users.can_register(user):
        return "learner"  # 登録の権限がない人は閲覧モードだけ
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
templates.env.globals["can_register"] = users.can_register
templates.env.globals["app_title"] = lambda: users.get_setting("app_title", "WAKAREME AI")
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
# 熟練レベル（マニュアルの作業者・アカウント共通の呼び方）
LEVELS = [(0, "未格付"), (1, "初級"), (2, "中級"), (3, "上級")]
templates.env.globals["LEVELS"] = LEVELS
templates.env.filters["lvname"] = lambda n: dict(LEVELS).get(int(n or 0), "未格付")
# 作業の順番を ①② で出す（21以上は (21)）
templates.env.filters["circled"] = lambda n: "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"[n - 1] if 1 <= int(n or 0) <= 20 else f"({n})"


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
            ("goal", "TEXT DEFAULT ''"),         # この作業のゴール（できあがりの条件・材料・用途）
            ("work_no", "TEXT DEFAULT ''"),      # （使わない。作業は ord の順番で ①② と呼ぶ）
            ("ord", "INTEGER DEFAULT 0"),        # マニュアルの中の作業の順番（1, 2, …）
        ]:
            if col not in cols:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {col} {ddl}")
                if col == "goal":  # はじめて足したときだけ：マニュアルのゴールを各作業に写す
                    conn.execute("UPDATE videos SET goal = (SELECT description FROM projects p WHERE p.id = videos.project_id)"
                                 " WHERE kind != 'trial'")
        # マニュアルのフォルダー（管理者が画面で作る。1段だけ）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS folders ("
            " id TEXT PRIMARY KEY, code TEXT NOT NULL, name TEXT NOT NULL,"
            " ord INTEGER DEFAULT 0, created_at TEXT NOT NULL)"
        )
        # ピン留め（アカウントごと）
        conn.execute("CREATE TABLE IF NOT EXISTS pins (user_id TEXT NOT NULL, project_id TEXT NOT NULL,"
                     " created_at TEXT NOT NULL, PRIMARY KEY (user_id, project_id))")
        # 分類の付箋の色
        if "color" not in {r["name"] for r in conn.execute("PRAGMA table_info(folders)")}:
            conn.execute("ALTER TABLE folders ADD COLUMN color TEXT DEFAULT '#2f6fb5'")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
        for col, ddl in [
            ("folder_id", "TEXT DEFAULT ''"),    # '' は未分類
            ("manual_no", "TEXT DEFAULT ''"),    # マニュアル番号：通し番号（例：001）
        ]:
            if col not in cols:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {ddl}")
        # トライ（自分でやってみた動画・完成品の写真）は本人だけが見る。shared=1 で「共有事例」としてみんなに見せる
        for table in ("videos", "builds"):
            if "shared" not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN shared INTEGER DEFAULT 0")
        admin = conn.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY created_at LIMIT 1").fetchone()
        if admin:
            conn.execute("UPDATE videos SET user_id = ? WHERE kind = 'trial' AND user_id = ''", (admin["id"],))
            conn.execute("UPDATE builds SET user_id = ? WHERE kind = 'trial' AND user_id = ''", (admin["id"],))
        # 文字起こしの「もう一度やる」の回数（失敗したとき2回まで）
        if "retry_count" not in {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}:
            conn.execute("ALTER TABLE videos ADD COLUMN retry_count INTEGER DEFAULT 0")
        # 管理者が認めた追加のやり直し回数と、その申請
        if "auto_draft" not in {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}:
            conn.execute("ALTER TABLE videos ADD COLUMN auto_draft TEXT DEFAULT ''")
        if "retry_grant" not in {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}:
            conn.execute("ALTER TABLE videos ADD COLUMN retry_grant INTEGER DEFAULT 0")
        conn.execute("CREATE TABLE IF NOT EXISTS retry_requests (id TEXT PRIMARY KEY, video_id TEXT NOT NULL, user_id TEXT DEFAULT '',"
                     " status TEXT DEFAULT 'pending', created_at TEXT NOT NULL, decided_at TEXT DEFAULT '')")
        # マニュアル新規作成：既存マニュアル（PDF・写真）と、AIへの細かい指示
        pcols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
        if "ref_files" not in pcols:
            conn.execute("ALTER TABLE projects ADD COLUMN ref_files TEXT DEFAULT '[]'")
        if "ai_prompt" not in pcols:
            conn.execute("ALTER TABLE projects ADD COLUMN ai_prompt TEXT DEFAULT ''")
        # AIを使った記録（だれが・何に・いくら）。管理者の🔔から見る。seen=1 は管理者が見た
        conn.execute("CREATE TABLE IF NOT EXISTS api_usage (id TEXT PRIMARY KEY, user_id TEXT DEFAULT '', kind TEXT NOT NULL,"
                     " ref_id TEXT DEFAULT '', project_id TEXT DEFAULT '', model TEXT DEFAULT '', input_tokens INTEGER DEFAULT 0,"
                     " output_tokens INTEGER DEFAULT 0, jpy REAL DEFAULT 0, ok INTEGER DEFAULT 1, seen INTEGER DEFAULT 0,"
                     " created_at TEXT NOT NULL)")
        # 番号のないマニュアル（昔の案件）に、作った順で番号を振る
        for r in conn.execute(
            "SELECT id, folder_id FROM projects WHERE manual_no = '' ORDER BY created_at"
        ).fetchall():
            conn.execute("UPDATE projects SET manual_no = ? WHERE id = ?",
                         (next_manual_no(conn, r["folder_id"]), r["id"]))


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- マニュアル番号と分類 ----------
# 分類は folders 表を使う（マニュアルに付ける印。階層ではない）。番号は分類と関係ない通し番号。

UNFILED_CODE = "M"   # 分類の記号の予約（使わない）


def next_manual_no(conn: sqlite3.Connection, folder_id: str = "") -> str:
    """マニュアル番号：通し番号（001, 002…）。いま使われている一番大きい数の次。"""
    top = 0
    for r in conn.execute("SELECT manual_no FROM projects"):
        if (r["manual_no"] or "").isdigit():
            top = max(top, int(r["manual_no"]))
    return f"{top + 1:03d}"


def renumber_plain(conn: sqlite3.Connection) -> None:
    """古い形（M-001 など）の番号が残っていれば、作った順に通し番号へ振り直す（一度だけ）。"""
    rows = conn.execute("SELECT id, manual_no FROM projects ORDER BY created_at").fetchall()
    if any(not (r["manual_no"] or "").isdigit() for r in rows):
        for i, r in enumerate(rows, 1):
            conn.execute("UPDATE projects SET manual_no = ? WHERE id = ?", (f"{i:03d}", r["id"]))


def fill_video_ord(conn: sqlite3.Connection) -> None:
    """順番のない作業（熟練者の動画）に、作った順で ①②… の順番を付ける。"""
    for r in conn.execute("SELECT id, project_id FROM videos WHERE kind != 'trial' AND COALESCE(ord, 0) = 0"
                          " ORDER BY created_at").fetchall():
        top = conn.execute("SELECT COALESCE(MAX(ord), 0) FROM videos WHERE project_id = ? AND kind != 'trial'",
                           (r["project_id"],)).fetchone()[0]
        conn.execute("UPDATE videos SET ord = ? WHERE id = ?", (top + 1, r["id"]))


def all_folders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM folders ORDER BY ord, created_at").fetchall()


init_db()
with get_db() as _conn:
    renumber_plain(_conn)
    fill_video_ord(_conn)
    # 熟練レベルを 1〜7 から 4段階（0 未格付・1 初級・2 中級・3 上級）へ一度だけ置き換える
    if users.get_setting("level4") != "1":
        _conn.execute("UPDATE projects SET expert_level = CASE WHEN expert_level <= 0 THEN 0 WHEN expert_level <= 2 THEN 1"
                      " WHEN expert_level <= 4 THEN 2 ELSE 3 END")
        _conn.execute("INSERT INTO settings (key, value) VALUES ('level4', '1')"
                      " ON CONFLICT(key) DO UPDATE SET value = excluded.value")


RETRY_MAX = 2  # 文字起こしが失敗したときの「もう一度やる」の上限
templates.env.globals["RETRY_MAX"] = RETRY_MAX


def retry_state(v, user) -> dict:
    """文字起こしをやり直せるか。失敗時は2回まで。それ以外は、管理者か、管理者が認めた分（retry_grant）だけ。"""
    with get_db() as conn:
        pending = conn.execute("SELECT 1 FROM retry_requests WHERE video_id = ? AND status = 'pending'", (v["id"],)).fetchone()
    left = max(0, RETRY_MAX - (v["retry_count"] or 0)) if v["status"] == "error" else 0
    grant = v["retry_grant"] or 0
    admin = user["role"] == "admin"
    can = admin or left > 0 or grant > 0
    return {"left": left, "grant": grant, "admin": admin, "can": can, "pending": bool(pending),
            "can_request": not can and not pending and v["status"] in ("error", "transcribed")}


templates.env.globals["retry_state"] = retry_state


def use_retry(v, user) -> bool:
    """やり直しを1回使う。使えなければ False。"""
    st = retry_state(v, user)
    if st["admin"]:
        return True
    if st["left"] > 0:
        set_video(v["id"], retry_count=(v["retry_count"] or 0) + 1)
        return True
    if st["grant"] > 0:
        set_video(v["id"], retry_grant=st["grant"] - 1)
        return True
    return False

USAGE_KINDS = {"draft": "判断カードの下書き", "merge": "答えの書き戻し", "compare": "トライ動画の比較",
               "evaluate": "完成品の比較", "local": "ローカルで作成"}


def log_usage(user_id: str, kind: str, ref_id: str = "", project_id: str = "", usage: dict | None = None, ok: bool = True) -> None:
    """AIを使ったら1行残す（失敗しても残す。料金は分かる分だけ）。ローカル化したら kind='local'・料金0で同じ一覧に出す。"""
    u = usage or {}
    with get_db() as conn:
        conn.execute("INSERT INTO api_usage (id, user_id, kind, ref_id, project_id, model, input_tokens, output_tokens, jpy, ok, created_at)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (uuid.uuid4().hex[:10], user_id or "", kind, ref_id, project_id, u.get("model", ""),
                      int(u.get("input_tokens", 0) or 0), int(u.get("output_tokens", 0) or 0), float(u.get("jpy", 0) or 0),
                      1 if ok else 0, now()))


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
    # 判断カードの下書き（Claude API・有料）は、新規作成ページで「文字起こしが終わったら下書きする」に
    # チェックしたときだけ自動で走らせる（目安の料金はそのページに出している）。それ以外は新規作成ページから料金を確かめて。
    if v["kind"] != "trial" and v["auto_draft"]:
        set_video(vid, auto_draft="")
        process_analysis(vid, v["auto_draft"])


def process_analysis(vid: str, user_id: str = "") -> None:
    """判断カードの下書き（Claude API）。既存の下書きは置き換える（承認済みは残す）。"""
    with get_db() as conn:
        v = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (v["project_id"],)).fetchone()
    try:
        set_video(vid, analysis_status="running", analysis_error="")
        # ゴールは作業（動画）ごと。名前も作業名を添える
        pd = dict(project, description=v["goal"] or "", name=f"{project['name']} ／ {v['title'] or ''}".strip(" ／"))
        out = analyze_mod.draft_cards(pd, json.loads(v["segments"] or "[]"),
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
        log_usage(user_id, "draft", vid, v["project_id"], out.get("_usage", {}))
    except Exception:
        set_video(vid, analysis_status="error", analysis_error=traceback.format_exc()[-2000:])
        log_usage(user_id, "draft", vid, v["project_id"], ok=False)


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
    if request.state.user is not None and not users.can_register(request.state.user):
        return RedirectResponse(next if next.startswith("/") and not next.startswith("/mode") else "/", status_code=303)
    return templates.TemplateResponse(request, "mode.html", {"next": next, "blocked": blocked, "back_url": None})


@app.post("/mode")
def set_mode(mode: str = Form(...), next: str = Form("/")):
    if mode not in ("expert", "learner"):
        mode = "learner"
    if not next.startswith("/") or next.startswith("/mode") or mode == "expert":
        next = "/"  # マニュアル登録モードにしたら、必ず最初のホーム画面へ
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
    if u["role"] != "admin":
        return RedirectResponse("/account", status_code=303)  # ID・パスワードの変更は管理者だけ
    if not users.check_password(current, u["password_hash"]):
        return RedirectResponse("/account?msg=wrong", status_code=303)
    if len(new) < 4:
        return RedirectResponse("/account?msg=short", status_code=303)
    users.set_password(u["id"], new)
    return RedirectResponse("/account?msg=ok", status_code=303)


@app.post("/account/login_id")
def account_login_id(request: Request, current: str = Form(""), login_id: str = Form("")):
    """自分のログインIDを変える（管理者だけ。今のパスワードで本人確認）。"""
    u = request.state.user
    if u["role"] != "admin":
        return RedirectResponse("/account", status_code=303)  # ID・パスワードの変更は管理者だけ
    new_id = login_id.strip()
    if not users.check_password(current, u["password_hash"]):
        return RedirectResponse("/account?msg=wrong", status_code=303)
    if not new_id:
        return RedirectResponse("/account?msg=empty", status_code=303)
    other = users.by_login(new_id)
    if other and other["id"] != u["id"]:
        return RedirectResponse("/account?msg=dup", status_code=303)
    users.update(u["id"], login_id=new_id)
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
        "settings": {"app_title": users.get_setting("app_title", "WAKAREME AI"),
                     "whisper_model": transcribe_mod.model_name(),
                     "jpy_per_usd": os.environ.get("JPY_PER_USD", "155")},
    })


@app.post("/admin/users")
def admin_add_user(login_id: str = Form(...), password: str = Form(...), name: str = Form(""), role: str = Form("unrated")):
    if not login_id.strip() or len(password) < 4:
        return RedirectResponse("/admin?msg=invalid", status_code=303)
    if users.by_login(login_id):
        return RedirectResponse("/admin?msg=dup", status_code=303)
    users.create(login_id, password, name, role)
    return RedirectResponse("/admin?msg=added", status_code=303)


@app.post("/admin/users/{uid}")
def admin_edit_user(request: Request, uid: str, action: str = Form(...), role: str = Form(""), password: str = Form(""), name: str = Form(""),
                    login_id: str = Form("")):
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
    elif action == "login_id":
        new_id = login_id.strip()
        if not new_id:
            return RedirectResponse("/admin?msg=invalid", status_code=303)
        other = users.by_login(new_id)
        if other and other["id"] != uid:
            return RedirectResponse("/admin?msg=dup", status_code=303)
        users.update(uid, login_id=new_id)
    return RedirectResponse("/admin?msg=ok", status_code=303)


@app.get("/admin/videos", response_class=HTMLResponse)
def admin_videos(request: Request, q: str = "", kind: str = "", folder: str = "", msg: str = ""):
    """動画編集モード：分類 → マニュアル → 動画の順に並べ、名前・分類を直せる。各画面に入って直すこともできる。"""
    with get_db() as conn:
        manuals = conn.execute("SELECT id, name, manual_no, folder_id FROM projects ORDER BY manual_no").fetchall()
        rows = conn.execute("SELECT * FROM videos ORDER BY ord, created_at").fetchall()
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
    # 番号順にマニュアルを並べ、分類は付箋で見せる（見出しでは分けない）
    fmap = {f["id"]: dict(f) for f in folders}
    groups = [{"folder": None, "manuals": []}]
    for m in manuals:
        mf = m["folder_id"] if m["folder_id"] in fids else ""
        if folder and folder != (mf or "none"):
            continue
        vs = [v for v in videos if v["project_id"] == m["id"] and hit(v, m)]
        if vs or (not words and not kind):
            groups[0]["manuals"].append({"manual": m, "folder": fmap.get(mf), "videos": vs})
    return templates.TemplateResponse(request, "admin_videos.html", {
        "groups": groups, "folders": folders, "total": len(videos),
        "hits": sum(len(m["videos"]) for g in groups for m in g["manuals"]),
        "f": {"q": q, "kind": kind, "folder": folder}, "msg": msg, "back_url": "/admin",
    })


def _back_to_videos(q: str, kind: str, folder: str, msg: str, anchor: str) -> RedirectResponse:
    from urllib.parse import urlencode
    return RedirectResponse(f"/admin/videos?{urlencode({'q': q, 'kind': kind, 'folder': folder, 'msg': msg})}#{anchor}",
                            status_code=303)


@app.get("/admin/open")
def admin_open(to: str = "/"):
    """管理者が一覧から各画面へ入る。マニュアル登録モードにして、直せる状態で開く。"""
    if not to.startswith("/") or to.startswith("//"):
        to = "/"
    resp = RedirectResponse(to, status_code=303)
    resp.set_cookie(MODE_COOKIE, "expert", max_age=30 * 24 * 3600, samesite="lax")
    return resp


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


def clean_color(c: str) -> str:
    """付箋の色：#RRGGBB だけ受け付ける。"""
    c = (c or "").strip()
    return c if len(c) == 7 and c[0] == "#" and all(x in "0123456789abcdefABCDEF" for x in c[1:]) else "#2f6fb5"


def tag_style(color: str) -> str:
    """付箋の色と、読みやすい文字色（明るい色なら黒、暗い色なら白）。"""
    c = clean_color(color)
    r, g, b = (int(c[i:i + 2], 16) for i in (1, 3, 5))
    return f"background:{c};color:{'#222' if (r * 299 + g * 587 + b * 114) / 1000 > 150 else '#fff'}"


templates.env.globals["tag_style"] = tag_style


def _short(text: str, n: int = 22) -> str:
    """図のコマ用に、最初の句点まで・長ければ切る。"""
    t = str(text or "").strip().split("。")[0].strip()
    return t if len(t) <= n else t[:n] + "…"


def flow_paths(d: dict) -> dict:
    """成功と失敗の導線（図のコマ）。AIが出したものを使い、無い古いカードは今ある欄から作る。"""
    ok = [str(x).strip() for x in (d.get("success_path") or []) if str(x).strip()]
    ng = [str(x).strip() for x in (d.get("failure_path") or []) if str(x).strip()]
    made = bool(ok or ng)
    if not ok:
        ok = [_short(d.get(k)) for k in ("watch", "decision") if d.get(k)]
        if d.get("stop_condition") or d.get("boundary"):
            ok.append(_short(d.get("stop_condition") or d.get("boundary")))
    if not ng:
        ng = [_short(d.get(k)) for k in ("novice_result", "ripple") if d.get(k)]
    # 安全のカードは、失敗の先にケガ・事故があることを必ず描く
    if d.get("safety") and ng and not any(w in ng[-1] for w in ("ケガ", "けが", "怪我", "事故", "火傷", "やけど")):
        ng.append("ケガ・事故のおそれ")
    return {"ok": ok, "ng": ng, "checked": bool(d.get("path_checked")),
            "ai": made or d.get("source_novice") == "ai"}


templates.env.globals["flow_paths"] = flow_paths


def clean_code(code: str) -> str:
    """フォルダー記号：英数字だけ、大文字、8文字まで。"""
    return "".join(c for c in code.upper() if c.isascii() and c.isalnum())[:8]


def make_folder(conn, name: str, code: str = "", color: str = "") -> str:
    """分類を作ってidを返す。記号が空なら C1, C2… を自動で付ける。重なれば後ろに数字を足す。"""
    base = clean_code(code) or "C"
    taken = {r[0] for r in conn.execute("SELECT code FROM folders").fetchall()} | {UNFILED_CODE}
    code, n = (base, 1) if code and base not in taken else (base, 0)
    while code in taken:
        n += 1
        code = f"{base}{n}"
    fid = uuid.uuid4().hex[:8]
    top = conn.execute("SELECT COALESCE(MAX(ord), 0) FROM folders").fetchone()[0]
    conn.execute("INSERT INTO folders (id, code, name, ord, created_at, color) VALUES (?, ?, ?, ?, ?, ?)",
                 (fid, code[:8], name.strip(), top + 1, now(), clean_color(color)))
    return fid


def pick_folder(conn, folder_id: str, new_name: str, new_code: str) -> str:
    """分類の選択。「＋新しい分類」を選んで名前があれば、その場で作る。"""
    if folder_id == "__new":  # （今は画面から選べない。分類は管理者メニューで作る）
        if not new_name.strip():
            return ""
        same = conn.execute("SELECT id FROM folders WHERE name = ?", (new_name.strip(),)).fetchone()
        return same["id"] if same else make_folder(conn, new_name, new_code)
    return folder_id


@app.post("/admin/folders")
def admin_add_folder(name: str = Form(""), color: str = Form("")):
    """分類を足す（名前だけ）。"""
    with get_db() as conn:
        if not name.strip():
            return RedirectResponse("/admin?msg=folder_invalid#folders", status_code=303)
        if conn.execute("SELECT 1 FROM folders WHERE name = ?", (name.strip(),)).fetchone():
            return RedirectResponse("/admin?msg=folder_dup#folders", status_code=303)
        make_folder(conn, name, color=color)
    return RedirectResponse("/admin?msg=ok#folders", status_code=303)


@app.post("/admin/folders/{fid}")
def admin_edit_folder(fid: str, action: str = Form(...), name: str = Form(""), code: str = Form(""), color: str = Form("")):
    with get_db() as conn:
        folders = all_folders(conn)
        ids = [f["id"] for f in folders]
        if fid not in ids:
            return RedirectResponse("/admin#folders", status_code=303)
        if action == "rename":
            if not name.strip():
                return RedirectResponse("/admin?msg=folder_invalid#folders", status_code=303)
            if conn.execute("SELECT 1 FROM folders WHERE name = ? AND id != ?", (name.strip(), fid)).fetchone():
                return RedirectResponse("/admin?msg=folder_dup#folders", status_code=303)
            conn.execute("UPDATE folders SET name = ?, color = ? WHERE id = ?", (name.strip(), clean_color(color), fid))
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
def admin_settings(app_title: str = Form("WAKAREME AI"), whisper_model: str = Form("small"), jpy_per_usd: str = Form("155")):
    users.set_setting("app_title", app_title.strip() or "WAKAREME AI")
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
        works = {}
        for r in conn.execute("SELECT project_id, title, goal FROM videos WHERE kind != 'trial'"):
            works.setdefault(r["project_id"], []).append(" ".join([r["title"] or "", r["goal"] or ""]))
        approved_n = {r["project_id"]: r["n"] for r in conn.execute(
            "SELECT project_id, COUNT(*) AS n FROM cards WHERE status = 'approved' GROUP BY project_id")}
    folder_ids = {f["id"] for f in folders}
    for m in manuals:
        m["approved"] = approved_n.get(m["id"], 0)
        m["works"] = len(works.get(m["id"], []))
        m["work_text"] = " ".join(works.get(m["id"], []))
        if m["folder_id"] not in folder_ids:   # 消えたフォルダーにいたものは未分類
            m["folder_id"] = ""
    experts = sorted({m["expert_name"] for m in manuals if m["expert_name"]})

    words = q.split()
    def hit(m: dict) -> bool:
        text = " ".join([m["manual_no"] or "", m["name"], m["work_text"], m["expert_name"] or ""]).lower()
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

    # 上に自分のピン留め、下にその他（あいうえお → abc → 123 の順）。分類は各マニュアルの付箋で見せる
    fmap = {f["id"]: dict(f) for f in folders}
    for m in shown:
        m["folder"] = fmap.get(m["folder_id"])
    uid = request.state.user["id"] if request.state.user else ""
    with get_db() as conn:
        pinned_ids = [r[0] for r in conn.execute("SELECT project_id FROM pins WHERE user_id = ? ORDER BY created_at", (uid,))]
    for m in shown:
        m["pinned"] = m["id"] in pinned_ids
    pinned = sorted([m for m in shown if m["pinned"]], key=lambda m: pinned_ids.index(m["id"]))
    others = sorted([m for m in shown if not m["pinned"]], key=lambda m: name_key(m["name"]))
    return templates.TemplateResponse(request, "index.html", {
        "pinned": pinned, "others": others, "folders": folders, "experts": experts, "total": len(manuals), "hits": len(shown),
        "f": {"q": q, "folder": folder, "expert": expert, "level": level, "approved": approved},
        "filtered": any([q, folder, expert, level, approved]), "back_url": None,
    })


def name_key(name: str) -> tuple:
    """並び順：あいうえお（かな・漢字）→ abc → 123 → その他。カタカナはひらがなとして並べる。"""
    import unicodedata
    t = unicodedata.normalize("NFKC", name or "").strip().lower()
    t = "".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in t)
    ch = t[:1]
    if ("ぁ" <= ch <= "ゖ") or ("一" <= ch <= "鿿") or ch == "ー":
        group = 0
    elif "a" <= ch <= "z":
        group = 1
    elif ch.isdigit():
        group = 2
    else:
        group = 3
    return (group, t)


@app.post("/projects/{pid}/pin")
def toggle_pin(request: Request, pid: str):
    """ピン留めを付ける・外す（自分のアカウントだけ）。"""
    uid = request.state.user["id"]
    with get_db() as conn:
        if conn.execute("DELETE FROM pins WHERE user_id = ? AND project_id = ?", (uid, pid)).rowcount == 0:
            conn.execute("INSERT INTO pins (user_id, project_id, created_at) VALUES (?, ?, ?)", (uid, pid, now()))
    return RedirectResponse("/", status_code=303)


REF_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".webp"}


@app.get("/projects/new", response_class=HTMLResponse)
def project_new_page(request: Request, pid: str = "", vid: str = ""):
    """作成とAIの下書きはすべてここから（登録モードだけ）。
    ・何も付けない：マニュアルを新しく作る ・pid：そのマニュアルに作業（動画）を足す ・vid：その作業のカードをAIで下書き（作り直し）"""
    if request.state.mode != "expert":
        return RedirectResponse("/", status_code=303)
    with get_db() as conn:
        folders = all_folders(conn)
        video = conn.execute("SELECT * FROM videos WHERE id = ? AND kind != 'trial'", (vid,)).fetchone() if vid else None
        if video:
            pid = video["project_id"]
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone() if pid else None
        n_cards = conn.execute("SELECT COUNT(*) FROM cards WHERE video_id = ? AND status = 'draft'", (vid,)).fetchone()[0] if video else 0
    mode = "draft" if video else ("add" if project else "new")
    refs = json.loads(project["ref_files"] or "[]") if project else []
    return templates.TemplateResponse(request, "project_new.html", {
        "folders": folders, "mode": mode, "project": project, "video": video, "refs": refs, "n_drafts": n_cards,
        "folder": next((f for f in folders if project and f["id"] == project["folder_id"]), None),
        "back_url": f"/videos/{video['id']}" if video else (f"/projects/{project['id']}" if project else "/")})


def save_refs(form, pid: str) -> list[dict]:
    """既存マニュアル（PDF・写真）を保存して、足した分を返す。"""
    refs = []
    ref_dir = MEDIA_DIR / pid / "manual"
    for f in form.getlist("ref_files"):
        if not getattr(f, "filename", "") or Path(f.filename).suffix.lower() not in REF_EXTS:
            continue
        ref_dir.mkdir(parents=True, exist_ok=True)
        rid = uuid.uuid4().hex[:8] + Path(f.filename).suffix.lower()
        with (ref_dir / rid).open("wb") as out:
            shutil.copyfileobj(f.file, out)
        refs.append({"file": f"{pid}/manual/{rid}", "name": f.filename})
    return refs


@app.post("/projects/new")
async def project_new(request: Request, background: BackgroundTasks):
    if request.state.mode != "expert":
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    mode = str(form.get("mode", "new"))
    detail = form.get("detail") == "on"
    prompt = str(form.get("ai_prompt", "")).strip()[:2000] if detail else ""
    auto = form.get("auto_draft") == "on"
    uid = request.state.user["id"]
    video = form.get("file")
    has_video = video is not None and getattr(video, "filename", "")

    def consume():
        # 中級未満の人が、管理者に1回だけ認めてもらって作ったときは、その1回を使う（作れたときだけ）
        if request.state.user["role"] not in users.CAN_REGISTER:
            with get_db() as conn:
                conn.execute("UPDATE users SET create_grant = MAX(0, COALESCE(create_grant, 0) - 1) WHERE id = ?", (uid,))

    if mode == "add" and not has_video:
        return RedirectResponse(f"/projects/new?pid={form.get('pid', '')}&msg=video", status_code=303)
    if mode in ("add", "draft"):
        # 今あるマニュアルに足す／その作業を下書きする：既存マニュアルと指示を引き継いで更新
        with get_db() as conn:
            vrow = conn.execute("SELECT * FROM videos WHERE id = ? AND kind != 'trial'", (str(form.get("vid", "")),)).fetchone() if mode == "draft" else None
            pid = vrow["project_id"] if vrow else str(form.get("pid", ""))
            project = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
            if not project or (mode == "draft" and not vrow):
                return RedirectResponse("/", status_code=303)
            refs = json.loads(project["ref_files"] or "[]") + save_refs(form, pid)
            conn.execute("UPDATE projects SET ref_files = ?, ai_prompt = ? WHERE id = ?", (json.dumps(refs, ensure_ascii=False), prompt, pid))
            if vrow:
                conn.execute("UPDATE videos SET title = ?, goal = ? WHERE id = ?",
                             (str(form.get("title", vrow["title"] or "")).strip(), str(form.get("goal", vrow["goal"] or "")).strip(), vrow["id"]))
        if mode == "draft":
            return RedirectResponse(f"/videos/{vrow['id']}/analyze", status_code=303)  # 料金を確かめて「はい」で下書き
        resp = await save_upload(pid, background, video, "expert", str(form.get("title", "")), user_id=uid, goal=str(form.get("goal", "")))
        if auto:
            set_video(resp.headers["location"].rsplit("/", 1)[-1], auto_draft=uid)
        consume()
        return resp

    # マニュアルを新しく作る
    name = str(form.get("name", "")).strip()
    if not name:
        return RedirectResponse("/projects/new", status_code=303)
    try:
        level = max(0, min(3, int(form.get("expert_level", 0))))
    except ValueError:
        level = 0
    pid = uuid.uuid4().hex[:8]
    refs = save_refs(form, pid)
    with get_db() as conn:
        folder_id = pick_folder(conn, str(form.get("folder_id", "")), "", "")
        conn.execute(
            "INSERT INTO projects (id, name, description, expert_name, expert_level, folder_id, manual_no, created_at, ref_files, ai_prompt)"
            " VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?)",
            (pid, name, str(form.get("expert_name", "")).strip(), level, folder_id, next_manual_no(conn, folder_id), now(),
             json.dumps(refs, ensure_ascii=False), prompt),
        )
    consume()
    if has_video:
        resp = await save_upload(pid, background, video, "expert", str(form.get("title", "")), user_id=uid, goal=str(form.get("goal", "")))
        if auto:
            set_video(resp.headers["location"].rsplit("/", 1)[-1], auto_draft=uid)
        return resp
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@app.post("/videos/{vid}/retry-request")
def retry_request(request: Request, vid: str):
    """文字起こしのやり直しを管理者に申請する（作業の動画は登録モードの人、トライ動画は本人）。"""
    with get_db() as conn:
        v = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
    back = (f"/trials/{vid}" if v and v["kind"] == "trial" else f"/videos/{vid}") if v else "/"
    if not v or (v["kind"] == "trial" and not is_owner(v, request)) or (v["kind"] != "trial" and request.state.mode != "expert"):
        return RedirectResponse(back, status_code=303)
    if retry_state(v, request.state.user)["can_request"]:
        with get_db() as conn:
            conn.execute("INSERT INTO retry_requests (id, video_id, user_id, created_at) VALUES (?, ?, ?, ?)",
                         (uuid.uuid4().hex[:10], vid, request.state.user["id"], now()))
    return RedirectResponse(back, status_code=303)


def register_state(user) -> dict:
    """マニュアル登録の申請の状態（中級未満の人のホームに出す）。"""
    if user is None or user["role"] in users.CAN_REGISTER:
        return {"need": False}
    with get_db() as conn:
        last = conn.execute("SELECT status FROM register_requests WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (user["id"],)).fetchone()
    return {"need": True, "grant": (user["create_grant"] or 0) > 0, "pending": bool(last and last["status"] == "pending"),
            "rejected": bool(last and last["status"] == "rejected")}


templates.env.globals["register_state"] = register_state


@app.post("/register-request")
def register_request(request: Request):
    """中級未満の人が、マニュアル登録（1回）を管理者に申請する。"""
    st = register_state(request.state.user)
    if st["need"] and not st["grant"] and not st["pending"]:
        with get_db() as conn:
            conn.execute("INSERT INTO register_requests (id, user_id, created_at) VALUES (?, ?, ?)",
                         (uuid.uuid4().hex[:10], request.state.user["id"], now()))
    return RedirectResponse("/", status_code=303)


@app.post("/admin/register/{rid}")
def admin_register_decide(request: Request, rid: str, action: str = Form("")):
    """管理者がマニュアル登録の申請を認める（1回だけ登録モード→新規作成できる）か、断る。"""
    if request.state.user["role"] != "admin":
        return RedirectResponse("/", status_code=303)
    with get_db() as conn:
        r = conn.execute("SELECT * FROM register_requests WHERE id = ? AND status = 'pending'", (rid,)).fetchone()
        if r:
            ok = action == "approve"
            conn.execute("UPDATE register_requests SET status = ?, decided_at = ? WHERE id = ?", ("approved" if ok else "rejected", now(), rid))
            if ok:
                conn.execute("UPDATE users SET create_grant = COALESCE(create_grant, 0) + 1 WHERE id = ?", (r["user_id"],))
    return RedirectResponse("/admin/usage#register", status_code=303)


@app.post("/admin/retry/{rid}")
def admin_retry_decide(request: Request, rid: str, action: str = Form("")):
    """管理者がやり直しの申請を認める（追加で1回）か、断る。"""
    if request.state.user["role"] != "admin":
        return RedirectResponse("/", status_code=303)
    with get_db() as conn:
        r = conn.execute("SELECT * FROM retry_requests WHERE id = ? AND status = 'pending'", (rid,)).fetchone()
        if r:
            ok = action == "approve"
            conn.execute("UPDATE retry_requests SET status = ?, decided_at = ? WHERE id = ?", ("approved" if ok else "rejected", now(), rid))
            if ok:
                conn.execute("UPDATE videos SET retry_grant = COALESCE(retry_grant, 0) + 1 WHERE id = ?", (r["video_id"],))
    return RedirectResponse("/admin/usage#requests", status_code=303)


@app.get("/admin/usage", response_class=HTMLResponse)
def admin_usage(request: Request):
    """管理者の🔔：アカウントごとのAIの使用回数と金額、最近の記録。開いたら既読にする。"""
    if request.state.user["role"] != "admin":
        return RedirectResponse("/", status_code=303)
    with get_db() as conn:
        names = owner_names(conn)
        per_user = [dict(r) for r in conn.execute(
            "SELECT user_id, COUNT(*) n, SUM(ok = 0) ng, ROUND(SUM(jpy), 1) jpy, MAX(created_at) last FROM api_usage"
            " GROUP BY user_id ORDER BY jpy DESC").fetchall()]
        recent = [dict(r) for r in conn.execute(
            "SELECT u.*, p.name project_name FROM api_usage u LEFT JOIN projects p ON p.id = u.project_id"
            " ORDER BY u.created_at DESC LIMIT 50").fetchall()]
        total = conn.execute("SELECT COUNT(*), ROUND(COALESCE(SUM(jpy), 0), 1) FROM api_usage").fetchone()
        reg_requests = [dict(r) for r in conn.execute(
            "SELECT * FROM register_requests ORDER BY status = 'pending' DESC, created_at DESC LIMIT 30").fetchall()]
        requests_ = [dict(r) for r in conn.execute(
            "SELECT q.*, v.title, v.original_name, v.kind, v.status vstatus, p.name project_name FROM retry_requests q"
            " JOIN videos v ON v.id = q.video_id LEFT JOIN projects p ON p.id = v.project_id"
            " ORDER BY q.status = 'pending' DESC, q.created_at DESC LIMIT 30").fetchall()]
        conn.execute("UPDATE api_usage SET seen = 1 WHERE seen = 0")
    for r in per_user + recent + requests_ + reg_requests:
        r["user_name"] = names.get(r["user_id"], "（不明）")
    return templates.TemplateResponse(request, "admin_usage.html", {
        "per_user": per_user, "recent": recent, "total_n": total[0], "total_jpy": total[1], "requests": requests_, "reg_requests": reg_requests,
        "KINDS": USAGE_KINDS, "back_url": "/admin"})


def unseen_usage() -> int:
    """🔔の数：まだ見ていないAIの記録＋まだ決めていないやり直しの申請。"""
    with get_db() as conn:
        return (conn.execute("SELECT COUNT(*) FROM api_usage WHERE seen = 0").fetchone()[0]
                + conn.execute("SELECT COUNT(*) FROM retry_requests WHERE status = 'pending'").fetchone()[0]
                + conn.execute("SELECT COUNT(*) FROM register_requests WHERE status = 'pending'").fetchone()[0])


templates.env.globals["unseen_usage"] = unseen_usage


@app.post("/projects")
def create_project(
    name: str = Form(...),
    description: str = Form(""),
    expert_name: str = Form(""),
    expert_level: int = Form(0),
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


@app.get("/projects/{pid}/edit", response_class=HTMLResponse)
def project_edit_page(request: Request, pid: str, msg: str = ""):
    """マニュアルの編集画面（名前・作業名・ゴール・熟練者・分類・番号・消す）。背景色を変えて編集中と分かるように。"""
    if request.state.mode != "expert":
        return RedirectResponse(f"/projects/{pid}", status_code=303)
    with get_db() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        folders = all_folders(conn)
        rows = conn.execute("SELECT * FROM videos WHERE project_id = ? AND kind != 'trial' ORDER BY ord, created_at", (pid,)).fetchall()
    return templates.TemplateResponse(request, "project_edit.html", {
        "project": project, "videos": [video_card(v) for v in rows], "folders": folders,
        "folder": next((f for f in folders if f["id"] == project["folder_id"]), None),
        "msg": msg, "back_url": f"/projects/{pid}"})


@app.post("/projects/{pid}/edit")
async def edit_project(request: Request, pid: str, name: str = Form(...), description: str = Form(None),
                       expert_name: str = Form(""), expert_level: int = Form(0), manual_no: str = Form(None)):
    """マニュアルの名前・作業名（動画の名前）・ゴール・熟練者を直す（マニュアル登録モード）。番号とフォルダーは別の欄で。"""
    if not name.strip():
        return RedirectResponse(f"/projects/{pid}/edit", status_code=303)
    form = await request.form()
    with get_db() as conn:
        if manual_no is not None:
            no = manual_no.strip()
            if not no or conn.execute("SELECT 1 FROM projects WHERE manual_no = ? AND id != ?", (no, pid)).fetchone():
                return RedirectResponse(f"/projects/{pid}/edit?msg=dupno", status_code=303)
            conn.execute("UPDATE projects SET manual_no = ? WHERE id = ?", (no, pid))
        conn.execute(
            "UPDATE projects SET name = ?, expert_name = ?, expert_level = ? WHERE id = ?",
            (name.strip(), expert_name.strip(), min(max(expert_level, 0), 3), pid),
        )
        # 作業名＝このマニュアルの中の各動画の名前
        for key, val in form.items():
            if key.startswith("vtitle_"):
                conn.execute("UPDATE videos SET title = ? WHERE id = ? AND project_id = ?",
                             (str(val).strip(), key[len("vtitle_"):], pid))
            if key.startswith("vgoal_"):
                conn.execute("UPDATE videos SET goal = ? WHERE id = ? AND project_id = ?",
                             (str(val).strip(), key[len("vgoal_"):], pid))
    return RedirectResponse(f"/projects/{pid}/edit?msg=ok", status_code=303)


@app.post("/projects/{pid}/folder")
def move_project(pid: str, folder_id: str = Form(""), new_folder_name: str = Form(""), new_folder_code: str = Form("")):
    """分類を変える（新しい分類もここで作れる）。番号は変えない。"""
    with get_db() as conn:
        folder_id = pick_folder(conn, folder_id, new_folder_name, new_folder_code)
        conn.execute("UPDATE projects SET folder_id = ? WHERE id = ?", (folder_id, pid))
    return RedirectResponse(f"/projects/{pid}/edit", status_code=303)


@app.post("/admin/manuals/{pid}/number")
def renumber_project(pid: str, manual_no: str = Form("")):
    """マニュアル番号を管理者が直す。ほかと重なる番号は受け付けない。"""
    no = manual_no.strip().upper()
    with get_db() as conn:
        if not no or conn.execute("SELECT 1 FROM projects WHERE manual_no = ? AND id != ?", (no, pid)).fetchone():
            return RedirectResponse(f"/projects/{pid}/edit?msg=dupno", status_code=303)
        conn.execute("UPDATE projects SET manual_no = ? WHERE id = ?", (no, pid))
    return RedirectResponse(f"/projects/{pid}/edit?msg=ok", status_code=303)


@app.post("/projects/{pid}/delete")
def delete_project(pid: str):
    with get_db() as conn:
        conn.execute("DELETE FROM videos WHERE project_id = ?", (pid,))
        conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
    shutil.rmtree(MEDIA_DIR / pid, ignore_errors=True)
    return RedirectResponse("/", status_code=303)


def card_counts(conn: sqlite3.Connection, pid: str) -> dict:
    """作業（動画）ごとのカードの数と、承認済みの数。不採用のカードは数えない。"""
    out = {}
    for r in conn.execute("SELECT video_id, COUNT(*) n, SUM(status = 'approved') ok FROM cards "
                          "WHERE project_id = ? AND status != 'rejected' GROUP BY video_id", (pid,)).fetchall():
        out[r["video_id"]] = {"n": r["n"], "ok": r["ok"] or 0}
    return out


@app.get("/projects/{pid}", response_class=HTMLResponse)
def project_page(request: Request, pid: str, msg: str = ""):
    with get_db() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        folders = all_folders(conn)
        rows = conn.execute(
            "SELECT * FROM videos WHERE project_id = ? ORDER BY kind = 'trial', ord, created_at DESC", (pid,)
        ).fetchall()
        # 動画ごとの最初のカードの題名（動画に名前がないときの表示に使う）
        first_titles = {}
        for r in conn.execute(
            "SELECT video_id, data FROM cards WHERE project_id = ? AND status != 'rejected' ORDER BY ord DESC", (pid,)
        ).fetchall():
            first_titles[r["video_id"]] = json.loads(r["data"] or "{}").get("title", "")
        appr = card_counts(conn, pid)
        names = owner_names(conn)
    me = request.state.user["id"]
    rows = [r for r in rows if r["kind"] != "trial" or r["user_id"] == me or r["shared"]]
    videos = [video_card(v, first_titles.get(v["id"], "")) for v in rows]
    for v in videos:
        v["appr"] = appr.get(v["id"], {"n": 0, "ok": 0})
        v["owner_name"] = names.get(v["user_id"], "")
    return templates.TemplateResponse(
        request, "project.html",
        {"project": project, "videos": [v for v in videos if v["kind"] != "trial"],
         "trials": [v for v in videos if v["kind"] == "trial" and v["user_id"] == me],
         "shared_trials": [v for v in videos if v["kind"] == "trial" and v["user_id"] != me], "back_url": "/",
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
async def upload_video(pid: str):
    """（作業の追加は新規作成ページだけ。古い送り先はそこへ案内する）"""
    return RedirectResponse(f"/projects/new?pid={pid}", status_code=303)


@app.post("/projects/{pid}/trials")
async def upload_trial(request: Request, pid: str, background: BackgroundTasks, file: UploadFile = File(...), title: str = Form("")):
    """学ぶ人が自分でやった動画。閲覧モードでも送れる。"""
    return await save_upload(pid, background, file, "trial", title, user_id=request.state.user["id"])


async def save_upload(pid: str, background: BackgroundTasks, file: UploadFile, kind: str, title: str = "", user_id: str = "",
                      goal: str = ""):
    vid = uuid.uuid4().hex[:8]
    ext = Path(file.filename or "video.mp4").suffix.lower() or ".mp4"
    dest_dir = MEDIA_DIR / pid
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{vid}{ext}"
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO videos (id, project_id, filename, original_name, created_at, kind, title, user_id, goal)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (vid, pid, f"{pid}/{vid}{ext}", file.filename or "", now(), kind, title.strip(), user_id, goal.strip()),
        )
        if kind != "trial":
            fill_video_ord(conn)
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


@app.post("/videos/{vid}/move")
def video_move(vid: str, action: str = Form(...)):
    """作業の順番（①②…）を1つ上げる・下げる。"""
    with get_db() as conn:
        v = conn.execute("SELECT project_id FROM videos WHERE id = ?", (vid,)).fetchone()
        ids = [r[0] for r in conn.execute("SELECT id FROM videos WHERE project_id = ? AND kind != 'trial' ORDER BY ord, created_at",
                                          (v["project_id"],)).fetchall()]
        i = ids.index(vid)
        j = i - 1 if action == "up" else i + 1
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
        for n, x in enumerate(ids, 1):
            conn.execute("UPDATE videos SET ord = ? WHERE id = ?", (n, x))
    return RedirectResponse(f"/projects/{v['project_id']}#works", status_code=303)


@app.post("/videos/{vid}/title")
def video_title(vid: str, title: str = Form(""), goal: str = Form(None)):
    """作業名・ゴールを直す（作業の画面）。"""
    if goal is None:
        set_video(vid, title=title.strip())
    else:
        set_video(vid, title=title.strip(), goal=goal.strip())
    return RedirectResponse(f"/videos/{vid}", status_code=303)


# ---------- 学ぶ人の動画：熟練者との違いを見る ----------

@app.get("/trials/{vid}", response_class=HTMLResponse)
def trial_page(request: Request, vid: str):
    with get_db() as conn:
        video = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
        owner = conn.execute("SELECT name FROM users WHERE id = ?", (video["user_id"],)).fetchone() if video else None
    if not can_see_try(video, request):
        return RedirectResponse("/", status_code=303)
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
         "mine": is_owner(video, request), "owner_name": owner["name"] if owner else "",
         "back_url": f"/projects/{project['id']}"},
    )


@app.get("/trials/{vid}/status")
def trial_status(request: Request, vid: str):
    with get_db() as conn:
        video = conn.execute("SELECT status, compare_status, kind, user_id, shared FROM videos WHERE id = ?", (vid,)).fetchone()
    if not can_see_try(video, request):
        video = None
    return JSONResponse({"status": video["status"] if video else "missing",
                         "compare": video["compare_status"] if video else ""})


@app.post("/trials/{vid}/retry")
def trial_retry(request: Request, vid: str, background: BackgroundTasks):
    with get_db() as conn:
        v = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
    if not is_owner(v, request):
        return RedirectResponse("/", status_code=303)
    if not use_retry(v, request.state.user):
        return RedirectResponse(f"/trials/{vid}", status_code=303)
    background.add_task(process_video, vid)
    return RedirectResponse(f"/trials/{vid}", status_code=303)


@app.post("/trials/{vid}/delete")
def trial_delete(request: Request, vid: str):
    with get_db() as conn:
        v = conn.execute("SELECT * FROM videos WHERE id = ? AND kind = 'trial'", (vid,)).fetchone()
        if not is_owner(v, request):
            return RedirectResponse("/", status_code=303)
        conn.execute("DELETE FROM videos WHERE id = ?", (vid,))
    (MEDIA_DIR / v["filename"]).unlink(missing_ok=True)
    shutil.rmtree(MEDIA_DIR / v["project_id"] / f"{vid}_frames", ignore_errors=True)
    return RedirectResponse(f"/projects/{v['project_id']}", status_code=303)


@app.post("/trials/{vid}/share")
def trial_share(request: Request, vid: str):
    with get_db() as conn:
        v = conn.execute("SELECT * FROM videos WHERE id = ? AND kind = 'trial'", (vid,)).fetchone()
        if not is_owner(v, request):
            return RedirectResponse("/", status_code=303)
        conn.execute("UPDATE videos SET shared = ? WHERE id = ?", (0 if v["shared"] else 1, vid))
    return RedirectResponse(f"/trials/{vid}", status_code=303)


@app.get("/trials/{vid}/compare", response_class=HTMLResponse)
def trial_compare_confirm(request: Request, vid: str):
    with get_db() as conn:
        video = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
    if not is_owner(video, request):
        return RedirectResponse("/", status_code=303)
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
def trial_compare(request: Request, vid: str, background: BackgroundTasks, confirm: str = Form("")):
    with get_db() as conn:
        if not is_owner(conn.execute("SELECT user_id FROM videos WHERE id = ?", (vid,)).fetchone(), request):
            return RedirectResponse("/", status_code=303)
    if confirm != "yes":
        return RedirectResponse(f"/trials/{vid}/compare", status_code=303)
    background.add_task(process_compare, vid, request.state.user["id"])
    return RedirectResponse(f"/trials/{vid}", status_code=303)


@app.get("/videos/{vid}", response_class=HTMLResponse)
def video_page(request: Request, vid: str, msg: str = "", s: str = "", e: str = ""):
    with get_db() as conn:
        video = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
        if not can_see_try(video, request):
            return RedirectResponse("/", status_code=303)
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (video["project_id"],)).fetchone()
        cards = conn.execute("SELECT * FROM cards WHERE video_id = ? ORDER BY ord", (vid,)).fetchall()
        work_ids = [r[0] for r in conn.execute("SELECT id FROM videos WHERE project_id = ? AND kind != 'trial' ORDER BY ord, created_at",
                                               (video["project_id"],)).fetchall()]
    segments = json.loads(video["segments"] or "[]")
    frames = json.loads(video["frames"] or "[]")
    cards = [card_view(c) for c in cards]
    return templates.TemplateResponse(
        request, "video.html",
        {"video": video, "project": project, "segments": segments, "frames": frames, "cards": cards,
         "last_cost": json.loads(video["analysis_cost"] or "{}"),
         "has_key": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
         "msg": msg, "pre_s": s, "pre_e": e,
         "work_pos": (work_ids.index(vid) + 1) if vid in work_ids else 0,
         "appr": {"n": sum(1 for c in cards if c["status"] != "rejected"),
                  "ok": sum(1 for c in cards if c["status"] == "approved")},
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
def video_retry(request: Request, vid: str, background: BackgroundTasks, model: str = Form("")):
    with get_db() as conn:
        v = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
    if not v or not use_retry(v, request.state.user):
        return RedirectResponse(f"/videos/{vid}" if v else "/", status_code=303)
    allowed = {m for m, _ in transcribe_mod.MODELS}
    background.add_task(process_video, vid, model if model in allowed else "")
    return RedirectResponse(f"/videos/{vid}", status_code=303)


@app.get("/videos/{vid}/analyze", response_class=HTMLResponse)
def video_analyze_confirm(request: Request, vid: str):
    """有料処理の前の確認画面：料金の予想を円で出し、「はい」を押したときだけ走らせる。"""
    with get_db() as conn:
        video = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
        n_approved = conn.execute("SELECT COUNT(*) FROM cards WHERE video_id = ? AND status = 'approved'", (vid,)).fetchone()[0]
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (video["project_id"],)).fetchone()
    est = analyze_mod.estimate(json.loads(video["segments"] or "[]"), json.loads(video["frames"] or "[]"),
                               analyze_mod.ref_estimate_tokens(dict(project), MEDIA_DIR))
    last = json.loads(video["analysis_cost"] or "{}")
    return templates.TemplateResponse(
        request, "confirm.html",
        {"video": video, "est": est, "last": last, "n_approved": n_approved,
         "has_key": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
         "rate": analyze_mod.jpy_rate(), "back_url": f"/videos/{vid}"},
    )


@app.post("/videos/{vid}/analyze")
def video_analyze(request: Request, vid: str, background: BackgroundTasks, confirm: str = Form("")):
    if confirm != "yes":
        return RedirectResponse(f"/videos/{vid}/analyze", status_code=303)
    background.add_task(process_analysis, vid, request.state.user["id"])
    return RedirectResponse(f"/videos/{vid}", status_code=303)


def process_compare(vid: str, user_id: str = "") -> None:
    """学ぶ人の動画を、熟練者の承認済みカードと比べる（Claude API・有料。確認画面の「はい」の後だけ）。"""
    with get_db() as conn:
        v = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (v["project_id"],)).fetchone()
    _, cards, _ = project_cards(v["project_id"])
    try:
        set_video(vid, compare_status="running", compare_error="")
        with get_db() as conn:
            goals = [r[0] for r in conn.execute("SELECT goal FROM videos WHERE project_id = ? AND kind != 'trial' AND goal != ''",
                                                (v["project_id"],)).fetchall()]
        out = analyze_mod.compare_trial(dict(project, description=" ／ ".join(goals)), [c["data"] for c in cards],
                                        json.loads(v["segments"] or "[]"), json.loads(v["frames"] or "[]"), MEDIA_DIR)
        usage = out.pop("_usage", {})
        set_video(vid, compare=json.dumps(out, ensure_ascii=False), compare_status="done",
                  compare_cost=json.dumps(usage), summary=out.get("summary", ""))
        log_usage(user_id, "compare", vid, v["project_id"], usage)
    except Exception:
        set_video(vid, compare_status="error", compare_error=traceback.format_exc()[-2000:])
        log_usage(user_id, "compare", vid, v["project_id"], ok=False)


# ---------- 判断カード ----------

# 画面に出す項目（番号は spec.md 9節）。(キー, 見出し, 種類)
CARD_FIELDS = [
    ("title", "手順名", "line"),
    ("stage", "段階", "line"),
    ("decision", "判断（どうする）", "text"),
    ("watch", "注目点（何を見る）", "text"),
    ("boundary", "正常／異常の境界", "text"),
    ("degree", "加減（どのくらい）", "text"),
    ("reason", "理由（なぜ）", "text"),
    ("stop_condition", "止める条件／次へ移る条件", "text"),
    ("novice_result", "守らないとどうなる？", "text"),
    ("tools", "必要な工具・物", "list"),
    ("judgment_type", "判断の種類", "judgment"),
    ("judgment_note", "判断の種類の補足（一般／ローカル、数値化の候補）", "text"),
    ("safety", "安全ポイント", "bool"),
    ("safety_note", "安全ポイントの内容（何に気をつけるか）", "text"),
    ("improvement", "さらに良くするには（AI・確度の高いものだけ）", "text"),
    ("improvement_basis", "改善の根拠", "text"),
    ("prerequisites", "この手順の前に必要なこと", "text"),
    ("order_matters", "手順の重要度", "order"),
    ("order_note", "制約の内容（どの程度か）", "text"),
    ("ripple", "波及先（後の手順にどう効くか）", "text"),
    ("is_branch", "分かれ目マーカー", "bool"),
    ("branch_reason", "分かれ目と判断した理由", "text"),
    ("evidence", "根拠にした熟練者の言葉", "text"),
]
# メインモデル（承認に必須の6項目）
MAIN_MODEL = ["title", "decision", "watch", "degree", "reason", "novice_result"]
JUDGMENT_LABELS = {"A": "A 知識", "B": "B 数値", "C": "C 体感", "D": "D 状況"}
ORDER_LABELS = {"high": "必ず遵守", "low": "任意の手順で可", "mid": "制約あり"}
STATUS_LABELS = {"draft": "下書き", "approved": "承認済み", "rejected": "不採用"}
templates.env.globals.update(CARD_FIELDS=CARD_FIELDS, JUDGMENT_LABELS=JUDGMENT_LABELS, MAIN_MODEL=MAIN_MODEL,
                             ORDER_LABELS=ORDER_LABELS, STATUS_LABELS=STATUS_LABELS)


def card_view(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["data"] = json.loads(row["data"] or "{}")
    d["answers"] = json.loads(row["answers"] or "{}")
    return d


@app.get("/cards/{cid}", response_class=HTMLResponse)
def card_page(request: Request, cid: str, back: str = ""):
    with get_db() as conn:
        card = conn.execute("SELECT * FROM cards WHERE id = ?", (cid,)).fetchone()
        video = conn.execute("SELECT * FROM videos WHERE id = ?", (card["video_id"],)).fetchone()
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (card["project_id"],)).fetchone()
    return templates.TemplateResponse(
        request, "card.html",
        {"card": card_view(card), "video": video, "project": project,
         "merged": request.query_params.get("merged"), "merge_error": request.query_params.get("merge_error"),
         "back_url": back if back.startswith("/") and not back.startswith("//") else f"/videos/{video['id']}#cards"},
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
    if action == "approve" and full_form:
        missing = [k for k in MAIN_MODEL if not str(data.get(k) or "").strip()]
        if data.get("safety") and not str(data.get("safety_note") or "").strip():
            missing.append("safety_note")
        if data.get("is_branch") and not str(data.get("branch_reason") or "").strip():
            missing.append("branch_reason")
        if missing:
            action = "save"  # 足りないときは保存だけして、承認しない
            with get_db() as conn:
                conn.execute("UPDATE cards SET data = ?, answers = ?, updated_at = ? WHERE id = ?",
                             (json.dumps(data, ensure_ascii=False), json.dumps(answers, ensure_ascii=False), now(), cid))
            return RedirectResponse(f"/cards/{cid}?need=1", status_code=303)
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
    if action == "merge" and answers:
        # 答えを保存したら、そのまま「欄に書き戻す」の料金確認へ（2回目のAI）
        return RedirectResponse(f"/cards/{cid}/merge", status_code=303)
    return RedirectResponse(f"/cards/{cid}", status_code=303)


@app.get("/cards/{cid}/merge", response_class=HTMLResponse)
def card_merge_confirm(request: Request, cid: str):
    """答えを欄に書き戻す前の確認（有料・軽いモデル）。"""
    with get_db() as conn:
        card = card_view(conn.execute("SELECT * FROM cards WHERE id = ?", (cid,)).fetchone())
    if request.state.mode != "expert" or card["status"] != "draft" or not card["answers"]:
        return RedirectResponse(f"/cards/{cid}", status_code=303)  # 書き戻しは登録モード・下書きのカードだけ
    est = analyze_mod.estimate_merge(card["data"], card["answers"])
    return templates.TemplateResponse(
        request, "confirm_merge.html",
        {"card": card, "est": est, "rate": analyze_mod.jpy_rate(),
         "has_key": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()), "back_url": f"/cards/{cid}"},
    )


@app.post("/cards/{cid}/paths")
async def card_paths_save(request: Request, cid: str):
    """分岐図（成功と失敗の導線）を熟練者が直す。1行＝1コマ。action=ok なら「承認」で確定、save は保存だけ（未確認に戻す）。"""
    form = await request.form()
    lines = lambda k: [x.strip() for x in str(form.get(k, "")).splitlines() if x.strip()][:6]
    with get_db() as conn:
        card = conn.execute("SELECT * FROM cards WHERE id = ?", (cid,)).fetchone()
        if not card:
            return RedirectResponse("/", status_code=303)
        data = json.loads(card["data"] or "{}")
        data["success_path"], data["failure_path"] = lines("ok"), lines("ng")
        data["path_checked"] = form.get("action") == "ok"
        conn.execute("UPDATE cards SET data = ?, updated_at = ? WHERE id = ?",
                     (json.dumps(data, ensure_ascii=False), now(), cid))
    return RedirectResponse(f"/cards/{cid}#map", status_code=303)


@app.post("/cards/{cid}/merge")
def card_merge(request: Request, cid: str, confirm: str = Form("")):
    if confirm != "yes":
        return RedirectResponse(f"/cards/{cid}/merge", status_code=303)
    with get_db() as conn:
        card = card_view(conn.execute("SELECT * FROM cards WHERE id = ?", (cid,)).fetchone())
    if request.state.mode != "expert" or card["status"] != "draft" or not card["answers"]:
        return RedirectResponse(f"/cards/{cid}", status_code=303)
    try:
        out = analyze_mod.merge_answers(card["data"], card["answers"])
    except Exception as e:
        log_usage(request.state.user["id"], "merge", cid, card["project_id"], ok=False)
        return RedirectResponse(f"/cards/{cid}?merge_error=1", status_code=303)
    log_usage(request.state.user["id"], "merge", cid, card["project_id"], out.get("_usage", {}))
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
        # 熟練者の作業だけ（トライ動画は本人だけのものなので混ぜない）
        videos = conn.execute("SELECT * FROM videos WHERE project_id = ? AND kind != 'trial' ORDER BY ord, created_at", (pid,)).fetchall()
        q = "SELECT c.* FROM cards c JOIN videos v ON v.id = c.video_id WHERE c.project_id = ? AND v.kind != 'trial'"
        if approved_only:
            q += " AND c.status = 'approved'"
        cards = conn.execute(q + " ORDER BY v.ord, v.created_at, c.ord", (pid,)).fetchall()
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


def is_owner(row, request: Request) -> bool:
    return bool(row) and row["user_id"] == request.state.user["id"]


def can_see_try(row, request: Request) -> bool:
    """トライ（動画・完成品）は本人か、共有されたものだけ見られる。熟練者のもの（kind が trial 以外）は誰でも。"""
    if not row:
        return False
    if row["kind"] != "trial":
        return True
    return is_owner(row, request) or bool(row["shared"])


def owner_names(conn) -> dict:
    return {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM users").fetchall()}


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
        builds = [build_view(b) for b in conn.execute("SELECT * FROM builds WHERE project_id = ? ORDER BY created_at DESC", (pid,)).fetchall()
                  if can_see_try(b, request)]
        names = owner_names(conn)
    reference = next((b for b in builds if b["kind"] == "reference"), None)
    me = request.state.user["id"]
    for b in builds:
        b["owner_name"] = names.get(b["user_id"], "")
    trials = [b for b in builds if b["kind"] == "trial" and b["user_id"] == me]
    shared_trials = [b for b in builds if b["kind"] == "trial" and b["user_id"] != me]
    for t in trials + shared_trials:
        t["checks"] = dim_check(measures, t["dims"])
    return templates.TemplateResponse(
        request, "eval.html",
        {"project": project, "measures": measures, "reference": reference, "trials": trials, "shared_trials": shared_trials,
         "back_url": f"/projects/{pid}"},
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
        row = conn.execute("SELECT * FROM builds WHERE id = ?", (bid,)).fetchone()
        if not can_see_try(row, request):
            return RedirectResponse("/", status_code=303)
        b = build_view(row)
        b["mine"] = is_owner(row, request)
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


@app.post("/builds/{bid}/share")
def build_share(request: Request, bid: str):
    with get_db() as conn:
        b = conn.execute("SELECT * FROM builds WHERE id = ? AND kind = 'trial'", (bid,)).fetchone()
        if not is_owner(b, request):
            return RedirectResponse("/", status_code=303)
        conn.execute("UPDATE builds SET shared = ? WHERE id = ?", (0 if b["shared"] else 1, bid))
    return RedirectResponse(f"/builds/{bid}", status_code=303)


@app.post("/builds/{bid}/evaluate")
def build_evaluate(request: Request, bid: str, confirm: str = Form("")):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM builds WHERE id = ?", (bid,)).fetchone()
    if not (is_owner(row, request) or (row and row["kind"] == "reference" and request.state.mode == "expert")):
        return RedirectResponse("/", status_code=303)
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
        log_usage(request.state.user["id"], "evaluate", bid, b["project_id"], out.get("_usage", {}))
    except Exception:
        out = {"error": traceback.format_exc()[-1500:]}
        status = "error"
        log_usage(request.state.user["id"], "evaluate", bid, b["project_id"], ok=False)
    with get_db() as conn:
        conn.execute("UPDATE builds SET result = ?, eval_status = ? WHERE id = ?", (json.dumps(out, ensure_ascii=False), status, bid))
    return RedirectResponse(f"/builds/{bid}", status_code=303)
