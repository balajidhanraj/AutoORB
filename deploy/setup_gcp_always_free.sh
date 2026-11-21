#!/usr/bin/env bash
# AutoORB — 1-Command Google Cloud Always Free (e2-micro) Provisioner
# Run this directly inside Google Cloud Shell (https://shell.cloud.google.com)
# Cost: $0/month under GCP Always Free (1x e2-micro in us-central1, 30GB pd-standard)

set -euo pipefail

REGION="${GCP_REGION:-us-central1}"
ZONE="${GCP_ZONE:-us-central1-a}"
VM_NAME="${GCP_VM_NAME:-autoorb-vm}"
IP_NAME="${GCP_IP_NAME:-autoorb-static-ip}"
FW_NAME="allow-autoorb-web"

echo "=== [1/5] Checking active Google Cloud project ==="
PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
if [[ -z "$PROJECT_ID" || "$PROJECT_ID" == "(unset)" ]]; then
  echo "ERROR: No GCP project selected. Run: gcloud config set project YOUR_PROJECT_ID"
  exit 1
fi
echo "Using GCP Project: $PROJECT_ID (Region: $REGION, Zone: $ZONE)"

echo "=== [2/5] Enabling Compute Engine API ==="
gcloud services enable compute.googleapis.com --quiet

echo "=== [3/5] Reserving Static External IPv4 ($IP_NAME) ==="
if ! gcloud compute addresses describe "$IP_NAME" --region="$REGION" >/dev/null 2>&1; then
  gcloud compute addresses create "$IP_NAME" \
    --region="$REGION" \
    --network-tier=STANDARD \
    --quiet
fi
STATIC_IP="$(gcloud compute addresses describe "$IP_NAME" --region="$REGION" --format='get(address)')"
echo ">>> Dedicated Static IPv4: $STATIC_IP"

echo "=== [4/5] Opening Firewall Ports (80, 443, 8000) ==="
if ! gcloud compute firewall-rules describe "$FW_NAME" >/dev/null 2>&1; then
  gcloud compute firewall-rules create "$FW_NAME" \
    --allow=tcp:80,tcp:443,tcp:8000 \
    --target-tags=autoorb-server \
    --description="Allow HTTP/HTTPS/8000 for AutoORB Terminal" \
    --quiet
fi

echo "=== [5/5] Creating Always Free e2-micro VM ($VM_NAME) ==="
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOOTSTRAP_FILE="$SCRIPT_DIR/gcp_vm_bootstrap.sh"
if [[ ! -f "$BOOTSTRAP_FILE" ]]; then
  curl -fsSL "https://raw.githubusercontent.com/balajidhanraj/AutoORB/main/deploy/gcp_vm_bootstrap.sh" -o /tmp/gcp_vm_bootstrap.sh
  BOOTSTRAP_FILE="/tmp/gcp_vm_bootstrap.sh"
fi

if ! gcloud compute instances describe "$VM_NAME" --zone="$ZONE" >/dev/null 2>&1; then
  gcloud compute instances create "$VM_NAME" \
    --zone="$ZONE" \
    --machine-type=e2-micro \
    --image-family=ubuntu-2204-lts \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size=30GB \
    --boot-disk-type=pd-standard \
    --network-tier=STANDARD \
    --address="$STATIC_IP" \
    --tags=autoorb-server,http-server,https-server \
    --metadata-from-file=startup-script="$BOOTSTRAP_FILE" \
    --quiet
else
  echo "VM $VM_NAME already exists in $ZONE."
fi

echo ""
echo "=================================================================="
echo "  AUTOORB GOOGLE CLOUD VM CREATED SUCCESSFULLY!"
echo "=================================================================="
echo "  1. Dedicated Static IPv4 (Whitelist in Shoonya Profile -> API Key):"
echo "       $STATIC_IP"
echo ""
echo "  2. Wait ~2 minutes for Docker to finish installing on the VM, then SSH in:"
echo "       gcloud compute ssh $VM_NAME --zone=$ZONE"
echo ""
echo "  3. Inside the VM, paste your .env file and start AutoORB 24/7:"
echo "       sudo nano /opt/autoorb/.env"
echo "       sudo /opt/autoorb/start_gcp.sh"
echo ""
echo "  4. Open your 24/7 AutoORB Terminal:"
echo "       http://$STATIC_IP:8000   (and http://$STATIC_IP)"
echo "=================================================================="
