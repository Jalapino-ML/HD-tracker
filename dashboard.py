#!/usr/bin/env python3
"""Tableau de bord local: python dashboard.py puis http://127.0.0.1:8766

Fait aussi office de superviseur: au demarrage, un thread d'arriere-plan
verifie le stock a intervalle regulier (voir la section `schedule` de
config.yaml) tant que ce process tourne. Un seul processus a lancer.
"""

import csv
import io
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, render_template_string, request

import db
from collect import run_check, load_config

try:
    import export_pivot
except ImportError:
    export_pivot = None

app = Flask(__name__)
CONFIG = Path(__file__).parent / "config.yaml"

# Jeton partage protegeant /api/readings (POST public une fois deploye sur
# Render). Charge depuis l'environnement -- sur Render, colle-le dans les
# variables d'environnement du service (voir render.yaml); en local, il
# vient de .env_cloud si present (charge par db._load_env_cloud() a
# l'import de `db` ci-dessus). Si absent (aucun des deux), la verification
# est desactivee -- pratique pour un usage 100% local sans jeton.
API_WRITE_TOKEN = os.environ.get("API_WRITE_TOKEN")

# Etat du superviseur d'arriere-plan, lu par /api/state pour que la page
# puisse afficher "verification en cours..." sans bloquer la requete HTTP.
RUN_LOCK = threading.Lock()
RUN_STATE = {"running": False, "last_started": None, "last_finished": None, "last_error": None}


def load_yaml_config():
    if CONFIG.exists():
        return yaml.safe_load(CONFIG.read_text()) or {}
    return {}


def human(seconds):
    if seconds is None:
        return "inconnu"
    if seconds < 0:
        return "en avance"
    if seconds < 60:
        return f"{int(seconds)} s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} j"


def sparkline(points, width=132, height=34):
    """Construit le trace du mini-graphique d'historique, ainsi qu'un point
    cliquable (avec info-bulle) pour chaque releve ou la quantite a
    effectivement change -- le detail de la difference (avant -> apres,
    date) apparait au survol de ce point.
    """
    values = [p["quantity"] for p in points]
    if len(values) < 2:
        return None
    low, high = min(values), max(values)
    span = (high - low) or 1
    step_x = width / (len(values) - 1)

    def y_of(value):
        return round(height - 3 - (value - low) / span * (height - 6), 1)

    segments = [f"M0,{y_of(values[0])}"]
    markers = []
    for index, value in enumerate(values[1:], start=1):
        x = round(index * step_x, 1)
        segments.append(f"H{x}V{y_of(value)}")
        previous_value = values[index - 1]
        delta = value - previous_value
        if delta != 0:
            stamp = db.parse_iso(points[index].get("taken_at"))
            when = stamp.astimezone(db.QUEBEC_TZ).strftime("%d %b %H:%M") if stamp else "?"
            markers.append({
                "x": x,
                "y": y_of(value),
                "previous": previous_value,
                "quantity": value,
                "delta": delta,
                "when": when,
                "up": delta > 0,
            })
    return {"path": " ".join(segments), "width": width, "height": height, "markers": markers}


def reading_feed(conn, names, models, store_names, per_store_limit=60):
    """Journal complet des prises d'information, regroupe par magasin puis
    par modele de boite: CHAQUE releve reussi (par produit + magasin), avec
    la variation par rapport au releve precedent au meme endroit -- que la
    quantite ait augmente, diminue ou soit restee identique -- ainsi que la
    date et l'heure exactes de la prise d'information. Contrairement a la
    table `changes`, rien n'est filtre ici: les releves ou rien n'a bouge
    apparaissent aussi (delta = 0).

    Retourne une liste de groupes {"store": nom, "models": [...]}, un
    groupe par magasin, tries du magasin le plus recemment verifie au
    moins recent. A l'interieur d'un groupe, un sous-groupe par modele de
    boite {"model": nom, "entries": [...]}, trie du modele le plus
    recemment verifie au moins recent; a l'interieur d'un sous-groupe, les
    releves sont du plus recent au plus ancien.
    """
    rows = conn.execute(
        "SELECT id, sku, store_id, quantity, taken_at FROM readings"
        " WHERE ok = 1 AND quantity IS NOT NULL ORDER BY sku, store_id, taken_at, id"
    ).fetchall()

    entries = []
    last_qty_by_key = {}
    for row in rows:
        key = (row["sku"], row["store_id"])
        previous_qty = last_qty_by_key.get(key)
        quantity = row["quantity"]
        delta = None if previous_qty is None else quantity - previous_qty

        if delta is None:
            kind = "first"
            delta_text = None
        elif delta == 0:
            kind = "same"
            delta_text = "0"
        elif quantity == 0:
            kind = "out"
            delta_text = f"{delta}"
        elif previous_qty == 0 and quantity > 0:
            kind = "restock"
            delta_text = f"+{delta}"
        elif delta < 0:
            kind = "decrease"
            delta_text = f"{delta}"
        else:
            kind = "increase"
            delta_text = f"+{delta}"

        stamp = db.parse_iso(row["taken_at"])
        entries.append({
            "taken_at": row["taken_at"],
            "when": stamp.astimezone(db.QUEBEC_TZ).strftime("%d %b %H:%M") if stamp else "?",
            "product": names.get(row["sku"], row["sku"]),
            "model": models.get(row["sku"], row["sku"]),
            "store": store_names.get(row["store_id"], row["store_id"]),
            "quantity": quantity,
            "previous": previous_qty,
            "delta_text": delta_text,
            "kind": kind,
        })
        last_qty_by_key[key] = quantity

    entries.sort(key=lambda e: e["taken_at"], reverse=True)

    by_store = {}
    for entry in entries:
        by_store.setdefault(entry["store"], []).append(entry)

    groups = []
    for store, store_entries in by_store.items():
        by_model = {}
        for entry in store_entries:
            by_model.setdefault(entry["model"], []).append(entry)
        model_groups = [
            {"model": model, "entries": items[:per_store_limit]}
            for model, items in by_model.items()
        ]
        model_groups.sort(key=lambda g: g["entries"][0]["taken_at"], reverse=True)
        groups.append({"store": store, "models": model_groups})

    groups.sort(key=lambda g: g["models"][0]["entries"][0]["taken_at"], reverse=True)
    return groups


def cell_history(points_detail):
    """Formate l'historique complet (le plus recent en premier) d'un
    produit dans un magasin pour le panneau depliable du tableau de bord:
    date/heure (Quebec), quantite, prix et variation par rapport au releve
    precedent (le precedent chronologiquement, donc l'entree suivante dans
    cette liste puisqu'elle est triee du plus recent au plus ancien).
    """
    entries = []
    for index, row in enumerate(points_detail):
        quantity = row["quantity"]
        previous_row = points_detail[index + 1] if index + 1 < len(points_detail) else None
        previous_qty = previous_row["quantity"] if previous_row else None
        delta = None if previous_qty is None else quantity - previous_qty

        if delta is None:
            kind, delta_text = "first", None
        elif delta == 0:
            kind, delta_text = "same", "0"
        elif quantity == 0:
            kind, delta_text = "out", f"{delta}"
        elif previous_qty == 0 and quantity > 0:
            kind, delta_text = "restock", f"+{delta}"
        elif delta < 0:
            kind, delta_text = "decrease", f"{delta}"
        else:
            kind, delta_text = "increase", f"+{delta}"

        stamp = db.parse_iso(row["taken_at"])
        entries.append({
            "when": stamp.astimezone(db.QUEBEC_TZ).strftime("%d %b %Y %H:%M") if stamp else "?",
            "quantity": quantity,
            "price": row.get("price"),
            "delta_text": delta_text,
            "kind": kind,
        })
    return entries


