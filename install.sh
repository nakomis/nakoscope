#!/bin/bash
# One-shot setup for the oscilloscope capture system.
# Run once after cloning: bash install.sh

set -e
REPO="$(cd "$(dirname "$0")" && pwd)"
PYTHON=$(asdf which python 2>/dev/null || which python3)

echo "=== Oscilloscope capture system setup ==="
echo "Repo: $REPO"
echo "Python: $PYTHON ($($PYTHON --version))"
echo

# ── 1. Data directory ─────────────────────────────────────────────────────────
echo "Creating data directory..."
mkdir -p "$REPO/data"

echo "Excluding data directory from Time Machine..."
tmutil addexclusion "$REPO/data" 2>/dev/null && echo "  Done." || echo "  (tmutil not available — skip)"

# ── 2. App dependencies ───────────────────────────────────────────────────────
echo "Installing app dependencies..."
"$PYTHON" -m pip install -q -r "$REPO/app/requirements.txt"

echo "Installing vds1022 Python API..."
VDS_API="$HOME/repos/florentbr/OWON-VDS1022/api/python"
if [ -d "$VDS_API" ]; then
    "$PYTHON" -m pip install -q -e "$VDS_API"
    echo "  Installed from $VDS_API"
else
    echo "  WARNING: $VDS_API not found."
    echo "  Clone https://github.com/florentbr/OWON-VDS1022 and re-run this script."
fi

# ── 3. USB access (macOS) ─────────────────────────────────────────────────────
echo "Configuring USB access (requires sudo)..."
SUDOERS_FILE="/etc/sudoers.d/oscilloscope-capture"
SUDOERS_LINE="${SUDO_USER:-$USER} ALL=(root) NOPASSWD: $PYTHON $REPO/app/cli.py *"

if [ "$EUID" -ne 0 ]; then
    echo "  Skipping sudoers setup (not running as root)."
    echo "  To avoid typing sudo each time, run:"
    echo "    sudo bash $REPO/install.sh"
else
    echo "$SUDOERS_LINE" > "$SUDOERS_FILE"
    chmod 440 "$SUDOERS_FILE"
    echo "  Written: $SUDOERS_FILE"
    echo "  You can now run: $PYTHON $REPO/app/cli.py record"
fi

# ── 4. MCP server ─────────────────────────────────────────────────────────────
echo "Installing MCP server..."
bash "$REPO/mcp/install.sh"

echo
echo "=== Setup complete ==="
echo
echo "To start a recording:"
if [ "$EUID" -ne 0 ]; then
    echo "  sudo $PYTHON $REPO/app/cli.py record --notes 'your notes here'"
else
    echo "  $PYTHON $REPO/app/cli.py record --notes 'your notes here'"
fi
echo
echo "Data will be stored in: $REPO/data/captures.h5"
