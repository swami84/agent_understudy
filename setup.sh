#!/usr/bin/env bash
# Interactive first-run setup. Picks a model provider, writes config from the
# templates in config/examples/, and validates the result.
#
#   ./setup.sh                 # interactive
#   ./setup.sh --provider anthropic --no-apply
set -uo pipefail
cd "$(dirname "$0")"
REPO="$(pwd)"
PROVIDER=""; APPLY=1
while [ $# -gt 0 ]; do
  case "$1" in
    --provider) PROVIDER="${2:-}"; shift ;;
    --no-apply) APPLY=0 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
  esac; shift
done

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad() { printf '  \033[31m✗\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$1"; }

say "1. Prerequisites"
NODE_OK=0
if command -v node >/dev/null 2>&1; then
  NV=$(node --version | sed 's/^v//'); MAJ=${NV%%.*}
  if [ "$MAJ" -ge 22 ]; then ok "node $NV"; NODE_OK=1; else bad "node $NV — OpenClaw needs 22.22.3+ / 24.15+ / 25.9+ (26 recommended)"; fi
else bad "node not found"; fi
command -v python3 >/dev/null 2>&1 && ok "python3 $(python3 -V | cut -d' ' -f2)" || bad "python3 not found"
if command -v openclaw >/dev/null 2>&1; then ok "openclaw $(openclaw --version 2>/dev/null | head -1)"
else
  bad "openclaw not found"
  echo "      npm install -g --allow-scripts=openclaw,@google/genai,koffi,tree-sitter-bash,protobufjs openclaw@latest"
  echo "      (without --allow-scripts the bundled-plugin postinstall never runs)"
fi
[ "$NODE_OK" = 1 ] || { echo; bad "fix Node first, then re-run"; exit 1; }

say "2. Model provider"
if [ -z "$PROVIDER" ]; then
  echo "  1) ollama     — local models, no API key, nothing leaves the machine"
  echo "  2) anthropic  — Claude API  (needs ANTHROPIC_API_KEY)"
  echo "  3) openai     — OpenAI API  (needs OPENAI_API_KEY)"
  printf "  choose [1-3]: "; read -r c
  case "$c" in 1) PROVIDER=ollama ;; 2) PROVIDER=anthropic ;; 3) PROVIDER=openai ;; *) bad "invalid"; exit 1 ;; esac
fi
SRC="config/examples/00-provider.${PROVIDER}.json5"
[ -f "$SRC" ] || { bad "no template for provider '$PROVIDER'"; exit 1; }
ok "provider: $PROVIDER"

case "$PROVIDER" in
  anthropic) [ -n "${ANTHROPIC_API_KEY:-}" ] && ok "ANTHROPIC_API_KEY is set" || warn "ANTHROPIC_API_KEY is not set — export it before starting the gateway" ;;
  openai)    [ -n "${OPENAI_API_KEY:-}" ]    && ok "OPENAI_API_KEY is set"    || warn "OPENAI_API_KEY is not set — export it before starting the gateway" ;;
  ollama)
    curl -sf -m 3 http://127.0.0.1:11434/api/version >/dev/null 2>&1 \
      && ok "ollama reachable on :11434" || warn "ollama not reachable on :11434 — install from ollama.com"
    warn "reasoning models need the no-think proxy: systemctl --user enable --now ollama-nothink.service"
    ;;
esac

say "3. Writing config"
mkdir -p config
cp "$SRC" config/00-provider.json5;               ok "config/00-provider.json5"
cp config/examples/10-guardrails.json5 config/;   ok "config/10-guardrails.json5"
sed "s#<REPO_PATH>#${REPO}#g" config/examples/30-memory.json5 > config/30-memory.json5
ok "config/30-memory.json5  (paths -> ${REPO})"
if [ ! -f config/20-whatsapp.json5 ]; then
  cp config/examples/20-whatsapp.json5 config/
  warn "config/20-whatsapp.json5 written — edit every <PLACEHOLDER> before applying it"
fi
[ -f config/identity.json ] || cp config/examples/identity.json.example config/identity.json
[ -f config/dm-allow.txt ]  || cp config/examples/dm-allow.txt.example  config/dm-allow.txt

if [ "$APPLY" = 1 ] && command -v openclaw >/dev/null 2>&1; then
  say "4. Applying (provider + guardrails + memory only)"
  for f in config/00-provider.json5 config/10-guardrails.json5 config/30-memory.json5; do
    if openclaw config patch --file "$f" --dry-run >/dev/null 2>&1; then
      openclaw config patch --file "$f" >/dev/null 2>&1 && ok "applied $(basename "$f")"
    else
      bad "$(basename "$f") failed validation — run: openclaw config patch --file $f --dry-run"
    fi
  done
  say "5. Invariants"
  python3 guard/invariants.py || true
else
  say "4. Skipped apply (--no-apply)"
fi

say "Next"
cat <<'NEXT'
  • WhatsApp:  edit config/20-whatsapp.json5, then
      openclaw config patch --file config/20-whatsapp.json5 --dry-run
      openclaw channels login --channel whatsapp     (needs a TTY; scan the QR)
  • Guards:    systemctl --user enable --now understudy-guard.timer
  • Panel:     python3 web/server.py     -> http://127.0.0.1:8765
  • Health:    ./diag.sh
  • Read guard/README.md before enabling any group.
NEXT
