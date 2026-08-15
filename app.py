import os
import secrets
import time
from functools import wraps

from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_compress import Compress
from flask_session import Session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

import config
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from cachetools import TTLCache

load_dotenv()

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# ── Response compression ──────────────────────────────────────────────
Compress(app)

# ── Database & server-side sessions ────────────────────────────────────
app.config["SQLALCHEMY_DATABASE_URI"] = config.DATABASE_URI
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "connect_args": {"check_same_thread": False},
    "pool_pre_ping": True,
    "pool_recycle": 300,
}
db = SQLAlchemy(app)
app.config["SESSION_TYPE"] = config.SESSION_TYPE
app.config["SESSION_SQLALCHEMY"] = db
app.config["SESSION_PERMANENT"] = config.SESSION_PERMANENT
app.config["SESSION_USE_SIGNER"] = config.SESSION_USE_SIGNER
Session(app)

# ── Admin credentials (hashed at startup) ─────────────────────────────
ADMIN_USERNAME = config.ADMIN_USERNAME
ADMIN_PASSWORD_HASH = generate_password_hash(config.ADMIN_PASSWORD)

# ── Connection pool & retry ───────────────────────────────────────────
TIMEOUT = 10
MAX_RETRIES = 2
RETRY_BACKOFF = 0.5  # seconds

API_SESSION = requests.Session()
RETRY_STRATEGY = Retry(
    total=MAX_RETRIES,
    backoff_factor=RETRY_BACKOFF,
    status_forcelist=[500, 502, 503, 504],
    allowed_methods=["GET", "HEAD"],
)
ADAPTER = HTTPAdapter(
    max_retries=RETRY_STRATEGY,
    pool_connections=10,
    pool_maxsize=20,
    pool_block=False,
)
API_SESSION.mount("http://", ADAPTER)
API_SESSION.mount("https://", ADAPTER)

# ── Response cache (TTL in seconds) ───────────────────────────────────
API_CACHE = TTLCache(maxsize=256, ttl=30)          # default 30s
CACHE_TTL = {
    "/users": 30,
    "/domains": 300,
    "/addresses": 60,
}
QUERY_CACHE = TTLCache(maxsize=128, ttl=30)         # search queries


# ═══════════════════════════════════════════════════════════════════════════
#  Database model
# ═══════════════════════════════════════════════════════════════════════════

class Setting(db.Model):
    """Single-row table holding the WildDuck API URL and access token."""

    id = db.Column(db.Integer, primary_key=True)
    api_url = db.Column(
        db.String(512), nullable=False, default="http://127.0.0.1:8080"
    )
    api_token = db.Column(db.String(512), nullable=False, default="")


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════

def get_api_settings():
    """Return (api_url, api_token) from the database.

    Falls back to defaults if no row exists yet (e.g. before first init).
    """
    s = db.session.get(Setting, 1)
    if s is None:
        return "http://127.0.0.1:8080", ""
    return s.api_url.rstrip("/"), s.api_token


def api_request(method, path, json_data=None, params=None, timeout=None, max_retries=None, use_cache=True):
    """Make an authenticated request to the WildDuck REST API.

    Reads the current API URL and token from the database on every call.
    Uses connection pooling for performance and an in-memory TTL cache.
    """
    if timeout is None:
        timeout = TIMEOUT

    # ── cache check (GET only, non-query, when cache enabled) ─────────
    cache_key = None
    if use_cache and method == "GET" and not params:
        cache_key = path
        data = API_CACHE.get(cache_key)
        if data is not None:
            return data, None
    elif use_cache and method == "GET" and params:
        cache_key = f"{path}?{frozenset(params.items())}"
        data = QUERY_CACHE.get(cache_key)
        if data is not None:
            return data, None

    api_url, api_token = get_api_settings()
    url = f"{api_url}{path}"
    headers = {
        "X-Access-Token": api_token,
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip, deflate",
    }

    last_error = None
    for attempt in range(1 + max_retries if max_retries is not None else 1 + MAX_RETRIES):
        try:
            resp = API_SESSION.request(
                method,
                url,
                headers=headers,
                json=json_data,
                params=params,
                timeout=timeout,
            )
            break
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            if attempt < (max_retries if max_retries is not None else MAX_RETRIES):
                backoff = RETRY_BACKOFF * (attempt + 1)
                time.sleep(backoff)
                continue
        except requests.exceptions.RequestException as e:
            return None, f"API request failed: {e}"
    else:
        if isinstance(last_error, requests.exceptions.Timeout):
            return None, f"API request timed out ({url})"
        return None, f"Cannot connect to WildDuck API at {api_url}"

    if resp.status_code == 401:
        return None, "Authentication failed. Check your API token."
    if resp.status_code == 404:
        return None, "Resource not found."
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("error", resp.text)
        except Exception:
            detail = resp.text
        return None, f"API error ({resp.status_code}): {detail}"

    try:
        data = resp.json()
    except Exception:
        return None, "Invalid JSON response from API"

    # ── populate cache ────────────────────────────────────────────────
    if cache_key and method == "GET":
        cache = QUERY_CACHE if params else API_CACHE
        ttl = CACHE_TTL.get(path, 30)
        cache[cache_key] = data

    return data, None


