#!/usr/bin/env python3
"""Copie complete stock.db (SQLite, local) -> la base Postgres du cloud
(Render/Neon), en preservant les memes ids (important: readings/changes
utilisent des cles comme "p1"/"s11" qui viennent des ids de products/
watch_stores -- il faut que ces ids soient identiques dans les deux bases).

Sert a la fois:
  1. a la migration initiale (premiere copie SQLite -> Postgres), et
  2. a chaque synchronisation apres une mise a jour manuelle sur le Mac,
     pour que la copie cloud (Render) reflete les memes chiffres que la
     copie locale (Tailscale).

C'est un remplacement complet (TRUNCATE puis re-insertion), pas une fusion
incrementale -- volontairement simple, la source de verite reste toujours
stock.db sur le Mac.

Usage (sur le Mac, dans le Terminal):

    cd ~/Documents/AI-Folder/Rona-Tracker
    source venv/bin/activate
    export DATABASE_URL="postgresql://...."   # colle l'URL Postgres fournie par Neon
    python sync_to_cloud.py

Ou, pour eviter de retaper l'URL a chaque fois, mets-la dans un fichier
".env_cloud" (une seule ligne "DATABASE_URL=postgresql://...") a cote de ce
script -- il sera lu automatiquement s'il existe.
"""

import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).parent
LOCAL_DB = HERE / "stock.db"
ENV_FILE = HERE / ".env_cloud"

TABLES_IN_ORDER = ["watch_stores", "products", "readings", "changes", "alert_log"]


def load_env_file():
    if ENV_FILE.exists() and "DATABASE_URL" not in os.environ:
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


def main():
    load_env_file()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print(
            "ERREUR: la variable DATABASE_URL n'est pas definie.\n"
            "Colle l'URL Postgres fournie par Neon, soit:\n"
            "  export DATABASE_URL=\"postgresql://...\"\n"
            "soit dans un fichier .env_cloud (DATABASE_URL=postgresql://...) a cote de ce script.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not LOCAL_DB.exists():
        print(f"ERREUR: {LOCAL_DB} introuvable.", file=sys.stderr)
        sys.exit(1)

    import psycopg2
    import psycopg2.extras

    src = sqlite3.connect(LOCAL_DB)
    src.row_factory = sqlite3.Row

    # db.py (importe ci-dessous) cree le schema Postgres au besoin.
    os.environ["DATABASE_URL"] = database_url
    import importlib
    import db
    importlib.reload(db)
    dst = db.connect()

    counts = {}
    try:
        for table in TABLES_IN_ORDER:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                counts[table] = 0
                continue
            columns = rows[0].keys()
            col_list = ", ".join(columns)
            placeholders = ", ".join(["%s"] * len(columns))

            cur = dst.raw.cursor()
            cur.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")
            values = [tuple(row[c] for c in columns) for row in rows]
            psycopg2.extras.execute_batch(
                cur, f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})", values
            )
            # Remet la sequence SERIAL au bon endroit puisqu'on a insere les
            # ids explicitement.
            if "id" in columns:
                cur.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'),"
                    f" COALESCE((SELECT MAX(id) FROM {table}), 1))"
                )
            cur.close()
            counts[table] = len(rows)
        dst.commit()
    finally:
        src.close()
        dst.close()

    print("Synchronisation terminee (SQLite -> Postgres cloud):")
    for table, n in counts.items():
        print(f"  - {table}: {n} ligne(s)")


if __name__ == "__main__":
    main()
