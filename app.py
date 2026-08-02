from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

import click
from flask import Flask, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import config
import db as db_module
import echomtg
import pricing
import scheduler
import sync

BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__)

# Site-level gate is opt-in and separate from EchoMTG auth: this protects the
# whole app once it's exposed on the personal server; EchoMTG auth (below) is
# about whether *this app* can pull your inventory from EchoMTG. Same
# both-or-neither pattern as basic-land-tracker.
AUTH_USERNAME = os.environ.get("AUTH_USERNAME")
AUTH_PASSWORD_HASH = os.environ.get("AUTH_PASSWORD_HASH")
AUTH_ENABLED = bool(AUTH_USERNAME or AUTH_PASSWORD_HASH)
if AUTH_ENABLED and not (AUTH_USERNAME and AUTH_PASSWORD_HASH):
    raise RuntimeError(
        "Set both AUTH_USERNAME and AUTH_PASSWORD_HASH together (or neither, for local dev)."
    )

if AUTH_ENABLED:
    _secret_key = os.environ.get("SECRET_KEY")
    if not _secret_key:
        raise RuntimeError(
            "SECRET_KEY must be set explicitly when AUTH_USERNAME/AUTH_PASSWORD_HASH are configured."
        )
    app.config["SECRET_KEY"] = _secret_key
else:
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "local-dev-collection-tracker")

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get(
    "SESSION_COOKIE_SECURE", "1" if AUTH_ENABLED else "0"
) == "1"
app.config["PERMANENT_SESSION_LIFETIME"] = dt.timedelta(days=30)

SITE_PUBLIC_ENDPOINTS = {"site_login", "static"}

LOCATION_LABELS = {"bulk": "Bulk", "sleeve_binder": "Sleeve / Binder", "top_loader": "Top Loader"}


# -----------------------------------------------------------------------------
# DB lifecycle
# -----------------------------------------------------------------------------

@app.teardown_appcontext
def _close_db(error: Optional[BaseException] = None) -> None:
    db_module.close_db(error)


def _bootstrap_db() -> None:
    """Runs once at process startup, not per-request: opening a fresh raw
    connection on every request (the previous behavior) raced against the
    background sync thread's writer connection and could raise 'database is
    locked' whenever a page was requested mid-sync."""
    db_module.init_db()
    with app.app_context():
        db = db_module.get_db()
        pricing.seed_default_thresholds(
            db, config.get_default_sleeve_threshold(), config.get_default_topload_threshold()
        )


@app.cli.command("init-db")
def init_db_command() -> None:
    db_module.init_db()
    print(f"Initialized {config.DB_PATH}")


@app.cli.command("hash-password")
@click.argument("password")
def hash_password_command(password: str) -> None:
    """Print a password hash suitable for the AUTH_PASSWORD_HASH env var."""
    print(generate_password_hash(password))


@app.cli.command("seed-fixture")
@click.argument("path", default=str(BASE_DIR / "tests" / "fixtures" / "sample_inventory.json"))
def seed_fixture_command(path: str) -> None:
    """Loads a fixture file (shaped like a real /inventory/view/ items array)
    through the same sync.upsert_items() code path real syncs use."""
    with app.app_context():
        db_module.init_db()
        db = db_module.get_db()
        pricing.seed_default_thresholds(
            db, config.get_default_sleeve_threshold(), config.get_default_topload_threshold()
        )
        items = json.loads(Path(path).read_text())
        sync_log_id = sync.start_sync_log(db, "fixture")
        counts = sync.upsert_items(db, items, sync_log_id)
        sync.finish_sync_log(db, sync_log_id, counts, "success")
        print(f"Seeded {counts['items_seen']} items ({counts['items_new']} new, "
              f"{counts['items_updated']} updated, {counts['flags_raised']} flags raised).")


# -----------------------------------------------------------------------------
# Site-level auth gate (opt-in, off by default)
# -----------------------------------------------------------------------------

@app.before_request
def _require_site_login():
    if not AUTH_ENABLED:
        return None
    if request.endpoint in SITE_PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if session.get("site_authed"):
        return None
    return redirect(url_for("site_login", next=request.full_path))


