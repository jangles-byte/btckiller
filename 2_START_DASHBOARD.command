#!/bin/bash
cd "$(dirname "$0")"
echo ""
echo "  Starting BTC.KILLER dashboard..."
echo "  Open http://localhost:5050 in your browser"
echo ""
"/Users/jangles/Desktop/btckiller/venv/bin/python3" dashboard.py
