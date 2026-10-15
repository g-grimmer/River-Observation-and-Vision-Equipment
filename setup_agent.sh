#!/bin/bash

# ==============================================================================
# Agent Alpha 1 Test Terrain - Deployment Script (Pi 5 / Headless)
# ==============================================================================
# This script automates the installation of the specialized terrain agent.
# ==============================================================================

# --- Colors ---
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# --- Banner ---
clear
echo -e "${BLUE}"
echo "   _____                            _     "
echo "  |_   _|                          (_)    "
echo "    | | ___ _ __ _ __ __ _ _ _ __  _ ___ "
echo "    | |/ _ \ '__| '__/ _' | | '_ \| / __|"
echo "    | |  __/ |  | | | (_| | | | | | \__ \\"
echo "    \_/\___|_|  |_|  \__,_|_|_| |_|_|___/"
echo "                                          "
echo -e "${NC}"
echo -e "${YELLOW}Agent Alpha 1 Test Terrain - Deployment Script${NC}"
echo "========================================="

# --- 1. Checks ---
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}Please run as root (sudo ./setup_agent.sh)${NC}"
  exit 1
fi

echo -e "${BLUE}>>> Checking Environment...${NC}"
WORK_DIR=$(pwd)
if [ ! -f "main.py" ]; then
    echo -e "${RED}Error: Run this script from the 'agent_alpha_1_test_terrain' folder.${NC}"
    exit 1
fi

# --- 2. Configuration Wizard ---
echo ""
echo "========================================="
echo "       CONFIGURATION WIZARD"
echo "========================================="

# -> Agent Identity
echo -e "\n${YELLOW}--- Agent Identity ---${NC}"
CURRENT_HOSTNAME=$(hostname)
read -p "New Agent Name (for Hostname) [$CURRENT_HOSTNAME]: " NEW_HOSTNAME
if [ ! -z "$NEW_HOSTNAME" ] && [ "$NEW_HOSTNAME" != "$CURRENT_HOSTNAME" ]; then
    echo -e "${BLUE}Hostname will be changed to '${NEW_HOSTNAME}'...${NC}"
    hostnamectl set-hostname "$NEW_HOSTNAME"
    sed -i "s/$CURRENT_HOSTNAME/$NEW_HOSTNAME/g" /etc/hosts
    CURRENT_HOSTNAME=$NEW_HOSTNAME
fi

# -> NAS Config (Optional Update)
echo -e "\n${YELLOW}--- NAS / SFTP Configuration ---${NC}"
# Defaut OUI : les valeurs sont vides dans le depot (voir utils/configs.py),
# repondre non produit une station qui enregistre mais ne transmet jamais.
read -p "Renseigner les identifiants NAS / SFTP dans utils/configs.py ? [Y/n]: " UPDATE_CONFIG
UPDATE_CONFIG=${UPDATE_CONFIG:-Y}
if [[ "$UPDATE_CONFIG" =~ ^[Yy]$ ]]; then
    read -p "NAS IP Address: " NAS_HOST
    read -p "NAS Username: " NAS_USER
    echo -n "NAS Password: "
    read -s NAS_PASS
    echo ""
    read -p "NAS Remote Path [/backups/agent_terrain]: " NAS_PATH
    NAS_PATH=${NAS_PATH:-"backups/agent_terrain"}

    CONFIG_FILE="utils/configs.py"
    if [ -f "$CONFIG_FILE" ]; then
        sed -i "s/SFTP_HOST = .*/SFTP_HOST = \"$NAS_HOST\"/" $CONFIG_FILE
        sed -i "s/SFTP_USERNAME = .*/SFTP_USERNAME = \"$NAS_USER\"/" $CONFIG_FILE
        sed -i "s/SFTP_PASSWORD = .*/SFTP_PASSWORD = \"$NAS_PASS\"/" $CONFIG_FILE
        sed -i "s/SFTP_REMOTE_DIR = .*/SFTP_REMOTE_DIR = \"$NAS_PATH\"/" $CONFIG_FILE
        echo -e "${GREEN}✓ Updated $CONFIG_FILE${NC}"
    else
        echo -e "${RED}Warning: $CONFIG_FILE not found, skipping update.${NC}"
    fi
fi

# -> Agent Mode Config
echo -e "\n${YELLOW}--- Mode d'upload 4G ---${NC}"
echo "1) Mode Vidéo           — Upload vidéo complète si détections, supprime sinon (comportement original)"
echo "2) Mode Snapshot        — JPEG annoté par détection uniquement, pas de vidéo (×100 moins de données 4G)"
echo "3) Mode Vidéo + Snapshot — Vidéo enregistrée en local (carte SD) + JPEG uploadé sur 4G par détection"
read -p "Sélectionnez le mode d'upload [2]: " AGENT_MODE_CHOICE
AGENT_MODE_CHOICE=${AGENT_MODE_CHOICE:-2}

