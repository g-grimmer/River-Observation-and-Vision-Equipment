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
echo "  _____   ____  __      __ ______ "
echo " |  __ \ / __ \ \ \    / /|  ____|"
echo " | |__) | |  | | \ \  / / | |__   "
echo " |  _  /| |  | |  \ \/ /  |  __|  "
echo " | | \ \| |__| |   \  /   | |____ "
echo " |_|  \_\\____/     \/    |______|"
echo "                                          "
echo -e "${NC}"
echo -e "${YELLOW}Agent - Deployment Script${NC}"
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
echo -e "\n${YELLOW}--- 4G upload mode ---${NC}"
echo "1) Video mode           - upload the full video if there were detections, delete it otherwise (original behaviour)"
echo "2) Snapshot mode        - annotated JPEG per detection only, no video (x100 less 4G data)"
echo "3) Video + Snapshot mode - video recorded locally (SD card) + JPEG uploaded over 4G per detection"
read -p "Select the upload mode [2]: " AGENT_MODE_CHOICE
AGENT_MODE_CHOICE=${AGENT_MODE_CHOICE:-2}

EXTRA_ARGS=""
if [ "$AGENT_MODE_CHOICE" -eq 1 ]; then
    EXTRA_ARGS="--upload-mode video"
    echo -e "${GREEN}✓ Video mode selected.${NC}"
    # The keep-all option only exists in video mode
    echo -e "\n${YELLOW}--- Video retention option ---${NC}"
    echo "1) Smart filtering: delete the videos without detections (saves bandwidth)"
    echo "2) Keep everything: send every video, even without detections"
    read -p "Select [1]: " KEEP_ALL_CHOICE
    KEEP_ALL_CHOICE=${KEEP_ALL_CHOICE:-1}
    if [ "$KEEP_ALL_CHOICE" -eq 2 ]; then
        EXTRA_ARGS="--upload-mode video --keep-all"
        echo -e "${GREEN}✓ Keep everything enabled.${NC}"
    else
        echo -e "${GREEN}✓ Smart filtering enabled.${NC}"
    fi
elif [ "$AGENT_MODE_CHOICE" -eq 3 ]; then
    EXTRA_ARGS="--upload-mode video+snapshot"
    echo -e "${GREEN}✓ Video + Snapshot mode selected (local video, JPEG over 4G).${NC}"
else
    EXTRA_ARGS="--upload-mode snapshot"
    echo -e "${GREEN}✓ Snapshot mode selected (JPEG over 4G only).${NC}"
fi

# -> GSM / 4G Connection Config (Optional)
echo -e "\n${YELLOW}--- GSM / 4G connection setup ---${NC}"
read -p "Enable and configure the GSM / 4G connection? (y/N): " SETUP_GSM
if [[ "$SETUP_GSM" =~ ^[Yy]$ ]]; then
    echo -e "${BLUE}>>> Installing NetworkManager and ModemManager...${NC}"
    apt-get update
    apt-get install network-manager modemmanager -y
    
    read -p "APN of your SIM card [sl2sfr]: " APN_NAME
    APN_NAME=${APN_NAME:-"sl2sfr"}
    
    read -p "PIN code of your SIM card (leave blank if none) []: " SIM_PIN
    
    echo -e "${BLUE}>>> Configuring the GSM connection with nmcli...${NC}"
    # Delete the previous connection, to avoid duplicates
    nmcli connection delete "4G-Mobile" >/dev/null 2>&1
    
    # Create the GSM connection
    nmcli connection add type gsm ifname "*" con-name "4G-Mobile" apn "$APN_NAME"
    
    # Settings (no PIN, autoconnect, route metric and timeout)
    nmcli connection modify "4G-Mobile" \
        gsm.pin "$SIM_PIN" \
        connection.autoconnect yes \
        ipv4.route-metric 10 \
        connection.wait-device-timeout 30
        
    echo -e "${GREEN}✓ GSM connection '4G-Mobile' configured successfully (APN: $APN_NAME).${NC}"
    
    # Note: the check_4g.sh bash script was removed, it conflicted with the Python watchdog.
fi

# --- 4. Optimisation de l'alimentation USB ---
echo -e "\n${YELLOW}--- USB power optimisation ---${NC}"
echo -e "${BLUE}>>> Lifting the USB current limits for the 4G modem...${NC}"
CONFIG_MODIFIED=0
for CFG_FILE in "/boot/firmware/config.txt" "/boot/config.txt"; do
    if [ -f "$CFG_FILE" ]; then
        if ! grep -q "^max_usb_current=1" "$CFG_FILE"; then
            echo "max_usb_current=1" >> "$CFG_FILE"
        fi
        if ! grep -q "^usb_max_current_enable=1" "$CFG_FILE"; then
            echo "usb_max_current_enable=1" >> "$CFG_FILE"
        fi
        echo -e "${GREEN}✓ USB power limits lifted in $CFG_FILE.${NC}"
        echo -e "${RED}⚠ A REBOOT of the Raspberry Pi is required to apply this change.${NC}"
        echo -e "${YELLOW}  Until it happens, the previous USB current limit stays in force.${NC}"
        CONFIG_MODIFIED=1
        break # Edit the right config file (firmware takes precedence on recent OS releases)
    fi
done
if [ "$CONFIG_MODIFIED" -eq 0 ]; then
    echo -e "${YELLOW}⚠ config.txt not found, cannot lift the USB power limits.${NC}"
