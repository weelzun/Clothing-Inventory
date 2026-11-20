"""
Database layer for the desktop app.

Every stock change goes through the stored procedures in database/schema.sql
(sp_adjust_stock and sp_set_stock_count), so the stock balance and the audit
log (stock_movements) never disagree. Never write to stock_levels directly.
"""

import re

import mysql.connector

DB_NAME = "clothing_inventory"
DEFAULT_USER_ID = 1  # the seeded admin user; replace when real logins exist
SOURCE = "desktop"

# Direction of each movement type. Quantities are entered as positive numbers.
SIGN = {"RECEIVE": 1, "RETURN": 1, "SALE": -1, "DAMAGED": -1, "LOST": -1}

MOVEMENT_TYPES = [
    "RECEIVE", "SALE", "RETURN", "ADJUSTMENT", "STOCKTAKE",
    "TRANSFER_IN", "TRANSFER_OUT", "DAMAGED", "LOST",
]

STYLE_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]*$")
COLOR_CODE_RE = re.compile(r"^[A-Z0-9]{1,5}$")


class ValidationError(Exception):
    """A problem with what the user typed. The message is shown to them as-is."""


def size_token(label):
    """'One Size' -> 'ONESIZE' so SKUs never contain spaces."""
    return re.sub(r"[^A-Za-z0-9]+", "", label).upper()


def make_sku(style, color_code, size_label):
    return f"{style}-{color_code}-{size_token(size_label)}"


def _money(value):
    return float(value) if value is not None else 0.0


def _like(term):
    """Escape LIKE wildcards in user input and wrap for a contains-search."""
    esc = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


