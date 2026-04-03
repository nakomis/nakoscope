#!/bin/bash
# One-shot setup for the nakoscope capture system.
# Run once after cloning: bash install.sh

set -e

if [ "$EUID" -ne 0 ]; then
    echo "Error: install.sh must be run as root (it needs to write to /etc/sudoers.d)."
    echo "  sudo bash \"$0\""
    exit 1
fi

REPO="$(cd "$(dirname "$0")" && pwd)"
PYTHON=$(asdf which python 2>/dev/null || which python3)

echo "=== Nakoscope setup ==="
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
echo "Configuring USB access..."
SUDOERS_FILE="/etc/sudoers.d/nakoscope-capture"
SUDOERS_LINE="${SUDO_USER:-$USER} ALL=(root) NOPASSWD: $PYTHON $REPO/app/cli.py *"
echo "$SUDOERS_LINE" > "$SUDOERS_FILE"
chmod 440 "$SUDOERS_FILE"
echo "  Written: $SUDOERS_FILE"

# ── 4. Config file ────────────────────────────────────────────────────────────
CONFIG_FILE="$HOME/.nakoscope.yaml"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Creating starter config at $CONFIG_FILE ..."
    cat > "$CONFIG_FILE" <<YAML
# nakoscope configuration
# Priority: CLI flags > environment variables > this file > built-in defaults

backend: hdf5        # hdf5 | s3  (omit to auto-detect)
device: vds1022      # device plugin

# HDF5 backend
data_path: $REPO/data/captures.h5

# S3 backend (uncomment and fill in to use S3)
# s3:
#   bucket: my-nakoscope-bucket
#   prefix: nakoscope/
#   cache_dir: ~/.cache/nakoscope
#   aws_profile: nakom.is-sandbox

# Capture defaults (all overridable per-run via CLI flags)
capture:
  sample_rate: 250000
  channels: [CH1, CH2]
  v_range: 10.0
  coupling: DC
  probe: 1.0
YAML
    echo "  Created. Edit $CONFIG_FILE to configure."
else
    echo "Config file already exists at $CONFIG_FILE — leaving it unchanged."
fi

# ── 5. MCP server ─────────────────────────────────────────────────────────────
echo "Installing MCP server..."
bash "$REPO/mcp/install.sh"

echo
echo "=== Setup complete ==="
echo
echo "Config:  $CONFIG_FILE"
echo "Data:    $REPO/data/captures.h5"
echo
echo "To start a recording:"
echo "  $PYTHON $REPO/app/cli.py record --notes 'your notes here'"
