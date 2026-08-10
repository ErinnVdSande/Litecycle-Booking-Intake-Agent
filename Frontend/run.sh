#!/usr/bin/env bash
# Installs frontend dependencies (first run only) and starts the Vite dev server.
#
# Usage:
#   ./run.sh

set -euo pipefail

if [ ! -d "node_modules" ]; then
    echo "Installing frontend dependencies..."
    npm install
fi

npm run dev
