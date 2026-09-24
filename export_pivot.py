#!/usr/bin/env python3
"""Genere un classeur Excel 'Suivi de stock RONA pivot (date heure).xlsx'
a partir des donnees actuelles de stock.db: un tableau croise (pivot) de
la somme des differences de quantite par modele de boite et par magasin,
plus le detail des calculs et toutes les donnees brutes.

Appele automatiquement a chaque mise a jour d'inventaire (voir
/api/readings dans dashboard.py). Peut aussi etre execute directement:

    python export_pivot.py

Le fichier est cree dans le sous-dossier exports_excel/ du projet, avec
un nom horodate, ex: "Suivi de stock RONA pivot (2026-08-18 14h32).xlsx".
"""
import os
import re
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.formatting.rule import CellIsRule

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXPORT_DIR = os.path.join(BASE_DIR, "exports_excel")

FONT_NAME = "Arial"
HEADER_FILL = PatternFill("solid", fgColor="1F5D63")
HEADER_FONT = Font(name=FONT_NAME, bold=True, color="FFFFFF")
TITLE_FONT = Font(name=FONT_NAME, bold=True, size=14)
SUBTITLE_FONT = Font(name=FONT_NAME, italic=True, size=10, color="666666")
BOLD = Font(name=FONT_NAME, bold=True)
NORMAL = Font(name=FONT_NAME)
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
TOTAL_FILL = PatternFill("solid", fgColor="EDEDED")


