"""分野プロファイル：app/profiles/{分野}.md を読む。

分野を足す＝このフォルダに md を1枚足す。文字起こしの用語ヒントも、判断カードの下書きの前提知識も、ここから取る。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

PROFILE_DIR = Path(__file__).resolve().parent / "profiles"


@lru_cache
def load(domain: str) -> str:
    path = PROFILE_DIR / f"{domain}.md"
    if not path.exists():
        path = PROFILE_DIR / "3dp.md"
    return path.read_text(encoding="utf-8")


def section(domain: str, heading: str) -> str:
    """「## 見出し」で始まる節の本文を返す（見出しは前方一致）。"""
    text = load(domain)
    m = re.search(rf"^## {re.escape(heading)}.*?$\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return m.group(1).strip() if m else ""


def transcribe_hint(domain: str) -> str:
    return section(domain, "用語ヒント").replace("\n", "")


def available() -> list[str]:
    return sorted(p.stem for p in PROFILE_DIR.glob("*.md"))
