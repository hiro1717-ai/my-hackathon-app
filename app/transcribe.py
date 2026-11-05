"""文字起こし（faster-whisper をローカルで動かす）。

結果は「時刻つきの文章」：[{"start": 秒, "end": 秒, "text": "..."}, ...]
モデルは .env の WHISPER_MODEL（開発中 small、本番 large-v3）。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from faster_whisper import WhisperModel

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = BASE_DIR / ".cache" / "whisper"

_model: WhisperModel | None = None
_model_name: str | None = None
_lock = threading.Lock()

# 画面で選べるモデル（名前, 説明）
MODELS = [
    ("small", "小（速い・開発用。数分）"),
    ("large-v3", "大（最良・本番用。Intel Mac では動画の5〜10倍の時間）"),
]


def model_name() -> str:
    return os.environ.get("WHISPER_MODEL", "small").strip() or "small"


def get_model(name: str | None = None) -> WhisperModel:
    """モデルは重いので一度だけ読み込んで使い回す（別のモデルを頼まれたら入れ替える）。"""
    global _model, _model_name
    name = name or model_name()
    with _lock:
        if _model is None or _model_name != name:
            _model = WhisperModel(
                name,
                device="cpu",
                compute_type="int8",  # Intel Mac でもメモリを抑えて動く
                download_root=str(CACHE_DIR),
            )
            _model_name = name
        return _model


# 分野ごとの用語ヒント。Whisper は「直前の文脈」として渡された言葉に寄せて聞き取るので、
# 現場の言葉（意匠面、根元、バリ…）を誤変換しにくくなる。分野プロファイルの一部。
DOMAIN_HINTS = {
    "3dp": (
        "3Dプリンターで樹脂部品を試作し、後処理をしています。"
        "サポート材をニッパーで根元から外し、意匠面に当て板を挟み、"
        "残ったバリは400番のやすりで軽くかけます。積層、ノズル、造形向き、ラフト、インフィル。"
    ),
}


def transcribe(video_path: Path, model: str | None = None, domain: str = "3dp") -> list[dict]:
    """動画ファイルから時刻つきの文章を返す。model を省くと .env の既定値。"""
    model = get_model(model)
    segments, _info = model.transcribe(
        str(video_path),
        language="ja",
        vad_filter=True,  # 無音を飛ばす
        beam_size=5,
        initial_prompt=DOMAIN_HINTS.get(domain, ""),
    )
    out: list[dict] = []
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        out.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": text})
    return out