def sales_summary(points):
    """Estime les unites vendues dans un magasin: la somme de toutes les
    baisses de quantite observees entre deux releves consecutifs (les
    hausses -- reassort -- ne sont pas comptees). C'est une estimation:
    une baisse peut aussi venir d'un ajustement d'inventaire cote magasin,
    pas seulement d'une vente.
    """
    if len(points) < 2:
        return None
    sold = 0
    for prev, cur in zip(points, points[1:]):
        prev_qty, cur_qty = prev.get("quantity"), cur.get("quantity")
        if prev_qty is None or cur_qty is None:
            continue
        if cur_qty < prev_qty:
            sold += prev_qty - cur_qty
    if sold <= 0:
        return None
    since_ts = points[0].get("taken_at")
    since_stamp = db.parse_iso(since_ts)
    since = since_stamp.astimezone(db.QUEBEC_TZ).strftime("%d %b") if since_stamp else "?"
    return {"sold": sold, "since": since, "since_ts": since_ts}


def net_change(points):
    """Somme nette de toutes les differences de quantite observees pour un
    produit dans un magasin (dernier releve - premier releve; les hausses
    et les baisses se compensent). Positif si le stock a globalement
    augmente sur la periode, negatif s'il a diminue, 0 si stable.
    """
    if len(points) < 2:
        return None
    first_qty = points[0].get("quantity")
    last_qty = points[-1].get("quantity")
    if first_qty is None or last_qty is None:
        return None
    return {"total": last_qty - first_qty, "since_ts": points[0].get("taken_at")}


def aggregate_net_change(cells):
    """Additionne la variation nette de tous les magasins pour un meme
    modele de boite, tous magasins confondus. Toujours retourne un
    resultat (y compris 0) des qu'au moins un magasin a un historique
    d'au moins 2 releves.
    """
    total = 0
    earliest = None
    found = False
    for cell in cells:
        net = cell.get("net")
        if not net:
            continue
        found = True
        total += net["total"]
        if earliest is None or net["since_ts"] < earliest:
            earliest = net["since_ts"]
    if not found:
        return None
    since_stamp = db.parse_iso(earliest)
    since = since_stamp.astimezone(db.QUEBEC_TZ).strftime("%d %b") if since_stamp else "?"
    return {"total": total, "since": since}


def short_store_label(store):
    """Etiquette courte pour l'en-tete de chaque case magasin (ex: 'CALGARY',
    'ST-BRUNO') plutot que le nom complet ('RONA+ S.W. Calgary (Signal Hill)'),
    qui casserait sur plusieurs lignes dans une case etroite.
    """
    if store.get("city"):
        return store["city"].upper()
    name = store.get("name") or ""
    name = re.sub(r"^(rona\+?\s*)", "", name, flags=re.IGNORECASE)
    name = name.split(",")[0].strip()
    return name.upper() or "MAGASIN"


def product_model(product):
    """N de modele du produit (ex: 'C1104LHU'). Utilise la valeur stockee en
    base si presente, sinon la deduit de l'URL rona.ca: le slug se termine
    generalement par ...-<modele>-<no-d-article>, ex:
    .../boitiers-utilitaires-rona-en-acier-2-x-3-po-paquet-de-6-c1104lhu-93515000
    """
    if product.get("model"):
        return product["model"].upper()
    url = product.get("url") or ""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    parts = slug.split("-")
    if len(parts) >= 2 and parts[-1].isdigit():
        candidate = parts[-2]
        if candidate.isalnum() and not candidate.isdigit():
            return candidate.upper()
    return None


