#!/usr/bin/env bash
# Fast health check. Fails loudly in seconds instead of hanging for minutes.
# Usage: ./diag.sh [turn_timeout_seconds]   (default 45)
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh" >/dev/null 2>&1; nvm use 26 >/dev/null 2>&1
TIMEOUT="${1:-45}"
OK="\033[32m✓\033[0m"; BAD="\033[31m✗\033[0m"; WARN="\033[33m!\033[0m"
fail=0

echo "── services ─────────────────────────────────"
if systemctl --user is-active --quiet openclaw-gateway.service; then
  echo -e " $OK gateway running"
else
  echo -e " $BAD gateway DOWN — systemctl --user start openclaw-gateway.service"; fail=1
fi
if curl -sf -m 3 http://127.0.0.1:11434/api/version >/dev/null; then
  echo -e " $OK ollama responding"
else
  echo -e " $BAD ollama unreachable on 11434"; fail=1
fi

echo "── whatsapp ─────────────────────────────────"
WA=$(timeout 25 openclaw channels list 2>/dev/null | grep -i whatsapp)
case "$WA" in
  *linked*) echo -e " $OK $WA" ;;
  "")       echo -e " $BAD whatsapp channel not reported"; fail=1 ;;
  *)        echo -e " $WARN $WA" ;;
esac
LOG=$(ls -t /tmp/openclaw/openclaw-*.log 2>/dev/null | head -1)
if [ -n "$LOG" ]; then
  IN=$(grep -o '"messagesHandled":[0-9]*' "$LOG" | tail -1 | cut -d: -f2)
  [ -n "$IN" ] && { [ "$IN" = "0" ] \
    && echo -e " $WARN inbound messages handled: 0 (nothing has arrived yet)" \
    || echo -e " $OK inbound messages handled: $IN"; }
fi

echo "── model ────────────────────────────────────"
PRIMARY=$(timeout 20 openclaw config get agents.defaults.model.primary 2>/dev/null | tr -d '"')
echo "   primary: ${PRIMARY:-unknown}"
MODEL="${PRIMARY##*/}"
curl -s -m 5 http://127.0.0.1:11434/api/ps | python3 -c "
import json,sys
ms=json.load(sys.stdin).get('models',[])
if not ms: print('   ! nothing resident — first call pays a cold load')
for m in ms: print(f\"   resident: {m['name']}  {m.get('size_vram',0)/1e9:.1f}GB\")" 2>/dev/null
nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/   gpu /'

echo "── raw model latency ────────────────────────"
RAW=$(curl -s -m 60 http://127.0.0.1:11434/api/chat -d "{\"model\":\"$MODEL\",\"stream\":false,\"think\":false,\"messages\":[{\"role\":\"user\",\"content\":\"Say ok\"}],\"keep_alive\":-1}")
echo "$RAW" | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('   ✗ raw model call FAILED'); sys.exit(1)
if 'error' in d: print('   ✗', d['error']); sys.exit(1)
tot=d.get('total_duration',0)/1e9; gen=d.get('eval_count',0)
print(f\"   {'✓' if tot<10 else '!'} raw call {tot:.1f}s, generated {gen} tokens\")
if gen>300: print(f'   ! model generated {gen} tokens for a 2-word answer — extended thinking is ON')
" || fail=1

echo "── end-to-end turn (timeout ${TIMEOUT}s) ─────"
echo "   NOTE: this uses \`openclaw agent\`, a COLD embedded run. The live"
echo "   WhatsApp path reuses warm gateway session state and is much faster."
echo "   Treat this as an upper bound, not the real reply latency."
MARK=$(date +"%Y-%m-%d %H:%M:%S")
START=$(date +%s)
RAWOUT=$(timeout "$TIMEOUT" openclaw agent --thinking off -m "Reply with exactly: ok" 2>&1)
RC=$?
OUT=$(printf '%s' "$RAWOUT" | tail -1)
DUR=$(( $(date +%s) - START ))
if [ $RC -eq 124 ]; then
  echo -e " $BAD TIMED OUT after ${DUR}s — no reply. Raise the timeout or fix the loop below."; fail=1
else
  echo -e " $OK ${DUR}s — reply: ${OUT:0:60}"
fi
CALLS=$(journalctl -u ollama --since "$MARK" --no-pager 2>/dev/null | grep -c 'POST .*chat')
ERRS=$(journalctl -u ollama --since "$MARK" --no-pager 2>/dev/null | grep -c 'no user query found')
TOKS=$(journalctl -u ollama --since "$MARK" --no-pager 2>/dev/null | grep -oE "eval time = *[0-9.]+ ms / *[0-9]+ tokens" | grep -oE '/ *[0-9]+' | tr -d '/ ' | paste -sd+ | bc 2>/dev/null)
echo "   model calls this turn: ${CALLS:-?}   generated tokens: ${TOKS:-?}"
[ "${CALLS:-0}" -gt 4 ] 2>/dev/null && echo -e "   $WARN >4 calls/turn: agent is looping through tools"
[ "${ERRS:-0}" -gt 0 ] 2>/dev/null && echo -e "   $BAD $ERRS x 'no user query found' — Ollama rejects tool-result continuations"

echo "─────────────────────────────────────────────"
[ $fail -eq 0 ] && echo -e " $OK healthy" || echo -e " $BAD problems above"
exit $fail
