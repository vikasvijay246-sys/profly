"""
Shared route utilities: role guard decorator.
"""
from functools import wraps
from flask import abort, redirect, url_for, jsonify, request
from flask_login import current_user


def role_required(*roles):
    """Protect a view to specific role(s). Returns JSON for API paths."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not current_user.is_authenticated:
                if request.path.startswith("/api/") or request.is_json:
                    return jsonify({"error": "Login required"}), 401
                return redirect(url_for("auth.login"))
            if current_user.role not in roles:
                if request.path.startswith("/api/") or request.is_json:
                    return jsonify({"error": "Access denied"}), 403
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator
