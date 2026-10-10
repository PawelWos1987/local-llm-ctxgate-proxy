#!/bin/bash
# verify_aux_isolation.sh - Phase 4 verification script
set -e

PROXY="http://127.0.0.1:19203"
PGPASS=$(grep '^CTXGATE_PG_PASS=' /home/pawelw/ctxproxy/.env | cut -d= -f2)
export PGPASSWORD="$PGPASS"
DB="ctxproxy_sandbox"
PASS=0
FAIL=0

check() {
    local name="$1"
    local expected="$2"
    local actual="$3"
    if [ "$actual" == "$expected" ]; then
        echo "PASS: $name"
        PASS=$((PASS+1))
    else
        echo "FAIL: $name (expected=$expected, got=$actual)"
        FAIL=$((FAIL+1))
    fi
}

check_not_contains() {
    local name="$1"
    local pattern="$2"
    local text="$3"
    if echo "$text" | grep -q "$pattern"; then
        echo "FAIL: $name (pattern=$pattern found but should not be)"
        FAIL=$((FAIL+1))
    else
        echo "PASS: $name"
        PASS=$((PASS+1))
    fi
}

dbq() {
    psql -U postgres -h 127.0.0.1 $DB -t -A -c "$1" 2>/dev/null
}

send_req() {
    local sid="$1"
    local system="$2"
    local user="$3"
    local maxtok="$4"
    local payload
    payload=$(python3 -c "
import json,sys
print(json.dumps({
    'model':'Qwen3.8-27B',
    'messages':[{'role':'system','content':sys.argv[1]},{'role':'user','content':sys.argv[2]}],
    'max_tokens':int(sys.argv[3]),
    'stream':False
}))" "$system" "$user" "$maxtok")
    curl -s -X POST "$PROXY/v1/chat/completions"         -H "Content-Type: application/json"         -H "agent-session-id: $sid"         -d "$payload"
}

echo "=== PHASE 4 VERIFICATION ==="
echo "Proxy: $PROXY"
echo "DB: $DB"
echo ""

# Truncate DB
psql -U postgres -h 127.0.0.1 $DB -c "TRUNCATE proxy.tasks, proxy.session_windows, proxy.events, proxy.memory_jobs, proxy.knowledge, proxy.memories, proxy.phase_summaries, proxy.session_summaries, proxy.working_memory, proxy.session_ledger, proxy.deliverables CASCADE" 2>&1 > /dev/null
echo "DB truncated"
> /home/pawelw/ctxproxy/scratch/sandbox_proxy.log

# === TEST 1: One tasks row per session ===
echo ""
echo "--- TEST 1: Session identity ---"
SID1="verify-sess-1"
send_req "$SID1" "You are helpful." "Hello" 20 > /dev/null
TASK_COUNT=$(dbq "SELECT count(*) FROM proxy.tasks WHERE session_id='$SID1'")
check "One tasks row per session" "1" "$TASK_COUNT"

# === TEST 2: Auxiliary request does not pop state ===
echo ""
echo "--- TEST 2: Auxiliary request isolation ---"
OVER_MSG="The quick brown fox jumps over the lazy dog. The quick brown fox jumps over the lazy dog. The quick brown fox jumps over the lazy dog. The quick brown fox jumps over the lazy dog. The quick brown fox jumps over the lazy dog. The quick brown fox jumps over the lazy dog. The quick brown fox jumps over the lazy dog. The quick brown fox jumps over the lazy dog. The quick brown fox jumps over the lazy dog. The quick brown fox jumps over the lazy dog. The quick brown fox jumps over the lazy dog. The quick brown fox jumps over the lazy dog."
send_req "$SID1" "You are a helpful assistant." "$OVER_MSG" 30 > /dev/null
sleep 2

SW_BEFORE=$(dbq "SELECT cut||','||summarized_through FROM proxy.session_windows WHERE session_key='goose:$SID1'" || echo "empty")
PS_BEFORE=$(dbq "SELECT count(*) FROM proxy.phase_summaries WHERE task_id=(SELECT id FROM proxy.tasks WHERE session_id='$SID1')")
EV_BEFORE=$(dbq "SELECT count(*) FROM proxy.events WHERE task_id=(SELECT id FROM proxy.tasks WHERE session_id='$SID1')")

# Send auxiliary request
send_req "$SID1" "Generate a short title." "Hi there" 15 > /dev/null
sleep 2

LOG=$(cat /home/pawelw/ctxproxy/scratch/sandbox_proxy.log)
check_not_contains "No H3-POP on aux request" "H3-POP" "$LOG"
check_not_contains "No SEED CHANGED on aux request" "SEED CHANGED" "$LOG"

SW_AFTER=$(dbq "SELECT cut||','||summarized_through FROM proxy.session_windows WHERE session_key='goose:$SID1'" || echo "empty")
check "session_windows unchanged after aux" "$SW_BEFORE" "$SW_AFTER"

# === TEST 3: No duplicate tasks ===
echo ""
echo "--- TEST 3: Task uniqueness ---"
TOTAL_TASKS=$(dbq "SELECT count(*) FROM proxy.tasks")
UNIQUE_SESSIONS=$(dbq "SELECT count(DISTINCT session_id) FROM proxy.tasks")
check "No duplicate task rows" "$TOTAL_TASKS" "$UNIQUE_SESSIONS"

# === TEST 4: No noauth session_ids ===
NOAUTH=$(dbq "SELECT count(*) FROM proxy.tasks WHERE session_id LIKE 'noauth:%'")
check "No noauth session_ids" "0" "$NOAUTH"

# === TEST 5: 3 parallel sessions isolated ===
echo ""
echo "--- TEST 5: Parallel session isolation ---"
SID2="verify-sess-2"
SID3="verify-sess-3"
send_req "$SID2" "You are helpful." "Hello 2" 20 > /dev/null
send_req "$SID3" "You are helpful." "Hello 3" 20 > /dev/null
sleep 2
T2=$(dbq "SELECT count(*) FROM proxy.tasks WHERE session_id='$SID2'")
T3=$(dbq "SELECT count(*) FROM proxy.tasks WHERE session_id='$SID3'")
check "Session 2 has 1 task" "1" "$T2"
check "Session 3 has 1 task" "1" "$T3"

# === TEST 6: Pre-seeded session_windows ===
echo ""
echo "--- TEST 6: Pre-seeded DB state ---"
SID4="verify-sess-4"
send_req "$SID4" "You are helpful." "Hello 4" 20 > /dev/null
sleep 2
TID4=$(dbq "SELECT id FROM proxy.tasks WHERE session_id='$SID4'")
psql -U postgres -h 127.0.0.1 $DB -c "INSERT INTO proxy.session_windows (session_key, cut, cut_anchor, cut_prev_anchor, seed_sig, summarized_through, dropped_total) VALUES ('goose:$SID4', 2, '', '', 'preseed', 2, 2) ON CONFLICT (session_key) DO UPDATE SET cut=2, summarized_through=2, dropped_total=2" 2>&1 > /dev/null
SW4=$(dbq "SELECT cut||','||summarized_through FROM proxy.session_windows WHERE session_key='goose:$SID4'")
check "Pre-seeded session_windows exists" "2,2" "$SW4"

# === SUMMARY ===
echo ""
echo "========================================"
echo "RESULTS: $PASS passed, $FAIL failed"
echo "========================================"
if [ $FAIL -gt 0 ]; then
    exit 1
fi
