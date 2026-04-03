#!/bin/bash
# Remove nakoscope system installation.
# Must be run as root: sudo bash uninstall.sh
#
# Removes:
#   /usr/local/lib/nakoscope/   app code
#   /usr/local/bin/nakoscope    wrapper script
#   /var/log/nakoscope/         log directory
#   /etc/sudoers.d/nakoscope    NOPASSWD grant
#
# Does NOT remove:
#   ~/.nakoscope.yaml            user config
#   ~/repos/nakomis/nakoscope/data/   captured data
#   Python packages installed by install.sh

set -e

if [ "$EUID" -ne 0 ]; then
    echo "Error: uninstall.sh must be run as root."
    echo "  sudo bash \"$0\""
    exit 1
fi

echo "=== Nakoscope uninstall ==="
echo

INSTALL_DIR="/usr/local/lib/nakoscope"
BIN_PATH="/usr/local/bin/nakoscope"
LOG_DIR="/var/log/nakoscope"
SUDOERS_FILE="/etc/sudoers.d/nakoscope"

if [ -d "$INSTALL_DIR" ]; then
    echo "Removing $INSTALL_DIR ..."
    rm -rf "$INSTALL_DIR"
    echo "  Done."
else
    echo "  $INSTALL_DIR not found — skipping."
fi

if [ -f "$BIN_PATH" ]; then
    echo "Removing $BIN_PATH ..."
    rm -f "$BIN_PATH"
    echo "  Done."
else
    echo "  $BIN_PATH not found — skipping."
fi

if [ -f "$SUDOERS_FILE" ]; then
    echo "Removing $SUDOERS_FILE ..."
    rm -f "$SUDOERS_FILE"
    echo "  Done."
else
    echo "  $SUDOERS_FILE not found — skipping."
fi

if [ -d "$LOG_DIR" ]; then
    echo "Removing $LOG_DIR ..."
    rm -rf "$LOG_DIR"
    echo "  Done."
else
    echo "  $LOG_DIR not found — skipping."
fi

echo
echo "=== Uninstall complete ==="
echo
echo "The following were NOT removed:"
echo "  ~/.nakoscope.yaml          (user config — delete manually if desired)"
echo "  ~/repos/nakomis/nakoscope/data/  (captured data — delete manually if desired)"
echo
echo "To also remove the MCP server registration:"
echo "  claude mcp remove nakoscope"
