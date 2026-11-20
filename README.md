# Clothing Store Inventory

An inventory system for a clothing store: a MySQL database, a desktop app for managing
products and stock and printing Code 128 barcode labels, a small API server, and an
iPhone/Android scanning app.

```
Phone app (Expo)  ──Wi-Fi──►  API server (FastAPI)  ──►  MySQL
                                                           ▲
Desktop app (PySide6) + label generator  ──────────────────┘
```

## Repository layout

| Folder | What it is |
|---|---|
| `database/schema.sql` | MySQL 8 schema, views, stored procedures and sample data |
| `desktop/` | Desktop app (PySide6): inventory, products, receiving, history, label printing |
| `labels/make_labels.py` | Label PDF generator (command line, and used by the desktop app) |
| `api/api.py` | FastAPI server the phone app talks to |
| `mobile/` | Expo (React Native) scanning app |

## Requirements

- MySQL 8.0.16 or newer (DBeaver is handy for running the schema)
- Python 3.9+
- Node.js (LTS)
- iPhone with the Expo Go app and a free Expo account (see "Mobile app")

## 1. Database

Open `database/schema.sql` in DBeaver and run it as a **script** (Alt+X), not a single
statement. It creates the `clothing_inventory` database. **It drops any existing
database with that name.**

All stock changes go through the stored procedures `sp_adjust_stock` and
`sp_set_stock_count`, so the stock balance and the audit log (`stock_movements`)
never disagree. Do not update `stock_levels` directly.

## 2. Python environment

```
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows PowerShell
python -m pip install -r requirements.txt
```

If PowerShell blocks the activate script, run once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

> Keep the project outside OneDrive. Synced folders cause errors when creating
> virtual environments.

## 3. Desktop app

```
$env:MYSQL_PASSWORD = 'your_mysql_password'     # optional: pre-fills the password box
python desktop/app.py
```

Connect with your MySQL user and password. The app has two tabs:

- **Inventory**: search, sort, and filter (low stock, inactive). Low items show orange, out of
  stock shows red. Buttons: **New product** (tick colors and sizes and it creates every
  SKU/barcode; re-using a style code adds only the missing ones), **Receive delivery** (enter
  quantities for every size/color of a style at once, then print labels for exactly what
  arrived), **Adjust stock** (receive, return, sale, damaged, lost, or an exact stocktake
  count; also on double-click), **Edit product** (name, cost, price, active), and
  **Print labels** (select rows, choose copies and layout, and a PDF opens for printing).
- **Movement history**: the audit log of every change, from the desktop and the phone.

All stock changes use the stored procedures, so the balance and history always agree.
Prices use Colombian formatting (`$59.900`).

## 4. Label generator (command line)

```
$env:MYSQL_PASSWORD = 'your_mysql_password'
python labels/make_labels.py --demo                       # layout test, no database
python labels/make_labels.py --style CT-001 --copies 2    # all variants of a style
python labels/make_labels.py --sku CT-001-BLK-M --copies 5
python labels/make_labels.py --style CT-001 --mode roll   # one label per page (thermal printer)
```

- `--mode sheet` (default): Avery 5160 layout, 30 labels per US Letter page. Print at 100% scale.
- `--mode roll`: one 2.25" x 1.25" label per page.
- Prices print in Colombian format (`$59.900`). Labels show size, not color.
- The barcode value is the SKU (`STYLE-COLOR-SIZE`).

## 5. API server

```
$env:MYSQL_PASSWORD = 'your_mysql_password'
$env:INVENTORY_API_KEY = 'a-long-random-phrase'
cd api
python -m uvicorn api:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/docs` to try the endpoints. The phone must be on the same
Wi-Fi; allow port 8000 on your private network in Windows Firewall.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Connectivity check (no key needed) |
| `GET /items/{barcode}` | Look up an item |
| `POST /stock/adjust` | Receive, sale, return, damaged, lost |
| `POST /stock/count` | Stocktake: set the exact counted quantity |

Every request except `/health` needs the `X-API-Key` header.

## 6. Mobile app

The app is a single file, `mobile/App.js`. To run it:

```
npx create-expo-app@latest scanner-app --template blank
cd scanner-app
npx expo install expo-camera expo-secure-store react-native-safe-area-context
```

Replace the generated `App.js` with `mobile/App.js` from this repo, then:

```
npx expo start
```

Scan the QR code with the iPhone Camera app to open it in Expo Go. Physical iPhones
require you to be signed in to the **same** Expo account in both Expo Go and the
terminal (`npx expo login`).

In the app's Settings screen, enter the server address (`http://<your-PC-IP>:8000`)
and the API key, then tap **Test connection**.

Modes: **Look up**, **Receive**, **Sell**, **Return**, **Count**.

## Security notes

- Never commit passwords or the API key. They are read from environment variables.
- The seeded `admin` user in `schema.sql` has a placeholder password hash; replace it
  before adding real logins.
- The API uses plain HTTP and a shared key. It is meant for the store's own Wi-Fi. Add
  HTTPS and per-user logins before exposing it to the internet.

## Roadmap

- Android build of the scanner app
- Desktop reports (sales, stock value, reorder suggestions) and purchase orders
- Package the desktop app as a single `.exe` with PyInstaller
- Per-user logins instead of the shared key
- Point-of-sale integration so sales reduce stock automatically
- Production app builds (no Expo Go) and HTTPS
