#!/bin/bash
# Test runner — tokens are in .env (not committed). Copy env-template.txt to .env and fill in.
cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a; source .env; set +a
else
  echo "Missing .env — copy env-template.txt to .env and fill in your tokens."
  exit 1
fi
python3 loyverse_shift_report.py "$@"