class Database:
    def __init__(self, host, port, user, password):
        # autocommit=True so reads always see fresh data (the phone app and the
        # desktop app share one database). Multi-step writes use explicit transactions.
        self.conn = mysql.connector.connect(
            host=host, port=port, user=user, password=password,
            database=DB_NAME, autocommit=True, connection_timeout=10,
        )
        rows = self.query(
            "SELECT location_id, name FROM locations WHERE is_active = 1 "
            "ORDER BY location_id LIMIT 1"
        )
        if not rows:
            raise RuntimeError("No active location found in the database.")
        self.location_id = rows[0]["location_id"]
        self.location_name = rows[0]["name"]
        self.who = f"{user}@{host}"

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def close(self):
        try:
            self.conn.close()
        except mysql.connector.Error:
            pass

    def _ping(self):
        self.conn.ping(reconnect=True, attempts=3, delay=1)

    def query(self, sql, params=()):
        self._ping()
        cur = self.conn.cursor(dictionary=True)
        try:
            cur.execute(sql, params)
            return cur.fetchall()
        finally:
            cur.close()

    def _execute(self, sql, params=()):
        self._ping()
        cur = self.conn.cursor()
        try:
            cur.execute(sql, params)
            return cur.lastrowid
        finally:
            cur.close()

    # ------------------------------------------------------------------
    # lookups
    # ------------------------------------------------------------------
    def brands(self):
        return [r["name"] for r in self.query("SELECT name FROM brands ORDER BY name")]

    def categories(self):
        """[(category_id, 'Parent > Child'), ...]"""
        rows = self.query("SELECT category_id, parent_id, name FROM categories")
        by_id = {r["category_id"]: r for r in rows}

        def path(r):
            parent = by_id.get(r["parent_id"])
            return f"{path(parent)} > {r['name']}" if parent else r["name"]

        return sorted(((r["category_id"], path(r)) for r in rows), key=lambda x: x[1].lower())

    def colors(self):
        return self.query("SELECT color_id, name, code FROM colors ORDER BY name")

    def sizes(self):
        return self.query("SELECT size_id, label, sort_order FROM sizes ORDER BY sort_order, label")

    def add_color(self, name, code):
        name, code = name.strip(), code.strip().upper()
        if not name:
            raise ValidationError("Enter a color name.")
        if not COLOR_CODE_RE.match(code):
            raise ValidationError("The color code must be 1-5 letters or digits (for example BLK).")
        if self.query("SELECT 1 FROM colors WHERE name = %s OR code = %s", (name, code)):
            raise ValidationError("A color with that name or code already exists.")
        return self._execute("INSERT INTO colors (name, code) VALUES (%s, %s)", (name, code))

    def add_size(self, label, sort_order):
        label = label.strip()
        if not label:
            raise ValidationError("Enter a size label.")
        if not size_token(label):
            raise ValidationError("The size label needs at least one letter or digit.")
        if self.query("SELECT 1 FROM sizes WHERE label = %s", (label,)):
            raise ValidationError("That size already exists.")
        return self._execute(
            "INSERT INTO sizes (label, sort_order) VALUES (%s, %s)", (label, sort_order)
        )

    def next_size_order(self):
        rows = self.query("SELECT COALESCE(MAX(sort_order), 0) + 10 AS n FROM sizes")
        return int(rows[0]["n"])

    # ------------------------------------------------------------------
    # inventory
    # ------------------------------------------------------------------
    def inventory(self, search="", low_only=False, show_inactive=False, product_id=None):
        sql = """
            SELECT v.variant_id, v.product_id, v.style_code, v.product_name, v.brand,
                   v.category, v.color, v.size, v.sku, v.barcode, v.quantity,
                   v.retail_price, v.cost_price, v.reorder_level, v.is_active
            FROM v_inventory v
            JOIN sizes s ON s.label = v.size
            WHERE v.location_id = %s
        """
        params = [self.location_id]
        if product_id is not None:
            sql += " AND v.product_id = %s"
            params.append(product_id)
        if not show_inactive:
            sql += " AND v.is_active = 1"
        if low_only:
            sql += " AND v.quantity <= v.reorder_level"
        for word in search.split():
            sql += (
                " AND CONCAT_WS(' ', v.style_code, v.product_name, v.brand, v.category,"
                " v.color, v.size, v.sku, v.barcode) LIKE %s"
            )
            params.append(_like(word))
        sql += " ORDER BY v.style_code, v.color, s.sort_order, v.size"

        rows = self.query(sql, params)
        for r in rows:
            r["retail_price"] = _money(r["retail_price"])
            r["cost_price"] = _money(r["cost_price"])
            r["quantity"] = int(r["quantity"])
        return rows

    def stats(self):
        r = self.query(
            """
            SELECT COUNT(*) AS variants,
                   COALESCE(SUM(quantity), 0) AS units,
                   COALESCE(SUM(stock_value_cost), 0) AS value,
                   COALESCE(SUM(quantity <= reorder_level), 0) AS low
            FROM v_inventory WHERE location_id = %s AND is_active = 1
            """,
            (self.location_id,),
        )[0]
        return {
            "variants": int(r["variants"]), "units": int(r["units"]),
            "value": _money(r["value"]), "low": int(r["low"]),
        }

    def product_by_style(self, style):
        rows = self.query(
            """
            SELECT p.product_id, p.style_code, p.name, p.gender, p.base_cost, p.base_price,
                   p.is_active, p.category_id, b.name AS brand
            FROM products p LEFT JOIN brands b ON b.brand_id = p.brand_id
            WHERE p.style_code = %s
            """,
            (style,),
        )
        if not rows:
            return None
        r = rows[0]
        r["base_cost"] = _money(r["base_cost"])
        r["base_price"] = _money(r["base_price"])
        return r

    # ------------------------------------------------------------------
    # products
    # ------------------------------------------------------------------
    def add_product(self, style, name, brand, category, gender, cost, price,
                    color_ids, size_ids):
        """
        Create a product (if the style code is new) and any missing color x size
        variants. SKU and barcode are STYLE-COLORCODE-SIZE.
        `category` may be an id, a new top-level category name, or None.
        Returns (product_was_created, [new_variant_ids]).
        """
        if not STYLE_RE.match(style):
            raise ValidationError("The style code may only contain letters, digits and hyphens.")
        if not color_ids or not size_ids:
            raise ValidationError("Pick at least one color and one size.")

        self._ping()
        conn = self.conn
        cur = conn.cursor(dictionary=True)
        try:
            conn.start_transaction()
            cur.execute("SELECT product_id FROM products WHERE style_code = %s FOR UPDATE", (style,))
            row = cur.fetchone()
            created = row is None
            if created:
                if not name.strip():
                    raise ValidationError("Enter a product name.")
                brand_id = self._ensure_brand(cur, brand.strip()) if brand and brand.strip() else None
                if isinstance(category, str):
                    category_id = self._ensure_category(cur, category.strip())
                else:
                    category_id = category
                cur.execute(
                    "INSERT INTO products (style_code, name, brand_id, category_id, gender,"
                    " base_cost, base_price) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (style, name.strip(), brand_id, category_id, gender, cost, price),
                )
                product_id = cur.lastrowid
            else:
                product_id = row["product_id"]

            cur.execute("SELECT color_id, size_id FROM product_variants WHERE product_id = %s", (product_id,))
            existing = {(r["color_id"], r["size_id"]) for r in cur.fetchall()}
            cur.execute("SELECT color_id, code FROM colors")
            codes = {r["color_id"]: r["code"] for r in cur.fetchall()}
            cur.execute("SELECT size_id, label FROM sizes")
            labels = {r["size_id"]: r["label"] for r in cur.fetchall()}

            new_ids = []
            for color_id in color_ids:
                for size_id in size_ids:
                    if (color_id, size_id) in existing:
                        continue
                    sku = make_sku(style, codes[color_id], labels[size_id])
                    cur.execute(
                        "INSERT INTO product_variants (product_id, color_id, size_id, sku, barcode)"
                        " VALUES (%s, %s, %s, %s, %s)",
                        (product_id, color_id, size_id, sku, sku),
                    )
                    new_ids.append(cur.lastrowid)
            conn.commit()
            return created, new_ids
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    @staticmethod
    def _ensure_brand(cur, name):
        cur.execute("SELECT brand_id FROM brands WHERE name = %s", (name,))
        row = cur.fetchone()
        if row:
            return row["brand_id"]
        cur.execute("INSERT INTO brands (name) VALUES (%s)", (name,))
        return cur.lastrowid

    @staticmethod
    def _ensure_category(cur, name):
        cur.execute("SELECT category_id FROM categories WHERE name = %s AND parent_id IS NULL", (name,))
        row = cur.fetchone()
        if row:
            return row["category_id"]
        cur.execute("INSERT INTO categories (parent_id, name) VALUES (NULL, %s)", (name,))
        return cur.lastrowid

    def edit_product(self, product_id, name, cost, price, active):
        if not name.strip():
            raise ValidationError("The product name cannot be empty.")
        self._ping()
        conn = self.conn
        cur = conn.cursor()
        try:
            conn.start_transaction()
            cur.execute(
                "UPDATE products SET name = %s, base_cost = %s, base_price = %s, is_active = %s"
                " WHERE product_id = %s",
                (name.strip(), cost, price, 1 if active else 0, product_id),
            )
            cur.execute(
                "UPDATE product_variants SET is_active = %s WHERE product_id = %s",
                (1 if active else 0, product_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    # ------------------------------------------------------------------
    # stock (always via stored procedures)
    # ------------------------------------------------------------------
    def adjust(self, variant_id, movement_type, quantity, reference=None, notes=None):
        if movement_type not in SIGN:
            raise ValidationError(f"Unknown movement type: {movement_type}")
        if quantity <= 0:
            raise ValidationError("The quantity must be at least 1.")
        self._call("sp_adjust_stock", (
            variant_id, self.location_id, SIGN[movement_type] * quantity, movement_type,
            DEFAULT_USER_ID, SOURCE, reference or None, notes or None,
        ))

    def set_count(self, variant_id, counted_qty, notes=None):
        if counted_qty < 0:
            raise ValidationError("The count cannot be negative.")
        self._call("sp_set_stock_count", (
            variant_id, self.location_id, counted_qty, DEFAULT_USER_ID, SOURCE, notes or None,
        ))

    def _call(self, proc, args):
        self._ping()
        cur = self.conn.cursor()
        try:
            cur.callproc(proc, args)
            for _ in cur.stored_results():  # drain any result sets
                pass
        finally:
            cur.close()

    # ------------------------------------------------------------------
    # history and label log
    # ------------------------------------------------------------------
    def movements(self, search="", movement_type=None, limit=500):
        sql = """
            SELECT m.created_at, v.sku, p.name AS product_name, m.movement_type,
                   m.quantity_change, m.quantity_after, COALESCE(u.username, '') AS username,
                   m.source, m.reference, m.notes
            FROM stock_movements m
            JOIN product_variants v ON v.variant_id = m.variant_id
            JOIN products p ON p.product_id = v.product_id
            LEFT JOIN users u ON u.user_id = m.user_id
            WHERE m.location_id = %s
        """
        params = [self.location_id]
        if movement_type:
            sql += " AND m.movement_type = %s"
            params.append(movement_type)
        for word in search.split():
            sql += " AND CONCAT_WS(' ', v.sku, p.name, m.reference, m.notes) LIKE %s"
            params.append(_like(word))
        sql += " ORDER BY m.movement_id DESC LIMIT %s"
        params.append(int(limit))
        return self.query(sql, params)

    def log_label_print(self, variant_id, copies):
        self._execute(
            "INSERT INTO label_print_jobs (variant_id, copies, printed_by) VALUES (%s, %s, %s)",
            (variant_id, copies, DEFAULT_USER_ID),
        )
