#!/usr/bin/env python3
"""Releve le stock de chaque produit (table `products`) a chaque magasin
surveille (table `watch_stores`), journalise les changements, envoie les
alertes. Remplace l'ancienne approche a base de config.yaml + appel reseau
prive: on pilote un navigateur headless via scrape.py.

Cle stable utilisee dans `readings`/`changes`:
  sku      -> "p{product_id}"   (jamais renomme, meme si le SKU RONA change)
  store_id -> "s{store_id}"
"""

import argparse
import sys
import time

import yaml
from pathlib import Path

import db
import scrape
from alerts import notify

CONFIG = Path(__file__).parent / "config.yaml"


def load_config(path=CONFIG):
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def run_check(conn, verbose=False, headless=True, channel=None):
    """Un passage complet sur tous les produits x tous les magasins surveilles.
    Retourne (rows, changes) comme l'ancien poll_once().

    channel: voir scrape._launch() -- "msedge" pour piloter le vrai Microsoft
    Edge installe au lieu du Chromium de Playwright.
    """
    products = db.list_products(conn)
    stores = db.list_stores(conn)
    rows, changes = [], []

    if not products:
        if verbose:
            print("Aucun produit suivi pour l'instant. Ajoutez-en depuis le tableau de bord.")
        return rows, changes
    if not stores:
        if verbose:
            print("Aucun magasin surveille pour l'instant. Ajoutez-en depuis le tableau de bord.")
        return rows, changes

    anchors = sorted({s["search_anchor"] for s in stores})

    for product in products:
        pkey = db.product_key(product)
        if verbose:
            print(f"\n{product['name'] or product['url']}")
        try:
            result = scrape.check_product(product["url"], anchors, headless=headless, channel=channel)
        except Exception as exc:
            for store in stores:
                db.record(conn, pkey, db.store_key(store), None, None, ok=False, note=str(exc)[:300])
            print(f"   echec complet: {exc}", file=sys.stderr)
            continue

        if result.get("product"):
            info = result["product"]
            db.update_product_info(
                conn, product["id"], sku=info.get("sku"), name=info.get("name"),
                model=info.get("model"), price=info.get("price"), image_url=info.get("image_url"),
            )

        for store in stores:
            skey = db.store_key(store)
            anchor_stores = result.get("stores_by_anchor", {}).get(store["search_anchor"], [])
            match = scrape.find_store_reading(anchor_stores, store["match_text"] or store["name"])
            previous = db.last_known_quantity(conn, pkey, skey)

            if match is None:
                note = (result.get("anchor_errors") or {}).get(
                    store["search_anchor"], "magasin non trouve dans les resultats"
                )
                db.record(conn, pkey, skey, None, None, ok=False, note=note[:300])
                if verbose:
                    print(f"   {store['name']:<28} introuvable ({note})")
                continue

            quantity = match.get("quantity")
            meta = {"latency_ms": result.get("latency_ms"), "http_status": 200}
            db.record(conn, pkey, skey, quantity, None, ok=True, note=None, meta=meta)
            rows.append((product["name"] or product["url"], store["name"], quantity, None))

            event = db.note_change(
                conn, pkey, skey,
                before=previous["quantity"] if previous else None,
                after=quantity,
                previous_at=previous["taken_at"] if previous else None,
            )
            if event:
                event["product"] = product["name"] or product["url"]
                event["store"] = store["name"]
                changes.append(event)

            if verbose:
                mark = "*" if event else " "
                dist = f" ({match['distance_km']}km)" if match.get("distance_km") is not None else ""
                print(f" {mark} {store['name']:<28} qte={quantity}{dist}")

    return rows, changes


def run(config=None, verbose=False, headless=True, channel=None):
    config = config or load_config()
    conn = db.connect()
    rows, changes = run_check(conn, verbose=verbose, headless=headless, channel=channel)
    sent = notify(conn, config, changes) if config.get("alerts") else 0
    if verbose:
        print(f"\n{len(changes)} changement(s), {sent} alerte(s) envoyee(s)")
    conn.close()
    return rows, changes


def main():
    parser = argparse.ArgumentParser(description="Releve le stock RONA par produit x magasin")
    parser.add_argument("--quiet", action="store_true", help="n'affiche que les erreurs")
    parser.add_argument("--show-browser", action="store_true", help="force Chromium visible (sinon: reglage headless de config.yaml)")
    parser.add_argument("--edge", action="store_true", help="pilote le vrai Microsoft Edge installe au lieu du Chromium de Playwright")
    args = parser.parse_args()

    if not args.quiet:
        print(f"Releve {db.now_iso()}")
    cfg = load_config()
    config_headless = cfg.get("schedule", {}).get("headless", True)
    headless = config_headless and not args.show_browser
    channel = "msedge" if args.edge else cfg.get("schedule", {}).get("browser_channel")
    if channel and not args.quiet:
        print(f"(navigateur: canal '{channel}')")
    run(verbose=not args.quiet, headless=headless, channel=channel)


if __name__ == "__main__":
    main()