fi

# --- 5. System dependencies (rsync + sshpass) ---
echo -e "\n${BLUE}>>> Installing the system dependencies...${NC}"
# rsync is generally pre-installed on Raspberry Pi OS.
# sshpass lets rsync use password authentication without manual intervention --
# required for the automatic upload to the NAS.
apt-get install -y rsync sshpass 2>/dev/null
if command -v rsync &>/dev/null && command -v sshpass &>/dev/null; then
    echo -e "${GREEN}✓ rsync + sshpass installed. rsync upload mode enabled.${NC}"
else
    echo -e "${YELLOW}⚠ rsync/sshpass unavailable. Falling back to paramiko SFTP.${NC}"
fi

# --- 5b. Camera stack (picamera2 / libcamera) ---
# Installed through apt and NOT through pip: the Python bindings of libcamera
# are built against the system libcamera and are not published on PyPI.
# The PyPI picamera2 package does not even declare libcamera among its
# dependencies, so a `pip install picamera2` would yield a module that fails to
# import. It is to make this apt package visible from the venv that the latter
# is created with --system-site-packages (section 6).
# Without picamera2, camera/camera_threads.py falls back to its mock camera,
# whose two-stream configuration fails: the agent then loops on
# "camera unavailable" without ever recording anything.
echo -e "\n${BLUE}>>> Installing the camera stack (picamera2)...${NC}"
apt-get install -y python3-picamera2 2>/dev/null
if python3 -c "import picamera2" 2>/dev/null; then
    echo -e "${GREEN}✓ picamera2 available.${NC}"
else
    echo -e "${RED}✗ picamera2 unavailable -- the agent will not be able to acquire images.${NC}"
    echo -e "${YELLOW}  Install it manually: sudo apt-get install python3-picamera2${NC}"
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

# --- 6b. Creation and permissions of the data directories ---
# The script runs as root but the service runs as USER_NAME.
# The directories are created explicitly and assigned to the right user, to
# avoid PermissionError at service start-up.
USER_NAME_TMP=$SUDO_USER
if [ -z "$USER_NAME_TMP" ]; then USER_NAME_TMP=$(logname); fi

echo -e "\n${BLUE}>>> Creating the data directories (logs/ and videos/)...${NC}"
mkdir -p "${WORK_DIR}/logs"
mkdir -p "${WORK_DIR}/videos"
chown -R "${USER_NAME_TMP}:${USER_NAME_TMP}" "${WORK_DIR}/logs"
chown -R "${USER_NAME_TMP}:${USER_NAME_TMP}" "${WORK_DIR}/videos"
chown -R "${USER_NAME_TMP}:${USER_NAME_TMP}" "${WORK_DIR}/${VENV_DIR}"
echo -e "${GREEN}✓ Directories logs/ and videos/ created and assigned to '${USER_NAME_TMP}'.${NC}"

# --- 6c. Configuration des droits sudo pour le Watchdog Modem ---
# The Python watchdog needs to manipulate the network interface (ip link) and to power the
# modem USB port off and on through /sys (written with `tee`, never through a shell). The rule
# is restricted to those precise commands -- never /bin/bash -- so that a flaw in a Python
# dependency does not become a full root privilege escalation.
echo -e "\n${BLUE}>>> Configuring the restricted sudo permissions for the watchdog...${NC}"
SUDOERS_FILE="/etc/sudoers.d/agent_terrain_watchdog"
SUDOERS_TMP="$(mktemp)"
cat > "$SUDOERS_TMP" <<EOF
# Permissions for the Agent Alpha 1 watchdog (strict minimum only)
${USER_NAME_TMP} ALL=(root) NOPASSWD: /usr/sbin/ip link set * up, /usr/sbin/ip link set * down, /usr/bin/ip link set * up, /usr/bin/ip link set * down, /usr/bin/tee /sys/bus/usb/devices/*/authorized, /usr/sbin/reboot, /sbin/reboot
EOF
if visudo -c -f "$SUDOERS_TMP" >/dev/null 2>&1; then
    install -m 0440 "$SUDOERS_TMP" "$SUDOERS_FILE"
    echo -e "${GREEN}✓ Restricted sudo rights configured for '${USER_NAME_TMP}' in ${SUDOERS_FILE}.${NC}"
else
    echo -e "${RED}✗ Syntax error in the generated sudoers rule -- nothing was installed.${NC}"
    echo -e "${YELLOW}  The modem watchdog will lack the required rights until this is fixed.${NC}"
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

# Make sure the working directory is reachable by the service
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
        echo -e "Upload Mode  : ${YELLOW}Video (keep everything)${NC}"
    else
        echo -e "Upload Mode  : ${YELLOW}Video (smart filtering)${NC}"
    fi
elif [ "$AGENT_MODE_CHOICE" -eq 3 ]; then
    echo -e "Upload Mode  : ${YELLOW}Local video + 4G snapshot${NC}"
else
    echo -e "Upload Mode  : ${YELLOW}JPEG snapshot only${NC}"
fi

if [[ "$SETUP_GSM" =~ ^[Yy]$ ]]; then
    echo -e "GSM / 4G     : ${YELLOW}Configured (APN: $APN_NAME)${NC}"
else
    echo -e "GSM / 4G     : ${YELLOW}Skipped / unchanged${NC}"
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
