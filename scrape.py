#!/usr/bin/env python3
"""Recolte les infos produit et la disponibilite par magasin sur rona.ca.

Contrairement a l'approche initiale du projet (capturer a la main l'appel
reseau prive), on pilote un navigateur headless et on lit directement le
widget "disponibilite en magasin" de la fiche produit: pas de cookies ni
d'en-tetes a capturer, juste une URL de produit et un code postal/ville
d'ancrage pour chaque magasin surveille.

Fragile par nature: si RONA change son gabarit de page, le parseur de texte
ci-dessous devra etre ajuste. C'est le compromis assume par ce projet.
"""

import random
import re
import time

try:
    from playwright_stealth import stealth_sync
except ImportError:
    stealth_sync = None  # `pip install playwright-stealth` recommande mais pas obligatoire

# Marqueurs de l'interstitiel anti-bot de RONA. Certains services de ce type
# (mode "sous attaque" de Cloudflare et similaires) se resolvent tout seuls
# apres quelques secondes sans intervention humaine: on attend avant d'abandonner.
CHALLENGE_MARKERS = (
    "Vérification de sécurité en cours",
    "Ce site utilise un service de sécurité",
)

STORE_LINE = re.compile(r"^(.*?)\s*-\s*([\d.,]+)\s*km$")
QTY_LINE = re.compile(r"^(\d+)\s+en stock$")
SKU_RE = re.compile(r"N[°o]\s*d.article\s*(\S+)", re.IGNORECASE)
MODEL_RE = re.compile(r"N[°o]\s*de mod[eè]le\s*(\S+)", re.IGNORECASE)
POSTAL_RE = re.compile(r"([A-Za-z]\d[A-Za-z]\s*\d[A-Za-z]\d)")

# "." a la place de l'apostrophe: RONA utilise l'apostrophe typographique
# (') et non l'apostrophe droite ('), une correspondance texte exacte les
# manque silencieusement. Un point regex matche n'importe laquelle des deux.
CHECK_OTHER_STORES_RE = re.compile(r"V.rifiez d.autres magasins")


