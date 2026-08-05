import os
from functools import wraps

from dotenv import load_dotenv
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
from werkzeug.security import check_password_hash, generate_password_hash

import requests

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

# ── Admin credentials (hashed at startup) ───────────────────────────────
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
_ADMIN_PASSWORD_RAW = os.getenv("ADMIN_PASSWORD", "admin")
ADMIN_PASSWORD_HASH = generate_password_hash(_ADMIN_PASSWORD_RAW)

API_URL = os.getenv("WILDDUCK_API_URL", "http://localhost:8080").rstrip("/")
API_TOKEN = os.getenv("WILDDUCK_API_TOKEN", "")
TIMEOUT = 10


# ── Auth decorator ───────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)

    return decorated


def api_request(method, path, json_data=None, params=None):
    url = f"{API_URL}{path}"
    headers = {
        "X-Access-Token": API_TOKEN,
        "Content-Type": "application/json",
    }
    try:
        resp = requests.request(
            method,
            url,
            headers=headers,
            json=json_data,
            params=params,
            timeout=TIMEOUT,
        )
    except requests.exceptions.Timeout:
        return None, f"API request timed out ({url})"
    except requests.exceptions.ConnectionError:
        return None, f"Cannot connect to WildDuck API at {API_URL}"
    except requests.exceptions.RequestException as e:
        return None, f"API request failed: {e}"

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


# ---------------------------------------------------------------------------
#  Auth routes
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
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


# ---------------------------------------------------------------------------
#  Dashboard / Home
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
#  Create user
# ---------------------------------------------------------------------------
@app.route("/user/create", methods=["GET", "POST"])
@login_required
def create_user():
    if request.method == "GET":
        return render_template("create_user.html")

    # Collect form fields
    username = request.form.get("username", "").strip()
    name = request.form.get("name", "").strip()
    password = request.form.get("password", "")
    address = request.form.get("address", "").strip()
    quota = request.form.get("quota", "1073741824").strip()  # 1 GB default
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
        flash(f"User '{username}' created successfully (ID: {user_id}).", "success")
    else:
        flash(f"User '{username}' created.", "success")

    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
#  User details
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
#  Toggle user enabled/disabled status (AJAX)
# ---------------------------------------------------------------------------
@app.route("/user/<user_id>/toggle-status", methods=["PUT"])
@login_required
def toggle_user_status(user_id):
    data, err = api_request("GET", f"/users/{user_id}")
    if err:
        flash_msg = f"Failed to fetch user: {err}"
        return jsonify({"error": flash_msg}), 400

    current_disabled = data.get("disabled", False)
    payload = {"disabled": not current_disabled}

    _, err = api_request("PUT", f"/users/{user_id}", json_data=payload)
    if err:
        flash_msg = f"Failed to update user: {err}"
        return jsonify({"error": flash_msg}), 400

    new_status = "disabled" if payload["disabled"] else "enabled"
    return jsonify({"status": new_status, "disabled": payload["disabled"]})


# ---------------------------------------------------------------------------
#  Delete user (AJAX)
# ---------------------------------------------------------------------------
@app.route("/user/<user_id>/delete", methods=["DELETE"])
@login_required
def delete_user(user_id):
    _, err = api_request("DELETE", f"/users/{user_id}")
    if err:
        return jsonify({"error": f"Failed to delete user: {err}"}), 400
    return jsonify({"status": "deleted"})


# ---------------------------------------------------------------------------
#  Error handlers
# ---------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(e):
    return render_template("base.html", content="<h3>Page not found</h3>"), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("base.html", content="<h3>Internal server error</h3>"), 500


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug, host="0.0.0.0", port=5000)