EXTRA_ARGS=""
if [ "$AGENT_MODE_CHOICE" -eq 1 ]; then
    EXTRA_ARGS="--upload-mode video"
    echo -e "${GREEN}✓ Mode Vidéo sélectionné.${NC}"
    # Option keep-all uniquement disponible en mode vidéo
    echo -e "\n${YELLOW}--- Option conservation vidéo ---${NC}"
    echo "1) Filtrage intelligent : supprime les vidéos sans détections (économise de la bande passante)"
    echo "2) Conservation systématique : envoie toutes les vidéos même sans détections"
    read -p "Sélectionnez [1]: " KEEP_ALL_CHOICE
    KEEP_ALL_CHOICE=${KEEP_ALL_CHOICE:-1}
    if [ "$KEEP_ALL_CHOICE" -eq 2 ]; then
        EXTRA_ARGS="--upload-mode video --keep-all"
        echo -e "${GREEN}✓ Conservation systématique activée.${NC}"
    else
        echo -e "${GREEN}✓ Filtrage intelligent activé.${NC}"
    fi
elif [ "$AGENT_MODE_CHOICE" -eq 3 ]; then
    EXTRA_ARGS="--upload-mode video+snapshot"
    echo -e "${GREEN}✓ Mode Vidéo + Snapshot sélectionné (vidéo locale, JPEG sur 4G).${NC}"
else
    EXTRA_ARGS="--upload-mode snapshot"
    echo -e "${GREEN}✓ Mode Snapshot sélectionné (JPEG uniquement sur 4G).${NC}"
fi

# -> GSM / 4G Connection Config (Optional)
echo -e "\n${YELLOW}--- Configuration Connexion GSM / 4G ---${NC}"
read -p "Activer et configurer la connexion GSM / 4G ? (y/N): " SETUP_GSM
if [[ "$SETUP_GSM" =~ ^[Yy]$ ]]; then
    echo -e "${BLUE}>>> Installation de NetworkManager et ModemManager...${NC}"
    apt-get update
    apt-get install network-manager modemmanager -y
    
    read -p "APN de votre carte SIM [sl2sfr]: " APN_NAME
    APN_NAME=${APN_NAME:-"sl2sfr"}
    
    read -p "Code PIN de votre carte SIM (laisser vide si aucun) []: " SIM_PIN
    
    echo -e "${BLUE}>>> Configuration de la connexion GSM avec nmcli...${NC}"
    # Suppression de l'ancienne connexion pour éviter les doublons
    nmcli connection delete "4G-Mobile" >/dev/null 2>&1
    
    # Création de la connexion GSM
    nmcli connection add type gsm ifname "*" con-name "4G-Mobile" apn "$APN_NAME"
    
    # Configuration (Pas de PIN, connexion automatique, métrique de route et timeout)
    nmcli connection modify "4G-Mobile" \
        gsm.pin "$SIM_PIN" \
        connection.autoconnect yes \
        ipv4.route-metric 10 \
        connection.wait-device-timeout 30
        
    echo -e "${GREEN}✓ Connexion GSM '4G-Mobile' configurée avec succès (APN: $APN_NAME).${NC}"
    
    # Note: Le script bash check_4g.sh a été retiré car il entre en conflit avec le watchdog Python.
fi

# --- 4. Optimisation de l'alimentation USB ---
echo -e "\n${YELLOW}--- Optimisation de l'alimentation USB ---${NC}"
echo -e "${BLUE}>>> Débridage du courant USB pour le modem 4G...${NC}"
CONFIG_MODIFIED=0
for CFG_FILE in "/boot/firmware/config.txt" "/boot/config.txt"; do
    if [ -f "$CFG_FILE" ]; then
        if ! grep -q "^max_usb_current=1" "$CFG_FILE"; then
            echo "max_usb_current=1" >> "$CFG_FILE"
        fi
        if ! grep -q "^usb_max_current_enable=1" "$CFG_FILE"; then
            echo "usb_max_current_enable=1" >> "$CFG_FILE"
        fi
        echo -e "${GREEN}✓ Limites d'alimentation USB débridées dans $CFG_FILE.${NC}"
        echo -e "${RED}⚠ Un REDÉMARRAGE du Raspberry Pi est nécessaire pour appliquer ce changement.${NC}"
        echo -e "${YELLOW}  Tant qu'il n'a pas eu lieu, l'ancienne limite de courant USB reste active.${NC}"
        CONFIG_MODIFIED=1
        break # Modifie le bon fichier de config (firmware a la priorité sur les OS récents)
    fi
