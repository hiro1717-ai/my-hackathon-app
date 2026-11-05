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


def model_name() -> str:
    return os.environ.get("WHISPER_MODEL", "small").strip() or "small"


def get_model() -> WhisperModel:
    """モデルは重いので一度だけ読み込んで使い回す。"""
    global _model, _model_name
    name = model_name()
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


def transcribe(video_path: Path) -> list[dict]:
    """動画ファイルから時刻つきの文章を返す。"""
    model = get_model()
    segments, _info = model.transcribe(
        str(video_path),
        language="ja",
        vad_filter=True,  # 無音を飛ばす
        beam_size=5,
    )
    out: list[dict] = []
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        out.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": text})
    return out
