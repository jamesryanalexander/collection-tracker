from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Optional

LOCATION_RANK = {"bulk": 0, "sleeve_binder": 1, "top_loader": 2}
LOCATIONS = tuple(LOCATION_RANK.keys())


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def recommend_location(price: float, sleeve_threshold: float, topload_threshold: float) -> str:
    if price >= topload_threshold:
        return "top_loader"
    if price >= sleeve_threshold:
        return "sleeve_binder"
    return "bulk"


def flag_for(assigned: str, recommended: str) -> Optional[str]:
    if assigned == recommended:
        return None
    return "upgrade" if LOCATION_RANK[recommended] > LOCATION_RANK[assigned] else "downgrade"


def get_thresholds(db: sqlite3.Connection) -> tuple[float, float]:
    rows = {
        row["key"]: row["value"]
        for row in db.execute(
            "SELECT key, value FROM settings WHERE key IN ('sleeve_threshold', 'topload_threshold')"
        )
    }
    sleeve = float(rows.get("sleeve_threshold", 1.00))
    topload = float(rows.get("topload_threshold", 10.00))
    return sleeve, topload


def set_thresholds(db: sqlite3.Connection, sleeve_threshold: float, topload_threshold: float) -> None:
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('sleeve_threshold', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(sleeve_threshold),),
    )
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('topload_threshold', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(topload_threshold),),
    )
    db.commit()


def seed_default_thresholds(db: sqlite3.Connection, sleeve_default: float, topload_default: float) -> None:
    """Only seeds if not already present -- the DB is authoritative after first run."""
    existing = {row["key"] for row in db.execute("SELECT key FROM settings")}
    if "sleeve_threshold" not in existing:
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('sleeve_threshold', ?)",
            (str(sleeve_default),),
        )
    if "topload_threshold" not in existing:
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('topload_threshold', ?)",
            (str(topload_default),),
        )
    db.commit()


def apply_pricing_for_item(
    db: sqlite3.Connection,
    inventory_id: int,
    current_price: float,
    sleeve_threshold: float,
    topload_threshold: float,
    is_new: bool,
) -> Optional[str]:
    """Updates storage_overlay for one item. Returns the new flag_direction (or None)."""
    recommended = recommend_location(current_price, sleeve_threshold, topload_threshold)
    now = now_iso()

    if is_new:
        db.execute(
            "INSERT INTO storage_overlay "
            "(inventory_id, assigned_location, recommended_location, flag_direction, updated_at) "
            "VALUES (?, ?, ?, NULL, ?)",
            (inventory_id, recommended, recommended, now),
        )
        return None

    row = db.execute(
        "SELECT assigned_location FROM storage_overlay WHERE inventory_id = ?", (inventory_id,)
    ).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO storage_overlay "
            "(inventory_id, assigned_location, recommended_location, flag_direction, updated_at) "
            "VALUES (?, ?, ?, NULL, ?)",
            (inventory_id, recommended, recommended, now),
        )
        return None

    assigned = row["assigned_location"]
    flag = flag_for(assigned, recommended)
    if flag:
        db.execute(
            "UPDATE storage_overlay SET recommended_location = ?, flag_direction = ?, "
            "flagged_at = ?, flagged_price = ?, updated_at = ? WHERE inventory_id = ?",
            (recommended, flag, now, current_price, now, inventory_id),
        )
    else:
        db.execute(
            "UPDATE storage_overlay SET recommended_location = ?, flag_direction = NULL, "
            "flagged_at = NULL, flagged_price = NULL, updated_at = ? WHERE inventory_id = ?",
            (recommended, now, inventory_id),
        )
    return flag


def recompute_all_flags(db: sqlite3.Connection) -> dict:
    """Recomputes recommended_location/flag_direction for every active item using
    current thresholds, without any EchoMTG call. Used after a threshold edit."""
    sleeve_threshold, topload_threshold = get_thresholds(db)
    flags_raised = 0
    flags_cleared = 0
    rows = db.execute(
        "SELECT i.inventory_id, i.current_price, o.assigned_location, o.flag_direction "
        "FROM inventory_items i JOIN storage_overlay o ON o.inventory_id = i.inventory_id "
        "WHERE i.active = 1"
    ).fetchall()
    for row in rows:
        had_flag = row["flag_direction"] is not None
        flag = apply_pricing_for_item(
            db,
            row["inventory_id"],
            row["current_price"],
            sleeve_threshold,
            topload_threshold,
            is_new=False,
        )
        if flag and not had_flag:
            flags_raised += 1
        elif not flag and had_flag:
            flags_cleared += 1
    db.commit()
    return {"flags_raised": flags_raised, "flags_cleared": flags_cleared}


def set_assigned_location(db: sqlite3.Connection, inventory_id: int, location: str) -> None:
    """Sets assigned_location (used by both 'mark handled' -- passing the current
    recommended_location -- and manual overrides -- passing an arbitrary location).
    Recomputes the flag against the *current* recommended_location rather than
    blindly clearing it, since a manual override doesn't necessarily resolve the
    mismatch (e.g. overriding to 'bulk' when the recommendation is 'top_loader')."""
    if location not in LOCATIONS:
        raise ValueError(f"Invalid location: {location}")
    now = now_iso()
    row = db.execute(
        "SELECT recommended_location FROM storage_overlay WHERE inventory_id = ?",
        (inventory_id,),
    ).fetchone()
    recommended = row["recommended_location"] if row else location
    flag = flag_for(location, recommended)
    if flag:
        db.execute(
            "UPDATE storage_overlay SET assigned_location = ?, flag_direction = ?, "
            "flagged_at = ?, last_handled_at = ?, updated_at = ? WHERE inventory_id = ?",
            (location, flag, now, now, now, inventory_id),
        )
    else:
        db.execute(
            "UPDATE storage_overlay SET assigned_location = ?, flag_direction = NULL, "
            "flagged_at = NULL, flagged_price = NULL, last_handled_at = ?, updated_at = ? "
            "WHERE inventory_id = ?",
            (location, now, now, inventory_id),
        )
    db.commit()