done
if [ "$CONFIG_MODIFIED" -eq 0 ]; then
    echo -e "${YELLOW}⚠ Fichier config.txt introuvable, impossible de débrider l'alimentation USB.${NC}"
fi

# --- 5. Dépendances système (rsync + sshpass) ---
echo -e "\n${BLUE}>>> Installation des dépendances système...${NC}"
# rsync est généralement pré-installé sur Raspberry Pi OS.
# sshpass permet à rsync d'utiliser une authentification par mot de passe
# sans intervention manuelle — requis pour l'upload automatique vers le NAS.
apt-get install -y rsync sshpass 2>/dev/null
if command -v rsync &>/dev/null && command -v sshpass &>/dev/null; then
    echo -e "${GREEN}✓ rsync + sshpass installés. Mode upload rsync activé.${NC}"
else
    echo -e "${YELLOW}⚠ rsync/sshpass non disponibles. Fallback sur SFTP paramiko.${NC}"
fi

# --- 5b. Pile caméra (picamera2 / libcamera) ---
# Installée par apt et NON par pip : les liaisons Python de libcamera sont
# construites contre la libcamera du système et ne sont pas publiées sur PyPI.
# Le paquet picamera2 de PyPI ne déclare même pas libcamera dans ses dépendances,
# un `pip install picamera2` donnerait donc un module qui échoue à l'import.
# C'est pour rendre ce paquet apt visible depuis le venv que celui-ci est créé
# avec --system-site-packages (section 6).
# Sans picamera2, camera/camera_threads.py bascule sur sa caméra simulée, dont la
# configuration à deux flux échoue : l'agent tourne alors en boucle
# « caméra indisponible » sans jamais rien enregistrer.
echo -e "\n${BLUE}>>> Installation de la pile caméra (picamera2)...${NC}"
apt-get install -y python3-picamera2 2>/dev/null
if python3 -c "import picamera2" 2>/dev/null; then
    echo -e "${GREEN}✓ picamera2 disponible.${NC}"
else
    echo -e "${RED}✗ picamera2 indisponible — l'agent ne pourra pas acquérir d'images.${NC}"
    echo -e "${YELLOW}  Installer manuellement : sudo apt-get install python3-picamera2${NC}"
fi

# --- 6. Python Environment ---
echo -e "\n${BLUE}>>> Setting up Python Environment...${NC}"
VENV_DIR=".venv"
if [ -d "$VENV_DIR" ]; then rm -rf "$VENV_DIR"; fi

# Create venv with system site packages for picamera2/libcamera
python3 -m venv --system-site-packages $VENV_DIR
source $VENV_DIR/bin/activate

echo "Installing Python Libraries..."
pip install --upgrade pip
pip install -r requirements.txt

# --- 6b. Création et permissions des dossiers de données ---
# Le script tourne en root mais le service tourne sous USER_NAME.
# On crée explicitement les dossiers et on les attribue au bon utilisateur
# pour éviter les PermissionError au démarrage du service.
USER_NAME_TMP=$SUDO_USER
if [ -z "$USER_NAME_TMP" ]; then USER_NAME_TMP=$(logname); fi

echo -e "\n${BLUE}>>> Création des dossiers de données (logs/ et videos/)...${NC}"
mkdir -p "${WORK_DIR}/logs"
mkdir -p "${WORK_DIR}/videos"
chown -R "${USER_NAME_TMP}:${USER_NAME_TMP}" "${WORK_DIR}/logs"
chown -R "${USER_NAME_TMP}:${USER_NAME_TMP}" "${WORK_DIR}/videos"
chown -R "${USER_NAME_TMP}:${USER_NAME_TMP}" "${WORK_DIR}/${VENV_DIR}"
echo -e "${GREEN}✓ Dossiers logs/ et videos/ créés et attribués à '${USER_NAME_TMP}'.${NC}"

