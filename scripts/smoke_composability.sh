#!/usr/bin/env bash
# Rollup smoke for the composable research service epic (#252).
# Spawns research-mcp and walks lifecycle, read, export, and tool-level MCP surfaces.

set -euo pipefail

UV_CACHE_DIR="${UV_CACHE_DIR:-.uv-cache}" uv run python tests/integration/rollup_composability.py "$@"

echo "PASS: composability rollup smoke green"
