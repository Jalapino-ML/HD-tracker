#!/usr/bin/env python3
"""Exporte l'historique et le journal des changements en CSV, et en Excel si openpyxl est la.

    python export.py            # stock-rona.csv + changements-rona.csv
    python export.py --xlsx     # ajoute stock-rona.xlsx (deux feuilles)
"""

import argparse
import csv
from pathlib import Path

import db

READING_HEADERS = ["horodatage", "produit", "magasin", "quantite", "statut", "ok", "date_source",
                   "age_cache", "max_age_cache", "http", "latence_ms", "note"]
CHANGE_HEADERS = ["detecte_le", "produit", "magasin", "type", "avant", "apres", "delta",
                  "date_source", "ecart_secondes"]


def gather():
    conn = db.connect()
    names = {db.product_key(p): (p["name"] or p["url"]) for p in db.list_products(conn)}
    store_names = {db.store_key(s): s["name"] for s in db.list_stores(conn)}
    readings = [
        [r["taken_at"], names.get(r["sku"], r["sku"]), store_names.get(r["store_id"], r["store_id"]),
         r["quantity"], r["status"], r["ok"],
         r["source_updated_at"], r["cache_age"], r["cache_max_age"], r["http_status"],
         r["latency_ms"], r["note"]]
        for r in db.all_readings(conn)
    ]
    changes = [
        [c["detected_at"], names.get(c["sku"], c["sku"]), store_names.get(c["store_id"], c["store_id"]),
         c["kind"], c["from_quantity"],
         c["to_quantity"], c["delta"], c["source_updated_at"], c["gap_seconds"]]
        for c in db.all_changes(conn)
    ]
    conn.close()
    return readings, changes


def write_csv(headers, rows, path):
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)
    return path


def write_xlsx(readings, changes, path):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    book = Workbook()
    sheets = [("Releves", READING_HEADERS, readings, [22, 12, 14, 11, 16, 6, 22, 11, 14, 7, 12, 34]),
              ("Changements", CHANGE_HEADERS, changes, [22, 12, 14, 12, 9, 9, 8, 22, 16])]

    for index, (title, headers, rows, widths) in enumerate(sheets):
        sheet = book.active if index == 0 else book.create_sheet()
        sheet.title = title
        sheet.append(headers)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for row in rows:
            sheet.append(row)
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(sheet.max_row, 1)}"
        for position, width in enumerate(widths, start=1):
            sheet.column_dimensions[get_column_letter(position)].width = width

    book.save(path)
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--xlsx", action="store_true", help="produit aussi un fichier Excel")
    parser.add_argument("--out", type=Path, default=Path("stock-rona"))
    args = parser.parse_args()

    readings, changes = gather()
    if not readings:
        print("Aucun releve a exporter.")
        return

    print(f"{write_csv(READING_HEADERS, readings, args.out.with_suffix('.csv'))} ({len(readings)} lignes)")
    print(f"{write_csv(CHANGE_HEADERS, changes, Path('changements-rona.csv'))} ({len(changes)} lignes)")
    if args.xlsx:
        try:
            print(write_xlsx(readings, changes, args.out.with_suffix(".xlsx")))
        except ImportError:
            print("Excel ignore: pip install openpyxl")


if __name__ == "__main__":
    main()
