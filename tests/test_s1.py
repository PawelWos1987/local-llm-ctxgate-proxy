import json, sys
sys.path.insert(0, '/home/pawelw/ctxproxy-dev/proxy')
import app
with open('/home/pawelw/ctxproxy-dev/tests/real_session_20261006_31.json') as f:
    msgs = json.load(f)
entries = app._extract_ledger_entries(msgs, 0, len(msgs), None)
kinds = {}
for e in entries:
    kinds[e['kind']] = kinds.get(e['kind'], 0) + 1
print(f'Total: {len(entries)} entries, kinds: {kinds}')
probes = ['cv-fmea-20261007.md','cv-implementation-plan-20261007.md','pii-residual-scan-20261007.md','NOTES.md','cv-architecture-master-20261006.md','cvparser-flow-20261006.md','matcher-flow-20261006.md','bff-frontend-flow-20261006.md']
found = 0
for p in probes:
    ok = any(p in e.get('title','') or p in e.get('detail','') for e in entries)
    if ok: found += 1
    status = 'Y' if ok else 'N'
    print(f'  {status} {p}')
print(f'S1: {found}/{len(probes)}')
print()
print('ARTIFACTS:')
for e in entries:
    if e['kind'] == 'ARTIFACT':
        print(f'  {e["title"]}')
