"""判断カードの下書き（Claude API）。

入力：マニュアルの情報 ＋ 時刻つきの文章 ＋ 静止画（道具と状態の確認用）＋ 分野プロファイル
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
        "success_path": {"type": "array", "items": {"type": "string"},
                         "description": "成功の導線（図に描く）。熟練者のやり方を 見る→決める→良い結果 の順に2〜4コマ。1コマ12文字以内の短い言葉"},
        "failure_path": {"type": "array", "items": {"type": "string"},
                         "description": "失敗の導線（図に描く）。素人のやり方→起きること→後の手順への波及 の順に2〜4コマ。1コマ12文字以内。安全のカードは最後のコマを必ずケガ・事故にする"},
        "source_novice": {"type": "string", "enum": ["expert", "ai"], "description": "8 の出どころ"},
        "questions": {"type": "array", "items": {"type": "string"}, "description": "足りない欄を埋めるため熟練者に聞く短い質問（判断モデル①〜⑦から。最大3つ）"},
        "evidence": {"type": "string", "description": "根拠にした熟練者の言葉（文字起こしからの引用）"},
    },
    "required": [
        "title", "stage", "watch", "boundary", "decision", "degree", "reason", "stop_condition",
        "novice_result", "tools", "judgment_type", "judgment_note", "safety", "improvement",
        "improvement_basis", "time_start", "time_end", "prerequisites", "order_matters", "order_note",
        "ripple", "is_branch", "branch_reason", "success_path", "failure_path", "source_novice", "questions", "evidence",
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
9. success_path / failure_path は図に描く短い言葉。文にしない（例：「光にかざす」「角が丸くなる」）。安全のカードの failure_path は最後をケガ・事故にし、省かない。
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
            "# マニュアル（作る物・ゴール）\n"
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


# ---------- 完成品の評価（基準写真との比較） ----------

FACES = ["正面", "意匠面", "問題が出やすい面"]

EVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "face": {"type": "string", "description": "どの面（正面／意匠面／問題が出やすい面）"},
                "where": {"type": "string", "description": "写真のどのあたりか（例：右上の角、穴のまわり）"},
                "kind": {"type": "string", "enum": ["サポート跡", "傷", "反り", "層割れ", "糸引き", "ゆがみ", "欠け", "その他"]},
                "level": {"type": "string", "enum": ["同等", "要確認", "違いあり"], "description": "はっきりしない所は 要確認。不良と断定しない"},
                "detail": {"type": "string", "description": "基準と比べて何が違うか。1〜2文"},
                "symptom": {"type": "string", "description": "失敗の導線を逆引きするための短い症状語（例：波打ち、サポート跡、反り）"},
            },
            "required": ["face", "where", "kind", "level", "detail", "symptom"],
            "additionalProperties": False,
        }},
        "overall": {"type": "string", "description": "全体の所見。2〜3文。良い点も書く"},
        "photo_quality": {"type": "string", "description": "写真の撮り方で比較しにくかった点（構図のずれ、暗さ、ブレ）。なければ空"},
    },
    "required": ["findings", "overall", "photo_quality"],
    "additionalProperties": False,
}

EVAL_SYSTEM = """熟練者が作った基準の完成品と、学ぶ人が作った完成品の写真を、同じ面ごとに見比べてください。
- 絶対評価ではなく、基準との比較。違いを箇所ごとに挙げる。
- はっきりしない所は「要確認」にし、不良と断定しない。見えないものを見えたと書かない。
- 構図や光の違いによる見え方の差は、欠陥として扱わない（photo_quality に書く）。
- 良い点（基準と同等の所）も1つは書く。
- すべて日本語、短く。"""


def estimate_eval(n_pairs: int) -> dict:
    input_tokens = 1500 + n_pairs * 2 * 1200
    output_tokens = 800
    model = heavy_model()
    mid = cost_jpy(model, input_tokens, output_tokens)
    return {"model": model, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "jpy_low": max(1, round(mid * 0.6)), "jpy_high": max(1, round(mid * 1.8)), "jpy_mid": max(1, round(mid))}


def evaluate_build(project: dict, ref_photos: dict, trial_photos: dict, media_dir: Path) -> dict:
    """面ごとに（基準, 新人）の写真の組を渡して違いを返す。ref/trial は {面: 保存パス}。"""
    model = heavy_model()
    content: list[dict] = [{"type": "text", "text": (
        "# マニュアル（作る物・ゴール）\n" + f"{project.get('name','')}：{project.get('description','')}\n\n"
        "# 分野プロファイル（よくある不具合の知識として）\n" + profiles.section(project.get("domain") or "3dp", "よくある分かれ目") + "\n\n"
        "# 写真（面ごとに、基準 → 学ぶ人 の順）")}]
    for face in FACES:
        if face in ref_photos and face in trial_photos:
            content.append({"type": "text", "text": f"## {face}：基準（熟練者）"})
            content.append(_image_block(media_dir / ref_photos[face]))
            content.append({"type": "text", "text": f"## {face}：学ぶ人"})
            content.append(_image_block(media_dir / trial_photos[face]))
    content.append({"type": "text", "text": "面ごとに見比べて、違いを挙げてください。"})
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model, max_tokens=6000, system=EVAL_SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": EVAL_SCHEMA}},
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("AIが応答を断りました")
    data = json.loads(next(b.text for b in response.content if b.type == "text"))
    u = response.usage
    data["_usage"] = {"model": response.model, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                      "jpy": round(cost_jpy(model, u.input_tokens, u.output_tokens), 1)}
    return data


# ---------- 自分の作業動画を、熟練者の判断カードと比べる ----------

COMPARE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "学ぶ人の動画でやっていた作業の要約（1〜2文）"},
        "items": {
            "type": "array",
            "description": "熟練者のカード1枚につき1件。順番はカードの順",
            "items": {
                "type": "object",
                "properties": {
                    "card_title": {"type": "string", "description": "対応する熟練者のカードの手順名（そのまま）"},
                    "level": {"type": "string", "enum": ["同じ", "違いあり", "やっていない", "確認できず"],
                              "description": "熟練者と同じようにできているか。動画や言葉から分からなければ 確認できず"},
                    "trial_time": {"type": "number", "description": "学ぶ人の動画でこの手順をやっている開始秒。やっていなければ -1"},
                    "difference": {"type": "string", "description": "何が違うか。熟練者の言葉（カード）と、学ぶ人の言葉・映像の事実を並べて。同じなら空"},
                    "risk": {"type": "string", "description": "その違いで起きうる結果（カードの『素人がやると』『波及先』を根拠に）。なければ空"},
                    "advice": {"type": "string", "description": "次に直すなら何を見てどうするか（カードの言葉で）。なければ空"},
                },
                "required": ["card_title", "level", "trial_time", "difference", "risk", "advice"],
                "additionalProperties": False,
            },
        },
        "extra_steps": {"type": "array", "items": {"type": "string"},
                        "description": "学ぶ人がやっていて熟練者のカードにない手順（事実だけ）"},
        "good_points": {"type": "array", "items": {"type": "string"}, "description": "熟練者と同じようにできていた点"},
        "notes": {"type": "string", "description": "撮り方・しゃべり方への注文（次は何を映す・言うと比べやすいか）"},
    },
    "required": ["summary", "items", "extra_steps", "good_points", "notes"],
    "additionalProperties": False,
}

COMPARE_SYSTEM = """学ぶ人が、熟練者と同じ作業を自分でやってしゃべった動画があります。熟練者の判断カード（承認済み）を基準に、何が違うかを挙げてください。

