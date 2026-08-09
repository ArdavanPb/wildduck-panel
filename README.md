# WildDuck Admin Panel

A modern, production-ready Flask web application for managing a
[WildDuck Mail Server](https://github.com/nodemailer/wildduck) via its REST API.

## Table of Contents

- [Features](#features)
- [Project Structure](#project-structure)
- [Quick Start (Local)](#quick-start-local)
- [Quick Start (Docker)](#quick-start-docker)
- [Configuration](#configuration)
  - [Static settings (`config.py`)](#static-settings-configpy)
  - [Configuration via Web Interface](#configuration-via-web-interface)
- [Docker API URL](#docker-api-url)
- [Session Persistence](#session-persistence)
- [CLI Commands](#cli-commands)
- [API Reference](#api-reference)
- [Dependencies](#dependencies)

## Features

- **Authentication** — Login-protected panel with bcrypt-hashed password.
  Sessions are stored in SQLite and survive restarts.
- **Dynamic API configuration** — Change the WildDuck API URL and token
  on the fly via the **Settings** page. No `.env` file, no restart required.
- **User Dashboard** — Search, list, and manage WildDuck users.
- **Create User** — Form-driven user creation with quota, spam level, and
  rate limit settings.
- **Enable / Disable** — One-click toggle to suspend or restore user accounts.
- **Delete User** — Permanently remove users with a confirmation prompt.
- **User Details** — View full profile including mailbox storage usage,
  quota, and message counts.
- **Domain Browser** — List all configured domains.
- **Address / Alias Browser** — List all email addresses and aliases with
  forwarding targets.
- **Connection retry** — Transient API failures (timeout, connection) are
  retried automatically before showing an error.
- **CLI support** — Reset API settings from the command line with
  `flask reset-settings`.

## Project Structure

```
wildduck-panel/
├── app.py                          # Flask application (routes + API helpers)
├── config.py                       # Static config (admin credentials, secret key)
├── requirements.txt                # Python dependencies
├── Dockerfile                      # python:3.13-alpine, runs as non-root
├── docker-compose.yml              # Docker Compose service definition
├── .gitignore
├── templates/
│   ├── base.html                   # Base layout with Bootstrap 5 + JavaScript
│   ├── login.html                  # Login page
│   ├── index.html                  # Dashboard (Users / Domains / Addresses tabs)
│   ├── create_user.html            # User creation form
│   ├── user_details.html           # User profile + mailbox statistics
│   ├── settings.html               # API URL / token configuration
│   └── partials/
│       ├── _users_table.html       # Users table partial
│       ├── _domains_table.html     # Domains table partial
│       └── _addresses_table.html   # Addresses table partial
└── README.md
```

## Quick Start (Local)

```bash
# 1. Clone the repository
cd wildduck-panel

# 2. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate       # Linux / macOS
# venv\Scripts\activate        # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run the application
python app.py
```

The panel starts at **http://localhost:5000**.

Default login: **admin** / **admin** (change these in `config.py`).

On first run the application creates a SQLite database (`instance/panel.db`)
that stores both the API connection settings and user sessions.

## Quick Start (Docker)

```bash
# 1. (Optional) Pre-seed the API URL so you don't have to visit the
#    Settings page right away.  Skip this step if you want to configure
#    everything through the web UI after login.
export WILDDUCK_API_URL="http://host.docker.internal:8080"
export WILDDUCK_API_TOKEN="your-real-token"

# 2. Build and start
docker compose up -d

# 3. View logs
docker compose logs -f
```

The container runs as a non-root `wildduck` user and includes a health
check on `/login`.

**After first login**, go to **Settings** (in the navbar) and enter your
real WildDuck API URL and token.  Click **Save & Test** to verify the
connection.

> **Tip:** You can also set the API URL in `docker-compose.yml` via the
> `environment` block — see the commented-out example in the file.

## Configuration

There is **no `.env` file** and **no `python-dotenv` dependency**.
Settings are managed in two places:

### Static settings (`config.py`)

These rarely change and require an application restart:

| Variable          | Description                   | Default              |
|-------------------|-------------------------------|----------------------|
| `ADMIN_USERNAME`  | Panel login username          | `admin`              |
| `ADMIN_PASSWORD`  | Panel login password          | `admin`              |
| `SECRET_KEY`      | Flask session signing key     | `a-very-secret-key`  |
| `FLASK_DEBUG`     | Enable Flask debug mode       | `false`              |

All of these can also be overridden via environment variables
(`ADMIN_USERNAME`, `ADMIN_PASSWORD`, `FLASK_SECRET_KEY`, `FLASK_DEBUG`).

### Configuration via Web Interface

API connection settings live in the SQLite database and are managed
through the **Settings** page (navbar link, visible after login).

1. Log in with your admin credentials.
2. Click **Settings** in the top navigation bar.
3. Enter your **WildDuck API URL** (e.g. `http://127.0.0.1:8080`).
4. Enter your **API Access Token** (sent as `X-Access-Token` header).
5. Click **Save & Test** — the panel immediately applies the new values
   and performs a connectivity check against the API.

| Button           | What it does                                              |
|------------------|-----------------------------------------------------------|
| **Save & Test**  | Saves the settings and tests the connection.              |
| **Test Connection** | Tests the current API URL/token without saving.       |
| **Reset to Defaults** | Resets URL to `http://127.0.0.1:8080` and token to empty. |

Changes take effect **immediately** — all subsequent API calls use the
new values.  No restart required.

## Docker API URL

When running the WildDuck API on the Docker **host**, the panel
container cannot reach it at `127.0.0.1` (that address refers to the
container itself).  Use one of these instead:

| Platform / Setup                  | API URL                                  |
|-----------------------------------|------------------------------------------|
| Windows / macOS (Docker Desktop)  | `http://host.docker.internal:8080`       |
| Linux (native Docker)             | `http://172.17.0.1:8080`                |
| Docker Compose (same network)     | `http://wildduck:8080` (service name)    |

The `docker-compose.yml` already includes `extra_hosts` for
`host.docker.internal`, so the first option works out of the box on all
platforms with Docker Desktop.

Set the URL once via the **Settings** page, or pre-seed it with the
`WILDDUCK_API_URL` environment variable on first run (see Quick Start).

## Session Persistence

Login sessions are stored in the SQLite database (`instance/panel.db`)
using Flask-Session with a SQLAlchemy backend.  This means:

- Logged-in users stay authenticated across application restarts.
- No in-memory state is lost when the server reloads or crashes.
- The session database is automatically created on first run.

## CLI Commands

The application exposes a Flask CLI command for maintenance:

```bash
# Activate the virtual environment first
source venv/bin/activate

# Reset API settings to defaults (http://127.0.0.1:8080, empty token)
flask --app app reset-settings
```

## API Reference

All requests to the WildDuck API include the header `X-Access-Token: <token>`.

| Action              | Method | Endpoint                  |
|---------------------|--------|---------------------------|
| List / Search Users | GET    | `/users?query=...`        |
| Create User         | POST   | `/users`                  |
| Get User            | GET    | `/users/{id}`             |
| Update User         | PUT    | `/users/{id}`             |
| Delete User         | DELETE | `/users/{id}`             |
| Get Mailbox         | GET    | `/users/{id}/mailbox`     |
| List Domains        | GET    | `/domains`                |
| List Addresses      | GET    | `/addresses?query=...`    |

## Dependencies

```
Flask==3.0.3
requests==2.32.4
Flask-Session==0.8.0
Flask-SQLAlchemy==3.1.1
SQLAlchemy==2.0.35
cachelib==0.9.0
```
