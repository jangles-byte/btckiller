#!/bin/bash
# BTC.KILLER — Setup Wizard
# Double-click this file to run setup.
# If macOS says "cannot be opened": right-click → Open → Open anyway.

cd "$(dirname "$0")"

# Find Python 3.9+
PYTHON=""
for cmd in python3 python3.12 python3.11 python3.10 python3.9; do
    if command -v "$cmd" &>/dev/null; then
        VER=$("$cmd" --version 2>&1 | awk '{print $2}')
        MAJOR=$(echo "$VER" | cut -d. -f1)
        MINOR=$(echo "$VER" | cut -d. -f2)
        if [ "$MAJOR" = "3" ] && [ "$MINOR" -ge 9 ] 2>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo ""
    echo "  ERROR: Python 3.9+ not found."
    echo ""
    echo "  Install from: https://www.python.org/downloads/"
    echo ""
    read -p "  Press Enter to exit..."
    exit 1
fi

"$PYTHON" setup.py

echo ""
read -p "  Press Enter to close this window..."
