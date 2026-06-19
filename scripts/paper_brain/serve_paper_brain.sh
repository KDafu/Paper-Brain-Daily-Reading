#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
port="${1:-8765}"
python -m http.server "$port"
