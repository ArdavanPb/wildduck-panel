# AGENTS.md — WildDuck Admin Panel

## Overview

A single-file Flask web application that provides a web UI for managing a [WildDuck Mail Server](https://github.com/nodemailer/wildduck) via its REST API. It proxies authenticated requests to the WildDuck API and renders Bootstrap 5 Jinja2 templates.

## Project Structure

```
wildduck-panel/
├── app.py                     # Entire Flask application (routes + API helper + auth)
├── config.py                  # Static config (admin creds, secret key, debug)
├── requirements.txt           # Python dependencies
├── Dockerfile                 # python:3.13-alpine, runs as non-root "wildduck"
├── docker-compose.yml         # Single-service compose, reads .env
├── .env.example               # Template for required env vars
├── .env                       # Actual secrets (gitignored)
├── templates/
│   ├── base.html              # Root layout (navbar, flash toasts, JS helpers)
│   ├── login.html             # Standalone login page (does NOT extend base.html)
│   ├── index.html             # Tabbed dashboard (users/domains/addresses)
│   ├── create_user.html       # User creation form
│   ├── user_details.html      # User profile + mailbox stats
│   ├── create_address.html    # Forwarder / alias creation form
│   ├── edit_address.html      # Forwarder edit form
│   ├── dkim.html              # DKIM key list + generation
│   ├── dkim_details.html      # Single DKIM key (DNS TXT record)
│   ├── dns_check.html         # Per-domain DNS verification
│   └── partials/
│       ├── _users_table.html
│       ├── _domains_table.html
│       └── _addresses_table.html
└── AGENTS.md
```

## Commands

```bash
# Local development
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then edit .env with real values
python app.py           # starts on http://0.0.0.0:5000

# Docker
cp .env.example .env    # edit .env first
docker compose up -d
docker compose logs -f
```

There are no tests, linters, formatters, Makefiles, or CI pipelines. `FLASK_DEBUG=true` in `.env` enables debug mode (auto-reload, debugger).

## Architecture & Data Flow

```
Browser ──► Flask (app.py) ──► WildDuck REST API (:8080)
                │                      │
                ▼                      ▼
           Jinja2 templates      JSON responses
```

Everything lives in `app.py`:

- **Auth**: `login_required` decorator checks `session["logged_in"]`. Admin credentials are bcrypt-hashed at startup from env vars. The `login.html` template is standalone (does not extend `base.html`).
- **API proxy**: `api_request(method, path, json_data, params)` returns `(data, error)` — a tuple of `(parsed JSON or None, error string or None)`. All WildDuck API calls go through this. It adds `X-Access-Token` header and handles timeouts, connection errors, auth failures, and 4xx/5xx status codes.
- **Dashboard**: `GET /?tab=users|domains|addresses&query=...` fetches data from the WildDuck API based on the active tab and renders the corresponding template partial.
- **AJAX endpoints**: `PUT /user/<id>/toggle-status` and `DELETE /user/<id>/delete` return JSON and are called by inline JS in `base.html`. The toggle first fetches current state with a GET, then sends the update. No page reload on success — the JS manipulates the DOM.

## Environment Variables

All read in `app.py` at module level via `dotenv`:

| Variable             | Default                    | Purpose                              |
|----------------------|----------------------------|--------------------------------------|
| `WILDDUCK_API_URL`   | `http://localhost:8080`    | WildDuck REST API base URL           |
| `WILDDUCK_API_TOKEN` | `""`                       | API auth token (sent as `X-Access-Token`) |
| `FLASK_SECRET_KEY`   | `dev-secret-change-me`     | Flask session signing key            |
| `FLASK_DEBUG`        | `false`                    | Enable Flask debug mode              |
| `ADMIN_USERNAME`     | `admin`                    | Panel login username                 |
| `ADMIN_PASSWORD`     | `admin`                    | Panel login password (bcrypt-hashed at startup) |

## Key Patterns & Conventions

### Template rendering

- `login.html` is a full standalone HTML page.
- All other authenticated pages extend `base.html` and use `{% block content %}`.
- `base.html` handles the navbar, flash messages (as Bootstrap toasts), and includes shared JS functions (`toggleUserStatus`, `deleteUser`, `confirmAction`).
- The 404/500 error handlers render `base.html` with an inline `content` string rather than a separate template.

### API helper conventions

```python
data, err = api_request("GET", "/users", params={"query": "foo"})
if err:
    flash(f"Failed: {err}", "danger")
    return ...
# data is dict or list from resp.json()
```

The function strips trailing slashes from `API_URL`, handles `requests.exceptions.Timeout`, `ConnectionError`, and generic `RequestException`, and parses error bodies from the WildDuck API's `{"error": "..."}` format.

### User data handling

- User records may have `storageUsed` in the top-level object (from certain WildDuck API response shapes). The users table partial reads both `u.get('storageUsed', 0)` and `u.get('quota', ...)`.
- The `api_request` helper normalizes both dict responses with `results`/`users`/`addresses` keys and plain list responses — this handles different WildDuck API versions.

### Frontend

- Bootstrap 5.3.5 CSS + JS and Bootstrap Icons 1.11.3 loaded from CDN (no local assets).
- AJAX calls use vanilla `fetch()` — no JS framework.
- Delete/toggle actions use `confirm()` for confirmation, then manipulate DOM directly.
- The `index.html` search form preserves the active tab via a hidden `tab` input.

## Gotchas

- **`login.html` does not extend `base.html`** — it's a self-contained page. Don't try to add navbar/global styles there; duplicate them inline if needed.
- **Cursor pagination**: users/addresses tabs use WildDuck `next`/`previous` cursors via `/api/dashboard` (limit defaults to 50). Domains are derived in full and sorted client-side.
- **Toggle endpoint does a GET first**: the `toggle_user_status` route fetches the user's current state before applying the toggle. This ensures the correct `disabled` value is sent.
- **Quota is entered in GB**: create/edit user forms accept gigabytes and convert with `gb_to_bytes()`/`bytes_to_gb()`; the WildDuck API still uses bytes.
- **Flask version is pinned to 3.0.3** — this is the latest available on the target Python version. Don't bump without verifying compatibility.
- **Global CSRF**: a `before_request` hook validates CSRF on all state-changing methods. Templates inject a hidden `csrf_token` field (via a context processor); AJAX calls read the `<meta name="csrf-token">` tag and send it as `X-CSRF-Token` via `csrfHeaders()` in `base.html`.
- **Login rate-limiting**: in-memory `LOGIN_ATTEMPTS` TTL cache blocks an IP after `MAX_LOGIN_ATTEMPTS` failures (15 min). Audit entries are written by `audit_log()` and surfaced on `/audit`; a generic `after_request` hook logs every successful mutation.
- **Secret key default is hardcoded** — the `FLASK_SECRET_KEY` falls back to `"dev-secret-change-me"`. Always set this in production.
- **Health check hits `/login`**: the Docker health check curls `/login`, which works without auth.
- **Domains are derived, not fetched**: modern WildDuck has no `/domains` endpoint. `get_domains_overview()` builds the domain list from `/addresses`, `/domainaliases`, and `/dkim`. The dashboard `domains` tab uses this.
- **Forwarders use `/addresses/forwarded`**: create/edit/delete of forwarding addresses go through `POST/PUT /addresses/forwarded` and `DELETE /addresses/forwarded/{address}` (delete is by address string, update is by id).
- **`dnspython` is imported lazily** inside `dns_check_records()`, so the panel still starts if the dependency is missing; DNS checks just report a friendly issue instead.
- **Delete is cascade by domain**: `delete_domain()` removes the domain's DKIM key, domain aliases (both directions), and all addresses (forwarded and mailbox).
- **Tests live in `tests/test_app.py`** and run with `pytest` (dev dep in `requirements-dev.txt`); they stub `api_request` and use a throwaway SQLite DB.
