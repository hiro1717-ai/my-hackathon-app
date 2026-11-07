#!/bin/bash
# AI技能コーチを起動する（Finder でダブルクリック。止めるときはこの窓で Ctrl+C か、窓を閉じる）
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"
IP=$(ipconfig getifaddr en0 2>/dev/null || echo "localhost")
# Tailscale（外からつなぐ仕組み）が入っていれば、その名前も出す
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
TSNAME=$([ -x "$TS" ] && "$TS" status --json 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null)
echo "============================================"
echo " AI技能コーチ を起動します"
echo " この Mac から : http://localhost:8000"
echo " スマホから    : http://$IP:8000 （同じ Wi-Fi）"
[ -n "$TSNAME" ] && echo " 外から        : http://$TSNAME:8000 （Tailscale をオンにした自分の端末だけ）"
echo " 止めるとき    : この窓で Ctrl + C"
echo "============================================"
# --reload：コードを直すと自動で再起動する（文字起こし中に直すと、その処理は止まる）
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
