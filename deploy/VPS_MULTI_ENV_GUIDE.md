# AutoORB Multi-Environment VPS Hosting Guide (Production + Dev on 1 Indian VPS)

This guide sets up **isolated Production (`LIVE`)** and **Development/Staging (`PAPER`)** instances of **AutoORB** on a single Ubuntu 22.04 / 24.04 VPS (recommended: Mumbai/Bengaluru region, **4 GB+ RAM**, Static Public IPv4), using **Docker Compose**, **PostgreSQL 16**, **Caddy automatic HTTPS**, and **GitHub Actions CI/CD**.

---

## Architecture Overview

| Component | Container | Internal Port | Database | Purpose |
|---|---|---|---|---|
| **Reverse Proxy** | `autoorb-caddy` | `80`, `443` | — | Auto Let's Encrypt HTTPS for `PROD_DOMAIN` and `DEV_DOMAIN` |
| **Production App** | `autoorb-prod` | `127.0.0.1:8000` | `autoorb_prod` | `MODE=LIVE`, `APP_ENV=production` (`main` branch) |
| **Dev / Paper App** | `autoorb-dev` | `127.0.0.1:8001` | `autoorb_dev` | `MODE=PAPER`, `APP_ENV=development` (`dev` branch) |
| **Database** | `autoorb-postgres` | `postgres:5432` (internal only) | `autoorb_prod` + `autoorb_dev` | Multi-user accounts, encrypted broker configs & trade logs |

Both environments maintain completely separate:
- Git checkouts (`/opt/autoorb/prod` on `main`, `/opt/autoorb/dev` on `dev`)
- Environment files (`/opt/autoorb/.env.prod`, `/opt/autoorb/.env.dev`)
- JWT & AES-256 encryption keys
- PostgreSQL databases (`autoorb_prod` vs `autoorb_dev`)
- Persistent data/logs/builds folders (`/opt/autoorb/volumes/prod` vs `/opt/autoorb/volumes/dev`)

---

## 1. Point DNS & Whitelist SEBI Static IP

1. Provision an **Ubuntu 24.04** VPS in **Mumbai** or **Bengaluru** (Hostinger KVM 2, DigitalOcean, Vultr, AWS Lightsail, or Oracle Cloud Always Free).
2. Create two **DNS A records** pointing to your VPS Static IPv4:
   - `app.yourdomain.com` → `<VPS_STATIC_IP>`
   - `dev.yourdomain.com` → `<VPS_STATIC_IP>`
3. Register `<VPS_STATIC_IP>` in your **Finvasia Shoonya API portal** for SEBI 2026 Static IP compliance.

---

## 2. Bootstrap the VPS (One-Time Setup)

SSH into your VPS (`ssh ubuntu@<VPS_STATIC_IP>` or `root@<VPS_STATIC_IP>`) and run:

```bash
export PROD_DOMAIN="app.yourdomain.com"
export DEV_DOMAIN="dev.yourdomain.com"
export ACME_EMAIL="you@yourdomain.com"

curl -fsSL https://raw.githubusercontent.com/balajidhanraj/AutoORB/main/deploy/setup_vps_multi_env.sh | bash
```

If you were newly added to the `docker` group, log out and SSH back in once (`exit`, then reconnect).

---

## 3. Configure Secrets & Launch

1. Inspect `/opt/autoorb/.env.vps`:
   ```bash
   nano /opt/autoorb/.env.vps
   ```
   Confirm `PROD_DOMAIN`, `DEV_DOMAIN`, and `ACME_EMAIL`.

2. Fill in **Production** credentials in `/opt/autoorb/.env.prod` (never commit this file):
   ```bash
   nano /opt/autoorb/.env.prod
   ```
   Set `SHOONYA_USER_ID`, `SHOONYA_PASSWORD`, `SHOONYA_TOTP_SECRET`, `SHOONYA_API_KEY`, `SHOONYA_SECRET_KEY`, `UI_PIN`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID`. Note that `JWT_SECRET`, `APP_ENCRYPTION_KEY`, and `DATABASE_URL` were already pre-populated with strong random secrets.

3. Review `/opt/autoorb/.env.dev` (`MODE=PAPER` by default).

4. Start the full stack:
   ```bash
   cd /opt/autoorb/prod
   docker compose -f docker-compose.vps.yml --env-file /opt/autoorb/.env.vps up -d --build
   ```

5. Verify health checks:
   ```bash
   curl -s http://127.0.0.1:8000/health
   curl -s http://127.0.0.1:8001/health
   curl -s https://app.yourdomain.com/health
   curl -s https://dev.yourdomain.com/health
   ```

---

## 4. Enable GitHub Actions Auto-Deploy & Branch Android Builds

Add these **Repository Secrets** (`Settings → Secrets and variables → Actions → Secrets`):
- `VPS_HOST` — Your VPS Static Public IP
- `VPS_USER` — SSH username (`ubuntu` or `root`)
- `VPS_SSH_KEY` — Private SSH key matching `~/.ssh/authorized_keys` on the VPS
- `VPS_PORT` — SSH port (default `22`)

Add these **Repository Variables** (`Settings → Secrets and variables → Actions → Variables`):
- `PROD_API_URL` — `https://app.yourdomain.com`
- `DEV_API_URL` — `https://dev.yourdomain.com`

### Branch Workflow
- **Push to `dev`** → runs `pytest -q`, deploys cleanly to `autoorb-dev` (`https://dev.yourdomain.com`), and builds the `AutoORB-dev-debug-apk` Android APK pointed at `DEV_API_URL`.
- **Push to `main`** → runs `pytest -q`, deploys cleanly to `autoorb-prod` (`https://app.yourdomain.com`), and builds `AutoORB-debug-apk` + `AutoORB-playstore-aab` pointed at `PROD_API_URL`.

> Inside the Android APK (or `/app`), you can also switch between **PROD**, **DEV**, or a custom LAN URL anytime from **Server Connection** on the login screen or Controls tab.

---

## 5. Operations, Logs & Database Backups

- **Tail logs:**
  ```bash
  docker logs -f autoorb-prod --tail 100
  docker logs -f autoorb-dev --tail 100
  docker logs -f autoorb-caddy --tail 50
  ```
- **Daily PostgreSQL backup (optional cron):**
  ```bash
  docker exec autoorb-postgres pg_dump -U autoorb autoorb_prod > /opt/autoorb/backup_prod_$(date +%F).sql
  docker exec autoorb-postgres pg_dump -U autoorb autoorb_dev > /opt/autoorb/backup_dev_$(date +%F).sql
  ```
