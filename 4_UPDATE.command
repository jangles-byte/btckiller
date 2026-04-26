#!/bin/bash
cd "$(dirname "$0")"

echo "========================================"
echo "  BTC.KILLER — Check for Updates"
echo "========================================"
echo ""

# Check if this is a git repo
if [ ! -d ".git" ]; then
  echo "❌ This folder wasn't installed via git clone."
  echo ""
  echo "To get future updates automatically, reinstall using:"
  echo "  git clone https://github.com/jangles-byte/btckiller.git"
  echo ""
  echo "Then copy your .env file and private key into the new folder."
  echo ""
  read -p "Press Enter to close..."; exit 1
fi

echo "Checking for updates..."
git fetch origin main 2>&1

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
  echo ""
  echo "✅ Already up to date — no changes."
else
  echo ""
  echo "📦 Update available! Pulling latest version..."
  git pull origin main
  echo ""
  echo "✅ Updated successfully!"
  echo "   Restart the dashboard to apply changes."
fi

echo ""
read -p "Press Enter to close..."