def build_view():
    config = load_yaml_config()
    conn = db.connect()
    low_threshold = config.get("alerts", {}).get("low_threshold", 4)
    products = db.list_products(conn)
    stores = db.list_stores(conn)
    names = {db.product_key(p): (p["name"] or p["url"]) for p in products}
    models = {db.product_key(p): (product_model(p) or p["name"] or p["url"]) for p in products}
    store_names = {db.store_key(s): s["name"] for s in stores}
    rows = []

    for product in products:
        pkey = db.product_key(product)
        cells = []
        for store in stores:
            skey = db.store_key(store)
            current = db.latest(conn, pkey, skey)
            previous = db.latest(conn, pkey, skey, offset=1)
            points = db.history(conn, pkey, skey, limit=48)
            points_detail = db.history_detail(conn, pkey, skey, limit=200)
            qty = current["quantity"] if current else None
            before = previous["quantity"] if previous else None
            delta = None if qty is None or before is None else qty - before

            if qty is None:
                state = "unknown"
            elif qty == 0:
                state = "out"
            elif qty <= low_threshold:
                state = "low"
            else:
                state = "ok"

            cells.append({
                "store": store["name"],
                "store_short": short_store_label(store),
                "address": ", ".join(x for x in [store["city"], store["province"]] if x),
                "qty": qty,
                "price": (current or {}).get("price"),
                "delta": delta,
                "state": state,
                "readings": len(points),
                "spark": sparkline(points),
                "sales": sales_summary(points),
                "net": net_change(points),
                "history": cell_history(points_detail),
                "checked_age": db.age_seconds((current or {}).get("taken_at")),
                "note": (current or {}).get("note") if current and not current.get("ok", 1) else None,
            })
        rows.append({
            "id": product["id"],
            "name": product["name"] or product["url"],
            "sku": product["sku"] or "-",
            "model": product_model(product),
            "url": product["url"],
            "image_url": product["image_url"],
            "starred": bool(product["starred"]),
            "cells": cells,
            "net_total": aggregate_net_change(cells),
        })

    fresh = db.freshness(conn)
    day_ago = (db.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    changes_24h = sum(1 for c in db.change_feed(conn, limit=500) if c["detected_at"] >= day_ago)

    feed = reading_feed(conn, names, models, store_names)

    stats = {
        "poll_age": fresh["last_poll_age"],
        "poll_age_text": human(fresh["last_poll_age"]),
        "changes_24h": changes_24h,
        "last_change_id": db.last_change_id(conn),
        "last_poll": fresh["last_poll"],
    }
    watch_stores = stores
    conn.close()
    return rows, feed, stats, watch_stores


TEMPLATE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Suivi de stock HD</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 100 100%27%3E%3Ctext y=%27.9em%27 font-size=%2790%27%3E%F0%9F%93%8A%3C/text%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo+Narrow:wght@500;700&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
  :root {
    --zinc: #dee2e1; --tag: #f5f6f4; --ink: #16211f; --steel: #1f5d63;
    --rust: #a3390e; --muted: #7c8785; --hairline: #c3c9c7; --gold: #b6862c;
    --rust-tint: #f7e6e2; --green: #2e7d32;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--zinc); color: var(--ink);
    font-family: "Archivo Narrow", "Helvetica Neue", Arial, sans-serif;
    font-size: 16px; line-height: 1.4;
  }
  .board { max-width: 1080px; margin: 0 auto; padding: 28px 18px 64px; }
  header { border-bottom: 2px solid var(--ink); padding-bottom: 12px; }
  h1 {
    font-size: clamp(28px, 6vw, 46px); font-weight: 700; letter-spacing: -0.01em;
    text-transform: uppercase; margin: 0;
  }
  .meta {
    display: flex; flex-wrap: wrap; gap: 6px 18px; align-items: baseline;
    font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 12px;
    color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin-top: 8px;
  }
  .meta a, .meta button.link {
    color: var(--steel); background: none; border: none; padding: 0; font: inherit;
    cursor: pointer; text-transform: uppercase; letter-spacing: 0.06em;
  }
  /* Barre d'action: gros bouton "Actualiser maintenant" bien visible (look CompShop). */
  .toolbar { display: flex; align-items: center; gap: 14px; margin-top: 14px; flex-wrap: wrap; }
  .cloud-note { font-size: 13px; color: #6b7280; font-style: italic; }
  .cloud-note code { font-style: normal; }
  .toolbar button {
    padding: 10px 16px; border: 1px solid var(--ink); background: var(--ink); color: var(--zinc);
    font-family: "Archivo Narrow", sans-serif; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.04em; cursor: pointer; font-size: 13px;
  }
  .toolbar button:disabled { opacity: 0.5; cursor: default; }
  #checkNowStatus {
    font-family: "IBM Plex Mono", monospace; font-size: 12px; color: var(--muted);
  }
  /* Barre d'ajout: coller une URL RONA. */
  .addbar {
    margin-top: 20px; display: flex; gap: 8px; flex-wrap: wrap;
  }
  .addbar input[type=text] {
    flex: 1; min-width: 240px; padding: 11px 12px; border: 1px solid var(--hairline);
    background: var(--tag); font-family: "IBM Plex Mono", monospace; font-size: 13px; color: var(--ink);
  }
  .addbar button, .panel button {
    padding: 11px 16px; border: 1px solid var(--ink); background: var(--ink); color: var(--zinc);
    font-family: "Archivo Narrow", sans-serif; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.05em; font-size: 13px; cursor: pointer;
  }
  .addbar button.secondary { background: var(--tag); color: var(--ink); }
  #addStatus, #storeStatus { font-family: "IBM Plex Mono", monospace; font-size: 12px; color: var(--muted); margin-top: 6px; }
  /* Panneau des magasins surveilles: repliable. */
  details.panel {
    margin-top: 18px; border: 1px solid var(--hairline); background: var(--tag);
  }
  details.panel summary {
    cursor: pointer; padding: 10px 14px; font-weight: 700; text-transform: uppercase;
    font-size: 13px; letter-spacing: 0.05em;
  }
  .panel-body { padding: 0 14px 14px; }
  .storelist { list-style: none; margin: 0 0 12px; padding: 0; }
  .storelist li {
    display: flex; justify-content: space-between; gap: 10px; padding: 6px 0;
    border-bottom: 1px solid var(--hairline); font-family: "IBM Plex Mono", monospace; font-size: 12px;
  }
  .storelist button {
    background: none; border: none; color: var(--rust); cursor: pointer; font-size: 12px;
    text-transform: uppercase; padding: 0;
  }
  .storeform { display: flex; gap: 8px; flex-wrap: wrap; }
  .storeform input {
    padding: 9px 10px; border: 1px solid var(--hairline); font-family: "IBM Plex Mono", monospace;
    font-size: 12px; flex: 1; min-width: 140px;
  }
  .clockbar {
    display: grid; gap: 1px; margin-top: 18px; background: var(--hairline);
    grid-template-columns: repeat(auto-fit, minmax(148px, 1fr)); border: 1px solid var(--hairline);
  }
  .clockbar div { background: var(--tag); padding: 10px 12px; }
  .clockbar dt {
    font-family: "IBM Plex Mono", monospace; font-size: 10px; letter-spacing: 0.1em;
    text-transform: uppercase; color: var(--muted); margin: 0 0 3px;
  }
  .clockbar dd {
    margin: 0; font-family: "IBM Plex Mono", monospace; font-weight: 600; font-size: 19px;
  }
  .group { margin-top: 34px; position: relative; display: flex; gap: 20px; align-items: flex-start; }
  .groupbody { flex: 1; min-width: 0; }
  .groupimg {
    flex-shrink: 0; width: 104px; padding-top: 2px;
  }
  .groupimg img {
    width: 100%; height: auto; display: block; background: #fff;
    border: 1px solid var(--hairline); object-fit: contain; aspect-ratio: 1 / 1;
  }
  .group h2 { font-size: 19px; font-weight: 700; margin: 0 0 2px; display: inline; }
  .group .sku {
    font-family: "IBM Plex Mono", monospace; font-size: 12px; color: var(--muted);
    letter-spacing: 0.08em;
  }
  @media (max-width: 560px) {
    .group { flex-direction: column-reverse; }
    .groupimg { width: 84px; }
    .tags { grid-template-columns: repeat(2, 1fr); }
  }
  @media (min-width: 561px) and (max-width: 900px) {
    .tags { grid-template-columns: repeat(3, 1fr); }
  }
  .star {
    background: none; border: none; cursor: pointer; font-size: 18px; vertical-align: middle;
    color: var(--hairline); margin-left: 6px;
  }
  .star.on { color: var(--gold); }
  .group.starred h2::before { content: "★ "; color: var(--gold); }
  .tags {
    display: grid; gap: 12px; margin-top: 14px;
    grid-template-columns: repeat(5, 1fr);
  }
  .tag {
    position: relative; background: var(--tag); border: 1px solid var(--hairline);
    border-left: 4px solid var(--steel); padding: 34px 14px 14px;
  }
  .tag::before {
    content: ""; position: absolute; top: 12px; left: 12px; width: 11px; height: 11px;
    border-radius: 50%; background: var(--zinc); box-shadow: inset 0 0 0 1px var(--hairline);
  }
  .tag.low { border-left-color: var(--rust); }
  .tag.out { border-left-color: var(--rust); background: var(--rust-tint); }
  .tag.unknown { border-left-color: var(--hairline); }
  .tag { cursor: pointer; }
  .tag:hover .store { text-decoration: underline; }
  .tag .store {
    position: absolute; top: 10px; left: 34px; font-size: 13px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.07em; right: 10px; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
  }
  .tag .store::after {
    content: "▾"; float: right; margin-left: 6px; color: var(--muted); font-size: 10px;
  }
  .tag[aria-expanded="true"] .store::after { content: "▴"; }
  .history-panel { cursor: default; }
  .history-panel {
    margin: 10px -14px -14px; padding: 10px 14px 12px; border-top: 1px solid var(--hairline);
    background: rgba(0,0,0,0.03); max-height: 220px; overflow-y: auto;
  }
  .history-panel ul { list-style: none; margin: 0; padding: 0; }
  .history-panel li {
    display: flex; justify-content: space-between; gap: 8px; padding: 4px 0;
    border-bottom: 1px solid var(--hairline); font-family: "IBM Plex Mono", monospace; font-size: 11px;
  }
  .history-panel li:last-child { border-bottom: none; }
  .history-panel .h-when { color: var(--muted); white-space: nowrap; }
  .history-panel .h-diff { font-weight: 600; white-space: nowrap; }
  .history-panel li.decrease .h-diff, .history-panel li.out .h-diff { color: var(--rust); }
  .history-panel li.restock .h-diff, .history-panel li.increase .h-diff { color: var(--green); }
  .history-empty { font-family: "IBM Plex Mono", monospace; font-size: 11px; color: var(--muted); }
  .qtyrow {
    display: flex; align-items: baseline; justify-content: space-between; gap: 8px;
  }
  .qty {
    font-family: "IBM Plex Mono", monospace; font-weight: 600; font-size: 30px;
    line-height: 1; letter-spacing: -0.03em;
  }
  .tag.out .qty { color: var(--rust); }
  .tag.unknown .qty { color: var(--muted); font-size: 22px; }
  .price {
    font-family: "IBM Plex Mono", monospace; font-weight: 600; font-size: 13px;
    color: var(--steel); white-space: nowrap; margin-left: auto;
  }
  .unit, .note {
    font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em;
  }
  .note { font-family: "IBM Plex Mono", monospace; font-size: 11px; margin-top: 10px; text-transform: none; }
  .sold {
    font-family: "IBM Plex Mono", monospace; font-size: 11px; color: var(--steel);
    margin-top: 6px; text-transform: none;
  }
  .soldtotal {
    font-family: "IBM Plex Mono", monospace; font-size: 12px; font-weight: 600;
    margin-top: 4px; text-transform: none;
  }
  .soldtotal.up { color: var(--green); }
  .soldtotal.down { color: var(--rust); font-weight: 700; }
  .soldtotal.flat { color: var(--ink); }
  .netbreakdown {
    list-style: none; margin: 4px 0 0; padding: 0; display: flex; flex-wrap: wrap;
    gap: 4px 14px; font-family: "IBM Plex Mono", monospace; font-size: 11px;
  }
  .netbreakdown li.up { color: var(--green); }
  .netbreakdown li.down { color: var(--rust); font-weight: 700; }
  .netbreakdown li.flat { color: var(--ink); }
  .delta { font-family: "IBM Plex Mono", monospace; font-size: 13px; margin-left: 8px; }
  .delta.down { color: var(--rust); }
  .delta.up { color: var(--green); }
  .spark { display: block; margin-top: 12px; overflow: visible; }
  .spark path { fill: none; stroke: var(--steel); stroke-width: 1.5; }
  .tag.low .spark path, .tag.out .spark path { stroke: var(--rust); }
  .spark-point { cursor: default; }
  .spark-point.up { fill: var(--green); }
  .spark-point.down { fill: var(--rust); }
  .feed { margin-top: 44px; border-top: 2px solid var(--ink); padding-top: 12px; }
  .feed h2 { font-size: 19px; text-transform: uppercase; letter-spacing: 0.04em; margin: 0 0 10px; }
  .feedgroup { margin-top: 22px; }
  .feedgroup:first-of-type { margin-top: 4px; }
  .feedgroup h3 {
    font-family: "IBM Plex Mono", monospace; font-size: 12px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.07em; color: var(--steel);
    margin: 0 0 4px; padding-bottom: 4px; border-bottom: 1px solid var(--ink);
  }
  .feedmodel { margin-top: 12px; }
  .feedmodel:first-of-type { margin-top: 2px; }
  .feedmodel h4 {
    font-family: "IBM Plex Mono", monospace; font-size: 11px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted);
    margin: 0 0 2px;
  }
  .feed ol { list-style: none; margin: 0; padding: 0; }
  .feed li {
    display: grid; grid-template-columns: 96px 1fr auto; gap: 10px; align-items: baseline;
    padding: 7px 0; border-bottom: 1px solid var(--hairline);
    font-family: "IBM Plex Mono", monospace; font-size: 13px;
  }
  .feed .when { color: var(--muted); font-size: 11px; letter-spacing: 0.04em; }
  .feed .what { font-family: "Archivo Narrow", sans-serif; font-size: 15px; }
  .feed .move { font-weight: 600; white-space: nowrap; }
  .feed li.decrease .move, .feed li.out .move { color: var(--rust); }
  .feed li.restock .move, .feed li.increase .move { color: var(--green); }
  .feed li.same .move { color: var(--ink); font-weight: 400; }
  .feed li.first .move { color: var(--muted); font-weight: 400; }
  .empty {
    border: 1px dashed var(--muted); padding: 22px; margin-top: 18px;
    font-family: "IBM Plex Mono", monospace; font-size: 13px;
  }
  @media (prefers-reduced-motion: no-preference) {
    .tag { transition: border-left-width 120ms ease; }
    .tag:hover { border-left-width: 8px; }
  }
