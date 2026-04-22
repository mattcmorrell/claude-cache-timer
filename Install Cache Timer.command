#!/bin/bash
# Double-click this file to install claude-cache-timer
# It will open Terminal, run the installer, and wait for you to close it

cd "$(dirname "$0")"

echo ""
echo "=================================="
echo "  Installing claude-cache-timer"
echo "=================================="
echo ""

if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required. Installing via Homebrew..."
  if command -v brew >/dev/null 2>&1; then
    brew install jq
  else
    echo ""
    echo "ERROR: jq not found and Homebrew not installed."
    echo "Install jq first: https://jqlang.github.io/jq/download/"
    echo ""
    read -p "Press Enter to close..."
    exit 1
  fi
fi

bash install.sh

echo ""
echo "=================================="
echo "  Done! Restart Claude Code."
echo "=================================="
echo ""
read -p "Press Enter to close..."
