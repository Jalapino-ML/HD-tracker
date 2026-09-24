"""Stockage: releves, journal des changements, mesure de fraicheur.

Deux backends geres avec la MEME interface (record/latest/note_change/...),
choisis automatiquement selon l'environnement:

  - SQLite (fichier stock.db local) -- utilise sur le Mac, pour l'acces via
    Tailscale/reseau local. C'est le comportement historique, inchange.
  - PostgreSQL -- utilise quand la variable d'environnement DATABASE_URL est
    definie (fournie par Render + un provider Postgres gratuit comme Neon).
    C'est le backend de la copie hebergee dans le cloud.

Rien dans dashboard.py, export_pivot.py ou les scripts update_inventaire_*.py
n'a besoin de savoir quel backend est actif: db.connect() retourne un objet
qui se comporte pareil dans les deux cas (.execute(), .commit(), .close(),
lignes accessibles par nom de colonne comme un dict).
"""

import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path(__file__).parent / "stock.db"
ENV_CLOUD_FILE = Path(__file__).parent / ".env_cloud"

# Fuseau horaire du Quebec (gere automatiquement EST/EDT). Toutes les dates
# affichees dans le dashboard et les exports doivent passer par
# `stamp.astimezone(QUEBEC_TZ)` plutot que par `stamp.astimezone()` seul --
# ce dernier utilise le fuseau du SERVEUR (UTC sur Render), pas celui du
# Quebec. Le stockage en base reste en UTC (bonne pratique, evite les bugs
# de changement d'heure); seul l'AFFICHAGE change. Comme ca s'applique a
# la conversion, ca couvre aussi bien les relevés déjà en base (passés)
# que les futurs.
QUEBEC_TZ = ZoneInfo("America/Toronto")


def _load_env_cloud():
    """Charge DATABASE_URL depuis .env_cloud si le fichier existe et que la
    variable n'est pas deja definie dans l'environnement (export manuel prend
    toujours priorite). Ainsi le Mac se connecte automatiquement a Postgres
    des que .env_cloud est present, sans avoir besoin de faire
    `export DATABASE_URL=...` a chaque nouvelle fenetre de Terminal."""
    if "DATABASE_URL" in os.environ:
        return
    if not ENV_CLOUD_FILE.exists():
        return
    for line in ENV_CLOUD_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key == "DATABASE_URL" and value:
            os.environ["DATABASE_URL"] = value
            break


_load_env_cloud()

DATABASE_URL = os.environ.get("DATABASE_URL")
BACKEND = "postgres" if DATABASE_URL else "sqlite"

if BACKEND == "postgres":
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3

# ---------------------------------------------------------------------------
# Schema -- une variante par backend (AUTOINCREMENT vs SERIAL, syntaxe
# d'upsert differente). Le reste (noms de tables/colonnes) est identique.
# ---------------------------------------------------------------------------

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS readings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at          TEXT    NOT NULL,
    sku               TEXT    NOT NULL,
    store_id          TEXT    NOT NULL,
    quantity          INTEGER,
    status            TEXT,
    ok                INTEGER NOT NULL DEFAULT 1,
    note              TEXT,
    source_updated_at TEXT,
    cache_age         INTEGER,
    cache_max_age     INTEGER,
    etag              TEXT,
    http_status       INTEGER,
    latency_ms        INTEGER,
    payload_hash      TEXT
);
CREATE INDEX IF NOT EXISTS idx_readings_lookup ON readings (sku, store_id, taken_at);

CREATE TABLE IF NOT EXISTS changes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at       TEXT    NOT NULL,
    sku               TEXT    NOT NULL,
    store_id          TEXT    NOT NULL,
    kind              TEXT    NOT NULL,
    from_quantity     INTEGER,
    to_quantity       INTEGER,
    delta             INTEGER,
    source_updated_at TEXT,
    gap_seconds       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_changes_time ON changes (detected_at);