def invalidate_cache(path_prefix=None):
    """Clear relevant cache entries after mutations.

    Call after any POST/PUT/DELETE that changes data.
    """
    if path_prefix is None:
        API_CACHE.clear()
        QUERY_CACHE.clear()
    else:
        for key in list(API_CACHE.keys()):
            if key.startswith(path_prefix):
                del API_CACHE[key]
        for key in list(QUERY_CACHE.keys()):
            if key.startswith(path_prefix):
                del QUERY_CACHE[key]


def init_db():
    """Create tables, enable WAL mode, and seed the default settings row.

    Called once at startup. The initial api_url can be seeded from the
    WILDDUCK_API_URL environment variable so that Docker users don't have
    to visit the Settings page just to fix the default 127.0.0.1 address.
    """
    with app.app_context():
        db.create_all()
        # ── SQLite performance: WAL mode for better concurrency ────────
        db.session.execute(db.text("PRAGMA journal_mode=WAL"))
        db.session.execute(db.text("PRAGMA synchronous=NORMAL"))
        db.session.execute(db.text("PRAGMA cache_size=-8000"))
        db.session.execute(db.text("PRAGMA temp_store=MEMORY"))
        db.session.commit()
        if db.session.get(Setting, 1) is None:
            default_url = os.environ.get(
                "WILDDUCK_API_URL", "http://127.0.0.1:8080"
            )
            default_token = os.environ.get("WILDDUCK_API_TOKEN", "")
            db.session.add(
                Setting(api_url=default_url, api_token=default_token)
            )
            db.session.commit()


# ═══════════════════════════════════════════════════════════════════════════
#  Auth decorator
# ═══════════════════════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)

    return decorated


# ═══════════════════════════════════════════════════════════════════════════
#  Middleware: request timing & session cleanup
# ═══════════════════════════════════════════════════════════════════════════

@app.before_request
def _start_timer():
    g.start_time = time.time()


@app.after_request
def _log_slow_requests(response):
    elapsed = time.time() - getattr(g, "start_time", time.time())
    if elapsed > 2.0:
        app.logger.warning(
            "Slow request: %s %s — %.2fs",
            request.method,
            request.path,
            elapsed,
        )
    return response


@app.teardown_appcontext
def _cleanup_db(exception=None):
    """Ensure SQLAlchemy sessions are removed after each request."""
    db.session.remove()


