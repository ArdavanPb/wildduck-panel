# WildDuck Admin Panel

A modern, production-ready Flask web application for managing a **WildDuck Mail Server** via its REST API.

## Features

- **Authentication** — Login-protected panel with session-based auth (bcrypt-hashed password)
- **User Dashboard** — Search, list, and paginate through WildDuck users
- **Create User** — Form-driven user creation with quota, spam level, and rate limit settings
- **Enable / Disable** — One-click toggle to suspend or restore user accounts
- **Delete User** — Permanently remove users with a confirmation prompt
- **User Details** — View full profile including mailbox storage usage, quota, and message counts
- **Domain Browser** — List all configured domains
- **Address / Alias Browser** — List all email addresses and aliases with forwarding targets

## Project Structure

```
wildduck-panel/
├── app.py                          # Flask application (routes + API helpers)
├── requirements.txt                # Python dependencies
├── Dockerfile                      # Multi-stage Docker image
├── docker-compose.yml              # Docker Compose service definition
├── .env.example                    # Environment variable template
├── .gitignore
├── .dockerignore
├── templates/
│   ├── base.html                   # Base layout with Bootstrap 5 + JavaScript
│   ├── login.html                  # Login page
│   ├── index.html                  # Dashboard with tabbed UI (Users / Domains / Addresses)
│   ├── create_user.html            # User creation form
│   ├── user_details.html           # User profile + mailbox statistics
│   └── partials/
│       ├── _users_table.html       # Users table partial
│       ├── _domains_table.html     # Domains table partial
│       └── _addresses_table.html   # Addresses table partial
└── README.md
```

## Prerequisites

- Python 3.8 or later
- A running [WildDuck Mail Server](https://github.com/nodemailer/wildduck) with its API enabled

## Setup

```bash
# 1. Clone or enter the project directory
cd wildduck-panel

# 2. Create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate      # Linux / macOS
# venv\Scripts\activate       # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create your .env file from the template
cp .env.example .env
# Edit .env with your actual WildDuck API URL and token

# 5. Run the application
python app.py
```

The panel will be available at **http://localhost:5000**.

## Running with Docker

```bash
# 1. Create and edit your .env file
cp .env.example .env
# Set WILDDUCK_API_URL, WILDDUCK_API_TOKEN, ADMIN_USERNAME, and ADMIN_PASSWORD.

# 2. Build and start
docker compose up -d

# 3. View logs
docker compose logs -f
```

The container runs as a non-root `wildduck` user and includes a health check on `/login`.

## Configuration

Edit the `.env` file:

| Variable              | Description                          | Default                     |
|----------------------|--------------------------------------|-----------------------------|
| `WILDDUCK_API_URL`   | Base URL of the WildDuck REST API   | `http://localhost:8080`     |
| `WILDDUCK_API_TOKEN` | API access token for authentication | *(required)*                |
| `FLASK_SECRET_KEY`   | Flask session secret key             | `dev-secret-change-me`      |
| `ADMIN_USERNAME`     | Panel login username                 | `admin`                     |
| `ADMIN_PASSWORD`     | Panel login password (plaintext)     | *(required)*                |

## API Reference

All requests to the WildDuck API include the header `X-Access-Token: <token>`.

| Action             | Method   | Endpoint                  |
|-------------------|----------|---------------------------|
| List / Search Users | GET    | `/users?query=...`        |
| Create User       | POST     | `/users`                  |
| Get User          | GET      | `/users/{id}`             |
| Update User       | PUT      | `/users/{id}`             |
| Delete User       | DELETE   | `/users/{id}`             |
| Get Mailbox       | GET      | `/users/{id}/mailbox`     |
| List Domains      | GET      | `/domains`                |
| List Addresses    | GET      | `/addresses?query=...`    |