# --- 6c. Configuration des droits sudo pour le Watchdog Modem ---
# Le watchdog Python a besoin de manipuler l'interface réseau (ip link) et d'éteindre/rallumer le port USB
# du modem via /sys (écrit avec `tee`, jamais via un shell). La règle est restreinte à ces
# commandes précises — jamais /bin/bash — pour qu'une faille dans une dépendance Python ne
# devienne pas une élévation de privilèges root complète.
echo -e "\n${BLUE}>>> Configuration des permissions sudo restreintes pour le Watchdog...${NC}"
SUDOERS_FILE="/etc/sudoers.d/agent_terrain_watchdog"
SUDOERS_TMP="$(mktemp)"
cat > "$SUDOERS_TMP" <<EOF
# Autorisations pour le watchdog de l'Agent Alpha 1 (strict nécessaire uniquement)
${USER_NAME_TMP} ALL=(root) NOPASSWD: /usr/sbin/ip link set * up, /usr/sbin/ip link set * down, /usr/bin/ip link set * up, /usr/bin/ip link set * down, /usr/bin/tee /sys/bus/usb/devices/*/authorized, /usr/sbin/reboot, /sbin/reboot
EOF
if visudo -c -f "$SUDOERS_TMP" >/dev/null 2>&1; then
    install -m 0440 "$SUDOERS_TMP" "$SUDOERS_FILE"
    echo -e "${GREEN}✓ Droits sudo restreints configurés pour '${USER_NAME_TMP}' dans ${SUDOERS_FILE}.${NC}"
else
    echo -e "${RED}✗ Erreur de syntaxe dans la règle sudoers générée — rien n'a été installé.${NC}"
    echo -e "${YELLOW}  Le watchdog modem n'aura pas les droits nécessaires tant que ce n'est pas corrigé.${NC}"
fi
rm -f "$SUDOERS_TMP"

# --- 6. Service Creation ---
echo -e "\n${BLUE}>>> Configuring Systemd Service...${NC}"
SERVICE_FILE="/etc/systemd/system/agent_terrain.service"
USER_NAME=$SUDO_USER
if [ -z "$USER_NAME" ]; then USER_NAME=$(logname); fi

cat > $SERVICE_FILE <<EOF
[Unit]
Description=Agent Alpha 1 Test Terrain
After=network.target

[Service]
User=${USER_NAME}
WorkingDirectory=${WORK_DIR}
ExecStart=/usr/bin/bash ${WORK_DIR}/run_agent.sh ${EXTRA_ARGS}
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable agent_terrain.service
echo -e "${GREEN}✓ Service agent_terrain.service created and enabled.${NC}"

# S'assurer que le répertoire de travail est accessible au service
chown "${USER_NAME}:${USER_NAME}" "${WORK_DIR}" 2>/dev/null || true

# --- 7. Final Summary ---
MY_IP=$(hostname -I | cut -d' ' -f1)

echo ""
echo "========================================="
echo -e "       ${GREEN}INSTALLATION COMPLETE${NC}"
echo "========================================="
echo -e "Agent Name   : ${YELLOW}${CURRENT_HOSTNAME}${NC}"
echo -e "Local IP     : ${YELLOW}${MY_IP}${NC}"
if [ "$AGENT_MODE_CHOICE" -eq 1 ]; then
    if echo "$EXTRA_ARGS" | grep -q "keep-all"; then
        echo -e "Upload Mode  : ${YELLOW}Vidéo (Conservation systématique)${NC}"
    else
        echo -e "Upload Mode  : ${YELLOW}Vidéo (Filtrage intelligent)${NC}"
    fi
elif [ "$AGENT_MODE_CHOICE" -eq 3 ]; then
    echo -e "Upload Mode  : ${YELLOW}Vidéo locale + Snapshot 4G${NC}"
else
    echo -e "Upload Mode  : ${YELLOW}Snapshot JPEG uniquement${NC}"
fi

if [[ "$SETUP_GSM" =~ ^[Yy]$ ]]; then
    echo -e "GSM / 4G     : ${YELLOW}Configuré (APN: $APN_NAME)${NC}"
else
    echo -e "GSM / 4G     : ${YELLOW}Ignoré / Non modifié${NC}"
fi
echo "-----------------------------------------"
echo -e "${YELLOW}IMPORTANT NEXT STEPS:${NC}"
if [ "$CONFIG_MODIFIED" -eq 1 ]; then
    echo -e "1. ${RED}sudo reboot${NC} ${YELLOW}(requis pour appliquer les nouvelles limites d'alimentation USB)${NC}"
    echo "2. Start the Agent:    ${BLUE}sudo systemctl start agent_terrain${NC}"
    echo "3. Monitor Logs:       ${BLUE}sudo journalctl -u agent_terrain -f${NC}"
else
    echo "1. Start the Agent:    ${BLUE}sudo systemctl start agent_terrain${NC}"
    echo "2. Monitor Logs:       ${BLUE}sudo journalctl -u agent_terrain -f${NC}"
fi
echo "========================================="
