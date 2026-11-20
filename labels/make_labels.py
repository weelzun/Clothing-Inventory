"""
Barcode label generator for the clothing_inventory database.

Reads variants from MySQL and writes a printable PDF of Code 128 barcode labels.

Examples
--------
  python make_labels.py --style CT-001 --copies 2          # every variant of a style
  python make_labels.py --sku CT-001-BLK-M --copies 5      # one specific item
  python make_labels.py --style CT-001 --mode roll         # one label per page (thermal printer)
  python make_labels.py --demo                             # test the PDF layout, no database needed

Setup
-----
  pip install reportlab mysql-connector-python
  Set your MySQL password in an environment variable (recommended):
    Windows (PowerShell):  $env:MYSQL_PASSWORD = "your_password"
    Mac/Linux:             export MYSQL_PASSWORD="your_password"
  If it isn't set, the script will ask for it when it runs.
"""

import argparse
import getpass
import os
import sys

from reportlab.graphics.barcode import code128
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

# ----------------------------------------------------------------------
# CONFIG: change these to match your setup
# ----------------------------------------------------------------------
DB_CONFIG = {
    "host": os.environ.get("MYSQL_HOST", "localhost"),
    "port": int(os.environ.get("MYSQL_PORT", "3306")),
    "user": os.environ.get("MYSQL_USER", "root"),
    "database": "clothing_inventory",
}

# Label layouts. Sizes are in inches.
#   sheet = Avery 5160-style sheet labels (30 per US Letter page, 3 x 10)
#   roll  = one label per page, for thermal label printers (Dymo, Brother, Zebra)
LAYOUTS = {
    "sheet": {
        "label_w": 2.625, "label_h": 1.0,
        "cols": 3, "rows": 10,
        "left_margin": 0.1875, "top_margin": 0.5,
        "col_gap": 0.125, "row_gap": 0.0,
    },
    "roll": {
        "label_w": 2.25, "label_h": 1.25,
    },
}

PADDING = 0.08 * inch          # inner padding inside each label
MIN_BAR_WIDTH = 0.7            # points; below this, barcodes may scan poorly
DEMO_ROWS = [
    {"variant_id": 0, "name": "Cuello Redondo de piedra", "size": "M",
     "sku": "CT-001-BLK-M", "barcode": "CT-001-BLK-M", "price": 60000},
    {"variant_id": 0, "name": "Chompa", "size": "L",
     "sku": "CT-001-WHT-L", "barcode": "CT-001-WHT-L", "price": 80000},
    {"variant_id": 0, "name": "Buzo", "size": "XL",
     "sku": "CT-001-NVY-XL", "barcode": "CT-001-NVY-XL", "price": 30000},
]


# ----------------------------------------------------------------------
# DATABASE
# ----------------------------------------------------------------------
QUERY = """
    SELECT v.variant_id,
           p.name,
           c.name  AS color,
           s.label AS size,
           v.sku,
           v.barcode,
           COALESCE(v.retail_price, p.base_price) AS price
    FROM product_variants v
    JOIN products p ON p.product_id = v.product_id
    JOIN colors   c ON c.color_id   = v.color_id
    JOIN sizes    s ON s.size_id    = v.size_id
    WHERE v.is_active = 1 AND {where}
    ORDER BY p.style_code, c.name, s.sort_order
"""


def get_connection():
    import mysql.connector

    password = os.environ.get("MYSQL_PASSWORD") or getpass.getpass("MySQL password: ")
    return mysql.connector.connect(password=password, **DB_CONFIG)


def fetch_variants(conn, style=None, sku=None):
    if sku:
        sql, param = QUERY.format(where="v.sku = %s"), sku
    else:
        sql, param = QUERY.format(where="p.style_code = %s"), style
    cur = conn.cursor(dictionary=True)
    cur.execute(sql, (param,))
    rows = cur.fetchall()
    cur.close()
    return rows


def log_print_job(conn, rows, copies):
    """Record what was printed in label_print_jobs."""
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO label_print_jobs (variant_id, copies) VALUES (%s, %s)",
        [(r["variant_id"], copies) for r in rows],
    )
    conn.commit()
    cur.close()


# ----------------------------------------------------------------------
# DRAWING
# ----------------------------------------------------------------------
def format_cop(amount):
    """Format a price as Colombian pesos: 100000 -> $100.000 (no decimals)."""
    whole = round(float(amount))
    return "$" + f"{whole:,}".replace(",", ".")

