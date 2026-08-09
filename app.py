import os
import secrets
import time
from functools import wraps

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_session import Session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

import config
import requests

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# ── Database & server-side sessions ────────────────────────────────────
app.config["SQLALCHEMY_DATABASE_URI"] = config.DATABASE_URI
db = SQLAlchemy(app)
app.config["SESSION_TYPE"] = config.SESSION_TYPE
app.config["SESSION_SQLALCHEMY"] = db
app.config["SESSION_PERMANENT"] = config.SESSION_PERMANENT
app.config["SESSION_USE_SIGNER"] = config.SESSION_USE_SIGNER
Session(app)

# ── Admin credentials (hashed at startup) ─────────────────────────────
ADMIN_USERNAME = config.ADMIN_USERNAME
ADMIN_PASSWORD_HASH = generate_password_hash(config.ADMIN_PASSWORD)

TIMEOUT = 30
MAX_RETRIES = 2
RETRY_BACKOFF = 1  # seconds


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


def api_request(method, path, json_data=None, params=None):
    """Make an authenticated request to the WildDuck REST API.

    Reads the current API URL and token from the database on every call.
    Retries once on transient errors (timeout, connection).
    """
    api_url, api_token = get_api_settings()
    url = f"{api_url}{path}"
    headers = {
        "X-Access-Token": api_token,
        "Content-Type": "application/json",
    }

    last_error = None
    for attempt in range(1 + MAX_RETRIES):
        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                json=json_data,
                params=params,
                timeout=TIMEOUT,
            )
            break  # success — exit retry loop
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * (attempt + 1))
                continue
        except requests.exceptions.RequestException as e:
            return None, f"API request failed: {e}"
    else:
        # All retries exhausted
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
        return resp.json(), None
    except Exception:
        return None, "Invalid JSON response from API"


def init_db():
    """Create tables and seed the default settings row (if missing).

    Called once at startup.  The initial api_url can be seeded from the
    WILDDUCK_API_URL environment variable so that Docker users don't have
    to visit the Settings page just to fix the default 127.0.0.1 address.
    """
    with app.app_context():
        db.create_all()
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

    users = []
    domains = []
    addresses = []
    api_error = None

    if tab == "users":
        params = {}
        if search_query:
            params["query"] = search_query
        data, err = api_request("GET", "/users", params=params)
        if err:
            api_error = err
        elif isinstance(data, dict):
            users = data.get("results", data.get("users", []))
        elif isinstance(data, list):
            users = data
    elif tab == "domains":
        data, err = api_request("GET", "/domains")
        if err:
            api_error = err
        elif isinstance(data, dict):
            domains = data.get("results", data.get("domains", []))
        elif isinstance(data, list):
            domains = data
    elif tab == "addresses":
        params = {}
        if search_query:
            params["query"] = search_query
        data, err = api_request("GET", "/addresses", params=params)
        if err:
            api_error = err
        elif isinstance(data, dict):
            addresses = data.get("results", data.get("addresses", []))
        elif isinstance(data, list):
            addresses = data

    return render_template(
        "index.html",
        tab=tab,
        search_query=search_query,
        users=users,
        domains=domains,
        addresses=addresses,
        api_error=api_error,
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

    mailbox, _ = api_request("GET", f"/users/{user_id}/mailbox")

    return render_template(
        "user_details.html",
        user=user,
        mailbox=mailbox,
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
        flash("Settings saved successfully.", "success")

    # ── Connectivity test (runs on "save" and "test" actions) ──────────
    _, err = api_request("GET", "/")
    if err:
        flash(
            f"Connection test failed: {err}",
            "warning",
        )
    else:
        test_url = new_url if action == "save" else (setting.api_url if setting else new_url)
        flash(
            f"Successfully connected to WildDuck API at {test_url}.",
            "success",
        )

    return redirect(url_for("settings"))


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
