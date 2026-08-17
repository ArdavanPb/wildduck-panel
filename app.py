import os
import secrets
import time
from datetime import datetime, timezone
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

# ── Admin credentials ─────────────────────────────────────────────────
ADMIN_USERNAME = config.ADMIN_USERNAME

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

# ── Login rate limiting (in-memory) ──────────────────────────────────────
LOGIN_ATTEMPTS = TTLCache(maxsize=1024, ttl=900)    # 15-minute window
MAX_LOGIN_ATTEMPTS = 5


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
    admin_password_hash = db.Column(db.String(255), nullable=True)


class AuditLog(db.Model):
    """Append-only record of admin actions."""

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    username = db.Column(db.String(128), nullable=True)
    ip = db.Column(db.String(64), nullable=True)
    action = db.Column(db.String(128), nullable=False)
    detail = db.Column(db.String(512), nullable=True)


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════

def get_admin_password_hash():
    """Return the current admin password hash from the database.

    Falls back to the ADMIN_PASSWORD environment variable if the hash has
    not been persisted yet (e.g. before first init or migration).
    """
    s = db.session.get(Setting, 1)
    if s is not None and s.admin_password_hash:
        return s.admin_password_hash
    return generate_password_hash(config.ADMIN_PASSWORD)

def get_api_settings():
    """Return (api_url, api_token) from the database.

    Falls back to defaults if no row exists yet (e.g. before first init).
    """
    s = db.session.get(Setting, 1)
    if s is None:
        return "http://127.0.0.1:8080", ""
    return s.api_url.rstrip("/"), s.api_token


# ═══════════════════════════════════════════════════════════════════════════
#  CSRF, audit logging, and rate limiting
# ═══════════════════════════════════════════════════════════════════════════

def get_csrf_token():
    """Return (creating if necessary) the per-session CSRF token."""
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(32)
        session["csrf_token"] = token
    return token


def _request_csrf_token():
    """Extract a CSRF token from the request (form, header, or JSON body)."""
    token = request.form.get("csrf_token")
    if token:
        return token
    token = request.headers.get("X-CSRF-Token")
    if token:
        return token
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        return body.get("csrf_token")
    return None