# ═══════════════════════════════════════════════════════════════════════════
#  Auth routes
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and check_password_hash(
            ADMIN_PASSWORD_HASH, password
        ):
            session["logged_in"] = True
            session["username"] = username
            flash("Logged in successfully.", "success")
            next_page = request.args.get("next")
            if next_page and next_page.startswith("/"):
                return redirect(next_page)
            return redirect(url_for("index"))
        error = "Invalid username or password."

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ═══════════════════════════════════════════════════════════════════════════
#  Dashboard / Home
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/")
@login_required
def index():
    tab = request.args.get("tab", "users")
    search_query = request.args.get("query", "").strip()

    return render_template(
        "index.html",
        tab=tab,
        search_query=search_query,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Create user
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/user/create", methods=["GET", "POST"])
@login_required
def create_user():
    if request.method == "GET":
        return render_template("create_user.html")

    username = request.form.get("username", "").strip()
    name = request.form.get("name", "").strip()
    password = request.form.get("password", "")
    address = request.form.get("address", "").strip()
    quota = request.form.get("quota", "1073741824").strip()
    recipients = request.form.get("recipients", "2000").strip()
    forwards = request.form.get("forwards", "2000").strip()
    spam_level = request.form.get("spam_level", "").strip()

    if not username or not password:
        flash("Username and password are required.", "danger")
        return render_template("create_user.html"), 400

    payload = {
        "username": username,
        "password": password,
    }
    if address:
        payload["address"] = address
    if name:
        payload["name"] = name
    try:
        payload["quota"] = int(quota)
    except ValueError:
        payload["quota"] = 1073741824
    try:
        payload["recipients"] = int(recipients)
    except ValueError:
        payload["recipients"] = 2000
    try:
        payload["forwards"] = int(forwards)
    except ValueError:
        payload["forwards"] = 2000
    if spam_level:
        payload["spamLevel"] = spam_level

    result, err = api_request("POST", "/users", json_data=payload)
    if err:
        flash(f"Failed to create user: {err}", "danger")
        return render_template("create_user.html"), 400

    invalidate_cache("/users")

    user_id = result.get("id") if isinstance(result, dict) else None
    if user_id:
        flash(
            f"User '{username}' created successfully (ID: {user_id}).",
            "success",
        )
    else:
        flash(f"User '{username}' created.", "success")

    return redirect(url_for("index"))


# ═══════════════════════════════════════════════════════════════════════════
#  User details
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/user/<user_id>")
@login_required
def user_details(user_id):
    user, err = api_request("GET", f"/users/{user_id}")
    if err:
        flash(f"Failed to fetch user details: {err}", "danger")
        return redirect(url_for("index"))

    return render_template(
        "user_details.html",
        user=user,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Toggle user enabled/disabled status (AJAX)
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/user/<user_id>/toggle-status", methods=["PUT"])
@login_required
def toggle_user_status(user_id):
    data, err = api_request("GET", f"/users/{user_id}")
    if err:
        return jsonify({"error": f"Failed to fetch user: {err}"}), 400

    current_disabled = data.get("disabled", False)
    payload = {"disabled": not current_disabled}

    _, err = api_request("PUT", f"/users/{user_id}", json_data=payload)
    if err:
        return jsonify({"error": f"Failed to update user: {err}"}), 400

    invalidate_cache("/users")

    new_status = "disabled" if payload["disabled"] else "enabled"
    return jsonify({"status": new_status, "disabled": payload["disabled"]})


# ═══════════════════════════════════════════════════════════════════════════
#  Delete user (AJAX)
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/user/<user_id>/delete", methods=["DELETE"])
@login_required
def delete_user(user_id):
    _, err = api_request("DELETE", f"/users/{user_id}")
    if err:
        return jsonify({"error": f"Failed to delete user: {err}"}), 400
    invalidate_cache("/users")
    return jsonify({"status": "deleted"})


# ═══════════════════════════════════════════════════════════════════════════
#  Settings page (dynamic API configuration)
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    setting = db.session.get(Setting, 1)

    if request.method == "GET":
        csrf_token = secrets.token_hex(32)
        session["csrf_token"] = csrf_token
        return render_template(
            "settings.html",
            api_url=setting.api_url if setting else "",
            api_token=setting.api_token if setting else "",
            csrf_token=csrf_token,
        )

    # ── POST: save or test ──────────────────────────────────────────────
    if request.form.get("csrf_token") != session.pop("csrf_token", None):
        flash("Invalid CSRF token. Please try again.", "danger")
        return redirect(url_for("settings"))

    action = request.form.get("action", "save")
    new_url = request.form.get("api_url", "").strip()
    new_token = request.form.get("api_token", "").strip()

    if not new_url:
        flash("API URL is required.", "danger")
        return redirect(url_for("settings"))

    if action == "reset":
        defaults = {"api_url": "http://127.0.0.1:8080", "api_token": ""}
        if setting is None:
            setting = Setting(**defaults)
            db.session.add(setting)
        else:
            setting.api_url = defaults["api_url"]
            setting.api_token = defaults["api_token"]
        db.session.commit()
        flash("Settings have been reset to defaults.", "info")
        return redirect(url_for("settings"))

    if action == "save":
        if setting is None:
            setting = Setting(api_url=new_url, api_token=new_token)
            db.session.add(setting)
        else:
            setting.api_url = new_url
            setting.api_token = new_token
        db.session.commit()
        invalidate_cache()
        flash("Settings saved successfully.", "success")

    # ── Connectivity test ──────────────────────────────────────────────
    if action in ("save", "test"):
        test_url = new_url.rstrip("/")
        test_token = new_token
        try:
            resp = requests.get(
                f"{test_url}/",
                headers={"X-Access-Token": test_token},
                timeout=5,
            )
            if resp.ok:
                flash(
                    f"Successfully connected to WildDuck API at {test_url}.",
                    "success",
                )
            else:
                flash(
                    f"Connection test returned HTTP {resp.status_code}.",
                    "warning",
                )
        except requests.exceptions.Timeout:
            flash(
                f"Connection test timed out for {test_url}.",
                "danger",
            )
        except requests.exceptions.ConnectionError:
            flash(
                f"Cannot connect to WildDuck API at {test_url}.",
                "danger",
            )
        except requests.exceptions.RequestException as e:
            flash(f"Connection test failed: {e}", "danger")

    return redirect(url_for("settings"))


# ═══════════════════════════════════════════════════════════════════════════
#  AJAX dashboard data endpoint
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/dashboard")
@login_required
def api_dashboard():
    """Return dashboard data as JSON for AJAX-based loading."""
    tab = request.args.get("tab", "users")
    search_query = request.args.get("query", "").strip()

    if tab == "users":
        params = {"limit": 250}
        if search_query:
            params["query"] = search_query
        data, err = api_request("GET", "/users", params=params)
    elif tab == "domains":
        data, err = api_request("GET", "/domains")
    elif tab == "addresses":
        params = {"limit": 250}
        if search_query:
            params["query"] = search_query
        data, err = api_request("GET", "/addresses", params=params)
    else:
        return jsonify({"error": "Invalid tab"}), 400

    if err:
        return jsonify({"error": err}), 502

    if isinstance(data, dict):
        results = data.get("results", data.get("users", data.get("domains", data.get("addresses", []))))
    elif isinstance(data, list):
        results = data
    else:
        results = []

    return jsonify({"tab": tab, "results": results, "total": len(results)})


# ═══════════════════════════════════════════════════════════════════════════
#  Health check
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/health")
def health():
    """Lightweight health check — also tests API connectivity."""
    t0 = time.time()
    _, err = api_request("GET", "/", timeout=3, max_retries=0, use_cache=False)
    api_ms = round((time.time() - t0) * 1000)
    return jsonify({
        "status": "ok" if not err else "degraded",
        "api_connect_ms": api_ms,
        "api_error": err,
    })


# ═══════════════════════════════════════════════════════════════════════════
#  Cache control (manual invalidation)
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/admin/clear-cache", methods=["POST"])
@login_required
def clear_cache():
    invalidate_cache()
    flash("API cache cleared.", "info")
    return redirect(request.referrer or url_for("index"))


# ═══════════════════════════════════════════════════════════════════════════
#  Error handlers
# ═══════════════════════════════════════════════════════════════════════════

@app.errorhandler(404)
def not_found(e):
    return render_template("base.html", content="<h3>Page not found</h3>"), 404


@app.errorhandler(500)
def server_error(e):
    return (
        render_template("base.html", content="<h3>Internal server error</h3>"),
        500,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Entrypoint
# ═══════════════════════════════════════════════════════════════════════════

@app.cli.command("reset-settings")
def reset_settings_command():
    """Reset the WildDuck API URL and token to their default values."""
    init_db()
    setting = db.session.get(Setting, 1)
    if setting:
        setting.api_url = "http://127.0.0.1:8080"
        setting.api_token = ""
        db.session.commit()
    print("Settings have been reset to defaults.")


if __name__ == "__main__":
    init_db()
    app.run(debug=config.FLASK_DEBUG, host="0.0.0.0", port=5000)
