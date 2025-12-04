#!/bin/bash

# Define the full path to the Python executable
PYTHON_EXEC="/usr/local/bin/python"

# --- 1. Database Initialization (Blocking but necessary) ---
# We call the server file with a specific argument.
# This runs the init_db() logic safely without starting Flask's app.run().
echo "Running database initialization and migration..."
$PYTHON_EXEC server.py init_db_sync

# --- 2. Start Gunicorn (The Web Server) ---
# We use exec to ensure Gunicorn replaces the shell process.
# --timeout 120: Increases the worker boot timeout from 60s to 120s (crucial for slow DB connections).
# --workers 2: Standard worker count for better concurrency.
echo "Starting Gunicorn server..."
exec gunicorn server:app --timeout 120 --workers 2 --bind 0.0.0.0:$PORT