#!/bin/bash
# Install the nakoscope MCP server into Claude Code at user scope.
# Run once after cloning the repo.

set -e
cd "$(dirname "$0")"

PYTHON=$(asdf which python 2>/dev/null || which python3)
MCP_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing nakoscope MCP dependencies..."
"$PYTHON" -m pip install -q -e .

echo "Registering MCP server with Claude Code (user scope)..."
claude mcp add nakoscope --scope user -- "$PYTHON" "$MCP_DIR/server.py"

echo "Done. Restart Claude Code for the change to take effect."
