"""判断カードの下書き（Claude API）。

入力：案件情報 ＋ 時刻つきの文章 ＋ 静止画（道具と状態の確認用）＋ 分野プロファイル
出力：判断カード（20項目）の一覧。分かれ目マーカー、判断の種類、足りない欄への質問つき。

方針（docs/judgment.md、spec.md 10節）：
- 主役は熟練者の言葉。AIは「言った事の整理」と「確度の高い一般知識」だけ。
- 根拠を示せない提案は出さない。推測した欄は空にして「質問」に回す。
- 安全に関わる手順は分野プロファイルの規則で必ず印を付ける。
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import anthropic

from . import profiles

MAX_IMAGES = 12  # 送る静止画の上限（コストと時間の抑え）

# 料金（100万トークンあたりの米ドル）。モデルを変えたらここも直す
PRICES_USD = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}


def jpy_rate() -> float:
    return float(os.environ.get("JPY_PER_USD", "155") or 155)


def heavy_model() -> str:
    return os.environ.get("CLAUDE_MODEL_HEAVY", "claude-opus-5-5").strip() or "claude-opus-5-5"


def cost_jpy(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES_USD.get(model, (4.0, 20.0))
    usd = input_tokens / 1e6 * pin + output_tokens / 1e6 * pout
    return usd * jpy_rate()


def estimate(segments: list[dict], frames: list[dict]) -> dict:
    """下書き1回の料金の予想。実測（31秒・6区切り・6枚 → 入力5.5k・出力2.6k トークン）を元にした目安。"""
    chars = sum(len(s.get("text", "")) for s in segments)
    n_img = min(len(frames), MAX_IMAGES)
    input_tokens = 3000 + int(chars * 1.3) + n_img * 400          # 固定の前提知識 ＋ 文章 ＋ 静止画
    cards = max(2, len(segments) // 2)
    output_tokens = 300 + cards * 800                              # カード1枚あたり約800トークン
    model = heavy_model()
    mid = cost_jpy(model, input_tokens, output_tokens)
    return {"model": model, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cards": cards, "chars": chars, "jpy_low": round(mid * 0.6), "jpy_high": round(mid * 1.8), "jpy_mid": round(mid)}


# 判断カード1枚の形（spec.md 9節の20項目に対応。番号はコメントに）
CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "1 手順名（短く）"},
        "stage": {"type": "string", "description": "1 段階（分野プロファイルの段階名から）"},
        "watch": {"type": "string", "description": "2 注目点：何を見ているか（①）"},
        "boundary": {"type": "string", "description": "3 正常／異常の境界（②）。言っていなければ空"},
        "decision": {"type": "string", "description": "4 判断：どうする（③）"},
        "degree": {"type": "string", "description": "5 加減：どのくらい（④）。力・感触は『身近な例え／結果の状態／強すぎ・弱すぎの失敗』で。言っていなければ空"},
        "reason": {"type": "string", "description": "6 理由：なぜ。言っていなければ空"},
        "stop_condition": {"type": "string", "description": "7 止める条件／次へ移る条件（⑤）。言っていなければ空"},
        "novice_result": {"type": "string", "description": "8 素人がやるとどうなる（⑦）。熟練者が言っていれば その言葉、言っていなければ一般知識から下書きし source_novice を ai にする"},
        "tools": {"type": "array", "items": {"type": "string"}, "description": "10 必要な工具・物"},
        "judgment_type": {"type": "string", "enum": ["A", "B", "C", "D"], "description": "11 判断の種類：A知識／B数値／C体感／D状況"},
        "judgment_note": {"type": "string", "description": "11 の補足。A のとき一般知識かローカル知識か。C のとき数値化できそうな候補があれば書く"},
        "safety": {"type": "boolean", "description": "12 安全に関わるか（分野プロファイルの規則に該当すれば必ず true）"},
        "improvement": {"type": "string", "description": "13 さらに良くするには。確度が高く根拠を示せるものだけ。なければ空"},
        "improvement_basis": {"type": "string", "description": "13 の根拠（一般知識の出どころ）。improvement が空なら空"},
        "time_start": {"type": "number", "description": "15 紐づく動画の開始秒"},
        "time_end": {"type": "number", "description": "15 紐づく動画の終了秒"},
        "prerequisites": {"type": "string", "description": "18 この手順の前に必要なこと。言っていなければ空"},
        "order_matters": {"type": "string", "enum": ["high", "mid", "low"], "description": "19 順番の重要度"},
        "order_note": {"type": "string", "description": "19 順番を変えると何が起きるか"},
        "ripple": {"type": "string", "description": "20 波及先：この失敗が後の手順にどう効くか（根拠つき）"},
        "is_branch": {"type": "boolean", "description": "分かれ目マーカー：素人と熟練者で結果が大きく変わるか"},
        "branch_reason": {"type": "string", "description": "分かれ目と判断した理由（8の対比・失敗の重さ・戻せるか・時間配分）"},
        "source_novice": {"type": "string", "enum": ["expert", "ai"], "description": "8 の出どころ"},
        "questions": {"type": "array", "items": {"type": "string"}, "description": "足りない欄を埋めるため熟練者に聞く短い質問（判断モデル①〜⑦から。最大3つ）"},
        "evidence": {"type": "string", "description": "根拠にした熟練者の言葉（文字起こしからの引用）"},
    },
    "required": [
        "title", "stage", "watch", "boundary", "decision", "degree", "reason", "stop_condition",
        "novice_result", "tools", "judgment_type", "judgment_note", "safety", "improvement",
        "improvement_basis", "time_start", "time_end", "prerequisites", "order_matters", "order_note",
        "ripple", "is_branch", "branch_reason", "source_novice", "questions", "evidence",
    ],
    "additionalProperties": False,
}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "この動画でやっている作業を1〜2文で"},
        "cards": {"type": "array", "items": CARD_SCHEMA},
        "notes_for_expert": {"type": "string", "description": "熟練者への全体的な確認事項（聞き取れなかった所、映像と言葉が合わない所など）。なければ空"},
    },
    "required": ["summary", "cards", "notes_for_expert"],
    "additionalProperties": False,
}

SYSTEM = """あなたは試作現場の技能を「判断カード」に書き起こす補助役です。熟練者が作業しながらしゃべった動画の文字起こしと静止画から、分かれ目1個＝カード1枚で下書きを作ります。

