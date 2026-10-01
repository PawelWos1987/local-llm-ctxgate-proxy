#!/bin/sh
# local-llm-ctxgate-proxy cutover: point goose custom_qwen provider at the proxy (:9200)
set -e
CFG=/home/user/.config/goose/custom_providers/custom_qwen.json
TS=$(date +%Y%m%d-%H%M%S)
BACKUP=${CFG}.bak-cutover-${TS}
cp "$CFG" "$BACKUP"
echo BACKUP=${BACKUP}
python3 - "$CFG" << 'PYEOF'
import sys, json
p = sys.argv[1]
d = json.load(open(p))
old = d.get('base_url')
d['base_url'] = 'http://127.0.0.1:9200/v1'
d['description'] = 'via local-llm-ctxgate-proxy :9200 (context assembly + PG memory + D9/D10/D11)'
json.dump(d, open(p,'w'), indent=2)
print('base_url:', old, '->', d['base_url'])
PYEOF
python3 -c "import json;print('NOW:',json.load(open('${CFG}'))['base_url'])"
echo CUTOVER_DONE