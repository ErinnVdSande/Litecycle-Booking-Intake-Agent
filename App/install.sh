#!/usr/bin/env bash
# One-time setup: creates a virtual environment and installs dependencies.
#
# Usage:
#   ./install.sh

set -euo pipefail

VENV_DIR=".venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment in $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
else
    echo "Virtual environment already exists in $VENV_DIR, reusing it."
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "Installing dependencies from requirements.txt..."
pip install --upgrade pip --quiet
pip install -r requirements.txt

if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo ""
        echo "Created .env from .env.example — fill in your OPENROUTER_API_KEY before running."
    else
        echo ""
        echo "No .env or .env.example found — create a .env with:"
        echo "  OPENROUTER_API_KEY=sk-or-..."
    fi
fi

echo ""
echo "Setup complete. Activate the environment with:"
echo "  source $VENV_DIR/bin/activate"
echo "Then run the tool with:"
echo "  ./run.sh"
