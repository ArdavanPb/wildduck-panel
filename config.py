import os

# ── Admin credentials ─────────────────────────────────────────────────────
# Password is bcrypt-hashed at startup; store the plaintext version here.
# These can be overridden via environment variables (docker-compose / K8s).
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")

# ── Flask core ────────────────────────────────────────────────────────────
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "a-very-secret-key")
FLASK_DEBUG = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

# ── Database & session storage ────────────────────────────────────────────
# Shared SQLite database for app settings and server-side sessions.
DATABASE_URI = "sqlite:///panel.db"

SESSION_TYPE = "sqlalchemy"
SESSION_PERMANENT = False
SESSION_USE_SIGNER = True
