#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
python scripts/paper_brain/paper_brain.py "$@"
