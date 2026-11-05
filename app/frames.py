"""動画から静止画を切り出す（ffmpeg）。

映像は「道具と状態を確かめる」ために使う。動きの解析はしない。
切り出す時刻：文字起こしの各区切りの開始時刻（多すぎる場合は間引く）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import static_ffmpeg

MAX_FRAMES = 40


def ffmpeg_path() -> str:
    static_ffmpeg.add_paths()
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg が見つかりません")
    return path


def video_duration(video_path: Path) -> float:
    static_ffmpeg.add_paths()
    probe = shutil.which("ffprobe")
    if not probe:
        return 0.0
    res = subprocess.run(
        [probe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True, text=True,
    )
    try:
        return float(res.stdout.strip())
    except ValueError:
        return 0.0


def pick_times(segments: list[dict], duration: float) -> list[float]:
    """静止画を抜く時刻を決める。区切りの開始時刻＋先頭。多ければ均等に間引く。"""
    times = sorted({0.0, *[float(s["start"]) for s in segments]})
    if duration and len(times) < 4:
        # しゃべりが少ない動画でも最低限は抜く（5秒ごと）
        t = 0.0
        while t < duration:
            times.append(round(t, 2))
            t += 5.0
        times = sorted(set(times))
    if len(times) > MAX_FRAMES:
        step = len(times) / MAX_FRAMES
        times = [times[int(i * step)] for i in range(MAX_FRAMES)]
    return times


def extract_frames(video_path: Path, out_dir: Path, times: list[float]) -> list[dict]:
    """指定した時刻の静止画を out_dir に書き出す。戻り値は [{"time": 秒, "file": ファイル名}]。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = ffmpeg_path()
    result: list[dict] = []
    for t in times:
        name = f"t{int(t * 100):07d}.jpg"
        dest = out_dir / name
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video_path),
             "-frames:v", "1", "-vf", "scale='min(720,iw)':-2", "-q:v", "4", str(dest)],
            check=False, capture_output=True,
        )
        if dest.exists():
            result.append({"time": t, "file": name})
    return result


def shrink_image(src: Path, dst: Path, max_w: int = 1024) -> None:
    """写真を幅 max_w 以下の JPEG にして保存（スマホ写真は大きいので送る前に縮める）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg_path(), "-y", "-loglevel", "error", "-i", str(src),
         "-vf", f"scale='min({max_w},iw)':-2", "-q:v", "3", str(dst)],
        check=True,
    )
