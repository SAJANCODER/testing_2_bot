import os
import json
from flask import request, Response, current_app
from functools import wraps

# Config: override via environment if needed
MAINTENANCE_FLAG_FILE = os.getenv("MAINTENANCE_FLAG_FILE", "/tmp/maintenance.flag")
ADMIN_KEY = os.getenv("ADMIN_KEY")  # REQUIRED: set in Render env

def is_maintenance_enabled():
    """Return True if maintenance flag file exists."""
    try:
        # Check if the process is running on Render's ephemeral filesystem
        # and assume flag file existence is the standard method.
        return os.path.exists(MAINTENANCE_FLAG_FILE)
    except Exception:
        return False

def enable_maintenance():
    """Create flag file to enable maintenance."""
    try:
        with open(MAINTENANCE_FLAG_FILE, "w") as f:
            f.write("1")
        return True
    except Exception as e:
        current_app.logger.exception("Failed to enable maintenance: %s", e)
        return False

def disable_maintenance():
    """Remove flag file to disable maintenance."""
    try:
        if os.path.exists(MAINTENANCE_FLAG_FILE):
            os.remove(MAINTENANCE_FLAG_FILE)
        return True
    except Exception as e:
        current_app.logger.exception("Failed to disable maintenance: %s", e)
        return False

def require_admin_key(fn):
    """Decorator to protect admin endpoints by ADMIN_KEY."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        key = request.args.get("key")
        # Ensure ADMIN_KEY is set and matches the provided key
        if not ADMIN_KEY or key != ADMIN_KEY:
            return ("Unauthorized", 401)
        return fn(*args, **kwargs)
    return wrapper

def maintenance_middleware(app):
    """
    Register before_request hook that blocks all non-admin routes
    when maintenance flag is present.
    """
    @app.before_request
    def _block_if_maintenance():
        # Allow admin endpoints so you can toggle/flush while blocked.
        if request.path.startswith("/_admin/"):
            return  # allow admin operations even during maintenance

        # If maintenance is on -> block everything else with a 503
        if is_maintenance_enabled():
            return Response(
                """<html>
                    <head><meta name="robots" content="noindex"/><title>Maintenance</title></head>
                    <body style="font-family:system-ui,Arial;text-align:center;padding:3rem;">
                    <img src="[https://placehold.co/100x100/A0A0A0/FFFFFF?text=MAINT](https://placehold.co/100x100/A0A0A0/FFFFFF?text=MAINT)" alt="Maintenance Icon"/>
                    <h1>🚧 Service Update in Progress</h1>
                    <p>We are currently performing scheduled maintenance. Your commits are being safely queued and will be processed once we are back online.</p>
                    </body>
                    </html>""",
                status=503,
                mimetype="text/html"
            )

def register_admin_routes(app, on_disable_flush_callback=None):
    """
    Register admin endpoints on the Flask app.
    The main app MUST pass the flush_pending_callback function here.
    """
    @app.route("/_admin/maintenance", methods=["GET"])
    @require_admin_key
    def _toggle_maintenance():
        mode = (request.args.get("mode") or "").lower()
        flush_chat = request.args.get("chat_id")

        if mode == "on":
            ok = enable_maintenance()
            if ok:
                return "Maintenance ENABLED. Webhooks will be queued.", 200
            else:
                return "Failed to enable maintenance (see logs).", 500

        if mode == "off":
            ok = disable_maintenance()
            if not ok:
                return "Failed to disable maintenance (see logs).", 500

            # Automatic flush check
            if request.args.get("auto_flush") == "1" and on_disable_flush_callback:
                try:
                    sent, failed, msg = on_disable_flush_callback(app, chat_id=flush_chat)
                    return (f"Maintenance DISABLED. Auto-flush completed. Sent: {sent}, Failed: {failed}. {msg}"), 200
                except Exception as e:
                    app.logger.error("Auto-flush after disable failed:", exc_info=True)
                    return ("Maintenance DISABLED, but auto-flush failed (see logs).", 200)

            return "Maintenance DISABLED.", 200

        # No mode provided -> status
        status = "ON" if is_maintenance_enabled() else "OFF"
        return (f"Usage: ?mode=on or ?mode=off. Current state: {status}. "
                f"Requires ADMIN_KEY. Optional: &auto_flush=1 and &chat_id=X."), 200

    @app.route("/_admin/flush_pending", methods=["POST","GET"])
    @require_admin_key
    def _flush_pending():
        """Manually flush pending commits using the provided callback."""
        if not on_disable_flush_callback:
            return "No flush callback configured on the app.", 400
        flush_chat = request.args.get("chat_id")
        try:
            sent, failed, msg = on_disable_flush_callback(app, chat_id=flush_chat)
            return (f"Flush completed. Sent: {sent}, Failed: {failed}. {msg}"), 200
        except Exception as e:
            current_app.logger.error("Manual flush failed:", exc_info=True)
            return ("Flush failed (see logs).", 500)