#!/usr/bin/env bash
# Full corpus build: exports -> session-chunked markdown -> profile cards -> index.
#
#   1. On your phone: open a chat -> ⋮ / contact name -> Export chat -> WITHOUT MEDIA
#   2. Get the .txt or .zip into corpus/raw/  (email/Drive to yourself, or scp)
#   3. ./ingest/run.sh
set -euo pipefail
cd "$(dirname "$0")/.."
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh" >/dev/null 2>&1; nvm use 26 >/dev/null 2>&1
MIN_MSGS="${1:-30}"

shopt -s nullglob
FILES=(corpus/raw/*.txt corpus/raw/*.zip)
if [ ${#FILES[@]} -eq 0 ]; then
  echo "No chat exports in corpus/raw/."
  echo
  echo "  Export from WhatsApp on your phone:"
  echo "    open the chat -> ⋮ (or tap the name) -> Export chat -> Without media"
  echo "  then put the .txt/.zip in corpus/raw/ and re-run."
  echo
  echo "  (contacts.csv there is your address book, not a chat export.)"
  exit 1
fi

echo "── 1/6  parsing ${#FILES[@]} export(s) ──"
python3 ingest/parse_export.py "${FILES[@]}"

echo "── 2/6  building profile cards (local GPU, slow) ──"
python3 ingest/build_profiles.py --min-messages "$MIN_MSGS"

echo "── 3/6  building group context cards ──"
python3 ingest/build_group_context.py

echo "── 4/6  applying contact names ──"
CONTACTS=(corpus/raw/*.vcf corpus/raw/*.csv)
if [ ${#CONTACTS[@]} -gt 0 ]; then
  python3 ingest/import_contacts.py "${CONTACTS[@]}" --apply || true
else
  echo "  (no address book in corpus/raw/ — skipping)"
fi

echo "── 5/6  building dated timelines ──"
python3 ingest/build_temporal.py

echo "── 6/6  reindexing ──"
openclaw memory index --force --agent main

echo
echo "done. corpus now holds:"
for d in chats people groups timeline; do
  printf "  corpus/%-7s %s file(s)\n" "$d" "$(find "corpus/$d" -type f 2>/dev/null | wc -l)"
done
echo
echo "after this, use ingest/refresh.py — it rebuilds only what changed."
echo
echo "try:  openclaw memory search \"what does <person> usually talk about\" --agent main"
