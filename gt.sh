#!/bin/bash
# ============================================================
# GT Application Deployment Script  — v2
# Usage: bash gt.sh
# ============================================================
set -e

echo "======================================"
echo "   GT APPLICATION DEPLOYMENT SCRIPT"
echo "======================================"

cd /root/GT

# ── Step 1: Stage and commit any local changes ────────────────────────────────
echo "Staging local changes..."
git add -A

if git diff --cached --quiet; then
    echo "No local changes to commit."
else
    git commit -m "Server changes $(date '+%Y-%m-%d %H:%M')"
    echo "Local changes committed."
fi

# ── Step 2: Pull with rebase (avoids divergent branch / merge conflict errors) ─
echo "Pulling latest from GitHub..."
git config pull.rebase true          # use rebase not merge — keeps history linear
git config rebase.autoStash true     # auto-stash/unstash dirty files during rebase

git pull origin main
echo "Pull complete."
git log --oneline -3

# ── Step 3: Install dependencies ──────────────────────────────────────────────
echo "Installing requirements..."
cd /root/GT/backend
source venv/bin/activate
pip install -r requirements.txt -q

# ── Step 4: Restart services ──────────────────────────────────────────────────
echo "Restarting GT service..."
sudo systemctl restart gt.service
sudo systemctl reload nginx

# ── Step 5: Health check ──────────────────────────────────────────────────────
sleep 2
echo ""
STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8011/health 2>/dev/null || echo "000")
if [ "$STATUS" = "200" ]; then
    echo "✓ Backend healthy (HTTP $STATUS)"
else
    echo "✗ Backend not responding (HTTP $STATUS) — check logs:"
    echo "  sudo journalctl -u gt.service -n 30 --no-pager"
    sudo journalctl -u gt.service -n 15 --no-pager
fi

echo ""
echo "======================================"
echo " GT APPLICATION DEPLOYED SUCCESSFULLY"
echo "======================================"
echo "http://187.127.128.128/gt"
