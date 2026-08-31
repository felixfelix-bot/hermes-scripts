#!/bin/bash
# t_ae90f911 post-transition evidence capture — scheduled 2026-08-16 03:16 IST.
# Read-only (+ the one task-mandated 16-token completion probe). Protects evidence
# against journald vacuum (user journal lost everything before 05:38 on Aug 15).
export XDG_RUNTIME_DIR=/run/user/$(id -u)
OUT=/home/c03rad0r/.hermes/kanban/boards/admin/workspaces/t_ae90f911/post-transition-evidence.txt
{
echo "=== POST-TRANSITION CAPTURE $(date -Is) ==="
echo "--- [A] state-sync status (want: inactive/dead, 0/SUCCESS; NOT 203/EXEC) ---"
systemctl --user status hermes-state-sync.service --no-pager -l
echo; echo "--- [B] state-sync journal tonight ---"
journalctl --user -u hermes-state-sync.service --since '2026-08-16 02:55' --no-pager
echo; echo "--- [C] zai-proxy status (want: active since ~03:10) ---"
systemctl --user status zai-proxy.service --no-pager -l | head -25
systemctl --user show zai-proxy.service -p ActiveEnterTimestamp,MainPID
echo; echo "--- [D] proxy-restart cron log ---"
tail -5 /home/c03rad0r/.hermes/scripts/proxy-restart.log 2>&1
echo; echo "--- [E] PPQ policy env in NEW process ---"
NEWPID=$(pgrep -f 'bot/zai_proxy.py' | head -1)
echo "pid=$NEWPID"; tr '\0' '\n' < /proc/$NEWPID/environ | grep -E '^PPQ|^ZAI|^DEEPINFRA|^OPENROUTER'
echo; echo "--- [F] /quota policy fields ---"
curl -s --max-time 10 localhost:9099/quota
echo; echo "--- [G] live glm completion through 9099 ---"
curl -s --max-time 60 localhost:9099/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"glm-4.5-flash","max_tokens":16,"messages":[{"role":"user","content":"Reply with exactly: R1-POST-OK"}]}'
echo; echo "--- [H] breaker log tail ---"
tail -5 /home/c03rad0r/.hermes/scripts/circuit-breaker.log
echo "=== END CAPTURE ==="
} >> "$OUT" 2>&1
echo "captured -> $OUT"
tail -60 "$OUT"