</style>
</head>
<body>
<div class="board">
  <header>
    <h1>Suivi de stock</h1>
    <div class="meta">
      <span>{{ rows|length }} article{{ 's' if rows|length != 1 else '' }}</span>
      <span>{{ stats.changes_24h }} changement(s) sur 24 h</span>
      <a href="/export.csv">Telecharger le CSV</a>
      <a href="/changes.csv">Journal des changements</a>
      <button type="button" class="link" id="langToggle" onclick="toggleLanguage()">English</button>
    </div>
    <div class="toolbar">
      {% if is_cloud %}
      <span class="cloud-note">Base de donnees partagee (Neon) &mdash; les relev&eacute;s se font toujours via navigateur Chrome connect&eacute; sur le Mac; les magasins/produits ajout&eacute;s ici ou sur le Mac apparaissent des deux c&ocirc;t&eacute;s.</span>
      {% else %}
      <span class="cloud-note">Mise a jour manuelle (navigateur Chrome connecte) &mdash; pas de verification automatique.</span>
      {% endif %}
    </div>
    <form class="addbar" onsubmit="return addProduct(event)">
      <input type="text" id="productUrl" placeholder="Collez une URL de produit homedepot.ca...">
      <button type="submit">Ajouter</button>
    </form>
    <div id="addStatus"></div>

    <details class="panel" id="storesPanel">
      <summary>Magasins surveilles</summary>
      <div class="panel-body">
        <ul class="storelist" id="storeList">
          {% for s in watch_stores %}
          <li data-id="{{ s.id }}">
            <span>{{ s.name }}{% if s.city %} &middot; {{ s.city }}{% endif %} (ancre: {{ s.search_anchor }})</span>
            <button onclick="deleteStore({{ s.id }})">retirer</button>
          </li>
          {% endfor %}
        </ul>
        <form class="storeform" onsubmit="return addStore(event)">
          <input type="text" id="storeName" placeholder="Nom du magasin (ex: Home Depot Calgary Sud)" required>
          <input type="text" id="storeAnchor" placeholder="Code postal ou ville pres de ce magasin" required>
          <button type="submit">Ajouter le magasin</button>
        </form>
        <div id="storeStatus"></div>
      </div>
    </details>
  </header>

  <dl class="clockbar">
    <div>
      <dt>Dernier releve</dt>
      <dd data-age="{{ stats.poll_age if stats.poll_age is not none else '' }}">{{ stats.poll_age_text }}</dd>
    </div>
  </dl>

  {% if not rows %}
  <div class="empty">Aucun produit suivi. Collez une URL homedepot.ca ci-dessus pour commencer.</div>
  {% endif %}

  {% for row in rows %}
  <section class="group {{ 'starred' if row.starred else '' }}">
    <div class="groupbody">
      <h2>{{ row.name }}</h2>
      <button class="star {{ 'on' if row.starred else '' }}" onclick="toggleStar({{ row.id }}, this)" title="Epingler en haut">&#9733;</button>
      <button class="star" onclick="removeProduct({{ row.id }}, '{{ row.name|e }}')" title="Retirer du suivi" style="color: var(--rust);">&times;</button>
      <div class="sku">Article {{ row.sku }}{% if row.model %} &middot; N&deg; de modele {{ row.model }}{% endif %} &middot; <a href="{{ row.url }}" target="_blank" rel="noopener">voir sur homedepot.ca</a></div>
      {% if row.net_total %}
      <div class="soldtotal {{ 'up' if row.net_total.total > 0 else ('down' if row.net_total.total < 0 else 'flat') }}">
        Variation nette (tous magasins) : {{ '%+d'|format(row.net_total.total) if row.net_total.total != 0 else '0' }} depuis le {{ row.net_total.since }}
      </div>
      <ul class="netbreakdown">
        {% for cell in row.cells %}
        {% if cell.net %}
        <li class="{{ 'up' if cell.net.total > 0 else ('down' if cell.net.total < 0 else 'flat') }}">
          {{ cell.store_short }} : {{ '%+d'|format(cell.net.total) if cell.net.total != 0 else '0' }}
        </li>
        {% endif %}
        {% endfor %}
      </ul>
      {% endif %}

      {% if not row.cells %}
      <div class="empty">Aucun magasin surveille pour ce produit. Ajoutez-en un ci-dessus.</div>
      {% endif %}
      <div class="tags">
        {% for cell in row.cells %}
        <article class="tag {{ cell.state }}" role="button" tabindex="0" aria-expanded="false"
                 title="{{ cell.store }}{% if cell.address %} &middot; {{ cell.address }}{% endif %} &mdash; cliquer pour l'historique"
                 onclick="toggleHistory(this)"
                 onkeydown="if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggleHistory(this); }">
          <div class="store">{{ cell.store_short }}</div>
          <div class="qtyrow">
            <span class="qty-group">
              <span class="qty">{{ cell.qty if cell.qty is not none else '--' }}</span>
              {% if cell.delta %}
              <span class="delta {{ 'down' if cell.delta < 0 else 'up' }}">{{ '%+d'|format(cell.delta) }}</span>
              {% endif %}
            </span>
            {% if cell.price %}
            <span class="price">{{ cell.price }}</span>
            {% endif %}
          </div>
          <div class="unit">{{ 'en stock' if cell.qty else ('rupture' if cell.qty == 0 else 'pas de donnee') }}</div>
          {% if cell.spark %}
          <svg class="spark" viewBox="0 0 {{ cell.spark.width }} {{ cell.spark.height }}"
               width="100%" height="{{ cell.spark.height }}" role="img"
               aria-label="Historique de {{ cell.readings }} releves">
            <path d="{{ cell.spark.path }}"/>
            {% for m in cell.spark.markers %}
            <circle class="spark-point {{ 'up' if m.up else 'down' }}" cx="{{ m.x }}" cy="{{ m.y }}" r="2.6">
              <title>{{ m.when }} : {{ m.previous }} &rarr; {{ m.quantity }} ({{ '%+d'|format(m.delta) }})</title>
            </circle>
            {% endfor %}
          </svg>
          {% elif cell.note %}
          <div class="note">{{ cell.note }}</div>
          {% else %}
          <div class="note">{{ cell.readings }} releve(s), historique trop court</div>
          {% endif %}
          {% if cell.sales %}
          <div class="sold">&darr; {{ cell.sales.sold }} vendu(s) depuis le {{ cell.sales.since }}</div>
          {% endif %}
          <div class="history-panel" hidden onclick="event.stopPropagation()">
            {% if cell.history %}
            <ul>
              {% for h in cell.history %}
              <li class="{{ h.kind }}">
                <span class="h-when">{{ h.when }}</span>
                <span class="h-diff">{% if h.delta_text is not none %}{{ h.delta_text if h.delta_text != '0' else 'inchange' }}{% else %}1er releve{% endif %}</span>
              </li>
              {% endfor %}
            </ul>
            {% else %}
            <div class="history-empty">Aucun releve enregistre pour ce magasin.</div>
            {% endif %}
          </div>
        </article>
        {% endfor %}
      </div>
    </div>
    {% if row.image_url %}
    <div class="groupimg">
      <img src="{{ row.image_url }}" alt="{{ row.name }}" loading="lazy" referrerpolicy="no-referrer">
    </div>
    {% endif %}
  </section>
  {% endfor %}

  <section class="feed">
    <h2>Journal des releves</h2>
    {% if feed %}
      {% for group in feed %}
      <div class="feedgroup">
        <h3>{{ group.store }}</h3>
        {% for mgroup in group.models %}
        <div class="feedmodel">
          <h4>{{ mgroup.model }}</h4>
          <ol>
            {% for event in mgroup.entries %}
            <li class="{{ event.kind }}">
              <span class="when">{{ event.when }}</span>
              <span class="what">{{ event.product }}</span>
              {% if event.previous is none %}
              <span class="move">1er releve : {{ event.quantity }}</span>
              {% else %}
              <span class="move">{{ event.previous }} &rarr; {{ event.quantity }} ({{ event.delta_text }})</span>
              {% endif %}
            </li>
            {% endfor %}
          </ol>
        </div>
        {% endfor %}
      </div>
      {% endfor %}
    {% else %}
    <div class="empty">Aucun releve enregistre pour l'instant. Le journal se remplit a chaque prise d'information.</div>
    {% endif %}
  </section>