CREATE TABLE IF NOT EXISTS alert_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at  TEXT NOT NULL,
    sku      TEXT NOT NULL,
    store_id TEXT NOT NULL,
    kind     TEXT NOT NULL,
    detail   TEXT
);

CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT    NOT NULL UNIQUE,
    sku             TEXT,
    name            TEXT,
    model           TEXT,
    price           TEXT,
    image_url       TEXT,
    starred         INTEGER NOT NULL DEFAULT 0,
    added_at        TEXT    NOT NULL,
    last_checked_at TEXT
);

CREATE TABLE IF NOT EXISTS watch_stores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    match_text    TEXT,
    search_anchor TEXT    NOT NULL,
    address       TEXT,
    city          TEXT,
    province      TEXT,
    added_at      TEXT    NOT NULL,
    UNIQUE(name, search_anchor)
);
"""

SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS readings (
    id                SERIAL PRIMARY KEY,
    taken_at          TEXT    NOT NULL,
    sku               TEXT    NOT NULL,
    store_id          TEXT    NOT NULL,
    quantity          INTEGER,
    status            TEXT,
    ok                INTEGER NOT NULL DEFAULT 1,
    note              TEXT,
    source_updated_at TEXT,
    cache_age         INTEGER,
    cache_max_age     INTEGER,
    etag              TEXT,
    http_status       INTEGER,
    latency_ms        INTEGER,
    payload_hash      TEXT
);
CREATE INDEX IF NOT EXISTS idx_readings_lookup ON readings (sku, store_id, taken_at);

CREATE TABLE IF NOT EXISTS changes (
    id                SERIAL PRIMARY KEY,
    detected_at       TEXT    NOT NULL,
    sku               TEXT    NOT NULL,
    store_id          TEXT    NOT NULL,
    kind              TEXT    NOT NULL,
    from_quantity     INTEGER,
    to_quantity       INTEGER,
    delta             INTEGER,
    source_updated_at TEXT,
    gap_seconds       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_changes_time ON changes (detected_at);

CREATE TABLE IF NOT EXISTS alert_log (
    id       SERIAL PRIMARY KEY,
    sent_at  TEXT NOT NULL,
    sku      TEXT NOT NULL,
    store_id TEXT NOT NULL,
    kind     TEXT NOT NULL,
    detail   TEXT
);

CREATE TABLE IF NOT EXISTS products (
    id              SERIAL PRIMARY KEY,
    url             TEXT    NOT NULL UNIQUE,
    sku             TEXT,
    name            TEXT,
    model           TEXT,
    price           TEXT,
    image_url       TEXT,
    starred         INTEGER NOT NULL DEFAULT 0,
    added_at        TEXT    NOT NULL,
    last_checked_at TEXT
);

CREATE TABLE IF NOT EXISTS watch_stores (
    id            SERIAL PRIMARY KEY,
    name          TEXT    NOT NULL,
    match_text    TEXT,
    search_anchor TEXT    NOT NULL,
    address       TEXT,
    city          TEXT,
    province      TEXT,
    added_at      TEXT    NOT NULL,
    UNIQUE(name, search_anchor)
);
"""

# Colonnes ajoutees apres la premiere version, migrees sans perdre l'historique.
LATE_COLUMNS = {
    "readings": {
        "source_updated_at": "TEXT",
        "cache_age": "INTEGER",
        "cache_max_age": "INTEGER",
        "etag": "TEXT",
        "http_status": "INTEGER",
        "latency_ms": "INTEGER",
        "payload_hash": "TEXT",
        "price": "TEXT",  # le prix varie parfois d'une succursale a l'autre
    }
}


