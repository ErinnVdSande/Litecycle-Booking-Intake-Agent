#!/usr/bin/env bash
# Runs the booking intake pipeline against sample emails in samples/*.txt
# (or a single built-in fallback if that folder is empty), writing results
# to outputs/.
#
# Usage:
#   ./run.sh                         # uses default paths
#   ./run.sh --samples-dir my_emails # override any main.py flag
#
# Run ./install.sh first if you haven't already.

set -euo pipefail

VENV_DIR=".venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "No virtual environment found at $VENV_DIR — run ./install.sh first."
    exit 1
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

if [ ! -f ".env" ] || ! python3 -c "
from dotenv import load_dotenv
import os, sys
load_dotenv('.env')
sys.exit(0 if os.environ.get('OPENROUTER_API_KEY') else 1)
" 2>/dev/null; then
    echo "Warning: OPENROUTER_API_KEY doesn't appear to be set (checked via the same"
    echo "python-dotenv loading main.py itself uses, not a plain grep on .env)."
    echo "The intake agent's LLM call will fail without it."
fi

python main.py "$@"