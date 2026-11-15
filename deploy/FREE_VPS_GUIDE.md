# 24/7 Free Cloud VPS Deployment Guide (SEBI 2026 Static IP Ready)

To run the Shoonya ORB Algo 24/7 with **zero monthly cost** and a **Static Public IP** (required by SEBI 2026 Retail Algo guidelines for Shoonya API whitelisting), use **Oracle Cloud Infrastructure (OCI) Always Free** or **AWS Free Tier**.

---

## Option A: Oracle Cloud Always Free (Recommended — Truly Free Forever + Static IP)
1. **Create Account & Select India Region**:
   - Sign up for Oracle Cloud Free Tier and choose **India West (Mumbai)** or **India South (Hyderabad)** for lowest latency to NSE/Shoonya servers.
2. **Launch Always Free Compute VM**:
   - Image: **Ubuntu 22.04 or 24.04 Minimal**
   - Shape: `VM.Standard.E2.1.Micro` (1GB RAM + 2GB swap) **or** `VM.Standard.A1.Flex` (Ampere ARM — up to 4 OCPUs & 24GB RAM Always Free!).
   - Assign a **Reserved Public IPv4 Address** (Free in OCI) — register this exact IP with Shoonya for SEBI Static IP compliance.
3. **Firewall / Security List**:
   - Keep port `22` (SSH) restricted to your personal IP.
   - Do **not** expose port `8000` publicly to `0.0.0.0/0` — instead access the Web UI securely over an SSH tunnel (or allow port `8000` only from your static IP / VPN).

---

## Option B: AWS EC2 Free Tier (`ap-south-1` Mumbai)
1. Launch `t2.micro` / `t3.micro` Ubuntu 24.04 LTS in `ap-south-1` (Mumbai).
2. Allocate an **Elastic IP** and associate it with your EC2 instance (free while attached to a running instance).
3. Register the Elastic IP in Shoonya API compliance settings.

---

## Provisioning in One Command
SSH into your VPS and run:

```bash
curl -fsSL https://gitlab.com/balajiethiraj/autoorb/-/raw/main/deploy/setup_free_vps.sh | bash
```

Or clone manually and run:

```bash
git clone https://gitlab.com/balajiethiraj/autoorb.git ~/autoorb
cd ~/autoorb
bash deploy/setup_free_vps.sh
```

Then configure your secrets and start 24/7 execution:

```bash
nano ~/autoorb/.env
sudo systemctl start shoonya-orb
sudo systemctl status shoonya-orb
```

### Alternative: Run via Docker Compose
```bash
cd ~/autoorb
cp .env.example .env
# Edit .env
docker compose up -d --build
```

---

## Accessing the Live Web UI Safely
From your local Windows/Mac machine, create an encrypted SSH tunnel:

```bash
ssh -L 8000:127.0.0.1:8000 ubuntu@<YOUR_STATIC_VPS_IP>
```

Then open your browser at **http://127.0.0.1:8000** to view live ORB levels, VWAP, RSI(14), active positions, logs, and the **PANIC KILL SWITCH**.