def product_model(product):
    """N de modele du produit (ex: 'C1104LHU'). Utilise la valeur stockee en
    base si presente, sinon la deduit de l'URL rona.ca. Duplique volontairement
    de dashboard.py pour eviter un import circulaire.
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
    return product.get("sku") or "?"


def _safe_filename(name):
    return re.sub(r'[\\/:*?"<>|]', "-", name)


def generate(conn=None, stamp=None, out_dir=None):
    """Construit et sauvegarde le classeur pivot. Retourne le chemin du
    fichier cree. `conn` est reutilise s'il est fourni (ne le ferme pas);
    sinon une connexion est ouverte et refermee ici.
    """
    own_conn = conn is None
    if own_conn:
        conn = db.connect()

    try:
        products = db.list_products(conn)
        stores = db.list_stores(conn)
        raw_readings = conn.execute(
            "SELECT taken_at, sku, store_id, quantity, price, status, ok, note"
            " FROM readings ORDER BY taken_at, id"
        ).fetchall()

        product_by_key = {db.product_key(p): p for p in products}
        store_by_key = {db.store_key(s): s for s in stores}
        model_by_key = {k: product_model(p) for k, p in product_by_key.items()}

        rows = []
        for r in raw_readings:
            pkey, skey = r["sku"], r["store_id"]
            product = product_by_key.get(pkey)
            store = store_by_key.get(skey)
            if not product or not store:
                continue
            dt = db.parse_iso(r["taken_at"])
            if dt is None:
                continue
            rows.append({
                "dt": dt.astimezone(db.QUEBEC_TZ).replace(tzinfo=None),
                "store": store["name"],
                "sku": product["sku"] or pkey,
                "model": model_by_key[pkey],
                "name": product["name"] or product["url"] or pkey,
                "qty": r["quantity"],
                "price": r["price"],
                "status": r["status"],
                "ok": bool(r["ok"]) and r["quantity"] is not None,
                "note": r["note"],
            })
        rows.sort(key=lambda x: (x["dt"], x["store"], x["model"]))

        store_order = [db.store_key(s) for s in stores]
        model_order = []
        for p in products:
            k = db.product_key(p)
            if model_by_key[k] not in model_order:
                model_order.append(model_by_key[k])

        wb = Workbook()

        # -------------------------------------------------------------
        # Feuille "Donnees" -- table brute complete (audit).
        # -------------------------------------------------------------
        ws_data = wb.active
        ws_data.title = "Donnees"
        data_headers = [
            "Date/Heure", "Magasin", "N d'article", "N de modele", "Produit",
            "Quantite", "Prix", "Statut", "Quantite disponible", "Note",
        ]
        for c, h in enumerate(data_headers, start=1):
            cell = ws_data.cell(row=1, column=c, value=h)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = BORDER

        for i, r in enumerate(rows, start=2):
            ws_data.cell(row=i, column=1, value=r["dt"]).number_format = "yyyy-mm-dd hh:mm"
            ws_data.cell(row=i, column=2, value=r["store"])
            ws_data.cell(row=i, column=3, value=r["sku"])
            ws_data.cell(row=i, column=4, value=r["model"])
            ws_data.cell(row=i, column=5, value=r["name"])
            ws_data.cell(row=i, column=6, value=r["qty"])
            ws_data.cell(row=i, column=7, value=r["price"])
            ws_data.cell(row=i, column=8, value=r["status"])
            ws_data.cell(row=i, column=9, value="Oui" if r["ok"] else "Non")
            ws_data.cell(row=i, column=10, value=r["note"])
            for c in range(1, 11):
                ws_data.cell(row=i, column=c).font = NORMAL

        last_data_row = max(len(rows) + 1, 2)
        widths = [17, 30, 12, 13, 46, 10, 10, 12, 12, 60]
        for c, w in enumerate(widths, start=1):
            ws_data.column_dimensions[get_column_letter(c)].width = w
        ws_data.freeze_panes = "A2"

        if rows:
            tab = Table(displayName="Donnees", ref=f"A1:J{last_data_row}")
            tab.tableStyleInfo = TableStyleInfo(
                name="TableStyleMedium2", showRowStripes=True, showColumnStripes=False
            )
            ws_data.add_table(tab)

        DATA_RANGE = {
            "date": f"Donnees!$A$2:$A${last_data_row}",
            "store": f"Donnees!$B$2:$B${last_data_row}",
            "model": f"Donnees!$D$2:$D${last_data_row}",
            "qty": f"Donnees!$F$2:$F${last_data_row}",
            "ok": f"Donnees!$I$2:$I${last_data_row}",
        }

        # -------------------------------------------------------------
        # Feuille "Calculs" -- 1er/dernier releve reussi et difference,
        # par combinaison magasin x modele.
        # -------------------------------------------------------------
        ws_calc = wb.create_sheet("Calculs")
        calc_headers = [
            "Magasin", "N de modele", "1er releve (date)", "Qte au 1er releve",
            "Dernier releve (date)", "Qte au dernier releve", "Difference (somme des variations)",
        ]
        for c, h in enumerate(calc_headers, start=1):
            cell = ws_calc.cell(row=1, column=c, value=h)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = BORDER

        combos = [(store_by_key[s]["name"], m) for s in store_order for m in model_order]
        r0 = 2
        for i, (store, model) in enumerate(combos):
            r = r0 + i
            ws_calc.cell(row=r, column=1, value=store).font = NORMAL
            ws_calc.cell(row=r, column=2, value=model).font = NORMAL

            first_date = (
                f'=IFERROR(_xlfn.MINIFS({DATA_RANGE["date"]},{DATA_RANGE["store"]},$A{r},'
                f'{DATA_RANGE["model"]},$B{r},{DATA_RANGE["ok"]},"Oui"),"")'
            )
            last_date = (
                f'=IFERROR(_xlfn.MAXIFS({DATA_RANGE["date"]},{DATA_RANGE["store"]},$A{r},'
                f'{DATA_RANGE["model"]},$B{r},{DATA_RANGE["ok"]},"Oui"),"")'
            )
            ws_calc.cell(row=r, column=3, value=first_date).number_format = "yyyy-mm-dd hh:mm"
            ws_calc.cell(row=r, column=5, value=last_date).number_format = "yyyy-mm-dd hh:mm"

            first_qty = (
                f'=IFERROR(AVERAGEIFS({DATA_RANGE["qty"]},{DATA_RANGE["store"]},$A{r},'
                f'{DATA_RANGE["model"]},$B{r},{DATA_RANGE["date"]},$C{r},{DATA_RANGE["ok"]},"Oui"),"")'
            )
            last_qty = (
                f'=IFERROR(AVERAGEIFS({DATA_RANGE["qty"]},{DATA_RANGE["store"]},$A{r},'
                f'{DATA_RANGE["model"]},$B{r},{DATA_RANGE["date"]},$E{r},{DATA_RANGE["ok"]},"Oui"),"")'
            )
            ws_calc.cell(row=r, column=4, value=first_qty)
            ws_calc.cell(row=r, column=6, value=last_qty)

            diff = f'=IF(OR($C{r}="",$E{r}=""),"",$F{r}-$D{r})'
            diff_cell = ws_calc.cell(row=r, column=7, value=diff)
            diff_cell.font = BOLD

            for c in range(1, 8):
                ws_calc.cell(row=r, column=c).border = BORDER

        last_calc_row = r0 + len(combos) - 1 if combos else 2
        for c, w in enumerate([30, 13, 17, 15, 17, 17, 26], start=1):
            ws_calc.column_dimensions[get_column_letter(c)].width = w
        ws_calc.freeze_panes = "A2"

        CALC_RANGE = {
            "store": f"Calculs!$A$2:$A${last_calc_row}",
            "model": f"Calculs!$B$2:$B${last_calc_row}",
            "diff": f"Calculs!$G$2:$G${last_calc_row}",
        }

        # -------------------------------------------------------------
        # Feuille "Pivot" -- tableau croise: somme des differences de
        # quantite, magasins en lignes, modeles de boite en colonnes.
        # -------------------------------------------------------------
        ws = wb.create_sheet("Pivot", 0)
        ws.sheet_view.showGridLines = False

        ws["A1"] = "Suivi de stock RONA - Somme des differences par modele et par magasin"
        ws["A1"].font = TITLE_FONT
        ws.merge_cells("A1:F1")
        ws["A2"] = (
            "Difference = quantite au dernier releve reussi moins quantite au 1er releve reussi, "
            "par magasin. Genere automatiquement a chaque mise a jour d'inventaire."
        )
        ws["A2"].font = SUBTITLE_FONT
        ws.merge_cells("A2:F2")

        header_row = 4
        ws.cell(row=header_row, column=1, value="Magasin").font = HEADER_FONT
        ws.cell(row=header_row, column=1).fill = HEADER_FILL
        ws.cell(row=header_row, column=1).border = BORDER

        for j, model in enumerate(model_order):
            col = 2 + j
            cell = ws.cell(row=header_row, column=col, value=model)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = Alignment(horizontal="center")
            cell.border = BORDER

        total_col = 2 + len(model_order)
        cell = ws.cell(row=header_row, column=total_col, value="Total (tous modeles)")
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
        cell.border = BORDER

        store_names = [store_by_key[s]["name"] for s in store_order]
        first_body_row = header_row + 1
        for i, store in enumerate(store_names):
            r = first_body_row + i
            ws.cell(row=r, column=1, value=store).font = BOLD
            ws.cell(row=r, column=1).border = BORDER
            for j, model in enumerate(model_order):
                col = 2 + j
                col_letter = get_column_letter(col)
                formula = (
                    f'=IFERROR(SUMIFS({CALC_RANGE["diff"]},{CALC_RANGE["store"]},$A{r},'
                    f'{CALC_RANGE["model"]},{col_letter}${header_row}),"")'
                )
                c = ws.cell(row=r, column=col, value=formula)
                c.number_format = "+#,##0;-#,##0;0"
                c.alignment = Alignment(horizontal="center")
                c.border = BORDER
            total_formula = f"=SUM({get_column_letter(2)}{r}:{get_column_letter(total_col - 1)}{r})"
            tc = ws.cell(row=r, column=total_col, value=total_formula)
            tc.font = BOLD
            tc.number_format = "+#,##0;-#,##0;0"
            tc.alignment = Alignment(horizontal="center")
            tc.border = BORDER
            tc.fill = TOTAL_FILL

        last_body_row = first_body_row + len(store_names) - 1 if store_names else header_row
        total_row = last_body_row + 1
        ws.cell(row=total_row, column=1, value="Total (tous magasins)").font = BOLD
        ws.cell(row=total_row, column=1).fill = TOTAL_FILL
        ws.cell(row=total_row, column=1).border = BORDER
        for j in range(len(model_order) + 1):
            col = 2 + j
            col_letter = get_column_letter(col)
            formula = f"=SUM({col_letter}{first_body_row}:{col_letter}{last_body_row})"
            c = ws.cell(row=total_row, column=col, value=formula)
            c.font = BOLD
            c.number_format = "+#,##0;-#,##0;0"
            c.alignment = Alignment(horizontal="center")
            c.fill = TOTAL_FILL
            c.border = BORDER

        green_font = Font(name=FONT_NAME, color="1E7D32", bold=True)
        red_font = Font(name=FONT_NAME, color="A3390E", bold=True)
        black_font = Font(name=FONT_NAME, color="16211F")
        if store_names:
            value_range = f"B{first_body_row}:{get_column_letter(total_col)}{total_row}"
            ws.conditional_formatting.add(
                value_range, CellIsRule(operator="greaterThan", formula=["0"], font=green_font)
            )
            ws.conditional_formatting.add(
                value_range, CellIsRule(operator="lessThan", formula=["0"], font=red_font)
            )
            ws.conditional_formatting.add(
                value_range, CellIsRule(operator="equal", formula=["0"], font=black_font)
            )

        for c, w in enumerate([30, 12, 12, 12, 20], start=1):
            ws.column_dimensions[get_column_letter(c)].width = w

        note_row = total_row + 2
        ws.cell(
            row=note_row, column=1,
            value="Note: une baisse peut aussi venir d'un ajustement d'inventaire cote magasin, pas seulement d'une vente.",
        )
        ws.cell(row=note_row, column=1).font = SUBTITLE_FONT
        ws.merge_cells(start_row=note_row, start_column=1, end_row=note_row, end_column=total_col)

        for sheet in (ws, ws_calc, ws_data):
            sheet.page_setup.orientation = "landscape"
            sheet.page_setup.fitToWidth = 1
            sheet.page_setup.fitToHeight = 0
            sheet.sheet_properties.pageSetUpPr.fitToPage = True

        os.makedirs(out_dir or EXPORT_DIR, exist_ok=True)
        stamp = stamp or datetime.now(db.QUEBEC_TZ).strftime("%Y-%m-%d %Hh%M")
        filename = _safe_filename(f"Suivi de stock RONA pivot ({stamp}).xlsx")
        out_path = os.path.join(out_dir or EXPORT_DIR, filename)
        wb.save(out_path)
        return out_path
    finally:
        if own_conn:
            conn.close()


if __name__ == "__main__":
    path = generate()
    print(f"Fichier cree: {path}")
