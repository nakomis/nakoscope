#!/bin/bash
# Install the nakoscope MCP server into Claude Code at user scope.
# Run once after cloning the repo.

set -e
cd "$(dirname "$0")"

PYTHON=$(asdf which python 2>/dev/null || which python3)
MCP_DIR="$(cd "$(dirname "$0")" && pwd)"
# Allow install.sh to pass in the resolved claude path (it may not be on PATH here)
CLAUDE_CMD="${CLAUDE_BIN:-$(command -v claude || true)}"

echo "Installing nakoscope MCP dependencies..."
"$PYTHON" -m pip install -q -r "$MCP_DIR/requirements.txt"

echo "Registering MCP server with Claude Code (user scope)..."
if [ -z "$CLAUDE_CMD" ]; then
    echo "  WARNING: claude CLI not found."
    echo "  Run manually: claude mcp add nakoscope --scope user -- $PYTHON $MCP_DIR/server.py"
else
    "$CLAUDE_CMD" mcp add nakoscope --scope user -- "$PYTHON" "$MCP_DIR/server.py"
fi

echo "Done. Restart Claude Code for the change to take effect."
