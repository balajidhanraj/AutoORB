#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# AutoORB Multi-Environment VPS Bootstrapper (Ubuntu 22.04 / 24.04)
# Runs isolated Production (LIVE) + Dev (PAPER) containers behind Caddy HTTPS
# + PostgreSQL 16 on one VPS.
# ==============================================================================

BASE_DIR="${BASE_DIR:-/opt/autoorb}"
REPO_URL="${REPO_URL:-https://github.com/balajidhanraj/AutoORB.git}"
PROD_DOMAIN="${PROD_DOMAIN:-app.yourdomain.com}"
DEV_DOMAIN="${DEV_DOMAIN:-dev.yourdomain.com}"
ACME_EMAIL="${ACME_EMAIL:-admin@yourdomain.com}"

echo "=== 1. Setting Timezone to Asia/Kolkata (IST) ==="
sudo timedatectl set-timezone Asia/Kolkata || true

echo "=== 2. Installing Base Tools, Git, UFW & Docker Engine ==="
sudo apt-get update -y
sudo apt-get install -y git curl ca-certificates gnupg ufw openssl

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
fi

if ! docker compose version >/dev/null 2>&1; then
  sudo apt-get install -y docker-compose-plugin
fi

echo "=== 3. Configuring UFW Firewall (SSH + HTTP/HTTPS only) ==="
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable

echo "=== 4. Configuring 2GB Swap (if <4GB RAM & no swap active) ==="
TOTAL_RAM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "${TOTAL_RAM_MB:-0}" -lt 4096 ] && ! swapon --show | grep -q '/swapfile'; then
  sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  if ! grep -q '^/swapfile ' /etc/fstab; then
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  fi
fi

echo "=== 5. Preparing Multi-Env Directory Layout ($BASE_DIR) ==="
sudo mkdir -p "$BASE_DIR"
sudo chown -R "$USER:$USER" "$BASE_DIR"
mkdir -p "$BASE_DIR/volumes/prod/data" "$BASE_DIR/volumes/prod/logs" "$BASE_DIR/volumes/prod/builds"
mkdir -p "$BASE_DIR/volumes/dev/data" "$BASE_DIR/volumes/dev/logs" "$BASE_DIR/volumes/dev/builds"

if [ ! -d "$BASE_DIR/prod/.git" ]; then
  git clone -b main "$REPO_URL" "$BASE_DIR/prod"
fi

if [ ! -d "$BASE_DIR/dev/.git" ]; then
  if git ls-remote --heads "$REPO_URL" dev | grep -q refs/heads/dev; then
    git clone -b dev "$REPO_URL" "$BASE_DIR/dev"
  else
    git clone -b main "$REPO_URL" "$BASE_DIR/dev"
    git -C "$BASE_DIR/dev" checkout -b dev
  fi
fi

echo "=== 6. Generating Environment Files (.env.vps, .env.prod, .env.dev) ==="
PG_PASS=$(openssl rand -hex 24)
PROD_JWT=$(openssl rand -hex 32)
PROD_AES=$(openssl rand -hex 32)
DEV_JWT=$(openssl rand -hex 32)
DEV_AES=$(openssl rand -hex 32)

if [ ! -f "$BASE_DIR/.env.vps" ]; then
  cat > "$BASE_DIR/.env.vps" <<EOF
PROD_DOMAIN=$PROD_DOMAIN
DEV_DOMAIN=$DEV_DOMAIN
ACME_EMAIL=$ACME_EMAIL

POSTGRES_USER=autoorb
POSTGRES_PASSWORD=$PG_PASS

PROD_BUILD_CONTEXT=$BASE_DIR/prod
DEV_BUILD_CONTEXT=$BASE_DIR/dev
PROD_ENV_FILE=$BASE_DIR/.env.prod
DEV_ENV_FILE=$BASE_DIR/.env.dev
DATA_ROOT=$BASE_DIR/volumes
EOF
  chmod 600 "$BASE_DIR/.env.vps"
fi

# Read actual PG password if .env.vps already existed
EXISTING_PG_PASS=$(grep '^POSTGRES_PASSWORD=' "$BASE_DIR/.env.vps" | cut -d= -f2- || echo "$PG_PASS")

if [ ! -f "$BASE_DIR/.env.prod" ]; then
  cp "$BASE_DIR/prod/.env.prod.example" "$BASE_DIR/.env.prod"
  sed -i "s|CHANGE_ME_STRONG_PG_PASS|$EXISTING_PG_PASS|g" "$BASE_DIR/.env.prod"
  sed -i "s|CHANGE_ME_GENERATE_64_HEX_CHAR_PRODUCTION_JWT_SECRET|$PROD_JWT|g" "$BASE_DIR/.env.prod"
  sed -i "s|CHANGE_ME_GENERATE_64_HEX_CHAR_PRODUCTION_AES_KEY|$PROD_AES|g" "$BASE_DIR/.env.prod"
  sed -i "s|app.yourdomain.com|$PROD_DOMAIN|g" "$BASE_DIR/.env.prod"
  chmod 600 "$BASE_DIR/.env.prod"
fi

if [ ! -f "$BASE_DIR/.env.dev" ]; then
  cp "$BASE_DIR/dev/.env.dev.example" "$BASE_DIR/.env.dev"
  sed -i "s|CHANGE_ME_STRONG_PG_PASS|$EXISTING_PG_PASS|g" "$BASE_DIR/.env.dev"
  sed -i "s|CHANGE_ME_GENERATE_64_HEX_CHAR_DEV_JWT_SECRET|$DEV_JWT|g" "$BASE_DIR/.env.dev"
  sed -i "s|CHANGE_ME_GENERATE_64_HEX_CHAR_DEV_AES_KEY|$DEV_AES|g" "$BASE_DIR/.env.dev"
  sed -i "s|dev.yourdomain.com|$DEV_DOMAIN|g" "$BASE_DIR/.env.dev"
  chmod 600 "$BASE_DIR/.env.dev"
fi

PUBLIC_IP=$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')

echo ""
echo "======================================================================"
echo "AutoORB Multi-Env VPS Setup Complete!"
echo "VPS Public IP (whitelist this in Shoonya SEBI Static IP): $PUBLIC_IP"
echo "======================================================================"
echo "Next steps:"
echo "1. Point DNS A-records to $PUBLIC_IP:"
echo "   - $PROD_DOMAIN -> $PUBLIC_IP"
echo "   - $DEV_DOMAIN  -> $PUBLIC_IP"
echo "2. Edit $BASE_DIR/.env.vps (set real domains & email), $BASE_DIR/.env.prod (Shoonya credentials + UI_PIN), and $BASE_DIR/.env.dev."
echo "3. Start all containers (Postgres + Prod + Dev + Caddy HTTPS):"
echo "   cd $BASE_DIR/prod && docker compose -f docker-compose.vps.yml --env-file $BASE_DIR/.env.vps up -d --build"
echo "4. Verify health checks:"
echo "   curl -s http://127.0.0.1:8000/health   # Production"
echo "   curl -s http://127.0.0.1:8001/health   # Dev"
