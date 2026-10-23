#!/bin/bash
# run_agent.sh
# Supervisor for Agent Alpha 1.
# Automatically restarts the agent after a Python crash,
# without requiring a reboot of the Raspberry Pi.

set -u

# Dynamic detection of the agent root directory (solves the hardcoded path problem)
AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Automatic detection of the virtual environment (.venv or venv)
if [ -d "${AGENT_DIR}/.venv" ]; then
    VENV_DIR="${AGENT_DIR}/.venv"
else
    VENV_DIR="${AGENT_DIR}/venv"
fi

LOG_FILE="${AGENT_DIR}/logs/supervisor.log"
MAX_RESTARTS=10          # Past this many consecutive crashes, give up
RESTART_DELAY_BASE=5     # Initial delay between two restarts (seconds)
RESTART_DELAY_MAX=120    # Maximum delay (exponential backoff)
MAX_LOG_SIZE_BYTES=$((20*1024*1024))  # Rotate beyond 20 MB

# Minimal rotation of supervisor.log, independent of the Python process (which
# never cleans this file): keeps a single .1 backup. Checked on every supervisor
# iteration, not only when the agent runs normally -- a fast crash loop must
# never prevent this rotation.
rotate_log_if_needed() {
    if [ -f "$LOG_FILE" ]; then
        local size
        size=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
        if [ "$size" -gt "$MAX_LOG_SIZE_BYTES" ]; then
            mv -f "$LOG_FILE" "${LOG_FILE}.1"
            echo "🔄 [$(date)] supervisor.log rotated (>$((MAX_LOG_SIZE_BYTES/1024/1024)) MB) -> supervisor.log.1" | tee -a "$LOG_FILE"
        fi
    fi
}

echo "🚀 Supervisor started — $(date)" | tee -a "$LOG_FILE"

mkdir -p "${AGENT_DIR}/logs"
mkdir -p "${AGENT_DIR}/videos"

if [ -d "$VENV_DIR" ]; then
    source "${VENV_DIR}/bin/activate"
fi

restart_count=0
delay=$RESTART_DELAY_BASE

while true; do
    rotate_log_if_needed
    echo "▶️  [$(date)] Starting the agent (attempt $((restart_count+1)))..." | tee -a "$LOG_FILE"

    # Run the agent and capture its return code
    set -o pipefail
    python3 "${AGENT_DIR}/main.py" "$@" 2>&1 | tee -a "$LOG_FILE"
    exit_code=$?
    set +o pipefail

    # Exit code 0 = clean shutdown (Ctrl+C / SIGTERM) -> do not restart
    if [ $exit_code -eq 0 ]; then
        echo "✅ [$(date)] Agent stopped cleanly (exit 0)." | tee -a "$LOG_FILE"
        break
    fi

    restart_count=$((restart_count + 1))
    echo "💥 [$(date)] Agent crashed (exit $exit_code) — restart $restart_count/$MAX_RESTARTS in ${delay}s..." | tee -a "$LOG_FILE"

    if [ $restart_count -ge $MAX_RESTARTS ]; then
        echo "🛑 [$(date)] Too many consecutive crashes ($MAX_RESTARTS). Supervisor giving up." | tee -a "$LOG_FILE"
        # Last-resort alert written directly in bash, without depending on Python
        # (which has just crashed $MAX_RESTARTS times): picked up and uploaded by
        # SFTPManager._rescan_orphaned_logs() at the next successful start
        # (e.g. after a manual fix or a redeployment).
        alert_file="${AGENT_DIR}/logs/ALERTE_supervisor_$(date +%Y%m%d_%H%M%S).txt"
        echo "[$(date -Iseconds)] The run_agent.sh supervisor gave up after ${MAX_RESTARTS} consecutive crashes (last exit code: ${exit_code}). Manual intervention is probably required." > "$alert_file" 2>/dev/null
        # Distinct exit code (not 0): before, this path always exited 0 (the last
        # command being that echo), indistinguishable from a deliberate stop.
        # systemd's Restart=always restarts regardless of the code, but a clear
        # code remains useful for diagnosis (journalctl, external monitoring).
        exit 2
    fi

    sleep "$delay"

    # Exponential backoff, capped at RESTART_DELAY_MAX
    delay=$(( delay * 2 ))
    if [ "$delay" -gt "$RESTART_DELAY_MAX" ]; then
        delay=$RESTART_DELAY_MAX
    fi
done
