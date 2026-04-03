#!/bin/bash
# One-shot setup for nakoscope.
# Must be run as root: sudo bash install.sh
#
# Installs app files to system locations (root-owned) so that the
# sudoers NOPASSWD grant cannot be abused by editing the source tree.
#
# Layout:
#   /usr/local/lib/nakoscope/   app code (root-owned, not user-writable)
#   /usr/local/bin/nakoscope    wrapper script (root-owned)
#   /var/log/nakoscope/         log directory
#   /etc/sudoers.d/nakoscope    NOPASSWD grant for /usr/local/bin/nakoscope only

set -e

if [ "$EUID" -ne 0 ]; then
    echo "Error: install.sh must be run as root."
    echo "  sudo bash \"$0\""
    exit 1
fi

REPO="$(cd "$(dirname "$0")" && pwd)"
# Resolve the invoking user's Python (asdf-managed) even under sudo
REAL_USER="${SUDO_USER:-$USER}"
PYTHON=$(su - "$REAL_USER" -c 'asdf which python 2>/dev/null || which python3')
# Resolve claude CLI — may live in ~/.local/bin which su - doesn't pick up
CLAUDE=$(su - "$REAL_USER" -c 'command -v claude || true')

INSTALL_DIR="/usr/local/lib/nakoscope"
BIN_PATH="/usr/local/bin/nakoscope"
LOG_DIR="/var/log/nakoscope"
SUDOERS_FILE="/etc/sudoers.d/nakoscope"

echo "=== Nakoscope setup ==="
echo "Repo:    $REPO"
echo "Install: $INSTALL_DIR"
echo "Python:  $PYTHON ($($PYTHON --version))"
echo "User:    $REAL_USER"
echo

# ── 1. libusb (required by PyUSB for USB oscilloscope access) ─────────────────
echo "Checking libusb..."
if su - "$REAL_USER" -c 'brew list libusb &>/dev/null'; then
    echo "  Already installed."
else
    echo "  Installing via Homebrew..."
    su - "$REAL_USER" -c 'brew install libusb'
fi

# ── 3. Install app files (root-owned) ─────────────────────────────────────────
echo "Installing app files to $INSTALL_DIR ..."
rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp -r "$REPO/app" "$INSTALL_DIR/"
chown -R root:wheel "$INSTALL_DIR"
chmod -R 755 "$INSTALL_DIR"
# Python files readable but not writable by anyone but root
find "$INSTALL_DIR" -type f -name "*.py" -exec chmod 644 {} \;
echo "  Done."

# ── 4. Install Python dependencies (as the real user) ─────────────────────────
echo "Installing Python dependencies..."
su - "$REAL_USER" -c "\"$PYTHON\" -m pip install -q -r \"$INSTALL_DIR/app/requirements.txt\""

echo "Installing vds1022 Python API..."
VDS_API="/Users/$REAL_USER/repos/florentbr/OWON-VDS1022/api/python"
if [ -d "$VDS_API" ]; then
    su - "$REAL_USER" -c "\"$PYTHON\" -m pip install -q -e \"$VDS_API\""
    echo "  Installed from $VDS_API"
else
    echo "  WARNING: $VDS_API not found."
    echo "  Clone https://github.com/florentbr/OWON-VDS1022 and re-run this script."
fi

# ── 5. Create root-owned wrapper script ───────────────────────────────────────
echo "Creating $BIN_PATH ..."
cat > "$BIN_PATH" <<WRAPPER
#!/bin/bash
# Root-owned wrapper for nakoscope CLI.
# Do not edit — re-run sudo install.sh to update.
if [ "\$EUID" -ne 0 ]; then
    exec sudo "$BIN_PATH" "\$@"
fi
exec "$PYTHON" "$INSTALL_DIR/app/cli.py" "\$@"
WRAPPER
chown root:wheel "$BIN_PATH"
chmod 755 "$BIN_PATH"
echo "  Done."

# ── 6. Log directory ──────────────────────────────────────────────────────────
echo "Creating $LOG_DIR ..."
mkdir -p "$LOG_DIR"
chown root:wheel "$LOG_DIR"
chmod 755 "$LOG_DIR"
echo "  Done."

# ── 7. Data directory (user-owned — can be large) ─────────────────────────────
DATA_DIR="$REPO/data"
echo "Creating data directory $DATA_DIR ..."
mkdir -p "$DATA_DIR"
chown "$REAL_USER" "$DATA_DIR"
echo "Excluding from Time Machine..."
tmutil addexclusion "$DATA_DIR" 2>/dev/null && echo "  Done." || echo "  (tmutil unavailable — skip)"

# ── 8. Sudoers entry ──────────────────────────────────────────────────────────
echo "Writing sudoers entry ..."
echo "$REAL_USER ALL=(root) NOPASSWD: $BIN_PATH" > "$SUDOERS_FILE"
chmod 440 "$SUDOERS_FILE"
echo "  $SUDOERS_FILE"
echo "  Grant: $REAL_USER -> $BIN_PATH only (root-owned, not user-editable)"

# ── 9. Config file (user-owned) ───────────────────────────────────────────────
CONFIG_FILE="/Users/$REAL_USER/.nakoscope.yaml"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Creating starter config at $CONFIG_FILE ..."
    su - "$REAL_USER" -c "cat > \"$CONFIG_FILE\"" <<YAML
# nakoscope configuration
# Priority: CLI flags > environment variables > this file > built-in defaults

backend: hdf5        # hdf5 | s3  (omit to auto-detect)
device: vds1022      # device plugin

# HDF5 backend
data_path: $DATA_DIR/captures.h5

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
    echo "Config already exists at $CONFIG_FILE — leaving it unchanged."
fi

# ── 10. MCP server ────────────────────────────────────────────────────────────
echo "Installing MCP server (as $REAL_USER)..."
if [ -z "$CLAUDE" ]; then
    echo "  WARNING: claude CLI not found — skipping MCP registration."
    echo "  Run 'claude mcp add nakoscope --scope user -- $PYTHON $REPO/mcp/server.py' manually."
else
    su - "$REAL_USER" -c "CLAUDE_BIN=\"$CLAUDE\" bash \"$REPO/mcp/install.sh\""
fi

echo
echo "=== Setup complete ==="
echo
echo "  Installed: $INSTALL_DIR  (root-owned)"
echo "  Wrapper:   $BIN_PATH"
echo "  Sudoers:   $SUDOERS_FILE"
echo "  Logs:      $LOG_DIR"
echo "  Config:    $CONFIG_FILE"
echo
echo "To start a recording (no sudo needed — wrapper handles it):"
echo "  nakoscope record --notes 'your notes here'"
echo
echo "To update after pulling new code, re-run:"
echo "  sudo bash $REPO/install.sh"
