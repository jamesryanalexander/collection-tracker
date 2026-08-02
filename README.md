# Collection Tracker

A local-first Flask app that mirrors your EchoMTG inventory (read-only) and tracks where each
card *should* physically live based on its current price:

- under $1.00 -> bulk box
- $1.00-$9.99 -> sleeved, in a binder
- $10.00+ -> top loader, in the top-loader binder

Thresholds are editable in Settings. When a card's price crosses a threshold since it was last
placed, it shows up under **Flags** so you know to move it.

Sibling app: [`basic-land-tracker`](../basic-land-tracker) tracks a separate special-lands
collection. Same stack and visual language, different accent color to tell them apart.

## Local dev

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
flask --app app run --debug
```

Open http://127.0.0.1:5000. The SQLite DB auto-creates at `data/collection_tracker.sqlite3`.

### Try it without EchoMTG credentials

```bash
flask --app app seed-fixture
```

Loads `tests/fixtures/sample_inventory.json` through the same ingestion path a real sync uses, so
the dashboard, flags, and settings pages are fully clickable before you ever log in to EchoMTG.

### Connecting to EchoMTG

Click **Sync Now** and you'll be prompted to log in with your EchoMTG email/password (this hits
EchoMTG's `/user/auth/` endpoint directly; credentials are never stored, only the resulting
~24-hour token, cached locally in `data/echomtg_token.json`).

Once you have a **permanent** EchoMTG API key, skip the login step entirely by adding it to a
gitignored `config.json` (copy `config.example.json`) or setting `ECHOMTG_API_KEY` in the
environment.

## Config

- `config.json` (gitignored, copy from `config.example.json`) -- local-dev convenience for
  `ECHOMTG_API_KEY` and default thresholds.
- `.env` (gitignored, copy from `.env.example`) -- used by the eventual Docker deployment.
- Resolution order: environment variable -> `config.json` -> built-in default.

## CLI commands

- `flask --app app init-db` -- create the SQLite schema.
- `flask --app app seed-fixture [path]` -- load fixture/sample inventory data.
- `flask --app app hash-password '<password>'` -- generate a hash for `AUTH_PASSWORD_HASH`
  (only needed if you enable the optional site-wide login gate).

## Deployment

Runs behind a shared reverse proxy (see [`edge-caddy`](../edge-caddy)) alongside
`basic-land-tracker` on the same personal server -- this app's own `docker-compose.yml`
publishes no host ports itself, only `expose`s port 5000 internally on the shared `edge`
Docker network.

```bash
cp .env.example .env
# fill in real ECHOMTG_API_KEY, and real (not placeholder) AUTH_USERNAME/AUTH_PASSWORD_HASH/
# SECRET_KEY if you want the site-wide login gate on -- worth enabling once deployed, since
# this app displays real collection dollar values
docker network inspect edge >/dev/null 2>&1 || docker network create edge
docker compose up -d --build
```

`--workers 1` in the Dockerfile's gunicorn command is required, not just a default: the
periodic background sync (`scheduler.py`) is guarded against double-scheduling under Flask's
dev-mode reloader, and that guard's correctness assumes exactly one process.

See `edge-caddy/README.md` for the full shared-proxy setup and the runbook used to migrate
`basic-land-tracker` from its own bundled Caddy onto this shared one.
