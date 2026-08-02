from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Optional

from flask import g

from config import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory_items (
    inventory_id      INTEGER PRIMARY KEY,
    emid              INTEGER NOT NULL,
    name              TEXT NOT NULL,
    set_code          TEXT,
    set_name          TEXT,
    expansion         TEXT,
    rarity            TEXT,
    main_type         TEXT,
    types             TEXT,
    mana_cost         TEXT,
    colors            TEXT,
    multiverse_id     INTEGER,
    condition         TEXT,
    language          TEXT,
    foil              INTEGER NOT NULL DEFAULT 0,
    tradable          INTEGER NOT NULL DEFAULT 0,
    reserve_list      INTEGER NOT NULL DEFAULT 0,
    date_acquired     TEXT,
    price_acquired    REAL,
    current_price     REAL NOT NULL,
    tcg_low           REAL,
    tcg_mid           REAL,
    tcg_market        REAL,
    foil_price        REAL,
    price_change      REAL,
    personal_gain     REAL,
    image_url         TEXT,
    image_cropped_url TEXT,
    set_image_url     TEXT,
    echo_url          TEXT,
    note_id           INTEGER,
    raw_json          TEXT,
    first_seen_at     TEXT NOT NULL,
    last_synced_at    TEXT NOT NULL,
    active            INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_inventory_items_name   ON inventory_items(name);
CREATE INDEX IF NOT EXISTS idx_inventory_items_emid   ON inventory_items(emid);
CREATE INDEX IF NOT EXISTS idx_inventory_items_active ON inventory_items(active);

CREATE TABLE IF NOT EXISTS storage_overlay (
    inventory_id          INTEGER PRIMARY KEY REFERENCES inventory_items(inventory_id) ON DELETE CASCADE,
    assigned_location      TEXT NOT NULL CHECK(assigned_location IN ('bulk','sleeve_binder','top_loader')),
    recommended_location    TEXT NOT NULL CHECK(recommended_location IN ('bulk','sleeve_binder','top_loader')),
    flag_direction           TEXT CHECK(flag_direction IN ('upgrade','downgrade')),
    flagged_at                TEXT,
    flagged_price               REAL,
    last_handled_at              TEXT,
    notes                          TEXT,
    updated_at                       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_storage_overlay_flag ON storage_overlay(flag_direction)
    WHERE flag_direction IS NOT NULL;

CREATE TABLE IF NOT EXISTS price_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    inventory_id  INTEGER NOT NULL REFERENCES inventory_items(inventory_id) ON DELETE CASCADE,
    price         REAL NOT NULL,
    recorded_at   TEXT NOT NULL,
    sync_log_id   INTEGER REFERENCES sync_log(id)
);

CREATE INDEX IF NOT EXISTS idx_price_history_inv_time ON price_history(inventory_id, recorded_at);

CREATE TABLE IF NOT EXISTS sync_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger            TEXT NOT NULL,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    items_seen         INTEGER NOT NULL DEFAULT 0,
    items_new          INTEGER NOT NULL DEFAULT 0,
    items_updated      INTEGER NOT NULL DEFAULT 0,
    items_deactivated  INTEGER NOT NULL DEFAULT 0,
    flags_raised       INTEGER NOT NULL DEFAULT 0,
    flags_cleared      INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'running',
    message            TEXT,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        DATA_DIR.mkdir(exist_ok=True)
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 5000")
        db.execute("PRAGMA journal_mode = WAL")
        g.db = db
    return g.db


def close_db(error: Optional[BaseException] = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    """Creates the schema if missing. Safe to call repeatedly, but callers
    should prefer calling this once at startup rather than per-request: it
    opens its own connection, and under WAL this is a cheap no-op once the
    schema exists, but every extra connection is still unnecessary contention
    against the background sync thread's writer connection."""
    DATA_DIR.mkdir(exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 5000")
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(SCHEMA)
        db.commit()
