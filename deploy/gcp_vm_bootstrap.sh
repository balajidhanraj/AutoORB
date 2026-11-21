#!/usr/bin/env bash
# Startup bootstrap script for AutoORB on Google Cloud Always Free e2-micro (Ubuntu 22.04)
set -euo pipefail

# 1. Configure 2 GB swap so 1 GB RAM e2-micro runs Python + Chromium reliably
if [[ ! -f /swapfile ]]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# 2. Set IST timezone
timedatectl set-timezone Asia/Kolkata || true

# 3. Install Docker & Git
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y ca-certificates curl gnupg git
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi

# 4. Clone or update AutoORB repository
if [[ ! -d /opt/autoorb/.git ]]; then
  git clone https://github.com/balajidhanraj/AutoORB.git /opt/autoorb
else
  git -C /opt/autoorb pull --ff-only origin main || true
fi

mkdir -p /opt/autoorb/data /opt/autoorb/logs

# 5. Create 1-command launcher /opt/autoorb/start_gcp.sh
cat <<'EOF' > /opt/autoorb/start_gcp.sh
#!/usr/bin/env bash
set -euo pipefail
cd /opt/autoorb
git pull --ff-only origin main || true
if [[ ! -f /opt/autoorb/.env ]]; then
  echo "ERROR: /opt/autoorb/.env not found. Paste your Neon + Shoonya .env first: sudo nano /opt/autoorb/.env"
  exit 1
fi
docker build -t autoorb:latest .
docker rm -f autoorb >/dev/null 2>&1 || true
docker run -d \
  --name autoorb \
  --restart unless-stopped \
  --env-file /opt/autoorb/.env \
  -e TZ=Asia/Kolkata \
  -e HEADLESS_BROWSER=true \
  -e APP_ENV=production \
  -e PORT=8000 \
  -p 80:8000 \
  -p 8000:8000 \
  -v /opt/autoorb/data:/app/data \
  -v /opt/autoorb/logs:/app/logs \
  autoorb:latest
echo "AutoORB container started! Checking /health..."
sleep 4
curl -fsS http://127.0.0.1:8000/health && echo ""
EOF
chmod +x /opt/autoorb/start_gcp.sh

# Auto-start if .env is already present
if [[ -f /opt/autoorb/.env ]]; then
  /opt/autoorb/start_gcp.sh
fi
