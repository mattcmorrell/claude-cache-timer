#!/bin/bash
# One-liner remote install for claude-cache-timer
# Usage: curl -fsSL https://raw.githubusercontent.com/mattcmorrell/claude-cache-timer/main/install-remote.sh | bash

set -e

INSTALL_DIR="$HOME/.claude/plugins/cache-timer"

if ! command -v jq >/dev/null 2>&1; then
  echo "cache-timer: jq is required. Install it first:"
  echo "  brew install jq    # macOS"
  echo "  apt install jq     # Linux"
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "cache-timer: git is required."
  exit 1
fi

if [ -d "$INSTALL_DIR" ]; then
  echo "cache-timer: Updating existing install..."
  git -C "$INSTALL_DIR" pull --ff-only
else
  echo "cache-timer: Installing to $INSTALL_DIR..."
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone https://github.com/mattcmorrell/claude-cache-timer.git "$INSTALL_DIR"
fi

bash "$INSTALL_DIR/install.sh"
