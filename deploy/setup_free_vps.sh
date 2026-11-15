#!/usr/bin/env bash
set -euo pipefail

# Automated provisioning script for Ubuntu 22.04 / 24.04 Free Cloud VPS
# Supports Oracle Cloud Always Free (Mumbai/Hyderabad) & AWS EC2 Free Tier

APP_DIR="${APP_DIR:-$HOME/autoorb}"
REPO_URL="${REPO_URL:-https://gitlab.com/balajiethiraj/autoorb.git}"

echo "=== 1. Setting timezone to Asia/Kolkata (IST) ==="
sudo timedatectl set-timezone Asia/Kolkata

echo "=== 2. Updating system packages & installing Python 3.11, Chromium & Docker ==="
sudo apt-get update -y
sudo apt-get install -y git curl ca-certificates gnupg software-properties-common

if ! command -v python3.11 >/dev/null 2>&1; then
  sudo add-apt-repository -y ppa:deadsnakes/ppa
  sudo apt-get update -y
  sudo apt-get install -y python3.11 python3.11-venv python3.11-dev
fi

sudo apt-get install -y chromium-browser chromium-chromedriver || sudo apt-get install -y chromium chromium-driver

echo "=== 3. Configuring 2GB Swap (recommended for 1GB RAM free tier instances) ==="
if ! swapon --show | grep -q '/swapfile'; then
  sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

echo "=== 4. Cloning repository ==="
if [ ! -d "$APP_DIR" ]; then
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"

python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example — fill in Shoonya & Telegram credentials before starting!"
fi

echo "=== 5. Installing systemd service ==="
sudo cp deploy/shoonya-orb.service /etc/systemd/system/shoonya-orb.service
sudo sed -i "s|/home/ubuntu/autoorb|$APP_DIR|g" /etc/systemd/system/shoonya-orb.service
sudo sed -i "s|User=ubuntu|User=$USER|g" /etc/systemd/system/shoonya-orb.service
sudo systemctl daemon-reload
sudo systemctl enable shoonya-orb

echo "=== Setup Complete ==="
echo "1. Edit $APP_DIR/.env with your Shoonya credentials and SEBI_ALGO_ID."
echo "2. Start 24/7 daemon: sudo systemctl start shoonya-orb"
echo "3. View logs: journalctl -u shoonya-orb -f"
echo "4. Open Web UI via SSH tunnel: ssh -L 8000:127.0.0.1:8000 $USER@<VPS_STATIC_IP>"