</div>

<script>
  const seen = {{ stats.last_change_id }};
  const boardEl = document.querySelector('.board');
  const pristineHTML = boardEl.innerHTML;
  let currentLang = 'fr';
  let clockTimer = null;
  let stamp = null;
  let age = null;

  function T(fr, en) { return currentLang === 'en' ? en : fr; }

  function label(seconds) {
    if (seconds < 60) return seconds + ' s';
    if (seconds < 5400) return Math.floor(seconds / 60) + ' min';
    if (seconds < 172800) return Math.floor(seconds / 3600) + ' h';
    return Math.floor(seconds / 86400) + (currentLang === 'en' ? ' d' : ' j');
  }

  function initClock() {
    if (clockTimer) { clearInterval(clockTimer); clockTimer = null; }
    stamp = boardEl.querySelector('[data-age]');
    age = stamp && stamp.dataset.age ? parseInt(stamp.dataset.age, 10) : null;
    if (age !== null) {
      clockTimer = setInterval(() => { age += 1; stamp.textContent = label(age); }, 1000);
    }
  }

  function translateToEnglish() {
    const root = boardEl;
    const h1 = root.querySelector('h1');
    if (h1) h1.textContent = 'Stock tracking';
    const toggle = document.getElementById('langToggle');
    if (toggle) toggle.textContent = 'Francais';
    const metaSpans = root.querySelectorAll('.meta > span');
    if (metaSpans[0]) {
      const n = parseInt((metaSpans[0].textContent.match(/\d+/) || ['0'])[0], 10);
      metaSpans[0].textContent = n + ' item' + (n === 1 ? '' : 's');
    }
    if (metaSpans[1]) {
      const n = parseInt((metaSpans[1].textContent.match(/\d+/) || ['0'])[0], 10);
      metaSpans[1].textContent = n + ' change(s) in the last 24h';
    }
    const cloudNote = root.querySelector('.cloud-note');
    if (cloudNote) {
      if (cloudNote.textContent.indexOf('Base de donnees partagee') !== -1) {
        cloudNote.textContent = 'Shared database (Neon) - readings are still taken via a Chrome browser connected on the Mac; stores/products added here or on the Mac appear on both sides.';
      } else if (cloudNote.textContent.indexOf('Mise a jour manuelle') !== -1) {
        cloudNote.textContent = 'Manual update (connected Chrome browser) - no automatic check.';
      }
    }
    const csvLink = root.querySelector('a[href="/export.csv"]');
    if (csvLink) csvLink.textContent = 'Download CSV';
    const changesLink = root.querySelector('a[href="/changes.csv"]');
    if (changesLink) changesLink.textContent = 'Change log';
    const addBtn = root.querySelector('.addbar button[type="submit"]');
    if (addBtn) addBtn.textContent = 'Add';
    const productUrl = root.querySelector('#productUrl');
    if (productUrl) productUrl.placeholder = 'Paste a homedepot.ca product URL...';
    const summary = root.querySelector('#storesPanel summary');
    if (summary) summary.textContent = 'Watched stores';
    root.querySelectorAll('#storeList li').forEach(function (li) {
      const span = li.querySelector('span');
      if (span) span.textContent = span.textContent.replace('(ancre:', '(anchor:');
      const btn = li.querySelector('button');
      if (btn) btn.textContent = 'remove';
    });
    const storeNameInput = root.querySelector('#storeName');
    if (storeNameInput) storeNameInput.placeholder = 'Store name (e.g. Home Depot Calgary South)';
    const storeAnchorInput = root.querySelector('#storeAnchor');
    if (storeAnchorInput) storeAnchorInput.placeholder = 'Postal code or city near this store';
    const storeSubmit = root.querySelector('.storeform button[type="submit"]');
    if (storeSubmit) storeSubmit.textContent = 'Add store';
    const clockDt = root.querySelector('.clockbar dt');
    if (clockDt) clockDt.textContent = 'Last reading';
    root.querySelectorAll('.board > .empty').forEach(function (el) {
      if (el.textContent.indexOf('Aucun produit suivi') !== -1) {
        el.textContent = 'No product tracked. Paste a homedepot.ca URL above to get started.';
      }
    });
    root.querySelectorAll('.group').forEach(function (group) {
      group.querySelectorAll('.star').forEach(function (btn) {
        if (btn.title === 'Epingler en haut') btn.title = 'Pin to top';
        if (btn.title === 'Retirer du suivi') btn.title = 'Remove from tracking';
      });
      const sku = group.querySelector('.sku');
      if (sku) {
        Array.from(sku.childNodes).forEach(function (node) {
          if (node.nodeType === Node.TEXT_NODE) {
            node.textContent = node.textContent
              .replace('Article', 'Item')
              .replace('N° de modele', 'Model No.');
          }
        });
        const link = sku.querySelector('a');
        if (link) link.textContent = 'view on homedepot.ca';
      }
      const soldtotal = group.querySelector('.soldtotal');
      if (soldtotal) {
        soldtotal.textContent = soldtotal.textContent
          .replace('Variation nette (tous magasins) :', 'Net change (all stores):')
          .replace('depuis le', 'since');
      }
      group.querySelectorAll('.empty').forEach(function (el) {
        if (el.textContent.indexOf('Aucun magasin surveille') !== -1) {
          el.textContent = 'No store watched for this product. Add one above.';
        }
      });
      group.querySelectorAll('.tag').forEach(function (tag) {
        if (tag.title) tag.title = tag.title.replace("cliquer pour l'historique", 'click for history');
        const unit = tag.querySelector('.unit');
        if (unit) {
          const map = {'en stock': 'in stock', 'rupture': 'out of stock', 'pas de donnee': 'no data'};
          if (map[unit.textContent]) unit.textContent = map[unit.textContent];
        }
        const note = tag.querySelector('.note');
        if (note && note.textContent.indexOf('historique trop court') !== -1) {
          note.textContent = note.textContent
            .replace('releve(s)', 'reading(s)')
            .replace('historique trop court', 'history too short');
        }
        const sold = tag.querySelector('.sold');
        if (sold) {
          sold.textContent = sold.textContent.replace('vendu(s)', 'sold').replace('depuis le', 'since');
        }
        tag.querySelectorAll('.history-panel li .h-diff').forEach(function (el) {
          if (el.textContent === 'inchange') el.textContent = 'unchanged';
          if (el.textContent === '1er releve') el.textContent = 'first reading';
        });
        const histEmpty = tag.querySelector('.history-empty');
        if (histEmpty) histEmpty.textContent = 'No reading recorded for this store.';
      });
    });
    const feedH2 = root.querySelector('.feed > h2');
    if (feedH2) feedH2.textContent = 'Reading log';
    root.querySelectorAll('.feed .move').forEach(function (el) {
      if (el.textContent.indexOf('1er releve :') !== -1) {
        el.textContent = el.textContent.replace('1er releve :', 'First reading:');
      }
    });
    const feedEmpty = root.querySelector('.feed > .empty');
    if (feedEmpty && feedEmpty.textContent.indexOf('Aucun releve enregistre pour') !== -1) {
      feedEmpty.textContent = 'No reading recorded yet. The log fills in with each check.';
    }
  }

  function setLanguage(lang) {
    currentLang = lang;
    try { localStorage.setItem('rona_lang', lang); } catch (e) { /* ignore */ }
    boardEl.innerHTML = pristineHTML;
    if (lang === 'en') translateToEnglish();
    initClock();
    if (sessionStorage.getItem('storesPanelOpen') === '1') {
      const panel = document.getElementById('storesPanel');
      if (panel) panel.open = true;
      sessionStorage.removeItem('storesPanelOpen');
    }
  }

  function toggleLanguage() {
    setLanguage(currentLang === 'en' ? 'fr' : 'en');
  }

  let storedLang = 'fr';
  try { storedLang = localStorage.getItem('rona_lang') || 'fr'; } catch (e) { /* ignore */ }
  setLanguage(storedLang);

  async function poll() {
    try {
      const state = await (await fetch('/api/state')).json();
      if (state.last_change_id !== seen) location.reload();
    } catch (e) { /* collecteur arrete ou hors ligne, on reessaie au prochain tour */ }
  }
  setInterval(poll, 8000);

  async function addProduct(ev) {
    ev.preventDefault();
    const input = document.getElementById('productUrl');
    const status = document.getElementById('addStatus');
    const url = input.value.trim();
    if (!url) return false;
    status.textContent = T('Ajout en cours (verification du produit)...', 'Adding (checking product)...');
    try {
      const res = await fetch('/api/products', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({url})
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'echec');
      status.textContent = T('Ajoute: ', 'Added: ') + (data.name || url);
      input.value = '';
      setTimeout(() => location.reload(), 800);
    } catch (e) {
      status.textContent = T('Erreur: ', 'Error: ') + e.message;
    }
    return false;
  }

  async function addStore(ev) {
    ev.preventDefault();
    const nameInput = document.getElementById('storeName');
    const anchorInput = document.getElementById('storeAnchor');
    const status = document.getElementById('storeStatus');
    const name = nameInput.value.trim();
    const anchor = anchorInput.value.trim();
    if (!name || !anchor) {
      status.textContent = T('Erreur: le nom et le code postal/ville sont requis.', 'Error: name and postal code/city are required.');
      return false;
    }
    status.textContent = T('Ajout en cours...', 'Adding...');
    try {
      const res = await fetch('/api/stores', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, search_anchor: anchor})
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || ('echec (HTTP ' + res.status + ')'));
      status.textContent = T('Ajoute: ', 'Added: ') + name;
      nameInput.value = '';
      anchorInput.value = '';
      sessionStorage.setItem('storesPanelOpen', '1');
      setTimeout(() => location.reload(), 600);
    } catch (e) {
      status.textContent = T('Erreur: ', 'Error: ') + e.message;
    }
    return false;
  }

  async function deleteStore(id) {
    sessionStorage.setItem('storesPanelOpen', '1');
    try {
      const res = await fetch('/api/stores/' + id, {method: 'DELETE'});
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || ('echec (HTTP ' + res.status + ')'));
      }
    } catch (e) {
      alert(T('Erreur lors du retrait du magasin: ', 'Error while removing the store: ') + e.message);
    }
    location.reload();
  }

  async function toggleStar(id, btn) {
    const on = !btn.classList.contains('on');
    await fetch('/api/products/' + id + '/star', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({starred: on})
    });
    location.reload();
  }

  async function removeProduct(id, name) {
    const msg = T('Retirer "', 'Remove "') + name + T('" du suivi ? L\\'historique de ce produit sera aussi supprime.', '" from tracking? This product\\'s history will also be deleted.');
    if (!confirm(msg)) return;
    await fetch('/api/products/' + id, {method: 'DELETE'});
    location.reload();
  }

  function toggleHistory(card) {
    const panel = card.querySelector('.history-panel');
    const open = panel.hasAttribute('hidden');
    panel.toggleAttribute('hidden', !open);
    card.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  let checkNowTimer = null;

  async function checkNow() {
    const btn = document.getElementById('checkNowBtn');
    const status = document.getElementById('checkNowStatus');
    btn.disabled = true;
    status.textContent = T('Verification en cours (peut prendre 1-2 min)...', 'Checking (may take 1-2 min)...');
    try {
      const res = await fetch('/api/check-now', {method: 'POST'});
      const data = await res.json();
      if (!data.started) {
        if (data.reason === 'deja_en_cours') {
          status.textContent = T('Une verification est deja en cours...', 'A check is already in progress...');
          pollCheckNow();
        } else {
          status.textContent = T("Impossible de demarrer la verification.", "Could not start the check.");
          btn.disabled = false;
        }
        return;
      }
      pollCheckNow();
    } catch (e) {
      status.textContent = T('Erreur reseau, reessayez.', 'Network error, try again.');
      btn.disabled = false;
    }
  }

  function pollCheckNow() {
    const btn = document.getElementById('checkNowBtn');
    const status = document.getElementById('checkNowStatus');
    if (checkNowTimer) clearInterval(checkNowTimer);
    checkNowTimer = setInterval(async () => {
      try {
        const state = await (await fetch('/api/state')).json();
        if (!state.running) {
          clearInterval(checkNowTimer);
          checkNowTimer = null;
          btn.disabled = false;
          if (state.last_error) {
            status.textContent = T('Echec : ', 'Failed: ') + state.last_error;
          } else {
            status.textContent = T('Termine.', 'Done.');
            setTimeout(() => location.reload(), 500);
          }
        }
      } catch (e) { /* reessaie au prochain tick */ }
    }, 2000);
  }
</script>
</body>
</html>
"""


@app.route("/")
def index():
    rows, feed, stats, watch_stores = build_view()
    return render_template_string(
        TEMPLATE, rows=rows, feed=feed, stats=stats, watch_stores=watch_stores,
        is_cloud=(db.BACKEND == "postgres"),
    )


@app.route("/api/state")
def api_state():
    conn = db.connect()
    fresh = db.freshness(conn)
    state = {
        "last_change_id": db.last_change_id(conn),
        "last_poll": fresh["last_poll"],
        "last_poll_age": fresh["last_poll_age"],
        "running": RUN_STATE["running"],
        "last_error": RUN_STATE["last_error"],
    }
    conn.close()
    return jsonify(state)


def _guess_name_from_url(url):
    """Devine un nom lisible a partir du slug de l'URL, sans ouvrir de
    navigateur. Ex: .../boitiers-utilitaires-rona-en-acier-2-x-3-po-...-93515000
    -> "Boitiers utilitaires rona en acier 2 x 3 po".
    """
    try:
        slug = url.rstrip("/").split("/")[-1]
        # retire le code produit final (ex: -93515000 ou -c1104lhu-93515000)
        parts = slug.split("-")
        while parts and (parts[-1].isdigit() or re.match(r"^[a-z0-9]{5,10}$", parts[-1]) and any(c.isdigit() for c in parts[-1])):
            parts.pop()
        guess = " ".join(parts).strip()
        return guess[:1].upper() + guess[1:] if guess else None
    except Exception:
        return None


@app.route("/api/products", methods=["POST"])
def api_add_product():
    # Aucun navigateur n'est ouvert ici: l'anti-bot de RONA detecte les
    # verifications automatisees. Le nom est devine depuis l'URL; SKU/prix/
    # image restent vides jusqu'a une mise a jour manuelle (voir README).
    payload = request.get_json(force=True, silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url.startswith("http"):
        return jsonify({"error": "URL invalide"}), 400

    conn = db.connect()
    product_id = db.add_product(conn, url)

    guessed_name = _guess_name_from_url(url)
    if guessed_name:
        db.update_product_info(conn, product_id, name=guessed_name)

    product = db.get_product(conn, product_id)
    conn.close()
    return jsonify({"id": product_id, "name": product.get("name") or url})


@app.route("/api/products/<int:product_id>/star", methods=["POST"])
def api_star(product_id):
    payload = request.get_json(force=True, silent=True) or {}
    conn = db.connect()
    db.set_starred(conn, product_id, bool(payload.get("starred")))
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/products/<int:product_id>", methods=["DELETE"])
def api_delete_product(product_id):
    conn = db.connect()
    db.delete_product(conn, product_id)
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/stores", methods=["POST"])
def api_add_store():
    payload = request.get_json(force=True, silent=True) or {}
    name = (payload.get("name") or "").strip()
    anchor = (payload.get("search_anchor") or "").strip()
    if not name or not anchor:
        return jsonify({"error": "nom et ancre requis"}), 400
    conn = db.connect()
    store_id = db.add_store(
        conn, name, anchor, match_text=payload.get("match_text"),
        address=payload.get("address"), city=payload.get("city"), province=payload.get("province"),
    )
    conn.close()
    return jsonify({"id": store_id})


@app.route("/api/stores/<int:store_id>", methods=["DELETE"])
def api_delete_store(store_id):
    conn = db.connect()
    db.delete_store(conn, store_id)
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/readings", methods=["POST"])
def api_add_readings():
    """Enregistre un lot de releves (quantite + prix) recueillis ailleurs
    (ex: Claude qui vient de naviguer sur rona.ca via l'outil Chrome et
    relever les 13 magasins). Ecrit directement dans la base connectee
    (Postgres/Neon sur Render, ou stock.db en local).

    Concu pour etre appele depuis le navigateur -- Claude charge la page du
    tableau de bord deploye (ex: https://rona-tracker.onrender.com) puis
    execute un fetch() JS depuis cette page (meme origine, donc pas de
    souci CORS):

      fetch("/api/readings", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-Api-Token": "..."},
        body: JSON.stringify({readings: [...]})
      })

    Ca contourne les deux blocages reseau habituels: le bac a sable cloud de
    Claude n'a pas d'acces internet general (liste blanche restreinte), et
    le pont mcp__remote-devices__device_bash n'en a aucun -- mais le
    navigateur Chrome de l'utilisateur, lui, a un acces normal.

    Protection: si API_WRITE_TOKEN est defini dans l'environnement (Render,
    ou .env_cloud en local), le jeton doit etre fourni via l'en-tete
    X-Api-Token ou le parametre ?token= -- sinon 401. Si la variable n'est
    pas definie du tout, la verification est desactivee (usage local sans
    jeton).

    Corps attendu:
      {"readings": [{"sku": "p1", "store_id": "s1", "quantity": 88,
                      "price": "8,99 $", "note": "..."}, ...]}
    """
    if API_WRITE_TOKEN:
        supplied = request.headers.get("X-Api-Token") or request.args.get("token")
        if supplied != API_WRITE_TOKEN:
            return jsonify({"error": "jeton invalide ou manquant"}), 401

    payload = request.get_json(force=True, silent=True) or {}
    readings = payload.get("readings")
    if not isinstance(readings, list) or not readings:
        return jsonify({"error": "readings (liste non vide) requis"}), 400

    conn = db.connect()
    changes = []
    errors = []
    recorded = 0
    try:
        for item in readings:
            if not isinstance(item, dict):
                errors.append({"item": item, "error": "doit etre un objet"})
                continue
            sku = item.get("sku")
            store_id = item.get("store_id")
            quantity = item.get("quantity")
            if not sku or not store_id or quantity is None:
                errors.append({"item": item, "error": "sku, store_id et quantity requis"})
                continue
            try:
                quantity = int(quantity)
            except (TypeError, ValueError):
                errors.append({"item": item, "error": "quantity doit etre un entier"})
                continue

            price = item.get("price")
            note = item.get("note") or "verifie via navigateur (session planifiee), enregistre via /api/readings"
            status = "in_stock" if quantity > 0 else "out_of_stock"

            before_row = db.last_known_quantity(conn, sku, store_id)
            before = before_row["quantity"] if before_row else None
            previous_at = before_row["taken_at"] if before_row else None

            db.record(conn, sku, store_id, quantity, status, ok=True, note=note, price=price)
            recorded += 1

            change = db.note_change(conn, sku, store_id, before, quantity, previous_at=previous_at)
            if change:
                changes.append(change)
    finally:
        conn.close()

    pivot_path = None
    if recorded and export_pivot:
        try:
            pivot_path = export_pivot.generate()
        except Exception as exc:  # noqa: BLE001 - ne jamais bloquer l'API pour ca
            print(f"[export_pivot] echec de la generation du classeur pivot: {exc}")

    return jsonify({
        "recorded": recorded, "changes": changes, "errors": errors,
        "pivot_file": pivot_path,
    })


def _background_check():
    """Verification automatisee via navigateur (Playwright), declenchee a la
    demande par le bouton "Actualiser maintenant". A deja ete bloquee par
    l'anti-bot de RONA lors d'essais precedents (voir scrape.py:
    CHALLENGE_MARKERS / wait_out_challenge) -- si ca se reproduit, l'erreur
    remonte dans RUN_STATE["last_error"] et s'affiche sous le bouton; la
    mise a jour manuelle (voir README / insert_manual_readings.py) reste la
    solution de secours.
    """
    if not RUN_LOCK.acquire(blocking=False):
        return
    RUN_STATE["running"] = True
    RUN_STATE["last_started"] = db.now_iso()
    print(f"[{db.now_iso()}] Verification en cours...")
    try:
        conn = db.connect()
        config = load_config()
        schedule_cfg = config.get("schedule", {})
        headless = schedule_cfg.get("headless", True)
        channel = schedule_cfg.get("browser_channel")
        rows, changes = run_check(conn, verbose=True, headless=headless, channel=channel)
        from alerts import notify
        notify(conn, config, changes)
        conn.close()
        RUN_STATE["last_error"] = None
        print(f"[{db.now_iso()}] Verification terminee: {len(rows)} releve(s), {len(changes)} changement(s)")
    except Exception as exc:
        import traceback
        RUN_STATE["last_error"] = str(exc)
        print(f"[{db.now_iso()}] ECHEC de la verification: {exc}")
        traceback.print_exc()
    finally:
        RUN_STATE["running"] = False
        RUN_STATE["last_finished"] = db.now_iso()
        RUN_LOCK.release()


@app.route("/api/check-now", methods=["POST"])
def api_check_now():
    if RUN_STATE["running"]:
        return jsonify({"started": False, "reason": "deja_en_cours"})
    threading.Thread(target=_background_check, daemon=True).start()
    return jsonify({"started": True})


def csv_response(headers, rows, filename):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    writer.writerows(rows)
    return Response(
        buffer.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/export.csv")
def export_csv():
    conn = db.connect()
    names = {db.product_key(p): (p["name"] or p["url"]) for p in db.list_products(conn)}
    store_names = {db.store_key(s): s["name"] for s in db.list_stores(conn)}
    rows = [
        [r["taken_at"], names.get(r["sku"], r["sku"]), store_names.get(r["store_id"], r["store_id"]),
         r["quantity"], r["status"], r["ok"], r["source_updated_at"], r["cache_age"],
         r["cache_max_age"], r["http_status"], r["latency_ms"], r["note"]]
        for r in db.all_readings(conn)
    ]
    conn.close()
    return csv_response(
        ["horodatage", "produit", "magasin", "quantite", "statut", "ok", "date_source",
         "age_cache", "max_age_cache", "http", "latence_ms", "note"],
        rows, "stock-hd.csv",
    )


@app.route("/changes.csv")
def changes_csv():
    conn = db.connect()
    names = {db.product_key(p): (p["name"] or p["url"]) for p in db.list_products(conn)}
    store_names = {db.store_key(s): s["name"] for s in db.list_stores(conn)}
    rows = [
        [c["detected_at"], names.get(c["sku"], c["sku"]), store_names.get(c["store_id"], c["store_id"]),
         c["kind"], c["from_quantity"], c["to_quantity"], c["delta"], c["gap_seconds"]]
        for c in db.all_changes(conn)
    ]
    conn.close()
    return csv_response(
        ["detecte_le", "produit", "magasin", "type", "avant", "apres", "delta", "ecart_secondes"],
        rows, "changements-hd.csv",
    )


def start_scheduler():
    """Thread d'arriere-plan qui lancait des verifications Chrome periodiques
    -- DESACTIVE (voir _background_check). Fonction conservee inutilisee.
    """
    config = load_config()
    schedule = config.get("schedule", {})
    interval = schedule.get("interval_active", 1800)

    def loop():
        time.sleep(10)
        while True:
            _background_check()
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True).start()


# Images des produits (photo rona.ca), affichees dans la marge droite du
# tableau de bord. Motif CDN previsible: images/<sku>_L.jpg. Renseignees
# automatiquement au demarrage (idempotent, ne touche que ces 3 SKUs) --
# aucun script separe a executer.
SEED_IMAGES_BY_SKU = {
    "1001975381": "https://images.homedepot.ca/productimages/p_1001975381.jpg?product-images=l",
    "1001974422": "https://images.homedepot.ca/productimages/p_1001974422.jpg?product-images=l",
    "1001974440": "https://images.homedepot.ca/productimages/p_1001974440.jpg?product-images=l",
    "1001974442": "https://images.homedepot.ca/productimages/p_1001974442.jpg?product-images=l",
}


def seed_product_images():
    conn = db.connect()
    try:
        rows = conn.execute("SELECT id, sku FROM products").fetchall()
        for row in rows:
            image_url = SEED_IMAGES_BY_SKU.get(row["sku"])
            if image_url:
                db.update_product_info(conn, row["id"], image_url=image_url)
    finally:
        conn.close()


def _print_access_urls(port):
    """Affiche l'URL locale ET l'URL reseau (utile pour Tailscale ou le
    Wi-Fi maison) au demarrage, pour eviter d'avoir a chercher l'adresse IP
    a la main."""
    import socket

    print(f"\nTableau de bord demarre. Acces:")
    print(f"  - sur ce Mac        : http://127.0.0.1:{port}")
    try:
        # Truc classique pour trouver l'IP locale principale sans dependance
        # externe: on "connecte" un socket UDP vers une adresse publique
        # (aucun paquet n'est reellement envoye) puis on lit l'IP source.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
        print(f"  - sur le reseau/VPN : http://{lan_ip}:{port}")
    except Exception:
        pass
    print()


if __name__ == "__main__":
    # start_scheduler() n'est plus appele: l'anti-bot de RONA detecte les
    # verifications automatisees par navigateur. Suivi 100% manuel desormais
    # (verifier sur rona.ca, puis inserer via un script -- voir README).
    seed_product_images()
    PORT = 8766
    # host="0.0.0.0" = ecoute sur toutes les interfaces reseau (pas juste
    # 127.0.0.1/localhost), donc accessible depuis le Wi-Fi local ou via
    # Tailscale, pas seulement depuis ce Mac.
    _print_access_urls(PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False)
