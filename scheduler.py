from __future__ import annotations

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

import config
import sync

_scheduler = BackgroundScheduler(daemon=True)
_log = logging.getLogger("gunicorn.error")


def start_scheduler(app) -> None:
    """Starts the periodic sync job, guarding against Werkzeug's --debug
    reloader (which forks a watcher + worker process, both importing this
    module) from scheduling the job twice. WERKZEUG_RUN_MAIN is only set in
    the actual worker child, never in the parent watcher, so checking it
    ensures exactly one scheduler runs under `flask run --debug`. Under
    gunicorn there's no reloader at all (app.debug is False), so the first
    branch fires once, correctly -- this only stays correct with a single
    gunicorn worker; more workers would need an explicit cross-process lock.

    Logs explicitly on every branch (skip/already-running/started) via the
    gunicorn error logger, which gunicorn routes to stdout/stderr by default
    -- otherwise there is no way to observe from `docker compose logs`
    whether the scheduler actually came up versus silently never started.
    """
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        _log.info("scheduler: skipped (reloader parent process)")
        return
    if _scheduler.running:
        _log.info("scheduler: already running, skipping duplicate start")
        return

    interval_hours = config.get_sync_interval_hours()
    _scheduler.add_job(
        lambda: sync.run_sync(app, "scheduled"),
        trigger=IntervalTrigger(hours=interval_hours),
        id="periodic_sync",
        replace_existing=True,
    )
    _scheduler.start()
    _log.info("scheduler: started, periodic sync every %s hour(s)", interval_hours)