@app.route("/site-login", methods=["GET", "POST"])
def site_login():
    if not AUTH_ENABLED:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == AUTH_USERNAME and check_password_hash(AUTH_PASSWORD_HASH, password):
            session.clear()
            session["site_authed"] = True
            session.permanent = True
            next_url = request.form.get("next") or url_for("dashboard")
            return redirect(next_url)
        time.sleep(1)  # crude brute-force slowdown
        flash("Invalid username or password.", "error")
    return render_template("site_login.html", next=request.args.get("next", ""))


@app.post("/site-logout")
def site_logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("site_login"))


# -----------------------------------------------------------------------------
# EchoMTG auth
#
# Deliberately NOT a global before_request gate: this app is a read-only
# mirror, so already-synced local data (dashboard/flags/settings) should stay
# browsable even if the EchoMTG token has expired. Only /sync actually needs a
# live token, so that's the only route that checks and redirects to /login.
# -----------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if echomtg.get_permanent_api_key():
        return redirect(url_for("dashboard"))
    try:
        echomtg.get_active_token()
        return redirect(url_for("dashboard"))
    except echomtg.EchoMTGAuthError:
        pass

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        try:
            echomtg.login(email, password)
            next_url = request.form.get("next") or url_for("dashboard")
            flash("Logged in to EchoMTG.", "success")
            return redirect(next_url)
        except echomtg.EchoMTGError as exc:
            flash(f"EchoMTG login failed: {exc}", "error")
    return render_template("login.html", next=request.args.get("next", ""))


@app.post("/logout")
def logout():
    echomtg.clear_cached_token()
    flash("Logged out of EchoMTG.", "success")
    return redirect(url_for("login"))


# -----------------------------------------------------------------------------
# Dashboard / flags / cards
# -----------------------------------------------------------------------------

SORT_COLUMNS = {
    "name": "i.name",
    "current_price": "i.current_price",
    "price_change": "i.price_change",
    "date_acquired": "i.date_acquired",
}


def _kpis(db) -> dict:
    totals = db.execute(
        "SELECT COUNT(DISTINCT emid) AS unique_cards, COUNT(*) AS total_copies, "
        "COALESCE(SUM(current_price), 0) AS total_value "
        "FROM inventory_items WHERE active = 1"
    ).fetchone()
    buckets = {
        row["assigned_location"]: {"count": row["count"], "value": row["value"]}
        for row in db.execute(
            "SELECT o.assigned_location AS assigned_location, COUNT(*) AS count, "
            "COALESCE(SUM(i.current_price), 0) AS value "
            "FROM storage_overlay o JOIN inventory_items i ON i.inventory_id = o.inventory_id "
            "WHERE i.active = 1 GROUP BY o.assigned_location"
        )
    }
    flag_count = db.execute(
        "SELECT COUNT(*) AS c FROM storage_overlay o JOIN inventory_items i "
        "ON i.inventory_id = o.inventory_id "
        "WHERE i.active = 1 AND o.flag_direction IS NOT NULL"
    ).fetchone()["c"]
    return {
        "unique_cards": totals["unique_cards"],
        "total_copies": totals["total_copies"],
        "total_value": totals["total_value"],
        "buckets": {loc: buckets.get(loc, {"count": 0, "value": 0}) for loc in pricing.LOCATIONS},
        "flag_count": flag_count,
    }


@app.route("/")
def dashboard() -> str:
    db = db_module.get_db()
    q = request.args.get("q", "").strip()
    location = request.args.get("location", "")
    flagged = request.args.get("flagged") == "1"
    sort = request.args.get("sort", "name")
    direction = "DESC" if request.args.get("dir") == "desc" else "ASC"
    sort_col = SORT_COLUMNS.get(sort, "i.name")

    clauses = ["i.active = 1"]
    params: list[Any] = []
    if q:
        clauses.append("i.name LIKE ?")
        params.append(f"%{q}%")
    if location in pricing.LOCATIONS:
        clauses.append("o.assigned_location = ?")
        params.append(location)
    if flagged:
        clauses.append("o.flag_direction IS NOT NULL")

    rows = db.execute(
        "SELECT i.*, o.assigned_location, o.recommended_location, o.flag_direction "
        "FROM inventory_items i JOIN storage_overlay o ON o.inventory_id = i.inventory_id "
        f"WHERE {' AND '.join(clauses)} "
        f"ORDER BY {sort_col} {direction} LIMIT 500",
        params,
    ).fetchall()

    return render_template(
        "dashboard.html",
        rows=rows,
        kpis=_kpis(db),
        location_labels=LOCATION_LABELS,
        q=q,
        location=location,
        flagged=flagged,
        sort=sort,
        dir=request.args.get("dir", "asc"),
    )


