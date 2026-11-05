# 環境構築の手順（別の端末で同じアプリを動かすため）

> 対象：Mac（Intel・Apple Silicon どちらも）。Windows / Linux でも `uv` のインストール方法だけ読み替えれば同じ。
> 所要時間：ダウンロードを除いて 10 分ほど。Whisper の最良モデル（large-v3、約1.6GB）のダウンロードは回線次第で数分〜数十分。

## 0. 前提
- git が入っていること（Mac は Xcode コマンドラインツールで入る。ターミナルで `git --version` と打って出ればOK）
- ディスクの空き：3GB 以上（Python 環境 約0.4GB ＋ Whisper モデル 約2.1GB ＋ 動画）
- Anthropic の API キー（https://console.anthropic.com/ で発行）

## 1. リポジトリを取ってくる
```bash
git clone https://github.com/hiro1717-ai/my-hackathon-app.git
cd my-hackathon-app
```

## 2. uv を入れる（Python 本体とライブラリをまとめて管理する道具）
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
終わったらターミナルを開き直す（または `source $HOME/.local/bin/env`）。確認：
```bash
uv --version
```

## 3. Python 3.12 とライブラリを入れる
`pyproject.toml` と `uv.lock` に必要なものが全部書いてあるので、1コマンドで揃う。
```bash
uv sync
```
これで `.venv/` に Python 3.12 と FastAPI・faster-whisper・anthropic などが入る。

> 固定している部品（`uv.lock` に記録済みなので、何もしなくてよい）：
> - `onnxruntime` 1.19 系：新しい版に Intel Mac 向けの配布がないため
> - `av` 15 系：faster-whisper 1.2 が新しい版（16以降）と合わないため

## 4. ffmpeg（動画から静止画を切り出す道具）を入れる
pip 版の `static-ffmpeg` が初回に自動でダウンロードする。先に済ませておくなら：
```bash
uv run python -c "import static_ffmpeg; static_ffmpeg.add_paths(); print('ok')"
```

## 5. Whisper のモデルを入れる（文字起こし。ローカルで動く）
開発用の small と、本番用の large-v3（最良）を両方入れる。
```bash
uv run python -c "from faster_whisper import download_model; [download_model(m, cache_dir='.cache/whisper') for m in ['small','large-v3']]; print('done')"
```
`.cache/whisper/` に保存される（git には入らない）。

## 6. API キーとパスワードを設定する
```bash
cp .env.example .env
```
`.env` を開いて次の2つを埋める。
- `ANTHROPIC_API_KEY=` の後に自分のキーを貼る。
- `APP_PASSWORD=` に現場で決めた共通パスワードを入れる（空ならログインなしで動くが、同じ Wi-Fi の誰でも開ける状態になる）。

`.env` は `.gitignore` 済みなので、コミットされない。
`WHISPER_MODEL` は既定のモデル（開発中 `small`、本番・デモ素材は `large-v3`）。動画の画面の「文字起こしをやり直す」でも1本ごとに選べる。

## 6b. 秘密情報を守る設定（初回だけ）
- **コミット前の検査**：`.env` や API キーらしき文字列をコミットしようとすると止まる。リポジトリ内の `.githooks/pre-commit` を使うよう git に教える：
  ```bash
  git config core.hooksPath .githooks
  ```
- **Claude Code に `.env` を読ませない**：`.claude/settings.json` に拒否ルールを入れてある（リポジトリに含まれるので、clone すれば効く）。

## 7. 起動する
Finder でプロジェクトのフォルダを開き、`start.command` をダブルクリック（初回は右クリック→「開く」）。黒い窓が開いて住所が表示される。

ターミナルから起動するなら：
```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```
- Mac 本体：ブラウザで http://localhost:8000
- スマホ：同じ Wi-Fi につなぎ、Mac の IP アドレス（`ipconfig getifaddr en0` で分かる）を使って http://192.168.x.x:8000 を開く。

止めるときはターミナルで `Ctrl + C`。

## 7b. 出先から使う（Tailscale。任意）
同じ Wi-Fi にいなくても、自分の端末だけからアプリに入れる仕組み。無料（個人用、3台まで）。https になるので、スマホの「しゃべって答える」も使える。
1. Mac：https://tailscale.com/download/mac からインストールし、メニューバーのアイコンからログイン（Apple か Google のアカウント）。
2. スマホ：App Store の Tailscale を入れ、**同じアカウント**でログインしてスイッチをオン。
3. 管理画面 https://login.tailscale.com/admin/dns で「Enable HTTPS」と「Enable MagicDNS」を押す。
4. Mac のターミナルで：
   ```bash
   /Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg 8000
   ```
5. スマホで `https://<Macの名前>.<網の名前>.ts.net` を開く（名前は Mac のメニューバーの Tailscale で分かる）。初回は証明書の発行で30秒ほど待つ。

止めるときは `... serve --https=443 off`。Mac の再起動後も設定は残る。公開はされない（自分の網の中だけ）。

## 8. データの場所
| 何 | どこ |
|---|---|
| データベース（案件・カード・記録） | `data/app.db`（SQLite の1ファイル） |
| 動画・写真・図面 | `media/案件ID/` |
| Whisper のモデル | `.cache/whisper/` |
| 設定（APIキーなど） | `.env` |

別の端末に引っ越すときは、`data/` と `media/` をコピーすれば中身ごと移せる。

## 困ったとき
- `uv: command not found` → ターミナルを開き直す。
- `onnxruntime ... can't be installed` → Intel Mac。`uv sync` を使えば固定版が入る。`uv add` で個別に足す場合は `"onnxruntime<1.20"` を指定。
- スマホから開けない → Mac とスマホが同じ Wi-Fi か、Mac のファイアウォールが 8000 番を止めていないかを確認。
- 文字起こしが遅い → `.env` の `WHISPER_MODEL=small` にする。実測：Intel Core i5・8GB の Mac で large-v3 は動画の長さの約2.5〜3倍（31秒の動画で74〜87秒）。4分の動画なら10〜12分の目安（送信して放っておく運用）。
- 現場の言葉（意匠面、根元、バリ など）が別の字になる → `app/transcribe.py` の `DOMAIN_HINTS` に分野の用語を足す。Whisper はここに書いた言葉に寄せて聞き取る。
