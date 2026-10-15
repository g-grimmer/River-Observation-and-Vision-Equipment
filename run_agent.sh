#!/bin/bash
# run_agent.sh
# Superviseur pour Agent Alpha 1.
# Relance automatiquement l'agent en cas de crash Python,
# sans nécessiter de reboot du Raspberry Pi.

set -u

# Détection dynamique du répertoire racine de l'agent (résout le problème de chemin hardcodé)
AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Détection automatique du venv (.venv ou venv)
if [ -d "${AGENT_DIR}/.venv" ]; then
    VENV_DIR="${AGENT_DIR}/.venv"
else
    VENV_DIR="${AGENT_DIR}/venv"
fi

LOG_FILE="${AGENT_DIR}/logs/supervisor.log"
MAX_RESTARTS=10          # Après ce nb de crashes consécutifs, on arrête
RESTART_DELAY_BASE=5     # Délai initial entre deux redémarrages (secondes)
RESTART_DELAY_MAX=120    # Délai max (backoff exponentiel)
MAX_LOG_SIZE_BYTES=$((20*1024*1024))  # Rotation au-delà de 20 Mo

# Rotation minimale de supervisor.log, indépendante du process Python (qui ne
# nettoie jamais ce fichier) : garde un seul fichier .1 en backup. Vérifiée à
# chaque itération du superviseur, pas seulement quand l'agent tourne normalement
# -- une boucle de crash rapide ne doit jamais empêcher cette rotation.
rotate_log_if_needed() {
    if [ -f "$LOG_FILE" ]; then
        local size
        size=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
        if [ "$size" -gt "$MAX_LOG_SIZE_BYTES" ]; then
            mv -f "$LOG_FILE" "${LOG_FILE}.1"
            echo "🔄 [$(date)] supervisor.log tourné (>$((MAX_LOG_SIZE_BYTES/1024/1024)) Mo) -> supervisor.log.1" | tee -a "$LOG_FILE"
        fi
    fi
}

echo "🚀 Superviseur démarré — $(date)" | tee -a "$LOG_FILE"

mkdir -p "${AGENT_DIR}/logs"
mkdir -p "${AGENT_DIR}/videos"

if [ -d "$VENV_DIR" ]; then
    source "${VENV_DIR}/bin/activate"
fi

restart_count=0
delay=$RESTART_DELAY_BASE

while true; do
    rotate_log_if_needed
    echo "▶️  [$(date)] Démarrage de l'agent (tentative $((restart_count+1)))..." | tee -a "$LOG_FILE"

    # Lance l'agent et capture le code de retour
    set -o pipefail
    python3 "${AGENT_DIR}/main.py" "$@" 2>&1 | tee -a "$LOG_FILE"
    exit_code=$?
    set +o pipefail

    # Exit code 0 = arrêt propre (Ctrl+C / SIGTERM) → on ne relance pas
    if [ $exit_code -eq 0 ]; then
        echo "✅ [$(date)] Agent arrêté proprement (exit 0)." | tee -a "$LOG_FILE"
        break
    fi

    restart_count=$((restart_count + 1))
    echo "💥 [$(date)] Agent crashé (exit $exit_code) — restart $restart_count/$MAX_RESTARTS dans ${delay}s..." | tee -a "$LOG_FILE"

    if [ $restart_count -ge $MAX_RESTARTS ]; then
        echo "🛑 [$(date)] Trop de crashes consécutifs ($MAX_RESTARTS). Arrêt du superviseur." | tee -a "$LOG_FILE"
        # Alerte de dernier recours écrite directement en bash, sans dépendre de
        # Python (qui vient justement de crasher $MAX_RESTARTS fois) : reprise et
        # uploadée par SFTPManager._rescan_orphaned_logs() au prochain démarrage
        # réussi (ex: après une correction manuelle ou un redéploiement).
        alert_file="${AGENT_DIR}/logs/ALERTE_supervisor_$(date +%Y%m%d_%H%M%S).txt"
        echo "[$(date -Iseconds)] Superviseur run_agent.sh a abandonné après ${MAX_RESTARTS} crashes consécutifs (dernier code de sortie : ${exit_code}). Intervention manuelle probablement nécessaire." > "$alert_file" 2>/dev/null
        # Code de sortie distinct (pas 0) : avant, ce chemin sortait toujours en
        # 0 (dernière commande réussie = cet echo), indiscernable d'un arrêt
        # volontaire. Restart=always de systemd relance de toute façon quel que
        # soit le code, mais un code clair reste utile pour le diagnostic
        # (journalctl, monitoring externe éventuel).
        exit 2
    fi

    sleep "$delay"

    # Backoff exponentiel plafonné à RESTART_DELAY_MAX
    delay=$(( delay * 2 ))
    if [ "$delay" -gt "$RESTART_DELAY_MAX" ]; then
        delay=$RESTART_DELAY_MAX
    fi
done