class Conn:
    """Enveloppe fine autour d'une connexion sqlite3 ou psycopg2 pour que le
    reste du fichier (et dashboard.py, export_pivot.py, etc.) puisse ecrire
    conn.execute("... WHERE x = ?", (val,)) sans se soucier du backend."""

    def __init__(self, raw, backend):
        self.raw = raw
        self.backend = backend

    def execute(self, sql, params=()):
        if self.backend == "postgres":
            cur = self.raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql.replace("?", "%s"), params)
        else:
            cur = self.raw.cursor()
            cur.execute(sql, params)
        return cur

    def executescript(self, sql):
        if self.backend == "postgres":
            cur = self.raw.cursor()
            cur.execute(sql)
            cur.close()
        else:
            self.raw.executescript(sql)

    def commit(self):
        self.raw.commit()

    def close(self):
        self.raw.close()


def connect(path=None):
    """Ouvre une connexion. En SQLite, `path` est le chemin du fichier
    (defaut: stock.db a cote de ce module). En Postgres, `path` est ignore --
    la chaine de connexion vient de DATABASE_URL."""
    if BACKEND == "postgres":
        raw = psycopg2.connect(DATABASE_URL)
        conn = Conn(raw, "postgres")
        conn.executescript(SCHEMA_POSTGRES)
        conn.commit()
        for table, columns in LATE_COLUMNS.items():
            cur = conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                (table,),
            )
            existing = {r["column_name"] for r in cur.fetchall()}
            for name, kind in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        conn.commit()
        return conn
    else:
        raw = sqlite3.connect(path or DB_PATH)
        raw.row_factory = sqlite3.Row
        conn = Conn(raw, "sqlite")
        conn.executescript(SCHEMA_SQLITE)
        for table, columns in LATE_COLUMNS.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        conn.commit()
        return conn


def now():
    return datetime.now(timezone.utc)


def now_iso():
    return now().isoformat(timespec="seconds")


def parse_iso(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def age_seconds(value):
    stamp = parse_iso(value) if isinstance(value, str) else value
    return None if stamp is None else int((now() - stamp).total_seconds())


def record(conn, sku, store_id, quantity, status, ok=True, note=None, meta=None, price=None):
    meta = meta or {}
    conn.execute(
        "INSERT INTO readings (taken_at, sku, store_id, quantity, status, ok, note,"
        " source_updated_at, cache_age, cache_max_age, etag, http_status, latency_ms, payload_hash, price)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now_iso(), sku, store_id, quantity, status, 1 if ok else 0, note,
            meta.get("source_updated_at"), meta.get("cache_age"), meta.get("cache_max_age"),
            meta.get("etag"), meta.get("http_status"), meta.get("latency_ms"),
            meta.get("payload_hash"), price,
        ),
    )
    conn.commit()


def latest(conn, sku, store_id, offset=0):
    row = conn.execute(
        "SELECT * FROM readings WHERE sku = ? AND store_id = ? AND ok = 1"
        " ORDER BY taken_at DESC, id DESC LIMIT 1 OFFSET ?",
        (sku, store_id, offset),
    ).fetchone()
    return dict(row) if row else None


def last_known_quantity(conn, sku, store_id):
    """Derniere quantite non nulle connue, utilisee pour comparer et pour les 304."""
    row = conn.execute(
        "SELECT quantity, payload_hash, source_updated_at, taken_at FROM readings"
        " WHERE sku = ? AND store_id = ? AND ok = 1 AND quantity IS NOT NULL"
        " ORDER BY taken_at DESC, id DESC LIMIT 1",
        (sku, store_id),
    ).fetchone()
    return dict(row) if row else None


def last_etag(conn, sku, store_id):
    row = conn.execute(
        "SELECT etag FROM readings WHERE sku = ? AND store_id = ? AND etag IS NOT NULL"
        " ORDER BY taken_at DESC, id DESC LIMIT 1",
        (sku, store_id),
    ).fetchone()
    return row["etag"] if row else None


