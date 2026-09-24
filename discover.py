#!/usr/bin/env python3
"""Ouvre une fiche produit et liste les appels reseau qui ressemblent a de l'inventaire.

But: trouver l'URL, les en-tetes et la forme de la reponse a recopier dans config.yaml.
Lancez sans --headless pour choisir vous-meme le magasin dans la page.

    python discover.py "https://www.rona.ca/fr/produit/...-93515000"
"""

import argparse
import json
import sys

KEYWORDS = ("inventory", "availability", "stock", "store", "disponib", "fulfillment")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="URL de la fiche produit")
    parser.add_argument("--headless", action="store_true", help="sans fenetre visible")
    parser.add_argument("--wait", type=int, default=45, help="secondes d'observation")
    parser.add_argument("--out", default="capture.json", help="fichier de sortie")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Installez Playwright: pip install playwright && playwright install chromium")

    hits = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context()
        page = context.new_page()

        def on_response(response):
            url = response.url.lower()
            if not any(word in url for word in KEYWORDS):
                return
            try:
                body = response.json()
            except Exception:
                return
            hits.append(
                {
                    "url": response.url,
                    "method": response.request.method,
                    "status": response.status,
                    "request_headers": dict(response.request.headers),
                    "request_body": response.request.post_data,
                    "response": body,
                }
            )
            print(f"[{response.status}] {response.request.method} {response.url}")

        page.on("response", on_response)
        page.goto(args.url, wait_until="networkidle", timeout=60000)

        print(f"\nChangez de magasin dans la page. Observation pendant {args.wait} s.\n")
        page.wait_for_timeout(args.wait * 1000)

        cookies = context.cookies()
        browser.close()

    with open(args.out, "w") as handle:
        json.dump({"cookies": cookies, "calls": hits}, handle, indent=2, ensure_ascii=False)

    print(f"\n{len(hits)} appel(s) retenu(s). Detail complet dans {args.out}")
    print("Cherchez dans ce fichier le champ qui contient la quantite, puis reportez")
    print("son chemin dans quantity_path de config.yaml.")


if __name__ == "__main__":
    main()
