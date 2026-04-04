#!/bin/bash
# Exit immediately if a command exits with a non-zero status
set -e

echo "--- Starting Kalshi Environment Setup ---"

# 1. Install the volume-mounted code as an editable package.
# This searches for the pyproject.toml in /app (your volume).
pip install -e /app

echo "--- Dependencies Linked Successfully ---"

# 2. Execute the main application. 
# 'exec' ensures that the Python process becomes PID 1, 
# which helps Docker handle stops and signals (like Ctrl+C) cleanly.
# exec python3 /app/src/app/verify_environment.py
exec bash