def note_change(conn, sku, store_id, before, after, source_updated_at=None, previous_at=None):
    """Enregistre un evenement de changement. Retourne le dict ou None si rien n'a bouge."""
    if before is None or after is None or before == after:
        return None
    delta = after - before
    if before == 0 and after > 0:
        kind = "restock"
    elif after == 0:
        kind = "out"
    elif delta < 0:
        kind = "decrease"
    else:
        kind = "increase"

    gap = None
    if previous_at:
        seen = parse_iso(previous_at)
        if seen:
            gap = int((now() - seen).total_seconds())

    conn.execute(
        "INSERT INTO changes (detected_at, sku, store_id, kind, from_quantity, to_quantity,"
        " delta, source_updated_at, gap_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now_iso(), sku, store_id, kind, before, after, delta, source_updated_at, gap),
    )
    conn.commit()
    return {
        "sku": sku, "store_id": store_id, "kind": kind, "from_quantity": before,
        "to_quantity": after, "delta": delta, "gap_seconds": gap,
    }


def changes_since(conn, change_id):
    rows = conn.execute(
        "SELECT * FROM changes WHERE id > ? ORDER BY id", (change_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def change_feed(conn, limit=40):
    rows = conn.execute(
        "SELECT * FROM changes ORDER BY detected_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def last_change_id(conn):
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS top FROM changes").fetchone()
    return row["top"]


def history(conn, sku, store_id, limit=60):
    rows = conn.execute(
        "SELECT taken_at, quantity FROM readings"
        " WHERE sku = ? AND store_id = ? AND ok = 1 AND quantity IS NOT NULL"
        " ORDER BY taken_at DESC, id DESC LIMIT ?",
        (sku, store_id, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def history_detail(conn, sku, store_id, limit=200):
    """Historique complet (le plus recent en premier) des releves reussis
    pour un produit dans un magasin donne, avec le prix -- utilise par le
    panneau d'historique du tableau de bord (clic sur un magasin).
    """
    rows = conn.execute(
        "SELECT taken_at, quantity, price FROM readings"
        " WHERE sku = ? AND store_id = ? AND ok = 1 AND quantity IS NOT NULL"
        " ORDER BY taken_at DESC, id DESC LIMIT ?",
        (sku, store_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def freshness(conn):
    """Ce que la source nous dit sur l'age de ses propres donnees."""
    row = conn.execute(
        "SELECT MAX(taken_at) AS last_poll FROM readings WHERE ok = 1"
    ).fetchone()
    src = conn.execute(
        "SELECT source_updated_at FROM readings WHERE source_updated_at IS NOT NULL"
        " ORDER BY taken_at DESC, id DESC LIMIT 1"
    ).fetchone()
    cache = conn.execute(
        "SELECT MIN(cache_max_age) AS min_max_age, MAX(cache_max_age) AS max_max_age,"
        " AVG(latency_ms) AS avg_latency FROM readings WHERE ok = 1"
    ).fetchone()
    return {
        "last_poll": row["last_poll"] if row else None,
        "last_poll_age": age_seconds(row["last_poll"]) if row and row["last_poll"] else None,
        "source_updated_at": src["source_updated_at"] if src else None,
        "source_age": age_seconds(src["source_updated_at"]) if src else None,
        "cache_max_age": cache["min_max_age"] if cache else None,
        "cache_max_age_high": cache["max_max_age"] if cache else None,
        "avg_latency": int(cache["avg_latency"]) if cache and cache["avg_latency"] else None,
    }


def all_readings(conn):
    rows = conn.execute(
        "SELECT taken_at, sku, store_id, quantity, status, ok, note, source_updated_at,"
        " cache_age, cache_max_age, http_status, latency_ms FROM readings ORDER BY taken_at, id"
    ).fetchall()
    return [dict(r) for r in rows]


def all_changes(conn):
    rows = conn.execute("SELECT * FROM changes ORDER BY detected_at, id").fetchall()
    return [dict(r) for r in rows]


def alert_already_sent(conn, sku, store_id, kind, within_hours=12):
    row = conn.execute(
        "SELECT sent_at FROM alert_log WHERE sku = ? AND store_id = ? AND kind = ?"
        " ORDER BY sent_at DESC LIMIT 1",
        (sku, store_id, kind),
    ).fetchone()
    if not row:
        return False
    last = parse_iso(row["sent_at"])
    return last is not None and (now() - last).total_seconds() / 3600 < within_hours


def log_alert(conn, sku, store_id, kind, detail):
    conn.execute(
        "INSERT INTO alert_log (sent_at, sku, store_id, kind, detail) VALUES (?, ?, ?, ?, ?)",
        (now_iso(), sku, store_id, kind, detail),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Produits geres depuis la barre "coller une URL" du tableau de bord.
# ---------------------------------------------------------------------------

def product_key(product):
    """Cle stable pour readings/changes: ne bouge jamais, meme si le SKU RONA change."""
    return f"p{product['id']}"


def store_key(store):
    return f"s{store['id']}"


def add_product(conn, url, sku=None, name=None, model=None, price=None, image_url=None):
    """Insere le produit s'il n'existe pas deja (par URL). Retourne son id."""
    existing = conn.execute("SELECT id FROM products WHERE url = ?", (url,)).fetchone()
    if existing:
        return existing["id"]
    if conn.backend == "postgres":
        cur = conn.execute(
            "INSERT INTO products (url, sku, name, model, price, image_url, starred, added_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 0, ?) RETURNING id",
            (url, sku, name, model, price, image_url, now_iso()),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        return new_id
    cur = conn.execute(
        "INSERT INTO products (url, sku, name, model, price, image_url, starred, added_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
        (url, sku, name, model, price, image_url, now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def update_product_info(conn, product_id, sku=None, name=None, model=None, price=None, image_url=None):
    conn.execute(
        "UPDATE products SET"
        " sku = COALESCE(?, sku), name = COALESCE(?, name), model = COALESCE(?, model),"
        " price = COALESCE(?, price), image_url = COALESCE(?, image_url), last_checked_at = ?"
        " WHERE id = ?",
        (sku, name, model, price, image_url, now_iso(), product_id),
    )
    conn.commit()


def list_products(conn):
    rows = conn.execute(
        "SELECT * FROM products ORDER BY starred DESC, added_at"
    ).fetchall()
    return [dict(r) for r in rows]


def get_product(conn, product_id):
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    return dict(row) if row else None


def set_starred(conn, product_id, starred):
    conn.execute("UPDATE products SET starred = ? WHERE id = ?", (1 if starred else 0, product_id))
    conn.commit()


def delete_product(conn, product_id):
    """Retire le produit et son historique (readings/changes/alert_log)."""
    pkey = f"p{product_id}"
    conn.execute("DELETE FROM readings WHERE sku = ?", (pkey,))
    conn.execute("DELETE FROM changes WHERE sku = ?", (pkey,))
    conn.execute("DELETE FROM alert_log WHERE sku = ?", (pkey,))
    conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Magasins surveilles.
# ---------------------------------------------------------------------------

def add_store(conn, name, search_anchor, match_text=None, address=None, city=None, province=None):
    if conn.backend == "postgres":
        conn.execute(
            "INSERT INTO watch_stores (name, match_text, search_anchor, address, city,"
            " province, added_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (name, search_anchor) DO NOTHING",
            (name, match_text or name, search_anchor, address, city, province, now_iso()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM watch_stores WHERE name = ? AND search_anchor = ?", (name, search_anchor)
        ).fetchone()
        return row["id"] if row else None
    cur = conn.execute(
        "INSERT OR IGNORE INTO watch_stores (name, match_text, search_anchor, address, city,"
        " province, added_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, match_text or name, search_anchor, address, city, province, now_iso()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM watch_stores WHERE name = ? AND search_anchor = ?", (name, search_anchor)
    ).fetchone()
    return row["id"] if row else cur.lastrowid


def list_stores(conn):
    rows = conn.execute("SELECT * FROM watch_stores ORDER BY added_at").fetchall()
    return [dict(r) for r in rows]


def delete_store(conn, store_id):
    conn.execute("DELETE FROM watch_stores WHERE id = ?", (store_id,))
    conn.commit()