def _launch(playwright, headless=True, channel=None):
    # Pas de executable_path: on laisse Playwright trouver le Chromium
    # installe par "playwright install chromium" a son emplacement par defaut.
    # --disable-blink-features=AutomationControlled: RONA utilise un service
    # anti-bot qui peut declencher une page de "verification de securite" en
    # detectant les marqueurs d'automatisation (navigator.webdriver, etc.).
    #
    # channel="msedge" (optionnel): pilote le vrai Microsoft Edge installe sur
    # la machine au lieu du Chromium "de test" fourni par Playwright. A
    # essayer si RONA bloque systematiquement -- certains services anti-bot
    # reagissent differemment a un navigateur reellement installe qu'au
    # Chromium generique de Playwright. Si Edge n'est pas installe, on
    # retombe silencieusement sur le Chromium par defaut plutot que de tout
    # faire echouer.
    kwargs = dict(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    if channel:
        try:
            return playwright.chromium.launch(channel=channel, **kwargs)
        except Exception as exc:
            print(f"[scrape] canal '{channel}' indisponible ({exc}); retour au Chromium par defaut")
    return playwright.chromium.launch(**kwargs)


STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', {get: () => ['fr-CA', 'fr', 'en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def wait_out_challenge(page, max_wait_s=20):
    """Attend que l'interstitiel anti-bot de RONA disparaisse (certains se
    resolvent seuls apres quelques secondes). Retourne False si toujours
    present a l'echeance.
    """
    deadline = time.monotonic() + max_wait_s
    while True:
        try:
            text = page.locator("body").inner_text(timeout=3000)
        except Exception:
            text = ""
        if not any(marker in text for marker in CHALLENGE_MARKERS):
            return True
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(2500)


def dismiss_overlays(page):
    """Ferme la banniere promo / le bandeau cookies s'ils apparaissent. Non
    bloquant: chaque tentative est silencieuse si l'element n'existe pas.
    """
    for label in ("Non merci", "Refuser", "Tout refuser", "J.accepte", "Accepter"):
        try:
            btn = page.get_by_text(re.compile(label), exact=False).first
            if btn.count() and btn.is_visible(timeout=1000):
                btn.click(timeout=1500)
                page.wait_for_timeout(300)
        except Exception:
            continue


def extract_product_info(page):
    """Best-effort: nom, sku, modele, prix, image. Ne leve jamais d'exception.

    Le titre <title> de la page ("Nom du produit | RONA") est plus fiable
    qu'un selecteur h1: sur une session fraiche, une banniere promo ou de
    consentement peut avoir son propre h1 et fausser la capture.
    """
    info = {"name": None, "sku": None, "model": None, "price": None, "image_url": None}
    try:
        title = page.title() or ""
        name = title.split(" | ")[0].strip()
        if name and "rona.ca" not in name.lower():
            info["name"] = name
    except Exception:
        pass
    if not info["name"]:
        try:
            info["name"] = page.locator("main h1").first.inner_text(timeout=5000).strip()
        except Exception:
            pass
    try:
        body_text = page.locator("main").first.inner_text(timeout=5000)
    except Exception:
        body_text = ""
    m = SKU_RE.search(body_text)
    if m:
        info["sku"] = m.group(1).strip()
    m = MODEL_RE.search(body_text)
    if m:
        info["model"] = m.group(1).strip()
    try:
        img = page.locator("main img").first
        info["image_url"] = img.get_attribute("src", timeout=3000)
    except Exception:
        pass
    return info


def parse_store_list(text):
    """Coupe le texte du widget en blocs par magasin. Voir docstring du module."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    stores = []
    i = 0
    while i < len(lines):
        m = STORE_LINE.match(lines[i])
        if not m:
            i += 1
            continue
        name = m.group(1).strip()
        try:
            distance_km = float(m.group(2).replace(",", "."))
        except ValueError:
            distance_km = None
        i += 1
        address_lines = []
        quantity = None
        found_qty = False
        scanned = 0
        while i < len(lines) and scanned < 8:
            if STORE_LINE.match(lines[i]):
                break
            qm = QTY_LINE.match(lines[i])
            if qm:
                quantity = int(qm.group(1))
                found_qty = True
                i += 1
                break
            address_lines.append(lines[i])
            i += 1
            scanned += 1
        address = ", ".join(a.rstrip(",") for a in address_lines) if address_lines else None
        postal = None
        pm = POSTAL_RE.search(address) if address else None
        if pm:
            postal = pm.group(1).upper().replace(" ", "")
        stores.append({
            "name": name,
            "distance_km": distance_km,
            "address": address,
            "postal_code": postal,
            "quantity": quantity if found_qty else None,
            "in_stock_known": found_qty,
        })
        # avance jusqu'a la prochaine ligne "nom - Xkm" (saute "Choisir ce magasin" etc.)
        while i < len(lines) and not STORE_LINE.match(lines[i]):
            i += 1
    return stores


def _open_availability_modal(page):
    """Clique le lien "Verifiez d'autres magasins" jusqu'a ce que le champ de
    recherche postal apparaisse (2 essais, avec scroll et attente genereuse:
    la page peut encore etre en train de s'hydrater juste apres le chargement).
    """
    field = page.get_by_placeholder("Code postal, ville").first
    link_count = None
    for attempt in range(2):
        try:
            if field.is_visible(timeout=1500):
                return field
        except Exception:
            pass
        try:
            link = page.get_by_text(CHECK_OTHER_STORES_RE).first
            link_count = link.count()
            link.scroll_into_view_if_needed(timeout=5000)
            link.click(timeout=5000)
        except Exception:
            pass
        try:
            field.wait_for(state="visible", timeout=6000)
            return field
        except Exception:
            page.wait_for_timeout(1000)
    # Diagnostic: si toujours invisible, on capture un extrait de la page
    # pour comprendre pourquoi (widget absent, page bloquee, etc.) plutot
    # que de laisser une simple erreur de timeout opaque.
    try:
        snippet = page.locator("body").inner_text(timeout=3000)[:300].replace("\n", " | ")
    except Exception:
        snippet = "(page illisible)"
    raise RuntimeError(
        f"lien 'Verifiez d'autres magasins' trouve={link_count} fois, "
        f"champ toujours invisible. Debut de page: {snippet}"
    )


def search_stores(page, anchor, show_all=True, timeout_ms=15000):
    """Ouvre le widget de disponibilite et cherche autour de `anchor`
    (code postal ou ville). Retourne la liste de magasins trouves.
    """
    field = _open_availability_modal(page)
    page.wait_for_timeout(random.randint(400, 900))  # temps de "lire" le champ avant de cliquer
    field.click(timeout=5000)
    field.fill("")
    field.type(anchor, delay=random.randint(70, 140))  # frappe humaine, pas 15ms/caractere robotique
    page.wait_for_timeout(random.randint(300, 700))
    field.press("Enter")

    if show_all:
        try:
            checkbox = page.get_by_label(
                "Afficher uniquement les magasins où la marchandise est disponible"
            ).first
            if checkbox.is_checked(timeout=2000):
                checkbox.uncheck(timeout=2000)
        except Exception:
            pass  # non bloquant: au pire on ne voit que les magasins en stock

    page.wait_for_timeout(2500)

    # On lit tout le texte de la page plutot que d'essayer de viser le conteneur
    # exact du widget: parse_store_list() ignore deja les lignes qui ne
    # correspondent a aucun magasin, donc c'est sans risque et plus robuste
    # aux changements de structure HTML.
    text = page.locator("body").inner_text(timeout=timeout_ms)
    return parse_store_list(text)


def check_product(url, anchors, headless=True, channel=None):
    """Charge la fiche produit une seule fois et cherche successivement
    autour de chaque ancre dans `anchors` (une par magasin surveille, ou
    une par region si plusieurs magasins partagent la meme ville).

    channel: voir _launch() -- passer "msedge" pour utiliser le vrai
    Microsoft Edge installe plutot que le Chromium de Playwright.

    Retourne {"product": {...}, "stores_by_anchor": {anchor: [ {...}, ... ]}}.
    """
    from playwright.sync_api import sync_playwright

    result = {"product": {}, "stores_by_anchor": {}, "error": None}
    started = time.monotonic()
    with sync_playwright() as p:
        browser = _launch(p, headless=headless, channel=channel)
        try:
            context = browser.new_context(
                locale="fr-CA",
                viewport={"width": 1440, "height": 900},
                user_agent=DESKTOP_USER_AGENT,
            )
            context.add_init_script(STEALTH_INIT_SCRIPT)
            page = context.new_page()
            if stealth_sync:
                stealth_sync(page)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            # Delai d'arrivee humain plutot qu'une pause fixe: un vrai visiteur
            # ne clique jamais exactement 1200ms apres le chargement.
            page.wait_for_timeout(random.randint(1000, 2200))

            if not wait_out_challenge(page):
                result["error"] = (
                    "bloque par la verification de securite de RONA (page "
                    "d'interstitiel toujours presente apres 20s d'attente)"
                )
            else:
                dismiss_overlays(page)
                result["product"] = extract_product_info(page)

                for anchor in anchors:
                    try:
                        result["stores_by_anchor"][anchor] = search_stores(page, anchor)
                    except Exception as exc:
                        result["stores_by_anchor"][anchor] = []
                        result.setdefault("anchor_errors", {})[anchor] = str(exc)
                    # Pause de longueur variable entre deux magasins, comme le
                    # ferait quelqu'un qui relit les resultats avant de continuer.
                    page.wait_for_timeout(random.randint(1500, 3500))
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            browser.close()
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    return result


def find_store_reading(stores, match_text):
    """Cherche dans une liste de magasins retournee par search_stores() celui
    dont le nom correspond (comparaison insensible a la casse, sous-chaine
    dans les deux sens). Retourne le dict du magasin ou None.
    """
    needle = (match_text or "").strip().lower()
    if not needle:
        return None
    best = None
    for store in stores:
        hay = (store.get("name") or "").strip().lower()
        if not hay:
            continue
        if needle == hay or needle in hay or hay in needle:
            best = store
            break
    return best
