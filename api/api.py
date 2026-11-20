"""
Inventory API: the bridge between the mobile scanning app and MySQL.

The phone talks to this server over Wi-Fi; this server talks to MySQL by
calling the stored procedures from clothing_inventory_schema.sql, so stock
and the audit log always stay consistent.

Setup
-----
  pip install fastapi "uvicorn[standard]" mysql-connector-python

Environment variables (PowerShell examples):
  $env:MYSQL_PASSWORD  = 'your_mysql_password'      # required
  $env:INVENTORY_API_KEY = 'pick-a-long-random-key' # required: the phone sends this
  $env:MYSQL_USER = 'root'                          # optional, default root
  $env:MYSQL_HOST = 'localhost'                     # optional
  $env:MYSQL_PORT = '3306'                          # optional

Run
---
  uvicorn api:app --host 0.0.0.0 --port 8000
  Then open http://localhost:8000/docs to try every endpoint in your browser.
"""

import os
import secrets
from typing import Literal, Optional

import mysql.connector
from mysql.connector import pooling
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------
API_KEY = os.environ.get("INVENTORY_API_KEY")
DB_PASSWORD = os.environ.get("MYSQL_PASSWORD")
if not API_KEY or not DB_PASSWORD:
    raise SystemExit(
        "Set INVENTORY_API_KEY and MYSQL_PASSWORD environment variables first "
        "(see the notes at the top of api.py)."
    )

DEFAULT_LOCATION_ID = 1   # "Main Store" from the seed data
DEFAULT_USER_ID = 1       # the seeded admin user; replace when you add real logins

pool = pooling.MySQLConnectionPool(
    pool_name="inventory",
    pool_size=5,
    host=os.environ.get("MYSQL_HOST", "localhost"),
    port=int(os.environ.get("MYSQL_PORT", "3306")),
    user=os.environ.get("MYSQL_USER", "root"),
    password=DB_PASSWORD,
    database="clothing_inventory",
)

app = FastAPI(title="Clothing Inventory API")


# ----------------------------------------------------------------------
# AUTH: every request must carry the X-API-Key header
# ----------------------------------------------------------------------
def require_key(x_api_key: Optional[str] = Header(default=None)):
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ----------------------------------------------------------------------
# MODELS
# ----------------------------------------------------------------------
# What each action does to stock. Quantities from the phone are always positive.
SIGN = {"receive": 1, "return": 1, "sale": -1, "damaged": -1, "lost": -1}


class AdjustRequest(BaseModel):
    barcode: str
    action: Literal["receive", "sale", "return", "damaged", "lost"]
    quantity: int = Field(gt=0, le=10000)
    location_id: int = DEFAULT_LOCATION_ID
    notes: Optional[str] = Field(default=None, max_length=255)


class CountRequest(BaseModel):
    barcode: str
    counted_qty: int = Field(ge=0, le=100000)
    location_id: int = DEFAULT_LOCATION_ID
    notes: Optional[str] = Field(default=None, max_length=255)


# ----------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------
def find_item(cur, barcode: str, location_id: int):
    cur.execute(
        """
        SELECT variant_id, product_name, color, size, sku, barcode,
               location_id, location, quantity, retail_price, reorder_level
        FROM v_inventory
        WHERE barcode = %s AND location_id = %s
        """,
        (barcode, location_id),
    )
    row = cur.fetchone()
    if row:
        row["retail_price"] = float(row["retail_price"])
    return row


def db_error_to_http(e: mysql.connector.Error) -> HTTPException:
    # Our stored procedures raise SQLSTATE 45000 with a readable message.
    if getattr(e, "sqlstate", None) == "45000":
        return HTTPException(status_code=409, detail=e.msg)
    return HTTPException(status_code=500, detail="Database error")


# ----------------------------------------------------------------------
# ENDPOINTS
# ----------------------------------------------------------------------
@app.get("/health")
def health():
    """No key needed. for checking the phone can reach the server."""
    return {"status": "ok"}


@app.get("/items/{barcode}", dependencies=[Depends(require_key)])
def get_item(barcode: str, location_id: int = DEFAULT_LOCATION_ID):
    """Look up an item by scanned barcode."""
    conn = pool.get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        row = find_item(cur, barcode, location_id)
        cur.close()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="No item with that barcode")
    return row


@app.post("/stock/adjust", dependencies=[Depends(require_key)])
def adjust_stock(req: AdjustRequest):
    """Receive, sell, return, or write off units of a scanned item."""
    conn = pool.get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        item = find_item(cur, req.barcode, req.location_id)
        if not item:
            raise HTTPException(status_code=404, detail="No item with that barcode")

        change = SIGN[req.action] * req.quantity
        try:
            cur.callproc("sp_adjust_stock", (
                item["variant_id"], req.location_id, change,
                req.action.upper(), DEFAULT_USER_ID, "mobile", None, req.notes,
            ))
            for _ in cur.stored_results():  # drain any procedure result sets
                pass
        except mysql.connector.Error as e:
            raise db_error_to_http(e)

        updated = find_item(cur, req.barcode, req.location_id)
        cur.close()
        return updated
    finally:
        conn.close()


@app.post("/stock/count", dependencies=[Depends(require_key)])
def set_count(req: CountRequest):
    """Stocktake: set the exact counted quantity for a scanned item."""
    conn = pool.get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        item = find_item(cur, req.barcode, req.location_id)
        if not item:
            raise HTTPException(status_code=404, detail="No item with that barcode")
        try:
            cur.callproc("sp_set_stock_count", (
                item["variant_id"], req.location_id, req.counted_qty,
                DEFAULT_USER_ID, "mobile", req.notes,
            ))
            for _ in cur.stored_results():
                pass
        except mysql.connector.Error as e:
            raise db_error_to_http(e)

        updated = find_item(cur, req.barcode, req.location_id)
        cur.close()
        return updated
    finally:
        conn.close()