守ること：
1. 主役は熟練者の言葉。欄は熟練者が言ったことで埋める。言っていない欄は空にし、代わりに questions に短い質問を入れる（例：「どうなったら止めますか？」「素人がやるとどうなりますか？」）。
2. 推測で欄を埋めない。例外は「素人がやるとどうなる」「波及先」「順番」「さらに良くするには」で、一般知識として確度が高く根拠を書けるものだけ下書きしてよい。その場合は根拠を書く。根拠を示せなければ空にする。
3. その会社・現場にしかないローカル知識（社内規格、この機械のクセ、得意先の好み）は熟練者を優先し、AIの一般知識で上書きしない。
4. 安全に関わる手順は分野プロファイルの規則に従い必ず safety を true にする。
5. 静止画は「道具と状態の確認」にだけ使う（何を持っているか、どんな状態か）。動きの解析はしない。
6. 分かれ目マーカーは、素人と熟練者で結果が大きく変わる所に付ける（失敗が重い、戻せない、熟練者が時間をかけている、素人がやるとの対比が大きい）。全部に付けない。
7. 時刻は文字起こしの区切りの時刻を使う。
8. すべて日本語。現場の言葉をそのまま使い、言い換えない。
"""


def _image_block(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def _pick_frames(frames: list[dict], media_dir: Path) -> list[tuple[float, Path]]:
    """送る静止画を間引いて最大 MAX_IMAGES 枚にする。"""
    items = []
    for f in frames:
        p = media_dir / f["url"].removeprefix("/media/")
        if p.exists():
            items.append((float(f["time"]), p))
    if len(items) <= MAX_IMAGES:
        return items
    step = len(items) / MAX_IMAGES
    return [items[int(i * step)] for i in range(MAX_IMAGES)]


def fmt(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def draft_cards(project: dict, segments: list[dict], frames: list[dict], media_dir: Path) -> dict:
    """判断カードの下書きを返す（OUTPUT_SCHEMA の形の dict）。"""
    domain = project.get("domain") or "3dp"
    profile = profiles.load(domain)
    model = heavy_model()

    transcript = "\n".join(f"[{fmt(s['start'])}〜{fmt(s['end'])}] {s['text']}" for s in segments)
    content: list[dict] = [
        {"type": "text", "text": (
            "# 分野プロファイル\n" + profile + "\n\n"
            "# 案件\n"
            f"名前：{project.get('name','')}\n説明：{project.get('description','')}\n"
            f"熟練者：{project.get('expert_name','') or '不明'}（レベル{project.get('expert_level', 5)}／7）\n\n"
            "# 熟練者がしゃべった内容（時刻つき）\n" + (transcript or "（聞き取れた言葉なし）") + "\n\n"
            "# 静止画（各区切りの1コマ。道具と状態の確認にだけ使う）"
        )},
    ]
    for t, p in _pick_frames(frames, media_dir):
        content.append({"type": "text", "text": f"静止画 {fmt(t)}："})
        content.append(_image_block(p))
    content.append({"type": "text", "text": "以上から判断カードの下書きを作ってください。"})

    client = anthropic.Anthropic()
    with client.messages.stream(
        model=model,
        max_tokens=16000,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        raise RuntimeError("AIが応答を断りました: " + str(getattr(response, "stop_details", "")))
    if response.stop_reason == "max_tokens":
        raise RuntimeError("出力が長すぎて途中で切れました。動画を短く分けてください。")
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    u = response.usage
    data["_usage"] = {
        "model": response.model,
        "input_tokens": u.input_tokens + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0),
        "output_tokens": u.output_tokens,
        "jpy": round(cost_jpy(model, u.input_tokens + (u.cache_creation_input_tokens or 0) * 1.25
                              + (u.cache_read_input_tokens or 0) * 0.1, u.output_tokens), 1),
    }
    return data


# ---------- 質問への答えをカードの欄に書き戻す（軽いモデル） ----------

MERGE_FIELDS = ["watch", "boundary", "decision", "degree", "reason", "stop_condition",
                "novice_result", "tools", "prerequisites", "order_note", "ripple", "judgment_note"]

MERGE_SCHEMA = {
    "type": "object",
    "properties": {
        **{k: {"type": "string", "description": f"更新後の {k}。変えないなら元の文をそのまま"} for k in MERGE_FIELDS if k != "tools"},
        "tools": {"type": "array", "items": {"type": "string"}},
        "changed": {"type": "array", "items": {"type": "string"}, "description": "書き換えた欄のキー"},
        "note": {"type": "string", "description": "書き換えの要点を1〜2文（熟練者が確認するため）"},
    },
    "required": MERGE_FIELDS + ["changed", "note"],
    "additionalProperties": False,
}

MERGE_SYSTEM = """熟練者が質問に答えました。その答えを判断カードの欄に書き戻してください。
- 答えに書いてあることだけを使う。推測で足さない。
- 元の欄の言葉は残し、答えの内容を自然につなぐ（現場の言葉をそのまま使う）。
- 答えと関係のない欄は一字も変えない。
- 力・感触は「身近な例え／結果の状態／強すぎ・弱すぎの失敗」の形で書けるなら、そう整える。
- すべて日本語。"""


def light_model() -> str:
    return os.environ.get("CLAUDE_MODEL_LIGHT", "claude-sonnet-5-5").strip() or "claude-sonnet-5-5"


def estimate_merge(card: dict, answers: dict) -> dict:
    chars = sum(len(str(card.get(k, ""))) for k in MERGE_FIELDS) + sum(len(q) + len(a) for q, a in answers.items())
    input_tokens = 600 + int(chars * 1.3)
    output_tokens = 200 + int(sum(len(str(card.get(k, ""))) for k in MERGE_FIELDS) * 1.3)
    model = light_model()
    mid = cost_jpy(model, input_tokens, output_tokens)
    return {"model": model, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "jpy_low": max(1, round(mid * 0.6)), "jpy_high": max(1, round(mid * 1.8)), "jpy_mid": max(1, round(mid))}


def merge_answers(card: dict, answers: dict) -> dict:
    """答えを欄に反映した結果（MERGE_SCHEMA の形）を返す。"""
    model = light_model()
    current = {k: card.get(k, "") for k in MERGE_FIELDS}
    qa = "\n".join(f"Q: {q}\nA: {a}" for q, a in answers.items())
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=4000,
        system=MERGE_SYSTEM,
        messages=[{"role": "user", "content":
                   "# カードの今の欄\n" + json.dumps(current, ensure_ascii=False, indent=1) +
                   "\n\n# 熟練者の答え\n" + qa + "\n\n答えを欄に書き戻してください。"}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": MERGE_SCHEMA}},
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("AIが応答を断りました")
    data = json.loads(next(b.text for b in response.content if b.type == "text"))
    u = response.usage
    data["_usage"] = {"model": response.model, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                      "jpy": round(cost_jpy(model, u.input_tokens, u.output_tokens), 1)}
    return data
