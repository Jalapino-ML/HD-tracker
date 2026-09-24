"""Alertes: seuils, tout changement, et livraison par courriel, SMS ou notification poussee."""

import json
import smtplib
import sys
import urllib.request
from email.message import EmailMessage

import db


def threshold_alerts(conn, config):
    """Etat actuel: rupture, stock bas. Une seule fois par periode de calme."""
    rules = config.get("alerts", {})
    low = rules.get("low_threshold", 4)
    products = db.list_products(conn)
    stores = db.list_stores(conn)
    found = []

    for product in products:
        pkey = db.product_key(product)
        pname = product["name"] or product["url"]
        for store in stores:
            skey = db.store_key(store)
            current = db.latest(conn, pkey, skey)
            if not current or current["quantity"] is None:
                continue
            qty = current["quantity"]
            label = f"{pname} chez {store['name']}"
            if qty == 0:
                found.append(("out", pkey, skey, f"{label}: rupture de stock"))
            elif qty <= low:
                found.append(("low", pkey, skey, f"{label}: seulement {qty} en stock"))
    return found


def change_alerts(config, changes):
    """Un evenement par changement detecte, envoye sans delai d'attente."""
    rules = config.get("alerts", {})
    if not rules.get("on_any_change", True):
        return []
    drop = rules.get("drop_threshold", 10)
    lines = []

    for event in changes:
        label = f"{event['product']} chez {event['store']}"
        delta = event["delta"]
        arrow = f"{event['from_quantity']} vers {event['to_quantity']}"
        if event["kind"] == "restock":
            text = f"{label}: de retour en stock, {event['to_quantity']} disponibles"
        elif event["kind"] == "out":
            text = f"{label}: rupture, {arrow}"
        elif delta <= -drop:
            text = f"{label}: chute de {abs(delta)}, {arrow}"
        elif delta < 0:
            text = f"{label}: -{abs(delta)}, {arrow}"
        else:
            text = f"{label}: +{delta}, {arrow}"
        lines.append(("change", event["sku"], event["store_id"], text))
    return lines


def send_email(settings, subject, body):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.get("sender") or settings["username"]
    message["To"] = ", ".join(settings["recipients"])
    message.set_content(body)
    with smtplib.SMTP(settings["smtp_host"], settings.get("smtp_port", 587)) as server:
        server.starttls()
        server.login(settings["username"], settings["password"])
        server.send_message(message)


def send_webhook(settings, subject, body):
    """Notification poussee. ntfy.sh donne une alerte sur telephone en quelques secondes."""
    style = settings.get("format", "ntfy")
    url = settings["url"]
    headers = {"User-Agent": "rona-tracker"}

    if style == "ntfy":
        data = body.encode()
        headers.update({"Title": subject, "Priority": settings.get("priority", "default"),
                        "Tags": "package"})
    elif style == "slack":
        data = json.dumps({"text": f"*{subject}*\n{body}"}).encode()
        headers["Content-Type"] = "application/json"
    elif style == "discord":
        data = json.dumps({"content": f"**{subject}**\n{body}"}).encode()
        headers["Content-Type"] = "application/json"
    else:
        data = json.dumps({"subject": subject, "body": body}).encode()
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.status


def deliver(rules, subject, body):
    """Retourne True si l'alerte est partie. Sans canal configure, la console fait foi."""
    email_settings = rules.get("email", {})
    webhook_settings = rules.get("webhook", {})
    if not email_settings.get("enabled") and not webhook_settings.get("enabled"):
        return True

    delivered = False

    if webhook_settings.get("enabled"):
        try:
            send_webhook(webhook_settings, subject, body)
            delivered = True
        except Exception as exc:
            print(f"Notification poussee echouee: {exc}", file=sys.stderr)

    if email_settings.get("enabled"):
        try:
            send_email(email_settings, subject, body)
            delivered = True
        except Exception as exc:
            print(f"Envoi courriel echoue: {exc}", file=sys.stderr)

    return delivered


def notify(conn, config, changes=None):
    """Envoie les alertes de changement tout de suite, les alertes d'etat avec anti-repetition."""
    rules = config.get("alerts", {})
    cooldown = rules.get("cooldown_hours", 12)
    pending = list(change_alerts(config, changes or []))

    for kind, sku, store_id, detail in threshold_alerts(conn, config):
        if not db.alert_already_sent(conn, sku, store_id, kind, within_hours=cooldown):
            pending.append((kind, sku, store_id, detail))

    if not pending:
        return 0

    body = "\n".join(detail for _, _, _, detail in pending)
    print("\nAlertes:")
    print(body)

    subject = f"Stock RONA: {len(pending)} changement(s)"
    if any(kind == "restock" for kind, *_ in pending):
        subject = "Stock RONA: retour en stock"

    if not deliver(rules, subject, body):
        return 0

    for kind, sku, store_id, detail in pending:
        db.log_alert(conn, sku, store_id, kind, detail)
    return len(pending)