def fit_text(c, text, font, size, max_width):
    """Shrink font size until the text fits max_width."""
    while size > 5 and c.stringWidth(text, font, size) > max_width:
        size -= 0.5
    return size


def draw_label(c, row, x, y, w, h):
    """Draw one label with its lower-left corner at (x, y)."""
    inner_w = w - 2 * PADDING
    cx = x + w / 2

    # Product name (top)
    name = row["name"]
    size = fit_text(c, name, "Helvetica-Bold", 9, inner_w)
    c.setFont("Helvetica-Bold", size)
    c.drawCentredString(cx, y + h - PADDING - size, name)

    # Color / size and price on one line
    detail = f'{row["size"]}'
    price = format_cop(row["price"])
    c.setFont("Helvetica", 8)
    detail_y = y + h - PADDING - size - 10
    c.drawString(x + PADDING, detail_y, detail)
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(x + w - PADDING, detail_y - 0.5, price)

    # SKU text (bottom)
    c.setFont("Helvetica", 8)
    sku_y = y + PADDING
    c.drawCentredString(cx, sku_y, row["sku"])

    # Barcode (fills the middle). Shrink bar width until it fits the label.
    bar_width = 1.0
    bc = code128.Code128(row["barcode"], barWidth=bar_width, barHeight=0.4 * inch,
                         humanReadable=False, quiet=False)
    while bc.width > inner_w and bar_width > 0.3:
        bar_width -= 0.02
        bc = code128.Code128(row["barcode"], barWidth=bar_width, barHeight=0.4 * inch,
                             humanReadable=False, quiet=False)
    if bar_width < MIN_BAR_WIDTH:
        print(f"Warning: barcode {row['barcode']} is tight on this label and may "
              f"scan poorly. Use shorter SKUs or a wider label.")

    bc_x = x + (w - bc.width) / 2
    bc_y = sku_y + 9
    bc.drawOn(c, bc_x, bc_y)


def build_pdf(rows, copies, mode, out_path):
    layout = LAYOUTS[mode]
    label_w = layout["label_w"] * inch
    label_h = layout["label_h"] * inch

    # Expand rows by number of copies
    labels = [r for r in rows for _ in range(copies)]

    if mode == "roll":
        c = canvas.Canvas(out_path, pagesize=(label_w, label_h))
        for row in labels:
            draw_label(c, row, 0, 0, label_w, label_h)
            c.showPage()
    else:
        page_w, page_h = letter
        c = canvas.Canvas(out_path, pagesize=letter)
        per_page = layout["cols"] * layout["rows"]
        for i, row in enumerate(labels):
            pos = i % per_page
            if i > 0 and pos == 0:
                c.showPage()
            col = pos % layout["cols"]
            r = pos // layout["cols"]
            x = layout["left_margin"] * inch + col * (label_w + layout["col_gap"] * inch)
            y = (page_h - layout["top_margin"] * inch - label_h
                 - r * (label_h + layout["row_gap"] * inch))
            draw_label(c, row, x, y, label_w, label_h)
    c.save()
    return len(labels)


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Generate printable barcode labels.")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--style", help="style code, e.g. CT-001 (all variants)")
    group.add_argument("--sku", help="a single SKU, e.g. CT-001-BLK-M")
    group.add_argument("--demo", action="store_true", help="use sample data, no database")
    ap.add_argument("--copies", type=int, default=1, help="labels per variant (default 1)")
    ap.add_argument("--mode", choices=LAYOUTS.keys(), default="sheet",
                    help="sheet = Avery 5160 on Letter; roll = one label per page")
    ap.add_argument("--out", default="labels.pdf", help="output PDF filename")
    ap.add_argument("--no-log", action="store_true", help="don't record in label_print_jobs")
    args = ap.parse_args()

    if not (args.style or args.sku or args.demo):
        ap.error("choose one of --style, --sku or --demo")
    if args.copies < 1:
        ap.error("--copies must be at least 1")

    conn = None
    if args.demo:
        rows = DEMO_ROWS
    else:
        try:
            conn = get_connection()
            rows = fetch_variants(conn, style=args.style, sku=args.sku)
        except Exception as e:
            sys.exit(f"Database error: {e}")
        if not rows:
            sys.exit("No matching active variants found. Check the style code / SKU.")

    count = build_pdf(rows, args.copies, args.mode, args.out)
    print(f"Wrote {count} labels to {args.out}")

    if conn:
        if not args.no_log:
            log_print_job(conn, rows, args.copies)
        conn.close()


if __name__ == "__main__":
    main()
