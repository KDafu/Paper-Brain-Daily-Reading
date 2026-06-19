#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

mkdir -p data/paper_brain doc/daily_paper_digest doc/paper_brain/deep_reads doc/paper_brain/figures papers

python scripts/paper_brain/paper_brain.py --offline

cat <<'MSG'

Paper Brain setup complete.

Run:
  source .venv/bin/activate
  scripts/paper_brain/serve_paper_brain.sh 8765

Open:
  http://127.0.0.1:8765/doc/paper_brain/index.html

MSG
