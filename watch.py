#!/usr/bin/env python3
"""Surveillance continue avec ordonnancement adaptatif.

Sonde vite apres un changement, ralentit quand rien ne bouge, se met en veille
la nuit, et ne descend jamais sous le plancher impose par le cache du site.

    python watch.py
    python watch.py --once      # un seul passage, pour tester
"""

import argparse
import signal
import sys
import time
from datetime import datetime, time as clock

import db
from alerts import notify
from collect import load_config, run_check

DEFAULTS = {
    "active_start": "07:00",
    "active_end": "21:00",
    "interval_active": 900,
    "interval_quiet": 3600,
    "interval_after_change": 300,
    "floor": 300,
    "backoff_factor": 1.5,
    "respect_cache": True,
}

running = True


def stop(*_):
    global running
    running = False
    print("\nArret propre demande, fin du passage en cours.")


def parse_clock(text):
    hour, minute = (int(part) for part in text.split(":"))
    return clock(hour, minute)


def in_active_window(settings, moment=None):
    moment = (moment or datetime.now()).time()
    start = parse_clock(settings["active_start"])
    end = parse_clock(settings["active_end"])
    if start <= end:
        return start <= moment <= end
    return moment >= start or moment <= end  # fenetre qui traverse minuit


def cache_floor(conn, settings):
    """Le CDN impose un plancher: inutile de sonder plus vite que son max-age."""
    if not settings.get("respect_cache", True):
        return settings["floor"]
    observed = db.freshness(conn).get("cache_max_age")
    return max(settings["floor"], observed or 0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="un seul passage puis sortie")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    config = load_config()
    settings = {**DEFAULTS, **(config.get("schedule") or {})}
    conn = db.connect()

    interval = settings["interval_active"]
    print(f"Surveillance active. Fenetre {settings['active_start']} a {settings['active_end']}, "
          f"plancher {cache_floor(conn, settings)} s. Ctrl-C pour arreter.")

    while running:
        active = in_active_window(settings)
        started = time.monotonic()
        _, changes = run_check(conn, verbose=False)
        sent = notify(conn, config, changes)

        floor = cache_floor(conn, settings)
        if changes:
            interval = max(floor, settings["interval_after_change"])
            summary = ", ".join(
                f"{c['product'][:28]} {c['store']}: {c['from_quantity']} vers {c['to_quantity']}"
                for c in changes
            )
            print(f"[{db.now_iso()}] {len(changes)} changement(s), {sent} alerte(s): {summary}")
        else:
            base = settings["interval_active"] if active else settings["interval_quiet"]
            interval = min(max(floor, interval * settings["backoff_factor"]), base)
            print(f"[{db.now_iso()}] aucun changement, prochain passage dans "
                  f"{int(interval / 60)} min")

        if args.once or not running:
            break

        if not active:
            interval = max(interval, settings["interval_quiet"])

        remaining = interval - (time.monotonic() - started)
        while remaining > 0 and running:
            time.sleep(min(5, remaining))
            remaining -= 5

    conn.close()
    print("Surveillance arretee.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
