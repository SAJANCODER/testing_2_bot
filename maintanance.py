# maintenance.py
import os
from flask import request, Response

MAINTENANCE_FLAG_FILE = "/tmp/maintenance.flag"
ADMIN_KEY = os.getenv("ADMIN_KEY")  # Set this in Render dashboard

def is_maintenance_enabled():
    return os.path.exists(MAINTENANCE_FLAG_FILE)

def enable_maintenance():
    with open(MAINTENANCE_FLAG_FILE, "w") as f:
        f.write("1")

def disable_maintenance():
    if os.path.exists(MAINTENANCE_FLAG_FILE):
        os.remove(MAINTENANCE_FLAG_FILE)

def maintenance_middleware(app):
    @app.before_request
    def maintenance_blocker():
        # allow admin route even during maintenance
        if request.path.startswith("/_admin/maintenance"):
            return  

        # allow webhook to function (optional)
        if request.path.startswith("/webhook"):
            return  

        if is_maintenance_enabled():
            return Response(
                "<h1>🚧 Maintenance Mode</h1><p>The service is temporarily unavailable.</p>",
                status=503,
                mimetype="text/html"
            )

def register_admin_routes(app):
    @app.route("/_admin/maintenance", methods=["GET"])
    def toggle_maintenance():
        key = request.args.get("key")
        mode = request.args.get("mode")

        if key != ADMIN_KEY:
            return "Unauthorized", 401

        if mode == "on":
            enable_maintenance()
            return "Maintenance ENABLED"

        if mode == "off":
            disable_maintenance()
            return "Maintenance DISABLED"

        return "Use ?mode=on or ?mode=off"
