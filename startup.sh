#!/bin/bash
# Start Gunicorn with multiple workers, pointing to the 'app' object in app.py
gunicorn --bind 0.0.0.0 --workers 4 server:app