守ること
1. 基準は熟練者のカードの言葉。一般知識で基準を足さない。
2. 事実と推測を分ける。学ぶ人の言葉・静止画で確かめられないことは「確認できず」にする。決めつけない。
3. 違いは「熟練者は〜、学ぶ人は〜」と並べて書く。良し悪しの断定より、分かれ目のどこで分かれたかを示す。
4. 安全に関わるカードの違いは必ず挙げる。
5. 静止画は道具と状態の確認に使う。動きや力加減は映像から推測しない。
6. すべて日本語。現場の言葉をそのまま使う。
"""


def estimate_compare(cards: list[dict], segments: list[dict], frames: list[dict]) -> dict:
    chars = sum(len(s.get("text", "")) for s in segments)
    n_img = min(len(frames), MAX_IMAGES)
    input_tokens = 2500 + len(cards) * 450 + int(chars * 1.3) + n_img * 400
    output_tokens = 300 + len(cards) * 250
    model = heavy_model()
    mid = cost_jpy(model, input_tokens, output_tokens)
    return {"model": model, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cards": len(cards), "chars": chars, "jpy_low": round(mid * 0.6), "jpy_high": round(mid * 1.8), "jpy_mid": round(mid)}


def compare_trial(project: dict, cards: list[dict], segments: list[dict], frames: list[dict], media_dir: Path) -> dict:
    """熟練者のカード（dict の data 部分の列）と、学ぶ人の動画（文字起こし・静止画）を比べる。"""
    model = heavy_model()
    lines = ["# マニュアル（作る物・ゴール）", f"{project.get('name','')}：{project.get('description','')}", "",
             "# 熟練者の判断カード（基準）"]
    for i, d in enumerate(cards, 1):
        lines.append(f"## {i}. {d.get('title','')}（{d.get('time_start',0)}〜{d.get('time_end',0)}秒）")
        for key, label in [("watch", "注目点"), ("boundary", "境界"), ("decision", "判断"), ("degree", "加減"),
                           ("reason", "理由"), ("stop_condition", "止める条件"), ("novice_result", "素人がやると"),
                           ("tools", "道具"), ("ripple", "波及先")]:
            val = d.get(key)
            if val:
                lines.append(f"- {label}：{'、'.join(val) if isinstance(val, list) else val}")
        if d.get("safety"):
            lines.append("- 安全に関わる")
    lines += ["", "# 学ぶ人の動画の文字起こし（時刻つき）"]
    for s in segments:
        lines.append(f"[{fmt(float(s['start']))}] {s.get('text','')}")
    lines += ["", "# 学ぶ人の動画の静止画（時刻順）"]
    content: list[dict] = [{"type": "text", "text": "\n".join(lines)}]
    for t, p in _pick_frames(frames, media_dir):
        content.append({"type": "text", "text": f"静止画 {fmt(t)}"})
        content.append(_image_block(p))
    content.append({"type": "text", "text": "カード1枚ずつ、学ぶ人の作業と比べてください。"})
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model, max_tokens=8000, system=COMPARE_SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": COMPARE_SCHEMA}},
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("AIが応答を断りました")
    data = json.loads(next(b.text for b in response.content if b.type == "text"))
    u = response.usage
    data["_usage"] = {"model": response.model, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                      "jpy": round(cost_jpy(model, u.input_tokens, u.output_tokens), 1)}
    return data
