# AGENTS.md — WildDuck Admin Panel

## Overview

A single-file Flask web application that provides a web UI for managing a [WildDuck Mail Server](https://github.com/nodemailer/wildduck) via its REST API. It proxies authenticated requests to the WildDuck API and renders Bootstrap 5 Jinja2 templates.

## Project Structure

```
wildduck-panel/
├── app.py                     # Entire Flask application (routes + API helper + auth)
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
- **No pagination**: all users/domains/addresses are fetched at once. The WildDuck API may have server-side pagination but the panel doesn't use it.
- **Toggle endpoint does a GET first**: the `toggle_user_status` route fetches the user's current state before applying the toggle. This ensures the correct `disabled` value is sent.
- **Quota display in bytes**: storage is always in bytes. The `_users_table.html` partial displays as MB/GB using hardcoded division constants (1048576, 1073741824). The `user_details.html` template uses a `pretty_size` macro for the same purpose.
- **Flask version is pinned to 3.0.3** — this is the latest available on the target Python version. Don't bump without verifying compatibility.
- **No CSRF protection**: forms don't use CSRF tokens. The panel is designed for internal/trusted network use behind the WildDuck server's auth.
- **Secret key default is hardcoded** — the `FLASK_SECRET_KEY` falls back to `"dev-secret-change-me"`. Always set this in production.
- **Health check hits `/login`**: the Docker health check curls `/login`, which works without auth.