@app.route("/flags")
def flags() -> str:
    db = db_module.get_db()
    rows = db.execute(
        "SELECT i.*, o.assigned_location, o.recommended_location, o.flag_direction, "
        "o.flagged_at, o.flagged_price "
        "FROM inventory_items i JOIN storage_overlay o ON o.inventory_id = i.inventory_id "
        "WHERE i.active = 1 AND o.flag_direction IS NOT NULL "
        "ORDER BY o.flagged_at DESC"
    ).fetchall()
    upgrades = [r for r in rows if r["flag_direction"] == "upgrade"]
    downgrades = [r for r in rows if r["flag_direction"] == "downgrade"]
    return render_template(
        "flags.html", upgrades=upgrades, downgrades=downgrades, location_labels=LOCATION_LABELS
    )


@app.post("/cards/<int:inventory_id>/assign")
def assign_card(inventory_id: int):
    db = db_module.get_db()
    location = request.form.get("location", "")
    if location not in pricing.LOCATIONS:
        flash("Invalid storage location.", "error")
        return redirect(request.referrer or url_for("dashboard"))
    pricing.set_assigned_location(db, inventory_id, location)
    flash("Storage location updated.", "success")
    return redirect(request.referrer or url_for("dashboard"))


# -----------------------------------------------------------------------------
# Sync
# -----------------------------------------------------------------------------

@app.post("/sync")
def trigger_sync():
    try:
        echomtg.get_active_token()
    except echomtg.EchoMTGAuthError:
        flash("Log in to EchoMTG before syncing.", "error")
        return redirect(url_for("login", next=url_for("dashboard")))

    if sync._sync_lock.locked():
        flash("A sync is already running.", "warning")
        return redirect(url_for("dashboard"))
    thread = threading.Thread(target=sync.run_sync, args=(app, "manual"), daemon=True)
    thread.start()
    flash("Sync started.", "info")
    return redirect(url_for("dashboard"))


@app.get("/sync/status")
def sync_status():
    db = db_module.get_db()
    row = db.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return jsonify({"status": "none"})
    return jsonify(dict(row))


# -----------------------------------------------------------------------------
# Settings
# -----------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
def settings():
    db = db_module.get_db()
    if request.method == "POST":
        try:
            sleeve = float(request.form.get("sleeve_threshold", ""))
            topload = float(request.form.get("topload_threshold", ""))
            if not (sleeve > 0 and topload > sleeve):
                raise ValueError
        except ValueError:
            flash("Thresholds must be positive numbers with sleeve < top-loader.", "error")
        else:
            pricing.set_thresholds(db, sleeve, topload)
            result = pricing.recompute_all_flags(db)
            flash(
                f"Thresholds updated. {result['flags_raised']} flag(s) raised, "
                f"{result['flags_cleared']} cleared.",
                "success",
            )
        return redirect(url_for("settings"))

    sleeve_threshold, topload_threshold = pricing.get_thresholds(db)
    sync_logs = db.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 20").fetchall()
    return render_template(
        "settings.html",
        sleeve_threshold=sleeve_threshold,
        topload_threshold=topload_threshold,
        sync_logs=sync_logs,
        auth_enabled=AUTH_ENABLED,
        has_permanent_key=bool(echomtg.get_permanent_api_key()),
    )


@app.post("/settings/recompute")
def settings_recompute():
    db = db_module.get_db()
    result = pricing.recompute_all_flags(db)
    flash(
        f"Recomputed. {result['flags_raised']} flag(s) raised, {result['flags_cleared']} cleared.",
        "success",
    )
    return redirect(url_for("settings"))


_bootstrap_db()
scheduler.start_scheduler(app)


if __name__ == "__main__":
    app.run(debug=True)
