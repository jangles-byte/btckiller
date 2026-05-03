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
  echo "  git clone YOUR_GITHUB_REPO_URL"
  echo ""
  echo "Then copy your .env file and private key into the new folder."
  echo ""
  read -p "Press Enter to close..."; exit 1
fi

echo "Fetching latest version from GitHub..."
git fetch origin main 2>&1

if [ $? -ne 0 ]; then
  echo ""
  echo "❌ Could not reach GitHub. Check your internet connection."
  read -p "Press Enter to close..."; exit 1
fi

echo ""
echo "Applying latest files..."

# Directly checkout each file from origin — bypasses all history issues
git checkout origin/main -- \
  bot.py \
  dashboard.py \
  dashboard.html \
  requirements.txt \
  HOW_TO_USE.txt \
  1_SETUP.command \
  2_START_DASHBOARD.command \
  4_UPDATE.command \
  .gitignore 2>&1

if [ $? -eq 0 ]; then
  echo ""
  echo "========================================"
  echo "  ✅ Updated successfully!"
  echo "  Restart the dashboard to apply changes."
  echo "========================================"
else
  echo ""
  echo "❌ Update failed. Try deleting this folder and cloning fresh."
fi

echo ""
read -p "Press Enter to close..."