def audit_log(action, detail=None, username=None):
    """Append an entry to the audit log (best-effort, never raises)."""
    try:
        entry = AuditLog(
            timestamp=datetime.now(timezone.utc),
            username=username if username is not None else session.get("username"),
            ip=request.headers.get("X-Forwarded-For", request.remote_addr),
            action=action,
            detail=detail,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()


def client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"


def is_login_blocked():
    """Return True if this client is currently rate-limited."""
    attempts = LOGIN_ATTEMPTS.get(client_ip(), 0)
    return attempts >= MAX_LOGIN_ATTEMPTS


def register_login_failure():
    key = client_ip()
    LOGIN_ATTEMPTS[key] = LOGIN_ATTEMPTS.get(key, 0) + 1


def reset_login_attempts():
    LOGIN_ATTEMPTS.pop(client_ip(), None)


def gb_to_bytes(gb):
    """Convert a gigabyte value to bytes for the WildDuck quota field."""
    try:
        return int(float(gb) * 1073741824)
    except (TypeError, ValueError):
        return None


def bytes_to_gb(value):
    """Convert a byte value to gigabytes (float) for display."""
    try:
        return round(float(value) / 1073741824, 2)
    except (TypeError, ValueError):
        return None


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


def _extract_results(data, *keys):
    """Return a list of result records from a WildDuck API response.

    Handles both plain-list responses and the dict envelope shape
    (``results`` / ``users`` / ``addresses`` / ``domains`` / ``aliases``).
    """
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in keys:
        val = data.get(key)
        if isinstance(val, list):
            return val
    results = data.get("results")
    return results if isinstance(results, list) else []


def _domain_of(address):
    """Extract the lowercased domain from an email address string."""
    if isinstance(address, str) and "@" in address:
        return address.rsplit("@", 1)[1].lower()
    return None


def get_domains_overview():
    """Build a list of domains from addresses, aliases, and DKIM keys.

    WildDuck's REST API has no dedicated domains collection, so the domain
    list is derived from the records that reference one. Returns
    ``(domains, error)`` where each domain is a dict with ``domain``,
    ``is_alias``, ``aliases`` (list of ``{id, alias}``), and ``dkim``
    (``{id, selector}`` or ``None``).
    """
    addresses, err = api_request("GET", "/addresses", params={"limit": 250})
    if err:
        return None, err
    aliases, err = api_request("GET", "/domainaliases")
    if err:
        return None, err
    dkim_keys, err = api_request("GET", "/dkim")
    if err:
        return None, err

    domains = {}

    def touch(domain):
        if not domain:
            return
        domains.setdefault(
            domain,
            {"domain": domain, "is_alias": False, "aliases": [], "dkim": None},
        )

    for a in _extract_results(addresses, "addresses"):
        touch(_domain_of(a.get("address")))

    for al in _extract_results(aliases, "aliases"):
        domain = (al.get("domain") or "").lower()
        alias = (al.get("alias") or "").lower()
        touch(domain)
        touch(alias)
        if domain:
            domains[domain]["aliases"].append({"id": al.get("id"), "alias": alias})
        if alias:
            domains[alias]["is_alias"] = True

    for dk in _extract_results(dkim_keys):
        domain = (dk.get("domain") or "").lower()
        touch(domain)
        if domain:
            domains[domain]["dkim"] = {"id": dk.get("id"), "selector": dk.get("selector")}

    return list(domains.values()), None


def dns_check_records(domain, dkim_selector=None, expected_dkim=None):
    """Resolve NS/MX/SPF/DKIM records for a domain and flag problems.

    Uses dnspython lazily so the panel still starts if the optional
    dependency is missing. Returns a dict with ``records`` and ``issues``.
    """
    try:
        import dns.resolver
    except ImportError:
        return {
            "records": {},
            "issues": [{"type": "dns", "message": "dnspython is not installed."}],
        }

    records = {"ns": [], "mx": [], "spf": [], "dkim": []}
    issues = []

    def resolve(name, rdtype):
        try:
            return dns.resolver.resolve(name, rdtype)
        except dns.resolver.NoAnswer:
            return []
        except dns.resolver.NXDOMAIN:
            return []
        except Exception:
            return None  # lookup failure (timeout, no resolver, etc.)

    ns = resolve(domain, "NS")
    if ns is None:
        issues.append({"type": "ns", "message": "DNS lookup failed."})
    elif not ns:
        issues.append({"type": "ns", "message": "No nameservers found."})
    else:
        records["ns"] = [str(r) for r in ns]

    mx = resolve(domain, "MX")
    if mx is None:
        issues.append({"type": "mx", "message": "DNS lookup failed."})
    elif not mx:
        issues.append({"type": "mx", "message": "No MX records found."})
    else:
        records["mx"] = sorted(
            ({"priority": r.preference, "exchange": str(r.exchange).rstrip(".")} for r in mx),
            key=lambda r: r["priority"],
        )

    txt = resolve(domain, "TXT")
    if txt is None:
        issues.append({"type": "spf", "message": "DNS lookup failed."})
    else:
        # Reassemble chunked TXT records and keep only SPF entries.
        spf = [
            "".join(part.decode() if isinstance(part, bytes) else part for part in r.strings)
            for r in txt
        ]
        spf = [s for s in spf if s.lower().startswith("v=spf1")]
        records["spf"] = spf
        if not spf:
            issues.append({"type": "spf", "message": "No SPF record found."})
        elif len(spf) > 1:
            issues.append({"type": "spf", "message": "Multiple SPF records found."})

    if dkim_selector:
        dkim_txt = resolve(f"{dkim_selector}._domainkey.{domain}", "TXT")
        if dkim_txt is None:
            issues.append({"type": "dkim", "message": "DNS lookup failed."})
        else:
            dkim_values = [
                "".join(part.decode() if isinstance(part, bytes) else part for part in r.strings)
                for r in dkim_txt
            ]
            dkim_values = [v for v in dkim_values if "DKIM1" in v]
            records["dkim"] = dkim_values
            if not dkim_values:
                issues.append({"type": "dkim", "message": "No DKIM record published."})
            elif expected_dkim and expected_dkim not in dkim_values:
                issues.append({"type": "dkim", "message": "DKIM record does not match the configured key."})
            elif len(dkim_values) > 1:
                issues.append({"type": "dkim", "message": "Multiple DKIM records found."})

    return {"records": records, "issues": issues}


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

        # ── Migration: add admin_password_hash column if missing ────────
        columns = [
            row[1]
            for row in db.session.execute(db.text("PRAGMA table_info(setting)"))
        ]
        if "admin_password_hash" not in columns:
            db.session.execute(
                db.text(
                    "ALTER TABLE setting "
                    "ADD COLUMN admin_password_hash VARCHAR(255)"
                )
            )
            db.session.commit()

        setting = db.session.get(Setting, 1)
        if setting is None:
            default_url = os.environ.get(
                "WILDDUCK_API_URL", "http://127.0.0.1:8080"
            )
            default_token = os.environ.get("WILDDUCK_API_TOKEN", "")
            setting = Setting(
                api_url=default_url,
                api_token=default_token,
                admin_password_hash=generate_password_hash(
                    config.ADMIN_PASSWORD
                ),
            )
            db.session.add(setting)
            db.session.commit()
        elif not setting.admin_password_hash:
            setting.admin_password_hash = generate_password_hash(
                config.ADMIN_PASSWORD
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


@app.before_request
def _csrf_protect():
    """Validate CSRF tokens on all state-changing requests."""
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None

    token = _request_csrf_token()
    expected = session.get("csrf_token")
    if not token or not expected or not secrets.compare_digest(token, expected):
        audit_log("csrf_failure", f"{request.method} {request.path}")
        if request.path.startswith("/api/") or request.is_json:
            return jsonify({"error": "Invalid CSRF token."}), 400
        flash("Invalid or missing CSRF token. Please try again.", "danger")
        return redirect(request.referrer or url_for("index"))

    return None


@app.context_processor
def inject_globals():
    return {"csrf_token": get_csrf_token()}


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


@app.after_request
def _audit_state_changes(response):
    """Record every successful state-changing request in the audit log."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path != "/login":
        if 200 <= response.status_code < 400:
            audit_log(f"{request.method} {request.path}")
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
        if is_login_blocked():
            error = "Too many failed attempts. Please wait 15 minutes."
            return render_template("login.html", error=error), 429

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and check_password_hash(
            get_admin_password_hash(), password
        ):
            reset_login_attempts()
            session["logged_in"] = True
            session["username"] = username
            audit_log("login_success", username=username)
            flash("Logged in successfully.", "success")
            next_page = request.args.get("next")
            if next_page and next_page.startswith("/"):
                return redirect(next_page)
            return redirect(url_for("index"))
        register_login_failure()
        audit_log("login_failure", f"username={username or '(empty)'}")
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
    quota = request.form.get("quota", "1").strip()
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
    quota_bytes = gb_to_bytes(quota)
    payload["quota"] = quota_bytes if quota_bytes and quota_bytes > 0 else 1073741824
    try:
        payload["recipients"] = int(recipients)
    except ValueError:
        payload["recipients"] = 2000
    try:
        payload["forwards"] = int(forwards)
    except ValueError:
        payload["forwards"] = 2000
    if spam_level:
        try:
            payload["spamLevel"] = int(spam_level)
        except ValueError:
            pass

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
#  Edit user
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/user/<user_id>/edit", methods=["GET", "POST"])
@login_required
def edit_user(user_id):
    user, err = api_request("GET", f"/users/{user_id}")
    if err:
        flash(f"Failed to fetch user: {err}", "danger")
        return redirect(url_for("index"))

    if request.method == "GET":
        addresses = []
        addr_data, addr_err = api_request("GET", f"/users/{user_id}/addresses")
        if addr_err is None:
            addresses = _extract_results(addr_data)
        return render_template("edit_user.html", user=user, addresses=addresses)

    name = request.form.get("name", "").strip()
    password = request.form.get("password", "")
    quota = request.form.get("quota", "").strip()
    recipients = request.form.get("recipients", "").strip()
    forwards = request.form.get("forwards", "").strip()
    spam_level = request.form.get("spam_level", "").strip()

    payload = {}
    if name:
        payload["name"] = name
    if password:
        payload["password"] = password
    if quota:
        quota_bytes = gb_to_bytes(quota)
        if quota_bytes is not None and quota_bytes > 0:
            payload["quota"] = quota_bytes
    if recipients:
        try:
            payload["recipients"] = int(recipients)
        except ValueError:
            pass
    if forwards:
        try:
            payload["forwards"] = int(forwards)
        except ValueError:
            pass
    if spam_level:
        try:
            payload["spamLevel"] = int(spam_level)
        except ValueError:
            pass

    if not payload:
        flash("No changes to save.", "info")
        return redirect(url_for("user_details", user_id=user_id))

    _, err = api_request("PUT", f"/users/{user_id}", json_data=payload)
    if err:
        flash(f"Failed to update user: {err}", "danger")
        return render_template("edit_user.html", user=user, addresses=[]), 400

    invalidate_cache("/users")
    flash("User updated successfully.", "success")
    return redirect(url_for("user_details", user_id=user_id))


# ═══════════════════════════════════════════════════════════════════════════
#  User address (alias) management
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/user/<user_id>/address", methods=["POST"])
@login_required
def add_user_address(user_id):
    address = request.form.get("address", "").strip()
    name = request.form.get("name", "").strip()

    if not address:
        flash("Address is required.", "danger")
        return redirect(url_for("edit_user", user_id=user_id))

    payload = {"address": address}
    if name:
        payload["name"] = name

    _, err = api_request("POST", f"/users/{user_id}/addresses", json_data=payload)
    if err:
        flash(f"Failed to add address: {err}", "danger")
    else:
        invalidate_cache("/users")
        flash(f"Address '{address}' added.", "success")
    return redirect(url_for("edit_user", user_id=user_id))


@app.route("/user/<user_id>/address/<path:address>/delete", methods=["POST"])
@login_required
def remove_user_address(user_id, address):
    _, err = api_request("DELETE", f"/users/{user_id}/addresses/{address}")
    if err:
        flash(f"Failed to remove address: {err}", "danger")
    else:
        invalidate_cache("/users")
        flash(f"Address '{address}' removed.", "success")
    return redirect(url_for("edit_user", user_id=user_id))


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
        return render_template(
            "settings.html",
            api_url=setting.api_url if setting else "",
            api_token=setting.api_token if setting else "",
        )

    # ── POST: save or test ──────────────────────────────────────────────
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
#  Change admin password
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/settings/password", methods=["POST"])
@login_required
def change_admin_password():
    current = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")

    if not check_password_hash(get_admin_password_hash(), current):
        flash("Current password is incorrect.", "danger")
        return redirect(url_for("settings"))

    if len(new_password) < 8:
        flash("New password must be at least 8 characters.", "danger")
        return redirect(url_for("settings"))

    if new_password != confirm:
        flash("New passwords do not match.", "danger")
        return redirect(url_for("settings"))

    setting = db.session.get(Setting, 1)
    if setting is None:
        setting = Setting(api_url="http://127.0.0.1:8080", api_token="")
        db.session.add(setting)
    setting.admin_password_hash = generate_password_hash(new_password)
    db.session.commit()

    flash("Admin password updated successfully.", "success")
    return redirect(url_for("settings"))


# ═══════════════════════════════════════════════════════════════════════════
#  Address (forwarder) management
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/address/create", methods=["GET", "POST"])
@login_required
def create_address():
    if request.method == "GET":
        return render_template("create_address.html")

    address = request.form.get("address", "").strip()
    name = request.form.get("name", "").strip()
    targets = [
        t.strip()
        for t in request.form.get("targets", "").splitlines()
        if t.strip()
    ]

    if not address:
        flash("Address is required.", "danger")
        return render_template("create_address.html"), 400

    payload = {"address": address}
    if name:
        payload["name"] = name
    if targets:
        payload["targets"] = targets

    _, err = api_request("POST", "/addresses/forwarded", json_data=payload)
    if err:
        flash(f"Failed to create address: {err}", "danger")
        return render_template("create_address.html"), 400

    invalidate_cache("/addresses")
    flash(f"Address '{address}' created successfully.", "success")
    return redirect(url_for("index", tab="addresses"))


@app.route("/address/<path:address>/edit", methods=["GET", "POST"])
@login_required
def edit_address(address):
    data, err = api_request("GET", f"/addresses/resolve/{address}")
    if err:
        flash(f"Failed to fetch address: {err}", "danger")
        return redirect(url_for("index", tab="addresses"))

    if request.method == "GET":
        return render_template("edit_address.html", address=data)

    name = request.form.get("name", "").strip()
    targets = [
        t.strip()
        for t in request.form.get("targets", "").splitlines()
        if t.strip()
    ]

    payload = {}
    if name:
        payload["name"] = name
    if targets:
        payload["targets"] = targets

    addr_id = data.get("id")
    _, err = api_request("PUT", f"/addresses/forwarded/{addr_id}", json_data=payload)
    if err:
        flash(f"Failed to update address: {err}", "danger")
        return render_template("edit_address.html", address=data), 400

    invalidate_cache("/addresses")
    flash("Address updated successfully.", "success")
    return redirect(url_for("index", tab="addresses"))


@app.route("/address/<path:address>/delete", methods=["DELETE"])
@login_required
def delete_address(address):
    _, err = api_request("DELETE", f"/addresses/forwarded/{address}")
    if err:
        return jsonify({"error": err}), 400
    invalidate_cache("/addresses")
    return jsonify({"status": "deleted"})


# ═══════════════════════════════════════════════════════════════════════════
#  Domain & domain alias management
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/domain/alias", methods=["POST"])
@login_required
def create_domain_alias():
    domain = request.form.get("domain", "").strip().lower()
    alias = request.form.get("alias", "").strip().lower()

    if not domain or not alias:
        flash("Both the domain and alias are required.", "danger")
        return redirect(url_for("index", tab="domains"))

    _, err = api_request(
        "POST", "/domainaliases", json_data={"domain": domain, "alias": alias}
    )
    if err:
        flash(f"Failed to add domain alias: {err}", "danger")
        return redirect(url_for("index", tab="domains"))

    invalidate_cache("/domainaliases")
    invalidate_cache("/addresses")
    flash(f"Alias '{alias}' added to '{domain}'.", "success")
    return redirect(url_for("index", tab="domains"))


@app.route("/domain/alias/<alias_id>/delete", methods=["DELETE"])
@login_required
def delete_domain_alias(alias_id):
    _, err = api_request("DELETE", f"/domainaliases/{alias_id}")
    if err:
        return jsonify({"error": err}), 400
    invalidate_cache("/domainaliases")
    invalidate_cache("/addresses")
    return jsonify({"status": "deleted"})


@app.route("/domain/<path:domain>/delete", methods=["DELETE"])
@login_required
def delete_domain(domain):
    domain = domain.lower()
    errors = []

    dkim, err = api_request("GET", f"/dkim/resolve/{domain}")
    if err is None and isinstance(dkim, dict) and dkim.get("id"):
        _, err = api_request("DELETE", f"/dkim/{dkim['id']}")
        if err:
            errors.append(f"DKIM: {err}")

    aliases, err = api_request("GET", "/domainaliases")
    if err is None:
        for al in _extract_results(aliases, "aliases"):
            if (al.get("domain") or "").lower() == domain or (al.get("alias") or "").lower() == domain:
                _, err = api_request("DELETE", f"/domainaliases/{al.get('id')}")
                if err:
                    errors.append(f"Alias {al.get('alias')}: {err}")

    addresses, err = api_request("GET", "/addresses", params={"limit": 250})
    if err is None:
        for a in _extract_results(addresses, "addresses"):
            if _domain_of(a.get("address")) != domain:
                continue
            if a.get("forwarded"):
                _, err = api_request("DELETE", f"/addresses/forwarded/{a.get('address')}")
            elif a.get("user"):
                _, err = api_request("DELETE", f"/users/{a.get('user')}/addresses/{a.get('address')}")
            else:
                continue
            if err:
                errors.append(f"Address {a.get('address')}: {err}")

    invalidate_cache()
    if errors:
        return jsonify({"error": "Some resources could not be removed: " + "; ".join(errors)}), 400
    return jsonify({"status": "deleted"})


# ═══════════════════════════════════════════════════════════════════════════
#  DKIM management
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/dkim", methods=["GET", "POST"])
@login_required
def dkim():
    generated = None

    if request.method == "POST":
        domain = request.form.get("domain", "").strip().lower()
        selector = request.form.get("selector", "").strip() or "default"
        if not domain:
            flash("Domain is required.", "danger")
            return redirect(url_for("dkim"))

        result, err = api_request(
            "POST", "/dkim", json_data={"domain": domain, "selector": selector}
        )
        if err:
            flash(f"Failed to generate DKIM key: {err}", "danger")
        else:
            invalidate_cache("/dkim")
            generated = result
            flash(
                f"DKIM key generated for {domain}. Publish the DNS TXT record shown below.",
                "success",
            )

    data, err = api_request("GET", "/dkim")
    keys = [] if err else _extract_results(data)
    if err:
        flash(f"Failed to list DKIM keys: {err}", "danger")
    return render_template("dkim.html", dkim_keys=keys, generated=generated)


@app.route("/dkim/<dkim_id>")
@login_required
def dkim_details(dkim_id):
    data, err = api_request("GET", f"/dkim/{dkim_id}")
    if err:
        flash(f"Failed to fetch DKIM key: {err}", "danger")
        return redirect(url_for("dkim"))
    return render_template("dkim_details.html", key=data)


@app.route("/dkim/<dkim_id>/delete", methods=["POST"])
@login_required
def delete_dkim(dkim_id):
    _, err = api_request("DELETE", f"/dkim/{dkim_id}")
    if err:
        flash(f"Failed to delete DKIM key: {err}", "danger")
    else:
        invalidate_cache("/dkim")
        flash("DKIM key deleted.", "success")
    return redirect(url_for("dkim"))


# ═══════════════════════════════════════════════════════════════════════════
#  DNS check
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/domain/<path:domain>/dns")
@login_required
def domain_dns(domain):
    domain = domain.lower()
    dkim_selector = None
    expected_dkim = None

    dkim, err = api_request("GET", f"/dkim/resolve/{domain}")
    if err is None and isinstance(dkim, dict) and dkim.get("id"):
        key, err2 = api_request("GET", f"/dkim/{dkim['id']}")
        if err2 is None and isinstance(key, dict):
            dkim_selector = key.get("selector")
            txt = key.get("dnsTxt") or {}
            expected_dkim = txt.get("value")

    result = dns_check_records(
        domain, dkim_selector=dkim_selector, expected_dkim=expected_dkim
    )
    return render_template(
        "dns_check.html",
        domain=domain,
        records=result["records"],
        issues=result["issues"],
        dkim_selector=dkim_selector,
        expected_dkim=expected_dkim,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  AJAX dashboard data endpoint
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/dashboard")
@login_required
def api_dashboard():
    """Return dashboard data as JSON for AJAX-based loading."""
    tab = request.args.get("tab", "users")
    search_query = request.args.get("query", "").strip()
    limit = request.args.get("limit", 50, type=int)
    page_next = request.args.get("next")
    page_previous = request.args.get("previous")

    total = 0
    next_cursor = None
    previous_cursor = None

    if tab == "users":
        params = {"limit": limit}
        if search_query:
            params["query"] = search_query
        if page_next:
            params["next"] = page_next
        if page_previous:
            params["previous"] = page_previous
        data, err = api_request("GET", "/users", params=params)
        results = _extract_results(data, "users")
        if isinstance(data, dict):
            total = data.get("total", len(results))
            next_cursor = data.get("nextCursor")
            previous_cursor = data.get("previousCursor")
    elif tab == "domains":
        results, err = get_domains_overview()
        if not err and search_query:
            q = search_query.lower()
            results = [d for d in results if q in d["domain"]]
        total = len(results)
    elif tab == "addresses":
        params = {"limit": limit}
        if search_query:
            params["query"] = search_query
        if page_next:
            params["next"] = page_next
        if page_previous:
            params["previous"] = page_previous
        data, err = api_request("GET", "/addresses", params=params)
        results = _extract_results(data, "addresses")
        if isinstance(data, dict):
            total = data.get("total", len(results))
            next_cursor = data.get("nextCursor")
            previous_cursor = data.get("previousCursor")
    else:
        return jsonify({"error": "Invalid tab"}), 400

    if err:
        return jsonify({"error": err}), 502

    return jsonify({
        "tab": tab,
        "results": results,
        "total": total,
        "next": next_cursor,
        "previous": previous_cursor,
        "limit": limit,
    })


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
#  Audit log
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/audit")
@login_required
def audit():
    entries = (
        AuditLog.query.order_by(AuditLog.id.desc()).limit(500).all()
    )
    return render_template("audit.html", entries=entries)


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
