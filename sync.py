from __future__ import annotations

import json
import sqlite3
import threading
import traceback
from typing import Iterable, Optional

import pricing

_sync_lock = threading.Lock()


def now_iso() -> str:
    return pricing.now_iso()


def _bool_int(value) -> int:
    return 1 if value in (1, "1", True, "true", "True") else 0


def _float_or_none(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def item_to_row(item: dict, now: str) -> dict:
    """Maps one raw EchoMTG /inventory/view/ item (or an equivalently-shaped
    fixture item) to the inventory_items column set."""
    return {
        "inventory_id": int(item["inventory_id"]),
        "emid": int(item["emid"]),
        "name": item["name"],
        "set_code": item.get("set_code"),
        "set_name": item.get("set"),
        "expansion": item.get("expansion"),
        "rarity": item.get("rarity"),
        "main_type": item.get("main_type"),
        "types": item.get("types"),
        "mana_cost": item.get("mc"),
        "colors": item.get("colors"),
        "multiverse_id": _int_or_none(item.get("mid")),
        "condition": item.get("condition"),
        "language": item.get("lang"),
        "foil": _bool_int(item.get("foil", 0)),
        "tradable": _bool_int(item.get("tradable", 0)),
        "reserve_list": _bool_int(item.get("reserve_list", 0)),
        "date_acquired": item.get("date_acquired_html") or item.get("date_acquired"),
        "price_acquired": _float_or_none(item.get("price_acquired")),
        "current_price": _float_or_none(item.get("current_price")) or 0.0,
        "tcg_low": _float_or_none(item.get("tcg_low")),
        "tcg_mid": _float_or_none(item.get("tcg_mid")),
        "tcg_market": _float_or_none(item.get("tcg_market")),
        "foil_price": _float_or_none(item.get("foil_price")),
        "price_change": _float_or_none(item.get("price_change")),
        "personal_gain": _float_or_none(item.get("personal_gain")),
        "image_url": item.get("image"),
        "image_cropped_url": item.get("image_cropped"),
        "set_image_url": item.get("set_image"),
        "echo_url": item.get("echo_url"),
        "note_id": _int_or_none(item.get("note_id")),
        "raw_json": json.dumps(item),
        "last_synced_at": now,
    }


def upsert_items(db: sqlite3.Connection, items: Iterable[dict], sync_log_id: int) -> dict:
    """Ingests raw EchoMTG-shaped items into inventory_items/storage_overlay/price_history.
    Fed either by echomtg.fetch_all_inventory() (real) or the fixture loader,
    so both paths exercise this exact same code."""
    sleeve_threshold, topload_threshold = pricing.get_thresholds(db)
    now = now_iso()

    seen_ids: set[int] = set()
    items_seen = 0
    items_new = 0
    items_updated = 0
    flags_raised = 0
    flags_cleared = 0

    existing_ids = {
        row["inventory_id"] for row in db.execute("SELECT inventory_id FROM inventory_items")
    }

    for item in items:
        items_seen += 1
        row = item_to_row(item, now)
        inventory_id = row["inventory_id"]
        seen_ids.add(inventory_id)
        is_new = inventory_id not in existing_ids

        if is_new:
            items_new += 1
            db.execute(
                "INSERT INTO inventory_items ("
                "inventory_id, emid, name, set_code, set_name, expansion, rarity, main_type, "
                "types, mana_cost, colors, multiverse_id, condition, language, foil, tradable, "
                "reserve_list, date_acquired, price_acquired, current_price, tcg_low, tcg_mid, "
                "tcg_market, foil_price, price_change, personal_gain, image_url, "
                "image_cropped_url, set_image_url, echo_url, note_id, raw_json, "
                "first_seen_at, last_synced_at, active"
                ") VALUES ("
                ":inventory_id, :emid, :name, :set_code, :set_name, :expansion, :rarity, "
                ":main_type, :types, :mana_cost, :colors, :multiverse_id, :condition, "
                ":language, :foil, :tradable, :reserve_list, :date_acquired, :price_acquired, "
                ":current_price, :tcg_low, :tcg_mid, :tcg_market, :foil_price, :price_change, "
                ":personal_gain, :image_url, :image_cropped_url, :set_image_url, :echo_url, "
                ":note_id, :raw_json, :last_synced_at, :last_synced_at, 1"
                ")",
                row,
            )
        else:
            items_updated += 1
            db.execute(
                "UPDATE inventory_items SET "
                "emid=:emid, name=:name, set_code=:set_code, set_name=:set_name, "
                "expansion=:expansion, rarity=:rarity, main_type=:main_type, types=:types, "
                "mana_cost=:mana_cost, colors=:colors, multiverse_id=:multiverse_id, "
                "condition=:condition, language=:language, foil=:foil, tradable=:tradable, "
                "reserve_list=:reserve_list, date_acquired=:date_acquired, "
                "price_acquired=:price_acquired, current_price=:current_price, "
                "tcg_low=:tcg_low, tcg_mid=:tcg_mid, tcg_market=:tcg_market, "
                "foil_price=:foil_price, price_change=:price_change, "
                "personal_gain=:personal_gain, image_url=:image_url, "
                "image_cropped_url=:image_cropped_url, set_image_url=:set_image_url, "
                "echo_url=:echo_url, note_id=:note_id, raw_json=:raw_json, "
                "last_synced_at=:last_synced_at, active=1 "
                "WHERE inventory_id=:inventory_id",
                row,
            )

        flag = pricing.apply_pricing_for_item(
            db, inventory_id, row["current_price"], sleeve_threshold, topload_threshold, is_new
        )
        if is_new:
            pass  # first sighting never raises a flag, per spec
        elif flag:
            flags_raised += 1
        db.execute(
            "INSERT INTO price_history (inventory_id, price, recorded_at, sync_log_id) "
            "VALUES (?, ?, ?, ?)",
            (inventory_id, row["current_price"], now, sync_log_id),
        )

    items_deactivated = 0
    if existing_ids:
        stale_ids = existing_ids - seen_ids
        if stale_ids:
            items_deactivated = len(stale_ids)
            db.executemany(
                "UPDATE inventory_items SET active = 0 WHERE inventory_id = ?",
                [(i,) for i in stale_ids],
            )

    db.commit()
    return {
        "items_seen": items_seen,
        "items_new": items_new,
        "items_updated": items_updated,
        "items_deactivated": items_deactivated,
        "flags_raised": flags_raised,
        "flags_cleared": flags_cleared,
    }


def start_sync_log(db: sqlite3.Connection, trigger: str) -> int:
    now = now_iso()
    cur = db.execute(
        "INSERT INTO sync_log (trigger, started_at, status, created_at) VALUES (?, ?, 'running', ?)",
        (trigger, now, now),
    )
    db.commit()
    return cur.lastrowid


def finish_sync_log(db: sqlite3.Connection, sync_log_id: int, counts: dict, status: str, message: str = "") -> None:
    db.execute(
        "UPDATE sync_log SET finished_at=?, items_seen=?, items_new=?, items_updated=?, "
        "items_deactivated=?, flags_raised=?, flags_cleared=?, status=?, message=? WHERE id=?",
        (
            now_iso(),
            counts.get("items_seen", 0),
            counts.get("items_new", 0),
            counts.get("items_updated", 0),
            counts.get("items_deactivated", 0),
            counts.get("flags_raised", 0),
            counts.get("flags_cleared", 0),
            status,
            message,
            sync_log_id,
        ),
    )
    db.commit()


def run_sync(app, trigger: str) -> None:
    """Runs a full inventory sync. Safe to call from a background thread --
    opens its own DB connection rather than using flask.g, and guards against
    two syncs running concurrently (manual click during a scheduled run, etc.)."""
    if not _sync_lock.acquire(blocking=False):
        return
    try:
        with app.app_context():
            import db as db_module

            conn = db_module.get_db()
            sync_log_id = start_sync_log(conn, trigger)
            try:
                import echomtg

                token = echomtg.get_active_token()
                items = echomtg.fetch_all_inventory(token.token)
                counts = upsert_items(conn, items, sync_log_id)
                finish_sync_log(conn, sync_log_id, counts, "success")
            except Exception as exc:  # noqa: BLE001 -- log and surface via sync_log, never crash the scheduler thread
                finish_sync_log(
                    conn,
                    sync_log_id,
                    {},
                    "error",
                    f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}",
                )
    finally:
        _sync_lock.release()
