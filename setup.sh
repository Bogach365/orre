#!/usr/bin/env bash
# =============================================================================
# OREE monitoring — host setup for Hetzner CAX21 (Ubuntu 24.04 LTS, ARM64)
# Run as root on a FRESH server: bash setup.sh
# =============================================================================
set -euo pipefail

# ---- 0. Variables -----------------------------------------------------------
NEW_USER="oree"                         # non-root user to create
TIMEZONE="Europe/Kyiv"
# SSH key is auto-copied from root's authorized_keys (you already logged in with it)

echo ">>> [1/8] System update"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get upgrade -y -qq

echo ">>> [2/8] Timezone + basic tools"
timedatectl set-timezone "$TIMEZONE"
apt-get install -y -qq git curl htop tmux ufw fail2ban ca-certificates gnupg unattended-upgrades

echo ">>> [3/8] Create non-root user with sudo"
if [ ! -s /root/.ssh/authorized_keys ]; then
    echo "ERROR: /root/.ssh/authorized_keys is empty or missing."
    echo "Cannot continue — the new user would have no way to log in."
    echo "Aborting before any SSH hardening so you don't lock yourself out."
    exit 1
fi
if ! id "$NEW_USER" &>/dev/null; then
    adduser --disabled-password --gecos "" "$NEW_USER"
    usermod -aG sudo "$NEW_USER"
    mkdir -p /home/$NEW_USER/.ssh
    # Reuse the same SSH key you already used to log in as root
    cp /root/.ssh/authorized_keys /home/$NEW_USER/.ssh/authorized_keys
    chmod 700 /home/$NEW_USER/.ssh
    chmod 600 /home/$NEW_USER/.ssh/authorized_keys
    chown -R $NEW_USER:$NEW_USER /home/$NEW_USER/.ssh
fi

echo ">>> [4/8] SSH hardening (key-only, no root login)"
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh

echo ">>> [5/8] Firewall (allow SSH only; dashboards via SSH tunnel)"
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable

echo ">>> [6/8] fail2ban (block brute-force SSH)"
systemctl enable --now fail2ban

echo ">>> [7/8] Automatic security updates"
dpkg-reconfigure -f noninteractive unattended-upgrades

echo ">>> [8/8] Docker + Compose plugin"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
usermod -aG docker "$NEW_USER"
# Python for the collector
apt-get install -y -qq python3 python3-venv python3-pip

echo ""
echo "============================================================"
echo " DONE. Next:"
echo "  1. Log out, log back in as: ssh $NEW_USER@<server-ip>"
echo "  2. Verify: docker ps   (should work without sudo)"
echo "  3. Deploy the stack (see docker-compose.yml + runbook)"
echo "============================================================"
