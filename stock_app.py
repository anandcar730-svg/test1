#!/usr/bin/env python3
"""
Multi-branch Stock In / Out manager for Raspberry Pi
----------------------------------------------------
Run:      python3 stock_app.py
Open:     http://<pi-ip-address>:5001   (from any phone/PC on the same network)

Install:  pip3 install flask
Data:     stock_data.db (SQLite) next to this file.
"""

import csv
import io
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from flask import (Flask, Response, flash, g, redirect, render_template_string,
                   request, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PHOTO_DIR = os.path.join(BASE_DIR, "product_photos")
MAX_PHOTO_BYTES = 8 * 1024 * 1024
DB_PATH = os.path.join(BASE_DIR, "stock_data.db")
DEFAULT_BRANCHES = ["Branch 1", "Branch 2", "Branch 3", "Branch 4"]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
app.secret_key = secrets.token_hex(16)   # replaced by a saved random key in init_db()
app.config.update(SESSION_COOKIE_NAME="stock_session", SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_HTTPONLY=True, PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
SALES_MINUTES = 30
MAX_LOYVERSE_ACCOUNTS = 50


# ---------- Database ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA synchronous = NORMAL")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS branches (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS products (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT UNIQUE NOT NULL,
            unit      TEXT NOT NULL DEFAULT 'pcs',
            low_limit REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS stock (
            product_id INTEGER NOT NULL,
            branch_id  INTEGER NOT NULL,
            qty        REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (product_id, branch_id),
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
            FOREIGN KEY (branch_id)  REFERENCES branches(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS transfers (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id     INTEGER NOT NULL,
            from_branch_id INTEGER NOT NULL,
            to_branch_id   INTEGER NOT NULL,
            qty            REAL NOT NULL,
            note           TEXT,
            created_at     TEXT NOT NULL,
            FOREIGN KEY (product_id)     REFERENCES products(id) ON DELETE CASCADE,
            FOREIGN KEY (from_branch_id) REFERENCES branches(id) ON DELETE CASCADE,
            FOREIGN KEY (to_branch_id)   REFERENCES branches(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS returns (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id     INTEGER NOT NULL,
            from_branch_id INTEGER NOT NULL,
            to_branch_id   INTEGER NOT NULL,
            qty            REAL NOT NULL,
            reason         TEXT NOT NULL,
            note           TEXT,
            created_at     TEXT NOT NULL,
            FOREIGN KEY (product_id)     REFERENCES products(id) ON DELETE CASCADE,
            FOREIGN KEY (from_branch_id) REFERENCES branches(id) ON DELETE CASCADE,
            FOREIGN KEY (to_branch_id)   REFERENCES branches(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS app_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('sales_only','no_loyverse_branch','no_branch','admin')),
            page_access TEXT,
            block_access TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            event TEXT NOT NULL,
            ip TEXT,
            device TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_presence (
            username TEXT PRIMARY KEY,
            last_seen REAL NOT NULL,
            logged_out INTEGER NOT NULL DEFAULT 0,
            ip TEXT,
            device TEXT
        );
        CREATE TABLE IF NOT EXISTS lv_store_map (
            lv_store_id   TEXT PRIMARY KEY,
            lv_store_name TEXT,
            branch_id     INTEGER
        );
        CREATE TABLE IF NOT EXISTS lv_alias (
            lv_name    TEXT PRIMARY KEY,
            product_id INTEGER NOT NULL,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS lv_pos (
            account_id TEXT NOT NULL,
            pos_id     TEXT NOT NULL,
            name       TEXT,
            store_name TEXT,
            activated  INTEGER NOT NULL DEFAULT 1,
            synced_at  TEXT,
            PRIMARY KEY (account_id, pos_id)
        );
        CREATE TABLE IF NOT EXISTS lv_ignore (
            lv_name    TEXT PRIMARY KEY,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS lv_group_items (
            lv_name    TEXT NOT NULL,
            product_id INTEGER NOT NULL,
            qty        REAL NOT NULL DEFAULT 1 CHECK(qty > 0),
            PRIMARY KEY (lv_name, product_id),
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS lv_receipts (
            receipt_key  TEXT PRIMARY KEY,
            receipt_type TEXT,
            store_id     TEXT,
            state        TEXT NOT NULL,
            note         TEXT,
            created_at   TEXT,
            applied_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lv_lines (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_key TEXT NOT NULL,
            product_id  INTEGER NOT NULL,
            branch_id   INTEGER NOT NULL,
            delta       REAL NOT NULL,
            sale_id     INTEGER
        );
        CREATE TABLE IF NOT EXISTS lv_issues (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            kind        TEXT NOT NULL,
            receipt_key TEXT,
            item_name   TEXT,
            detail      TEXT,
            payload     TEXT,
            created_at  TEXT NOT NULL,
            resolved    INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sales (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            branch_id  INTEGER NOT NULL,
            qty        REAL NOT NULL,
            price      REAL NOT NULL DEFAULT 0,
            total      REAL NOT NULL DEFAULT 0,
            pay_type   TEXT NOT NULL CHECK (pay_type IN ('PAID','FREE','CREDIT')),
            customer   TEXT,
            note       TEXT,
            created_at TEXT NOT NULL,
            settled_at TEXT,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
            FOREIGN KEY (branch_id)  REFERENCES branches(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS lv_customers (
            customer_id TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            place       TEXT,
            phone       TEXT,
            updated_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            branch_id  INTEGER NOT NULL,
            kind       TEXT NOT NULL CHECK (kind IN ('IN','OUT')),
            qty        REAL NOT NULL,
            note       TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
            FOREIGN KEY (branch_id)  REFERENCES branches(id) ON DELETE CASCADE
        );
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_lv_lines_receipt ON lv_lines(receipt_key)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_transactions_created ON transactions(created_at)")
    # Existing installations may already have the app_users table.  Add the
    # optional page-permission column without disturbing their accounts.
    user_columns = {row[1] for row in db.execute("PRAGMA table_info(app_users)")}
    product_columns = {row[1] for row in db.execute("PRAGMA table_info(products)")}
    if "photo" not in product_columns:
        db.execute("ALTER TABLE products ADD COLUMN photo TEXT")
    transaction_columns = {row[1] for row in db.execute("PRAGMA table_info(transactions)")}
    if "photo" not in transaction_columns:
        db.execute("ALTER TABLE transactions ADD COLUMN photo TEXT")
    if "page_access" not in user_columns:
        db.execute("ALTER TABLE app_users ADD COLUMN page_access TEXT")
    if "block_access" not in user_columns:
        db.execute("ALTER TABLE app_users ADD COLUMN block_access TEXT")
    if db.execute("SELECT COUNT(*) FROM branches").fetchone()[0] == 0:
        db.executemany("INSERT INTO branches (name) VALUES (?)",
                       [(n,) for n in DEFAULT_BRANCHES])
    row = db.execute("SELECT value FROM settings WHERE key='secret_key'").fetchone()
    if not row:
        db.execute("INSERT INTO settings (key, value) VALUES ('secret_key', ?)", (secrets.token_hex(32),))
        row = db.execute("SELECT value FROM settings WHERE key='secret_key'").fetchone()
    app.secret_key = row[0]
    db.commit()
    db.close()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt(n):
    n = float(n or 0)
    return int(n) if n.is_integer() else round(n, 2)


def money(n):
    return f"{float(n or 0):,.2f}"


app.jinja_env.filters["fmt"] = fmt
app.jinja_env.filters["money"] = money


def all_branches():
    return get_db().execute("SELECT * FROM branches ORDER BY id").fetchall()


def all_products():
    return get_db().execute("SELECT * FROM products ORDER BY name").fetchall()


# Every app page can be granted individually to a user.
USER_PAGES = (
    ("stock", "Stock"),
    ("sales", "Sales"),
    ("credit", "Credit"),
    ("transfer", "Transfer"),
    ("returns", "Return Product"),
    ("branchchat", "Branch Dashboard"),
    ("loyverse", "Loyverse"),
    ("groups", "Product Groups"),
    ("lvmatch", "Loyverse Match"),
    ("history", "History"),
    ("products", "Products"),
    ("branches", "Branches"),
    ("users", "Users"),
    ("userlog", "User Log"),
    ("settings", "Settings"),
)
USER_PAGE_KEYS = {key for key, _label in USER_PAGES}
BLOCKS = (
    ("stock_summary", "Stock — summary by branch"), ("stock_move", "Stock — Stock In / Out"),
    ("stock_warnings", "Stock — warnings"), ("stock_by_branch", "Stock — stock by branch"),
    ("history_filter", "History — find / download"), ("history_transactions", "History — transactions"),
    ("products_add", "Products — add product"), ("products_bulk", "Products — add many products"), ("products_list", "Products — all products"),
    ("branches_add", "Branches — add branch"), ("branches_manage", "Branches — manage branches"),
    ("sales_record", "Sales — record a sale"), ("sales_credit", "Sales — pending credit"), ("sales_list", "Sales — sales list"),
    ("credit_pending", "Credit — pending sales"), ("transfer_move", "Transfer — move products"), ("transfer_recent", "Transfer — recent transfers"),
    ("branchchat_view", "Branch Dashboard — transfers and returns"),
    ("returns_move", "Return Product — give products back"), ("returns_recent", "Return Product — recent returns"),
    ("loyverse_accounts", "Loyverse — accounts"), ("loyverse_map", "Loyverse — store mapping"),
    ("loyverse_sync", "Loyverse — syncing"), ("loyverse_issues", "Loyverse — issues"), ("loyverse_receipts", "Loyverse — receipts"),
    ("lvmatch_unmatched", "Loyverse Match — needs matching"), ("lvmatch_matched", "Loyverse Match — matched items"),
    ("groups_editor", "Product Groups — editor"), ("groups_saved", "Product Groups — saved groups"),
    ("users_create", "Users — create account"), ("users_accounts", "Users — accounts"),
    ("userlog_status", "User Log — online / offline"), ("userlog_history", "User Log — sign-in history"),
    ("users_assign_pages", "Users — page assignments"), ("users_assign_blocks", "Users — block assignments"),
    ("settings_app", "Settings — app password"), ("settings_sales", "Settings — sales password"),
    ("settings_lock", "Settings — lock sales"), ("settings_forgot", "Settings — password help"),
)
BLOCK_KEYS = {key for key, _label in BLOCKS}
LEGACY_PAGE_ACCESS = {
    "sales_only": {"stock", "sales"},
    "no_loyverse_branch": {"stock", "sales", "credit", "transfer", "returns", "branchchat", "history", "products"},
    "no_branch": {"stock", "sales", "credit", "transfer", "returns", "branchchat", "loyverse", "groups", "lvmatch", "history", "products"},
}


def allowed_pages():
    """Return the pages available to the signed-in user."""
    if session.get("role") == "admin":
        return USER_PAGE_KEYS
    saved = session.get("page_access")
    if isinstance(saved, list):
        return set(saved) & USER_PAGE_KEYS
    return LEGACY_PAGE_ACCESS.get(session.get("role"), set())


def can_open_page(page):
    return page in allowed_pages()


def allowed_blocks():
    if session.get("role") == "admin":
        return BLOCK_KEYS
    saved = session.get("block_access")
    # Accounts created before block control retain all their page blocks until
    # an administrator sets specific block assignments.
    if isinstance(saved, list):
        return set(saved) & BLOCK_KEYS
    return BLOCK_KEYS


def can_open_block(block):
    return block in allowed_blocks()


# ---------- Templates ----------
PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stock Manager</title>
<style>
:root{
  --sb:230px;
  --accent:#2563eb;
  --bg:#f1f5f9;
  --surf:#ffffff;
  --txt:#0f172a;
  --muted:#64748b;
  --border:#e2e8f0;
  --green:#16a34a;
  --red:#dc2626;
  --yellow:#ca8a04;
  --shadow:0 1px 3px #0000000a,0 2px 8px #00000008;
  --accent:#3157d5;
  --bg:#f5f7fb;
  --surf:#ffffff;
  --border:#e7eaf1;
  --shadow:0 5px 22px #1725540a;
}
*,*::before,*::after{box-sizing:border-box}
html{scroll-behavior:smooth}
body{font-family:system-ui,-apple-system,sans-serif;margin:0;background:var(--bg);color:var(--txt);display:flex;min-height:100vh}

/* ─── Sidebar ──────────────────────────────────────── */
.sidebar{width:var(--sb);background:#ffffff;color:#334155;border-right:1px solid var(--border);display:flex;flex-direction:column;position:fixed;top:0;left:0;bottom:0;z-index:100;overflow-y:auto;transition:transform .25s}
.sb-logo{padding:20px 16px 16px;border-bottom:1px solid var(--border)}
.sb-logo h1{font-size:16px;font-weight:800;margin:0 0 3px;color:#172033;display:flex;align-items:center;gap:8px}
.sb-logo .live{font-size:11px;color:#86efac;font-weight:500;display:inline-block}
.sb-nav{flex:1;padding:10px 8px;display:flex;flex-direction:column;gap:1px;overflow-y:auto}
.sb-nav a{display:flex;align-items:center;gap:9px;padding:10px 12px;border-radius:9px;color:#64748b;text-decoration:none;font-size:13.5px;font-weight:500;transition:background .12s,color .12s;white-space:nowrap}
.sb-nav a:hover{background:#f1f5f9;color:#172033}
.sb-nav a.cur{background:#eaf0ff;color:#2449bd;box-shadow:none;font-weight:700}
.sb-sep{height:1px;background:var(--border);margin:9px 4px}
.sb-logout a{color:#fca5a5 !important}
.sb-logout a:hover{background:#dc262618 !important}

/* ─── Main wrapper ─────────────────────────────────── */
.wrapper{flex:1;margin-left:var(--sb);display:flex;flex-direction:column;min-width:0}
.topbar{background:rgba(255,255,255,.94);border-bottom:1px solid var(--border);padding:14px 22px;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:50;box-shadow:0 1px 4px #00000008}
.topbar-title{font-size:16px;font-weight:700;color:var(--txt);flex:1}
.menu-btn{display:none;background:none;border:1.5px solid var(--border);border-radius:7px;padding:6px 10px;font-size:18px;cursor:pointer;color:var(--txt);line-height:1}
main{flex:1;padding:26px 28px;width:100%;max-width:1500px}

/* ─── Cards ────────────────────────────────────────── */
.card{background:var(--surf);border-radius:14px;padding:21px 22px;margin-bottom:20px;border:1px solid var(--border);box-shadow:var(--shadow)}
h2{margin:0 0 16px;font-size:15px;font-weight:700;color:var(--txt);display:flex;align-items:center;gap:7px}

/* ─── KPI grid ─────────────────────────────────────── */
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:14px;margin-bottom:20px}
.bcard{background:var(--surf);border-radius:14px;padding:18px 17px;border:1px solid var(--border);box-shadow:var(--shadow);position:relative;overflow:hidden}
.bcard::after{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:#3157d5}
.bcard-link{display:block;text-decoration:none;color:var(--txt);transition:transform .12s,box-shadow .12s}
.bcard-link:hover{transform:translateY(-3px);box-shadow:0 8px 24px #00000014}
.bcard b{display:block;font-size:11px;font-weight:700;color:var(--muted);margin-bottom:10px;text-transform:uppercase;letter-spacing:.05em}
.bcard span{font-size:12px;color:var(--muted);display:block;margin-top:2px;line-height:1.5}
.bcard .bnum{font-size:26px;font-weight:800;color:var(--txt);line-height:1.1;margin-bottom:4px}

/* ─── Tables ───────────────────────────────────────── */
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:10px 10px;border-bottom:1px solid var(--border);text-align:left;vertical-align:middle}
th{color:var(--muted);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;background:#f8fafc}
tbody tr:hover{background:#fafbff}
tbody tr:last-child td{border-bottom:none}
td.n,th.n{text-align:right}
.scroll{overflow-x:auto;border-radius:12px;border:1px solid var(--border)}
.sbb th:first-child,.sbb td:first-child{position:sticky;left:0;z-index:2;background:#fff;box-shadow:2px 0 0 var(--border);min-width:90px}
.sbb th:last-child,.sbb td:last-child{position:sticky;right:0;z-index:2;background:#fff;box-shadow:-2px 0 0 var(--border)}
.sbb th:first-child,.sbb th:last-child{background:#f8fafc}
@media(max-width:600px){.sbb{font-size:13px}.sbb th,.sbb td{padding:8px 5px}.sbb th:not(:first-child):not(:last-child),.sbb td:not(:first-child):not(:last-child){min-width:64px}}

/* ─── Form elements ────────────────────────────────── */
input,select,button,textarea{font-size:15px;padding:9px 12px;border:1.5px solid var(--border);border-radius:9px;font-family:inherit;outline:none;transition:border-color .15s,box-shadow .15s}
input:focus,select:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px #2563eb1a}
button{background:var(--accent);color:#fff;border:0;cursor:pointer;font-weight:600;transition:opacity .15s,transform .08s}
button:hover{opacity:.88}
button:active{transform:scale(.98)}
button.out{background:var(--red)}
button.in{background:var(--green)}
button.del{background:#94a3b8;padding:5px 10px;font-size:12px}
.row{display:flex;gap:8px;flex-wrap:wrap}
.row>*{flex:1;min-width:110px}

/* ─── Flash messages ───────────────────────────────── */
.flash{padding:12px 16px;border-radius:10px;margin-bottom:14px;font-size:14px;font-weight:500;background:#dbeafe;color:#1e40af;border:1px solid #bfdbfe;display:flex;align-items:flex-start;gap:8px}
.flash::before{content:'ℹ';font-size:16px;flex-shrink:0}
.flash.error{background:#fee2e2;color:#991b1b;border-color:#fecaca}
.flash.error::before{content:'⚠'}

/* ─── Tags ─────────────────────────────────────────── */
.low{color:var(--red);font-weight:700}
.zero{color:#9ca3af}
.tag{font-size:11.5px;padding:3px 10px;border-radius:99px;color:#fff;font-weight:600;display:inline-block}
.tag.IN{background:var(--green)}.tag.OUT{background:var(--red)}
.tag.PAID{background:var(--green)}.tag.FREE{background:#eab308;color:#111}.tag.CREDIT{background:var(--red)}
.g{color:var(--green);font-weight:600}.r{color:var(--red);font-weight:600}
small{color:var(--muted);font-size:12px}

/* ─── Step labels & misc ────────────────────────────── */
.step{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin:18px 0 8px}
.step:first-of-type{margin-top:0}
.qtyrow{display:flex;gap:8px;align-items:stretch}
.qtyrow button{flex:0 0 52px;font-size:22px;background:#e2e8f0;color:var(--txt)}
.qtyrow input{flex:1;text-align:center;font-size:20px;font-weight:700;min-width:0}
.hint{font-size:13px;color:var(--muted)}
.errbox{background:#fee2e2;color:#991b1b;padding:12px 14px;border-radius:9px;margin-top:10px;display:none;font-size:14px;border:1px solid #fecaca}

/* ─── Product picker tiles ──────────────────────────── */
.psearch{width:100%;margin-bottom:10px}
.pgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(112px,1fr));gap:8px}
.ptile{position:relative;display:flex;flex-direction:column;gap:3px;text-align:left;background:var(--surf);color:var(--txt);border:2px solid var(--border);border-radius:12px;padding:10px;min-height:70px;cursor:pointer;font-size:14px;line-height:1.2;transition:border-color .12s,box-shadow .12s}
.ptile:hover{box-shadow:0 2px 8px #0001}
.ptile .pn{font-weight:600;overflow-wrap:anywhere;padding-right:18px}
.ptile .pimg{width:100%;height:64px;object-fit:cover;border-radius:8px;background:#f1f5f9}
.pthumb{width:48px;height:48px;object-fit:cover;border-radius:8px;background:#f1f5f9;display:block}
.pnone{width:48px;height:48px;border-radius:8px;background:#f1f5f9;color:#9ca3af;display:flex;align-items:center;justify-content:center;font-size:20px}
.photobtn{display:inline-block;cursor:pointer;background:#e2e8f0;color:var(--txt);border-radius:8px;padding:6px 10px;font-size:13px;white-space:nowrap}
.ptile .ps{font-size:12px;color:var(--green)}
.ptile.zero .ps{color:var(--red)}
.ptile.none{opacity:.45}
.ptile.none .ps{color:var(--red)}
.ptile.sel{border-color:var(--accent);background:#eff6ff}
.ptile .pq{position:absolute;top:6px;right:6px;min-width:22px;height:22px;border-radius:99px;background:var(--accent);color:#fff;font-size:12px;font-weight:700;display:none;align-items:center;justify-content:center;padding:0 6px}
.ptile.sel .pq{display:flex}
.cart{margin-top:14px}
.cartempty{padding:16px;text-align:center;color:var(--muted);font-size:13px;border:2px dashed var(--border);border-radius:10px}
.line{border:1px solid var(--border);border-radius:10px;padding:12px;margin-bottom:8px;background:#f8fafc}
.line .qtyrow .rm{flex:0 0 44px;background:#fee2e2;color:#991b1b;font-size:16px}
.page-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:7px;margin:10px 0}
.page-grid label{display:flex;align-items:center;gap:7px;padding:7px 8px;border:1px solid var(--border);border-radius:6px;font-size:14px}
.page-grid input{width:auto;padding:0}

/* ─── Shop-floor dashboard: clear numbers and touch-friendly actions ─── */
.shop-dashboard-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:2px 0 16px}
.shop-dashboard-head h1{font-size:25px;line-height:1.2;margin:0;color:#172033}
.shop-dashboard-head p{margin:6px 0 0;color:#64748b;font-size:13px}
.shop-quick-actions{display:flex;gap:9px;flex-wrap:wrap;margin:0 0 18px}
.shop-quick-actions a{display:inline-flex;align-items:center;justify-content:center;min-height:46px;padding:10px 16px;border-radius:10px;text-decoration:none;font-size:14px;font-weight:800;background:#3157d5;color:#fff}
.shop-quick-actions a.secondary{background:#e8edf7;color:#24324b}
.shop-branch-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(205px,1fr));gap:12px;margin-bottom:18px}
.shop-branch-card{display:block;position:relative;background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:16px;text-decoration:none;color:#172033;box-shadow:0 2px 8px #0f172a08;min-width:0}
.shop-branch-card:active{transform:scale(.99)}
.shop-branch-name{font-size:13px;font-weight:800;color:#475569;text-transform:uppercase;letter-spacing:.04em}
.shop-branch-number{font-size:34px;line-height:1.1;font-weight:900;margin:9px 0 2px;color:#172033;font-variant-numeric:tabular-nums}
.shop-branch-caption{font-size:12px;color:#64748b}
.shop-branch-totals{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:13px;padding-top:11px;border-top:1px solid #edf0f5}
.shop-branch-totals span{display:block;font-size:11px;color:#64748b}
.shop-branch-totals strong{display:block;font-size:17px;margin-top:2px}
.shop-low-alert{display:inline-block;margin-top:10px;padding:5px 8px;border-radius:7px;background:#fff1f2;color:#be123c;font-size:12px;font-weight:800}
.shop-movement-card{border-radius:14px;padding:18px;border:1px solid #e2e8f0;background:#fff;box-shadow:0 2px 8px #0f172a08;margin-bottom:18px}
.shop-movement-card h2{font-size:18px;margin-bottom:5px}
.shop-movement-hint{font-size:13px;color:#64748b;margin:0 0 10px}
.shop-movement-buttons{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px}
.shop-movement-buttons button{min-height:58px;border-radius:12px;font-size:17px;font-weight:900}
.shop-movement-buttons button.in{background:#15803d}
.shop-movement-buttons button.out{background:#b91c1c}
.stockout-extra{margin-top:14px;padding:16px;border:1px solid #fecaca;border-left:4px solid #dc2626;border-radius:12px;background:#fff8f7}
.stock-photo-picker{display:inline-flex;align-items:center;gap:8px;padding:10px 14px;border:1px dashed #94a3b8;border-radius:9px;background:#fff;color:#334155;font-weight:600;cursor:pointer}
.stock-photo-picker:hover{border-color:var(--accent);background:#f8faff}
@media(max-width:600px){
  .shop-dashboard-head h1{font-size:22px}
  .shop-quick-actions{display:grid;grid-template-columns:1fr 1fr}
  .shop-quick-actions a{min-height:50px;padding:9px 8px;text-align:center}
  .shop-branch-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
  .shop-branch-card{padding:12px;border-radius:12px}
  .shop-branch-number{font-size:28px}
  .shop-branch-totals strong{font-size:15px}
  .shop-movement-card{padding:14px}
  .shop-movement-buttons button{min-height:62px;font-size:16px}
}
@media(max-width:840px){main{padding:16px 12px}.topbar{padding:12px 14px}}

/* ─── Responsive ────────────────────────────────────── */
.overlay{display:none;position:fixed;inset:0;background:#0009;z-index:99}
@media(max-width:840px){
  .sidebar{transform:translateX(-100%)}
  .sidebar.open{transform:translateX(0)}
  .overlay.open{display:block}
  .wrapper{margin-left:0}
  .menu-btn{display:flex}
  main{padding:14px 12px}
  .grid{grid-template-columns:repeat(2,1fr)}
  .bcard .bnum{font-size:22px}
}
</style>
<script>
window.ProductPicker = function(o){
  var tiles = {}, rows = {}, order = [], selected = "", names = {};
  function esc(t){ return String(t).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
  function A(pid){ return o.avail(String(pid)); }
  function U(pid){ return (o.units && o.units[pid]) || ""; }
  function qin(pid){ return rows[pid].querySelector("input[name=qty]"); }
  function changed(){ if(o.onChange) o.onChange(); }
  o.products.forEach(function(p){
    var pid = String(p.id); names[pid] = p.name;
    var b = document.createElement("button");
    b.type = "button"; b.className = "ptile"; b.setAttribute("data-name", p.name.toLowerCase());
    b.innerHTML = (p.photo ? '<img class="pimg" loading="lazy" alt="" src="'+esc(p.photo)+'">' : '')+
      '<span class="pn">'+esc(p.name)+'</span><span class="ps"></span><span class="pq"></span>';
    b.onclick = function(){ tap(pid); };
    o.grid.appendChild(b); tiles[pid] = b;
  });
  function paint(){
    Object.keys(tiles).forEach(function(pid){
      var t = tiles[pid], a = A(pid), on = o.single ? (selected === pid) : (rows[pid] !== undefined);
      t.querySelector(".ps").textContent = (a > 0 || o.allowZero) ? (a + " " + U(pid)) : "none left";
      t.classList.toggle("sel", on);
      t.classList.toggle("none", !o.allowZero && a <= 0);
      t.classList.toggle("zero", o.allowZero && a <= 0);
      var q = t.querySelector(".pq");
      q.textContent = (!o.single && rows[pid]) ? (parseFloat(qin(pid).value) || "") : "";
    });
    if(o.empty) o.empty.style.display = (!o.single && order.length === 0) ? "block" : "none";
  }
  function hintRow(pid){
    var r = rows[pid], a = A(pid), h = r.querySelector(".ch"), q = qin(pid);
    h.textContent = o.rowHint ? o.rowHint(pid) : ("In stock: " + a + " " + U(pid));
    h.style.color = a > 0 ? "#166534" : "#dc2626";
    if(a > 0 && !o.allowZero) q.max = a; else q.removeAttribute("max");
  }
  function removeRow(pid){
    if(!rows[pid]) return;
    rows[pid].remove(); delete rows[pid]; order = order.filter(function(x){ return x !== pid; });
    paint(); changed();
  }
  function addRow(pid){
    var r = document.createElement("div"); r.className = "line";
    r.innerHTML = '<input type="hidden" name="product_id" value="'+pid+'">'+
      '<div style="display:flex;justify-content:space-between;gap:8px;margin-bottom:8px;align-items:baseline">'+
      '<strong>'+esc(names[pid])+'</strong><span class="ch hint"></span></div>'+
      '<div class="qtyrow"><button type="button" class="qm" aria-label="Less">&#8722;</button>'+
      '<input name="qty" type="number" step="any" min="0.01" inputmode="decimal">'+
      '<button type="button" class="qp" aria-label="More">+</button>'+
      '<button type="button" class="rm" aria-label="Remove">&#10005;</button></div>';
    rows[pid] = r; order.push(pid); o.cart.appendChild(r);
    var q = qin(pid);
    r.querySelector(".qm").onclick = function(){
      var v = Math.round(((parseFloat(q.value) || 0) - 1) * 100) / 100;
      if(v <= 0) removeRow(pid); else { q.value = v; paint(); changed(); }
    };
    r.querySelector(".qp").onclick = function(){
      var v = Math.round(((parseFloat(q.value) || 0) + 1) * 100) / 100, a = A(pid);
      if(!o.allowZero && a > 0 && v > a) v = a;
      q.value = v; paint(); changed();
    };
    r.querySelector(".rm").onclick = function(){ removeRow(pid); };
    q.addEventListener("input", function(){ paint(); changed(); });
    hintRow(pid);
    return q;
  }
  function tap(pid){
    var a = A(pid);
    if(o.single){
      selected = (selected === pid) ? "" : pid; o.single.value = selected;
      paint(); changed(); return;
    }
    if(!o.allowZero && a <= 0){ if(o.notify) o.notify(names[pid] + " has none left here."); return; }
    var capped = false;
    if(!rows[pid]){ addRow(pid).value = 1; }
    else {
      var q = qin(pid), v = Math.round(((parseFloat(q.value) || 0) + 1) * 100) / 100;
      if(!o.allowZero && v > a){ v = a; capped = true; }
      q.value = v;
    }
    paint(); changed();
    if(capped && o.notify) o.notify("Only " + a + " " + U(pid) + " of " + names[pid] + " available.");
  }
  if(o.search){
    o.search.addEventListener("input", function(){
      var k = o.search.value.trim().toLowerCase();
      Object.keys(tiles).forEach(function(pid){
        tiles[pid].style.display = (!k || tiles[pid].getAttribute("data-name").indexOf(k) !== -1) ? "" : "none";
      });
    });
  }
  paint();
  return {
    refresh: function(){ paint(); order.forEach(hintRow); },
    items: function(){ return order.map(function(pid){ return {pid: pid, name: names[pid], qty: parseFloat(qin(pid).value) || 0}; }); },
    selected: function(){ return selected; },
    name: function(pid){ return names[pid] || ""; }
  };
};
</script>
</head>
<body>
<div class="overlay" id="overlay"></div>

<nav class="sidebar" id="sidebar">
  <div class="sb-logo">
    <h1>&#128230; Stock Manager</h1>
    <span id="live" class="live">&#11044; live</span>
  </div>
  <div class="sb-nav">
    {% if can_open_page('stock') %}<a href="{{ url_for('index') }}">&#127968; Stock</a>{% endif %}
    {% if can_open_page('sales') %}<a href="{{ url_for('sales_page') }}">&#128176; Sales</a>{% endif %}
    {% if can_open_page('credit') %}<a href="{{ url_for('credit_page') }}">&#128203; Credit</a>{% endif %}
    {% if can_open_page('transfer') %}<a href="{{ url_for('transfer_page') }}">&#128260; Transfer</a>{% endif %}
    {% if can_open_page('returns') %}<a href="{{ url_for('returns_page') }}">&#8617; Return Product</a>{% endif %}
    {% if can_open_page('branchchat') %}<a href="{{ url_for('branchchat_page') }}">&#128202; Branch Dashboard</a>{% endif %}
    <div class="sb-sep"></div>
    {% if can_open_page('loyverse') %}<a href="{{ url_for('loyverse_page') }}">&#128279; Loyverse</a>{% endif %}
    {% if can_open_page('groups') %}<a href="{{ url_for('loyverse_groups_page') }}">&#128230; Product Groups</a>{% endif %}
    {% if can_open_page('lvmatch') %}<a href="{{ url_for('lvmatch_page') }}">&#128257; Loyverse Match</a>{% endif %}
    <div class="sb-sep"></div>
    {% if can_open_page('history') %}<a href="{{ url_for('history') }}">&#128220; History</a>{% endif %}
    {% if can_open_page('products') %}<a href="{{ url_for('products_page') }}">&#128218; Products</a>{% endif %}
    {% if can_open_page('branches') %}<a href="{{ url_for('branches_page') }}">&#127978; Branches</a>{% endif %}
    {% if can_open_page('users') %}<a href="{{ url_for('users_page') }}">&#128101; Users</a>{% endif %}
    {% if can_open_page('userlog') %}<a href="{{ url_for('userlog_page') }}">&#128203; User Log</a>{% endif %}
    {% if can_open_page('settings') %}<a href="{{ url_for('settings_page') }}">&#9881; Settings</a>{% endif %}
    <div class="sb-sep"></div>
    <div class="sb-logout"><a href="{{ url_for('logout') }}">&#128682; Log out</a></div>
  </div>
</nav>

<div class="wrapper">
  <div class="topbar">
    <button class="menu-btn" id="menuBtn" aria-label="Menu">&#9776;</button>
    <span class="topbar-title">&#128230; Stock Manager</span>
  </div>
  <main>
    {% with msgs = get_flashed_messages(with_categories=true) %}
      {% for cat, m in msgs %}<div class="flash {{ cat }}">{{ m }}</div>{% endfor %}
    {% endwith %}
    {{ body|safe }}
  </main>
</div>

<script>
(function(){
  var ov=document.getElementById("overlay"),sb=document.getElementById("sidebar"),mb=document.getElementById("menuBtn");
  if(mb){mb.addEventListener("click",function(){sb.classList.toggle("open");ov.classList.toggle("open");});}
  if(ov){ov.addEventListener("click",function(){sb.classList.remove("open");ov.classList.remove("open");});}
  var cur=location.pathname;
  sb.querySelectorAll("a[href]").forEach(function(a){
    var h=a.getAttribute("href");
    if(h && (cur===h || (h!=="/" && cur.startsWith(h)))) a.classList.add("cur");
  });
})();
/* Keep the scroll position after a form is submitted (page reloads), so phone users
   are not thrown back to the top every time they press OK. */
function saveScroll(){
  try { sessionStorage.setItem("scroll:" + location.pathname, String(window.scrollY || 0)); } catch(e){}
}
(function(){
  document.addEventListener("submit", saveScroll, true);
  function restore(){
    if (location.hash) return;
    var y = null;
    try { y = sessionStorage.getItem("scroll:" + location.pathname); sessionStorage.removeItem("scroll:" + location.pathname); } catch(e){}
    y = parseInt(y, 10);
    if (y > 0) { window.scrollTo(0, y); setTimeout(function(){ window.scrollTo(0, y); }, 250); }
  }
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", restore); else restore();
})();
(function(){
  var cur = {{ version|tojson }}, live = document.getElementById("live");
  function busy(){
    var a = document.activeElement;
    if (a && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName)) return true;
    var f = document.querySelectorAll("input[name=qty],input[name=note],input[name=name],input[name=customer],textarea");
    for (var i=0;i<f.length;i++) if (f[i].value && f[i].defaultValue!==f[i].value) return true;
    var h = document.querySelectorAll("input[type=hidden][name=product_id]");
    for (var j=0;j<h.length;j++) if (h[j].value) return true;
    return false;
  }
  function tick(){
    fetch("{{ url_for('version') }}", {cache:"no-store"})
      .then(function(r){return r.text();})
      .then(function(v){
        live.style.color = "#86efac"; live.textContent = "● live";
        if (v !== cur && !busy()) { saveScroll(); location.reload(); }
      })
      .catch(function(){ live.style.color = "#fca5a5"; live.textContent = "● offline"; });
  }
  setInterval(tick, 3000);
})();
</script>
</body>
</html>
"""

INDEX_BODY = """
<div class="shop-dashboard-head">
  <div>
    <div style="font-size:11px;font-weight:800;letter-spacing:.09em;text-transform:uppercase;color:#64748b;margin-bottom:5px">Shop stock desk</div>
    <h1>Stock dashboard</h1>
    <p>Quick stock updates and branch balances.</p>
  </div>
</div>
<div class="shop-quick-actions">
  {% if can_open_block('stock_move') %}<a href="#stock-in-out">＋ Update stock</a>{% endif %}
  {% if can_open_page('history') %}<a class="secondary" href="{{ url_for('history') }}">View stock history</a>{% endif %}
  {% if can_open_page('products') %}<a class="secondary" href="{{ url_for('products_page') }}">＋ Products</a>{% endif %}
</div>
{% if can_open_block('stock_summary') %}<div class="shop-branch-grid">
  {% for b in branches %}
  {% if can_open_block('stock_move') %}<a class="shop-branch-card" href="#stock-in-out" data-branch-id="{{ b.id }}" aria-label="Open Stock In / Out for {{ b.name }}">{% else %}<div class="shop-branch-card">{% endif %}
    <div class="shop-branch-name">{{ b.name }}</div>
    <div class="shop-branch-number">{{ summary[b.id].units|fmt }}</div>
    <div class="shop-branch-caption">Total units available</div>
    <div class="shop-branch-totals">
      <div><span>Today IN</span><strong class="g">+{{ summary[b.id].tin|fmt }}</strong></div>
      <div><span>Today OUT</span><strong class="r">−{{ summary[b.id].tout|fmt }}</strong></div>
    </div>
    {% if summary[b.id].low %}<span class="shop-low-alert">⚠ {{ summary[b.id].low }} low-stock item(s)</span>{% endif %}
  {% if can_open_block('stock_move') %}</a>{% else %}</div>{% endif %}
  {% endfor %}
</div>{% endif %}

{% if can_open_block('stock_move') %}<div class="shop-movement-card" id="stock-in-out">
  <h2>Stock In / Out</h2>
  <p class="shop-movement-hint">Select a branch, tap products and enter quantities. Use the large buttons below to save.</p>
  {% if not products %}
    <p>No products yet. <a href="{{ url_for('products_page') }}">Add your products first.</a></p>
  {% else %}
  <form method="post" action="{{ url_for('move') }}" id="mform" enctype="multipart/form-data" novalidate>
    <div class="step">1 · Branch</div>
    <select name="branch_id" id="mb" style="width:100%">
      {% for b in branches %}<option value="{{ b.id }}" {{ 'selected' if b.id == sel_branch }}>{{ b.name }}</option>{% endfor %}
    </select>

    <div class="step">2 · Tap the products (tap again to add one more)</div>
    <input class="psearch" id="msearch" type="search" placeholder="Search product…" autocomplete="off">
    <div class="pgrid" id="mgrid"></div>
    <div class="cart">
      <div class="cartempty" id="mempty">Tap a product above to add it</div>
      <div id="mcart"></div>
    </div>

    <div class="step">Note (optional)</div>
    <input name="note" placeholder="Anything to remember" style="width:100%">
    <input type="hidden" name="kind" id="mkind" value="">

    <div id="reasonbox" class="stockout-extra" style="display:none">
      <div class="step" style="margin-top:0;color:#991b1b">Stock OUT remark <span style="color:#dc2626">* Required</span></div>
      <textarea name="reason" id="mreason" rows="3" maxlength="200" placeholder="Why is this stock going out? e.g. Damaged, expired, missing, given away…" style="width:100%"></textarea>
      <div class="step" style="color:#334155">Photo evidence <span class="hint">· Optional</span></div>
      <label class="stock-photo-picker" for="stockoutphoto">📷 Take a photo or choose an image</label>
      <input type="file" name="stockout_photo" id="stockoutphoto" accept="image/*" capture="environment" style="display:none">
      <div id="stockout-preview-wrap" style="display:none;margin-top:10px">
        <img id="stockout-preview" alt="Stock OUT photo preview" style="max-width:180px;max-height:180px;border-radius:10px;object-fit:cover;border:1px solid #cbd5e1">
        <button type="button" class="del" id="stockout-clear" style="display:block;margin-top:6px">Remove photo</button>
      </div>
      <small>The photo will be attached to each product entry in this Stock OUT operation.</small>
    </div>

    <div class="errbox" id="merr"></div>
    <div class="shop-movement-buttons">
      <button type="button" class="in" id="bin" data-kind="IN">＋ Stock IN</button>
      <button type="button" class="out" id="bout" data-kind="OUT">− Stock OUT</button>
    </div>
  </form>
  <script>
  (function(){
    var STOCK = {{ stockmap|tojson }}, UNIT = {{ unitmap|tojson }}, BR = {{ branchmap|tojson }},
        PRODUCTS = {{ productlist|tojson }};
    var mb=document.getElementById("mb"), err=document.getElementById("merr"),
        form=document.getElementById("mform"), kind=document.getElementById("mkind"),
        bin=document.getElementById("bin"), bout=document.getElementById("bout");
    function avail(pid){ var b=STOCK[mb.value]||{}; return b[pid]||0; }
    function showErr(m){ err.textContent=m; err.style.display="block"; }
    var pk;
    function summarize(){
      err.style.display="none";
      var n = pk ? pk.items().length : 0, tag = n>1 ? " ("+n+" products)" : "";
      bin.textContent = "+ Stock IN" + tag; bout.textContent = "− Stock OUT" + tag;
    }
    pk = ProductPicker({
      grid: document.getElementById("mgrid"), search: document.getElementById("msearch"),
      cart: document.getElementById("mcart"), empty: document.getElementById("mempty"),
      products: PRODUCTS, units: UNIT, avail: avail, allowZero: true,
      notify: showErr, onChange: summarize
    });
    mb.addEventListener("change", function(){ pk.refresh(); });
    var photoInput=document.getElementById("stockoutphoto"), previewWrap=document.getElementById("stockout-preview-wrap"), preview=document.getElementById("stockout-preview");
    photoInput.addEventListener("change",function(){
      var f=this.files && this.files[0];
      if(!f){previewWrap.style.display="none";return;}
      if(!f.type || f.type.indexOf("image/")!==0){showErr("Please choose an image file.");this.value="";return;}
      if(f.size>8*1024*1024){showErr("Photo must be 8 MB or smaller.");this.value="";return;}
      preview.src=URL.createObjectURL(f);previewWrap.style.display="block";
    });
    document.getElementById("stockout-clear").addEventListener("click",function(){photoInput.value="";preview.removeAttribute("src");previewWrap.style.display="none";});
    document.querySelectorAll(".shop-branch-card[data-branch-id]").forEach(function(card){
      card.addEventListener("click", function(){
        var branchId = card.getAttribute("data-branch-id");
        if (mb.value !== branchId) {
          mb.value = branchId;
          mb.dispatchEvent(new Event("change"));
        }
        var section = document.getElementById("stock-in-out");
        if (section) section.scrollIntoView({behavior:"smooth", block:"start"});
      });
    });
    [bin, bout].forEach(function(b){
      b.onclick=function(){
        var k=b.getAttribute("data-kind"), items=pk.items(), msg="";
        if(!items.length) msg="Tap at least one product first.";
        items.forEach(function(it){
          if(!msg && !(it.qty>0)) msg="Enter how many "+it.name+", or remove it.";
          else if(!msg && k==="OUT" && it.qty>avail(it.pid))
            msg="Not enough "+it.name+" in "+BR[mb.value]+". Only "+avail(it.pid)+" "+(UNIT[it.pid]||"")+" in stock.";
        });
        if(msg){ showErr(msg); return; }
        if(k==="OUT"){
          var rb=document.getElementById("reasonbox"), rt=document.getElementById("mreason");
          rb.style.display="block";
          if(!rt.value.trim()){
            showErr("Please enter the required Stock OUT remark before submitting.");
            rt.focus(); rb.scrollIntoView({behavior:"smooth", block:"center"});
            return;
          }
        } else {
          document.getElementById("reasonbox").style.display="none";
          document.getElementById("mreason").value="";
          document.getElementById("stockoutphoto").value="";
          preview.removeAttribute("src"); previewWrap.style.display="none";
        }
        kind.value=k; bin.disabled=true; bout.disabled=true; form.submit();
      };
    });
    summarize();
  })();
  </script>
  {% endif %}
</div>{% endif %}

{% if stock_warnings and can_open_block('stock_warnings') %}
<div class="card" style="border-left:5px solid #dc2626;background:#fff7ed">
  <h2>⚠ Stock / Loyverse warnings ({{ stock_warnings|length }})</h2>
  {% for w in stock_warnings %}<p style="margin:8px 0;padding:8px;background:#ffedd5;border-radius:6px"><strong>{{ 'Product not in app list' if w.kind == 'unmatched' else 'Sale exceeded available stock' }}: {{ w.item_name }}</strong><br>{{ w.detail }} <small>· {{ w.created_at }}</small></p>{% endfor %}
  <small>Repeated alerts for the same product are grouped into one active warning. Open Loyverse to match or dismiss unmatched products.</small>
</div>
{% endif %}
{% if can_open_block('stock_by_branch') %}<div class="card">
  <h2>Stock by Branch</h2>
  <form method="get" class="row" style="margin-bottom:10px">
    <input name="q" value="{{ q }}" placeholder="Search product name">
    <button>Search</button>
  </form>
  <div class="scroll">
  <table class="sbb">
    <tr>
      <th>Product</th>
      {% for b in branches %}<th class="n">{{ b.name }}</th>{% endfor %}
      <th class="n">Total</th>
    </tr>
    {% for r in rows %}
    <tr>
      <td>{{ r.p.name }}<br><small>{{ r.p.unit }}</small></td>
      {% for c in r.cells %}
        <td class="n {{ 'low' if r.p.low_limit > 0 and c <= r.p.low_limit else ('zero' if c == 0) }}">{{ c|fmt }}</td>
      {% endfor %}
      <td class="n"><strong>{{ r.total|fmt }}</strong></td>
    </tr>
    {% else %}
    <tr><td colspan="{{ branches|length + 2 }}">No products found.</td></tr>
    {% endfor %}
  </table>
  </div>
  <small>Red = at or below the product's low-stock level.</small>
</div>{% endif %}
"""

HISTORY_BODY = """
{% if can_open_block('history_filter') %}<div class="card">
  <h2>Find or download history</h2>
  <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
    {% for c in chips %}<a href="{{ c.url }}" style="padding:6px 12px;border-radius:99px;font-size:13px;text-decoration:none;{{ 'background:#1f2937;color:#fff' if c.on else 'background:#e5e7eb;color:#374151' }}">{{ c.label }}</a>{% endfor %}
  </div>
  <form method="get" class="row">
    <select name="branch_id">
      <option value="">All branches</option>
      {% for b in branches %}<option value="{{ b.id }}" {{ 'selected' if f.branch_id == b.id|string }}>{{ b.name }}</option>{% endfor %}
    </select>
    <select name="product_id">
      <option value="">All products</option>
      {% for p in products %}<option value="{{ p.id }}" {{ 'selected' if f.product_id == p.id|string }}>{{ p.name }}</option>{% endfor %}
    </select>
    <div><label style="font-size:12px;color:#6b7280">From date</label><input type="date" name="from" value="{{ f['from'] }}" style="width:100%"></div>
    <div><label style="font-size:12px;color:#6b7280">To date</label><input type="date" name="to" value="{{ f.to }}" style="width:100%"></div>
    <button>Show</button>
    <button type="submit" formaction="{{ url_for('export_csv') }}" class="in">⬇ Download CSV</button>
  </form>
  <small>The download has every entry in the chosen dates and branch, not just the {{ shown }} shown below. Open it in Excel or Google Sheets.</small>
</div>{% endif %}
{% if can_open_block('history_transactions') %}<div class="card">
  <h2>Transactions ({{ total_rows }}{{ ' · showing latest 300' if total_rows > 300 }})</h2>
  <p style="margin:0 0 10px">Total IN: <span class="g">+{{ tot_in|fmt }}</span> &nbsp; Total OUT: <span class="r">−{{ tot_out|fmt }}</span></p>
  <div class="scroll">
  <table>
    <tr><th>Time</th><th>Branch</th><th>Product</th><th>Type</th><th class="n">Qty</th><th>Remark / Note</th><th>Photo</th></tr>
    {% for t in rows %}
    <tr>
      <td>{{ t.created_at }}</td>
      <td>{{ t.branch }}</td>
      <td>{{ t.name }}</td>
      <td><span class="tag {{ t.kind }}">{{ t.kind }}</span></td>
      <td class="n">{{ t.qty|fmt }} {{ t.unit }}</td>
      <td>{{ t.note or '' }}</td>
      <td>{% if t.photo %}<a href="{{ url_for('product_photo_file', filename=t.photo) }}" target="_blank" rel="noopener"><img src="{{ url_for('product_photo_file', filename=t.photo) }}" alt="Stock OUT evidence" style="width:54px;height:54px;object-fit:cover;border-radius:7px"></a>{% else %}<small>—</small>{% endif %}</td>
    </tr>
    {% else %}
    <tr><td colspan="7">No transactions found.</td></tr>
    {% endfor %}
  </table>
  </div>
</div>{% endif %}
"""

PRODUCTS_BODY = """
{% if can_open_block('products_add') %}<div class="card">
  <h2>Add Product</h2>
  <form method="post" action="{{ url_for('add_product') }}" class="row" enctype="multipart/form-data">
    <input name="name" placeholder="Product name" required>
    <label class="photobtn" id="addphotolbl">📷 Add photo (optional)<input type="file" name="photo" accept="image/*" class="pphoto" style="display:none"></label>
    <input name="unit" placeholder="Unit (pcs, kg…)" value="pcs">
    <input name="low_limit" type="number" step="any" min="0" placeholder="Low-stock level" value="0">
    <select name="branch_id">
      {% for b in branches %}<option value="{{ b.id }}">{{ b.name }}</option>{% endfor %}
    </select>
    <input name="qty" type="number" step="any" min="0" placeholder="Opening stock (optional)">
    <button>Add product</button>
  </form>
</div>{% endif %}

{% if can_open_block('products_bulk') %}<div class="card">
  <h2>Add Many Products at Once</h2>
  <p style="margin:0 0 8px;font-size:13px;color:#4b5563">One per line: <code>Name, Opening stock, Unit, Low-stock level</code> (everything after the name is optional)</p>
  <form method="post" action="{{ url_for('bulk_add') }}">
    <textarea name="lines" rows="6" style="width:100%" placeholder="sald,23,pcs,10"></textarea>
    <div class="row" style="margin-top:8px;align-items:center">
      <label style="flex:0 0 auto">Opening stock goes to:</label>
      <select name="branch_id">
        {% for b in branches %}<option value="{{ b.id }}">{{ b.name }}</option>{% endfor %}
      </select>
      <button>Add all</button>
    </div>
  </form>
</div>{% endif %}

{% if can_open_block('products_list') %}<div class="card">
  <h2>All Products ({{ products|length }})</h2>
  <div class="scroll">
  <table>
    <tr><th>Photo</th><th>Name</th><th>Unit</th><th class="n">Low level</th><th></th></tr>
    {% for p in products %}
    <tr>
      <td>{% if p.photo %}<a href="{{ url_for('product_photo_file', filename=p.photo) }}" target="_blank"><img class="pthumb" loading="lazy" alt="" src="{{ url_for('product_photo_file', filename=p.photo) }}"></a>{% else %}<div class="pnone">📦</div>{% endif %}</td>
      <td>{{ p.name }}</td><td>{{ p.unit }}</td><td class="n">{{ p.low_limit|fmt }}</td>
      <td>
        <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
          <form method="post" action="{{ url_for('product_photo_set', pid=p.id) }}" enctype="multipart/form-data">
            <label class="photobtn">📷 {{ 'Change photo' if p.photo else 'Add photo' }}<input type="file" name="photo" accept="image/*" class="pphoto" data-auto="1" style="display:none"></label>
          </form>
          {% if p.photo %}<form method="post" action="{{ url_for('product_photo_set', pid=p.id) }}" onsubmit="return confirm('Remove the photo of {{ p.name|e }}?')"><input type="hidden" name="remove" value="1"><button class="del">Remove photo</button></form>{% endif %}
          <form method="post" action="{{ url_for('delete_product', pid=p.id) }}"
                onsubmit="return confirm('Delete {{ p.name|e }} and all its history?')">
            <button class="del">Delete</button>
          </form>
        </div>
      </td>
    </tr>
    {% else %}
    <tr><td colspan="5">No products yet.</td></tr>
    {% endfor %}
  </table>
  </div>
</div>{% endif %}
<script>
(function(){
  // Shrink big phone photos in the browser before uploading (max 900px), so uploads stay small and fast.
  document.querySelectorAll("input.pphoto").forEach(function(inp){
    var form = inp.form, pending = null;
    function shrink(file){
      return new Promise(function(done){
        if(!file || !file.type || file.type.indexOf("image/")!==0 || file.type==="image/gif"){ done(file); return; }
        var url = URL.createObjectURL(file), img = new Image();
        img.onload = function(){
          var m = 900, w = img.width, h = img.height, k = Math.min(1, m/Math.max(w,h));
          var c = document.createElement("canvas"); c.width = Math.round(w*k); c.height = Math.round(h*k);
          c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
          c.toBlob(function(b){ URL.revokeObjectURL(url);
            done(b ? new File([b], "photo.jpg", {type:"image/jpeg"}) : file); }, "image/jpeg", 0.82);
        };
        img.onerror = function(){ URL.revokeObjectURL(url); done(file); };
        img.src = url;
      });
    }
    inp.addEventListener("change", function(){
      var f = inp.files && inp.files[0];
      if(!f) return;
      var lbl = inp.parentNode;
      if(lbl && lbl.id === "addphotolbl") lbl.firstChild.nodeValue = "📷 " + f.name.slice(0,22) + " ✓ ";
      pending = shrink(f).then(function(nf){
        if(nf !== f && window.DataTransfer){ var dt = new DataTransfer(); dt.items.add(nf); inp.files = dt.files; }
        pending = null;
        if(inp.getAttribute("data-auto")) form.submit();
      });
    });
    form.addEventListener("submit", function(e){
      if(pending){ e.preventDefault(); pending.then(function(){ form.submit(); }); }
    });
  });
})();
</script>
"""

BRANCHES_BODY = """
{% if can_open_block('branches_add') %}<div class="card">
  <h2>Add a branch</h2>
  <form method="post" action="{{ url_for('add_branch') }}" class="row">
    <input name="name" placeholder="New branch name" maxlength="40" required>
    <button class="in">Add branch</button>
  </form>
  <small>You have {{ branches|length }} branch(es). A new branch shows up everywhere straight away: stock, sales, transfers and history.</small>
</div>{% endif %}
{% if can_open_block('branches_manage') %}<div class="card">
  <h2>Rename or remove branches</h2>
  <form method="post" action="{{ url_for('branches_page') }}" class="row" style="flex-direction:column">
    {% for b in branches %}
      <div style="display:flex;gap:8px;align-items:center">
        <input name="name_{{ b.id }}" value="{{ b.name }}" maxlength="40" required style="flex:1;min-width:0">
        <button class="del" type="submit" formaction="{{ url_for('delete_branch', bid=b.id) }}"
                onclick="return confirm('Remove this branch? This only works if it has never had any stock or sales.')"
                style="flex:0 0 auto;min-width:0;padding:9px 12px">Remove</button>
      </div>
    {% endfor %}
    <button>Save names</button>
  </form>
  <small>A branch that has any stock history can't be removed. Rename it instead.</small>
</div>{% endif %}
"""


def data_version():
    db = get_db()
    a = db.execute("SELECT COALESCE(MAX(id),0), COUNT(*) FROM transactions").fetchone()
    b = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    names = "|".join(r["name"] for r in all_branches())
    sl = db.execute("SELECT COALESCE(MAX(id),0), COUNT(*), COUNT(settled_at) FROM sales").fetchone()
    tr = db.execute("SELECT COALESCE(MAX(id),0), COUNT(*) FROM transfers").fetchone()
    lv = db.execute("SELECT COUNT(*), (SELECT COUNT(*) FROM lv_issues WHERE resolved=0) FROM lv_receipts").fetchone()
    return f"{a[0]}-{a[1]}-{b}-{sum(map(ord, names))}-{sl[0]}-{sl[1]}-{sl[2]}-{tr[0]}-{tr[1]}-{lv[0]}-{lv[1]}"


@app.route("/version")
def version():
    return Response(data_version(), mimetype="text/plain",
                    headers={"Cache-Control": "no-store"})


def photo_url(name):
    return url_for("product_photo_file", filename=name) if name else ""


def picker_products(products):
    return [{"id": p["id"], "name": p["name"], "photo": photo_url(p["photo"])} for p in products]


def picker_data():
    db = get_db()
    stockmap = {}
    for r in db.execute("SELECT * FROM stock"):
        stockmap.setdefault(str(r["branch_id"]), {})[str(r["product_id"])] = fmt(r["qty"])
    products = all_products()
    return dict(
        stockmap=stockmap,
        unitmap={str(p["id"]): p["unit"] for p in products},
        productlist=picker_products(products),
        branchmap={str(b["id"]): b["name"] for b in all_branches()},
    )


def render(body_tpl, **ctx):
    body = render_template_string(body_tpl, **ctx,
                                  can_open_page=can_open_page,
                                  can_open_block=can_open_block)
    return render_template_string(PAGE, body=body, version=data_version(),
                                  can_open_page=can_open_page, can_open_block=can_open_block)


# ---------- Routes ----------
@app.route("/")
def index():
    db = get_db()
    q = request.args.get("q", "").strip()
    branches = all_branches()
    sel_branch = request.args.get("branch", type=int) or (branches[0]["id"] if branches else None)

    if q:
        like = f"%{q}%"
        products = db.execute(
            "SELECT * FROM products WHERE name LIKE ? ORDER BY name",
            (like,)).fetchall()
    else:
        products = all_products()

    stock = {(r["product_id"], r["branch_id"]): r["qty"]
             for r in db.execute("SELECT * FROM stock")}
    rows = []
    for p in products:
        cells = [stock.get((p["id"], b["id"]), 0) for b in branches]
        rows.append({"p": p, "cells": cells, "total": sum(cells)})

    # per-branch summary cards
    summary = {b["id"]: {"units": 0, "tin": 0, "tout": 0, "low": 0} for b in branches}
    for p in all_products():
        for b in branches:
            qty = stock.get((p["id"], b["id"]), 0)
            summary[b["id"]]["units"] += qty
            if p["low_limit"] > 0 and qty <= p["low_limit"]:
                summary[b["id"]]["low"] += 1
    today = datetime.now().strftime("%Y-%m-%d")
    for r in db.execute(
        """SELECT branch_id, kind, SUM(qty) s FROM transactions
           WHERE created_at >= ? GROUP BY branch_id, kind""", (today + " 00:00:00",)):
        key = "tin" if r["kind"] == "IN" else "tout"
        if r["branch_id"] in summary:
            summary[r["branch_id"]][key] = r["s"]

    stock_warnings = db.execute("SELECT * FROM lv_issues WHERE resolved=0 ORDER BY id DESC LIMIT 100").fetchall()
    return render(INDEX_BODY, branches=branches, products=all_products(), rows=rows,
                  q=q, summary=summary, sel_branch=sel_branch, stock_warnings=stock_warnings, **picker_data())


@app.route("/move", methods=["POST"])
def move():
    db = get_db()
    kind = request.form.get("kind")
    try:
        bid = int(request.form["branch_id"])
    except (ValueError, KeyError):
        flash("Choose a branch.", "error")
        return redirect(url_for("index"))
    note = request.form.get("note", "").strip()
    if kind not in ("IN", "OUT"):
        flash("Choose Stock IN or Stock OUT.", "error")
        return redirect(url_for("index", branch=bid))
    if kind == "OUT":
        reason = " ".join(request.form.get("reason", "").split())[:200]
        if not reason:
            flash("Please enter the reason for Stock OUT. Nothing was saved.", "error")
            return redirect(url_for("index", branch=bid))
        note = f"Reason: {reason}" + (f" | {note}" if note else "")

    photo_upload = request.files.get("stockout_photo") if kind == "OUT" else None
    lines = {}
    for ps, qs in zip(request.form.getlist("product_id"), request.form.getlist("qty")):
        if not ps.strip() and not qs.strip():
            continue
        try:
            pid, q = int(ps), float(qs)
        except ValueError:
            flash("Choose a product and a quantity on every line.", "error")
            return redirect(url_for("index", branch=bid))
        if q <= 0:
            flash("Quantities must be more than zero.", "error")
            return redirect(url_for("index", branch=bid))
        lines[pid] = lines.get(pid, 0) + q
    if not lines:
        flash("Tap at least one product and enter how many.", "error")
        return redirect(url_for("index", branch=bid))

    branch = db.execute("SELECT * FROM branches WHERE id=?", (bid,)).fetchone()
    products = {}
    for pid in lines:
        pr = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not pr:
            flash("A product was not found. Refresh the page and try again.", "error")
            return redirect(url_for("index", branch=bid))
        products[pid] = pr
    if not branch:
        flash("Branch not found.", "error")
        return redirect(url_for("index"))

    db.execute("BEGIN IMMEDIATE")
    if kind == "OUT":
        short = []
        for pid, q in lines.items():
            row = db.execute("SELECT qty FROM stock WHERE product_id=? AND branch_id=?", (pid, bid)).fetchone()
            have = row["qty"] if row else 0
            if q > have:
                short.append(f"{products[pid]['name']} (only {fmt(have)} {products[pid]['unit']} left)")
        if short:
            db.rollback()
            flash(f"Not enough stock in {branch['name']} for: " + ", ".join(short) + ". Nothing was saved.", "error")
            return redirect(url_for("index", branch=bid))

    stockout_photo_name = None
    if kind == "OUT" and photo_upload and photo_upload.filename:
        stockout_photo_name, photo_error = save_product_photo(photo_upload)
        if photo_error:
            db.rollback()
            flash(photo_error + " Stock was not changed.", "error")
            return redirect(url_for("index", branch=bid))

    stamp = now()
    for pid, q in lines.items():
        delta = q if kind == "IN" else -q
        db.execute(
            """INSERT INTO stock (product_id, branch_id, qty) VALUES (?,?,?)
               ON CONFLICT(product_id, branch_id) DO UPDATE SET qty = qty + excluded.qty""",
            (pid, bid, delta))
        db.execute(
            "INSERT INTO transactions (product_id, branch_id, kind, qty, note, created_at, photo) "
            "VALUES (?,?,?,?,?,?,?)", (pid, bid, kind, q, note, stamp, stockout_photo_name))
    db.commit()
    items = ", ".join(f"{products[pid]['name']} {fmt(q)} {products[pid]['unit']}" for pid, q in lines.items())
    flash(f"{branch['name']}: stock {kind} – {items}.")
    return redirect(url_for("index", branch=bid))


@app.route("/products")
def products_page():
    return render(PRODUCTS_BODY, products=all_products(), branches=all_branches())


def save_product_photo(upload):
    """Validate and store an uploaded photo. Returns (filename, error)."""
    if not upload or not upload.filename:
        return None, None
    data = upload.read(MAX_PHOTO_BYTES + 1)
    if not data:
        return None, None
    if len(data) > MAX_PHOTO_BYTES:
        return None, "The photo is too big (maximum 8 MB)."
    if data[:3] == b"\xff\xd8\xff":
        ext = ".jpg"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = ".png"
    elif data[:4] == b"GIF8":
        ext = ".gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = ".webp"
    else:
        return None, "That file is not a photo. Use a JPG, PNG, WEBP or GIF picture."
    os.makedirs(PHOTO_DIR, exist_ok=True)
    name = secrets.token_hex(12) + ext
    with open(os.path.join(PHOTO_DIR, name), "wb") as f:
        f.write(data)
    return name, None


def delete_photo_file(name):
    if name and os.path.basename(name) == name:
        try:
            os.remove(os.path.join(PHOTO_DIR, name))
        except OSError:
            pass


@app.route("/product-photo/<path:filename>")
def product_photo_file(filename):
    resp = send_from_directory(PHOTO_DIR, filename, max_age=31536000)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.route("/products/photo/<int:pid>", methods=["POST"])
def product_photo_set(pid):
    db = get_db()
    pr = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not pr:
        flash("Product not found.", "error")
        return redirect(url_for("products_page"))
    if request.form.get("remove"):
        delete_photo_file(pr["photo"])
        db.execute("UPDATE products SET photo=NULL WHERE id=?", (pid,))
        db.commit()
        flash(f"Removed the photo of {pr['name']}.")
        return redirect(url_for("products_page"))
    name, err = save_product_photo(request.files.get("photo"))
    if err or not name:
        flash(err or "Choose a photo first.", "error")
        return redirect(url_for("products_page"))
    delete_photo_file(pr["photo"])
    db.execute("UPDATE products SET photo=? WHERE id=?", (name, pid))
    db.commit()
    flash(f"Saved the photo of {pr['name']}.")
    return redirect(url_for("products_page"))


@app.route("/products/add", methods=["POST"])
def add_product():
    db = get_db()
    name = request.form["name"].strip()
    unit = request.form.get("unit", "pcs").strip() or "pcs"
    try:
        low = float(request.form.get("low_limit") or 0)
        qty = float(request.form.get("qty") or 0)
        bid = int(request.form.get("branch_id") or 0)
    except ValueError:
        flash("Numbers only for quantity fields.", "error")
        return redirect(url_for("products_page"))
    if not name or low < 0 or qty < 0:
        flash("Please fill the form correctly.", "error")
        return redirect(url_for("products_page"))
    try:
        cur = db.execute("INSERT INTO products (name, unit, low_limit) VALUES (?,?,?)",
                         (name, unit, low))
        if qty > 0 and bid:
            db.execute("INSERT INTO stock (product_id, branch_id, qty) VALUES (?,?,?)",
                       (cur.lastrowid, bid, qty))
            db.execute(
                "INSERT INTO transactions (product_id, branch_id, kind, qty, note, created_at) "
                "VALUES (?,?,?,?,?,?)", (cur.lastrowid, bid, "IN", qty, "Opening stock", now()))
        photo, perr = save_product_photo(request.files.get("photo"))
        if photo:
            db.execute("UPDATE products SET photo=? WHERE id=?", (photo, cur.lastrowid))
        db.commit()
        flash(f"Added {name}." + (" Photo saved." if photo else ""))
        if perr:
            flash(f"{name} was added, but the photo was not saved: {perr}", "error")
    except sqlite3.IntegrityError:
        flash(f"'{name}' already exists.", "error")
    return redirect(url_for("products_page"))


@app.route("/products/bulk", methods=["POST"])
def bulk_add():
    db = get_db()
    added, skipped = 0, []
    try:
        bid = int(request.form.get("branch_id") or 0)
    except ValueError:
        bid = 0
    for line in request.form.get("lines", "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if not parts[0]:
            if line.strip():
                skipped.append(line.strip())
            continue
        name = parts[0]
        try:
            qty = float(parts[1]) if len(parts) > 1 and parts[1] else 0
            unit = parts[2] if len(parts) > 2 and parts[2] else "pcs"
            low = float(parts[3]) if len(parts) > 3 and parts[3] else 0
            if qty < 0 or low < 0:
                raise ValueError
            cur = db.execute("INSERT INTO products (name, unit, low_limit) VALUES (?,?,?)",
                             (name, unit, low))
            if qty > 0 and bid:
                db.execute("INSERT INTO stock (product_id, branch_id, qty) VALUES (?,?,?)",
                           (cur.lastrowid, bid, qty))
                db.execute(
                    "INSERT INTO transactions (product_id, branch_id, kind, qty, note, created_at) "
                    "VALUES (?,?,?,?,?,?)", (cur.lastrowid, bid, "IN", qty, "Opening stock", now()))
            added += 1
        except (ValueError, sqlite3.IntegrityError):
            skipped.append(line.strip())
    db.commit()
    flash(f"Added {added} product(s)." + (f" Skipped: {'; '.join(skipped)}" if skipped else ""),
          "error" if skipped else "ok")
    return redirect(url_for("products_page"))


@app.route("/products/delete/<int:pid>", methods=["POST"])
def delete_product(pid):
    db = get_db()
    old = db.execute("SELECT photo FROM products WHERE id=?", (pid,)).fetchone()
    db.execute("DELETE FROM products WHERE id=?", (pid,))
    db.commit()
    if old:
        delete_photo_file(old["photo"])
    flash("Product deleted.")
    return redirect(url_for("products_page"))


@app.route("/branches/add", methods=["POST"])
def add_branch():
    db = get_db()
    name = " ".join(request.form.get("name", "").split())[:40]
    if not name:
        flash("Enter a branch name.", "error")
    elif len(all_branches()) >= 30:
        flash("That is the maximum number of branches (30).", "error")
    elif db.execute("SELECT 1 FROM branches WHERE lower(name)=lower(?)", (name,)).fetchone():
        flash(f"There is already a branch called “{name}”.", "error")
    else:
        db.execute("INSERT INTO branches (name) VALUES (?)", (name,))
        db.commit()
        flash(f"Branch “{name}” added.")
    return redirect(url_for("branches_page"))


@app.route("/branches/delete/<int:bid>", methods=["POST"])
def delete_branch(bid):
    db = get_db()
    b = db.execute("SELECT * FROM branches WHERE id=?", (bid,)).fetchone()
    if not b:
        flash("Branch not found.", "error")
    elif len(all_branches()) <= 1:
        flash("You need at least one branch.", "error")
    else:
        used = db.execute(
            """SELECT (SELECT COUNT(*) FROM transactions WHERE branch_id=?) +
                      (SELECT COUNT(*) FROM sales WHERE branch_id=?) +
                      (SELECT COUNT(*) FROM transfers WHERE from_branch_id=? OR to_branch_id=?) +
                      (SELECT COUNT(*) FROM stock WHERE branch_id=? AND qty != 0) +
                      (SELECT COUNT(*) FROM lv_store_map WHERE branch_id=?)""",
            (bid, bid, bid, bid, bid, bid)).fetchone()[0]
        if used:
            flash(f"“{b['name']}” has stock or history, so it can't be removed. Rename it instead.", "error")
        else:
            db.execute("DELETE FROM stock WHERE branch_id=?", (bid,))
            db.execute("DELETE FROM branches WHERE id=?", (bid,))
            db.commit()
            flash(f"Branch “{b['name']}” removed.")
    return redirect(url_for("branches_page"))


@app.route("/branches", methods=["GET", "POST"])
def branches_page():
    db = get_db()
    if request.method == "POST":
        for b in all_branches():
            name = request.form.get(f"name_{b['id']}", "").strip()
            if name:
                db.execute("UPDATE branches SET name=? WHERE id=?", (name, b["id"]))
        db.commit()
        flash("Branch names saved.")
        return redirect(url_for("branches_page"))
    return render(BRANCHES_BODY, branches=all_branches())


SALES_BODY = """
<style>
  .step{font-size:12px;font-weight:700;color:#6b7280;text-transform:uppercase;letter-spacing:.04em;margin:14px 0 6px}
  .step:first-of-type{margin-top:0}
  .paygrid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}
  .paygrid input{position:absolute;opacity:0;pointer-events:none}
  .paygrid label{display:block;border:2px solid #d1d5db;border-radius:10px;padding:14px 4px;text-align:center;
                 cursor:pointer;font-weight:600;font-size:15px;background:#fff;user-select:none}
  .paygrid small{display:block;font-weight:400;font-size:11px;margin-top:2px}
  .paygrid input:focus-visible + label{outline:2px solid #2563eb}
  #pFREE:checked + label{border-color:#ca8a04;background:#fef9c3;color:#713f12}
  #pCREDIT:checked + label{border-color:#dc2626;background:#fee2e2;color:#991b1b}
  .qtyrow{display:flex;gap:8px;align-items:stretch}
  .qtyrow button{flex:0 0 52px;font-size:22px;background:#e5e7eb;color:#111}
  .qtyrow input{flex:1;text-align:center;font-size:20px;font-weight:600;min-width:0}
  .hint{font-size:13px;margin-top:6px;color:#4b5563}
  .big{width:100%;padding:14px;font-size:17px;font-weight:600;margin-top:14px;background:#16a34a}
  .chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
  .chip{padding:6px 12px;border-radius:99px;background:#e5e7eb;color:#374151;text-decoration:none;font-size:13px}
  .chip.on{background:#1f2937;color:#fff}
  details summary{cursor:pointer;color:#2563eb;font-size:14px;margin-bottom:8px}
  .two{display:grid;grid-template-columns:1fr 1fr;gap:8px}
  .two>*{min-width:0}
  .mini{padding:6px 10px;font-size:13px}
  .line{border:1px solid #d1d5db;border-radius:10px;padding:10px;margin-bottom:8px;background:#fafafa}
  .line select{width:100%;margin-bottom:8px}
  .line .qtyrow .rm{flex:0 0 44px;background:#fee2e2;color:#991b1b;font-size:16px}
  .addbtn{width:100%;background:#fff;color:#2563eb;border:2px dashed #93c5fd;font-weight:600;padding:12px}
  .errbox{background:#fee2e2;color:#991b1b;padding:10px;border-radius:8px;margin-top:10px;display:none;font-size:14px}
</style>

{% if can_open_block('sales_record') %}<div class="card">
  <h2>Record a sale</h2>
  {% if not products %}
    <p>No products yet. <a href="{{ url_for('products_page') }}">Add your products first.</a></p>
  {% else %}
  <form method="post" action="{{ url_for('add_sale') }}" id="saleform" novalidate>
    <div class="step">1 · Which branch is selling</div>
    <select name="branch_id" id="sb" style="width:100%">
      {% for b in branches %}<option value="{{ b.id }}" {{ 'selected' if b.id == sel_branch }}>{{ b.name }}</option>{% endfor %}
    </select>

    <div class="step">2 · Tap the products sold (tap again to add one more)</div>
    <input class="psearch" id="ssearch" type="search" placeholder="Search product…" autocomplete="off">
    <div class="pgrid" id="sgrid"></div>
    <div class="cart">
      <div class="cartempty" id="sempty">Tap a product above to add it to this sale</div>
      <div id="scart"></div>
    </div>

    <div class="step">3 · How was it given</div>
    <div class="paygrid">
      <div><input type="radio" name="pay_type" id="pFREE" value="FREE" {{ 'checked' if sel_pay == 'FREE' or sel_pay not in ('CREDIT',) }}><label for="pFREE">Free<small>no payment</small></label></div>
      <div><input type="radio" name="pay_type" id="pCREDIT" value="CREDIT" {{ 'checked' if sel_pay == 'CREDIT' }}><label for="pCREDIT">Credit<small>pays later</small></label></div>
    </div>

    <div id="custbox" style="display:none">
      <div class="step">4 · Who is taking it on credit?</div>
      <input name="customer" id="scust" list="custlist" placeholder="Customer name" style="width:100%" autocomplete="off">
      <datalist id="custlist">{% for c in customers %}<option value="{{ c }}">{% endfor %}</datalist>
    </div>

    <div class="step">Note (optional)</div>
    <input name="note" placeholder="Anything to remember" style="width:100%">

    <div class="errbox" id="saleerr"></div>
    <button class="big" id="savebtn">Record paid sale</button>
  </form>
  <script>
  (function(){
    var STOCK = {{ stockmap|tojson }}, UNIT = {{ unitmap|tojson }}, BR = {{ branchmap|tojson }},
        PRODUCTS = {{ productlist|tojson }};
    var sb=document.getElementById("sb"), cust=document.getElementById("scust"), box=document.getElementById("custbox"),
        form=document.getElementById("saleform"), btn=document.getElementById("savebtn"), err=document.getElementById("saleerr");
    function pay(){ return form.querySelector("input[name=pay_type]:checked").value; }
    function avail(pid){ var b=STOCK[sb.value]||{}; return b[pid]||0; }
    function showErr(m){ err.textContent=m; err.style.display="block"; }
    var pk;
    function summarize(){
      err.style.display="none";
      var n = pk ? pk.items().length : 0, c=pay()==="CREDIT", f=pay()==="FREE";
      btn.style.background = c ? "#dc2626" : (f ? "#eab308" : "#16a34a");
      btn.style.color = f ? "#111" : "#fff";
      btn.textContent = "Record " + (c ? "credit" : f ? "free" : "paid") + " sale" + (n>1 ? " ("+n+" products)" : "");
      box.style.display = c ? "block" : "none";
    }
    pk = ProductPicker({
      grid: document.getElementById("sgrid"), search: document.getElementById("ssearch"),
      cart: document.getElementById("scart"), empty: document.getElementById("sempty"),
      products: PRODUCTS, units: UNIT, avail: avail, allowZero: false,
      notify: showErr, onChange: summarize,
      rowHint: function(pid){ return "In stock at "+BR[sb.value]+": "+avail(pid)+" "+(UNIT[pid]||""); }
    });
    sb.addEventListener("change", function(){ pk.refresh(); });
    form.querySelectorAll("input[name=pay_type]").forEach(function(r){ r.addEventListener("change", summarize); });
    form.addEventListener("submit", function(e){
      var msg="", items=pk.items();
      if(!items.length) msg="Tap at least one product.";
      items.forEach(function(it){
        if(!msg && !(it.qty>0)) msg="Enter a quantity for "+it.name+", or remove it.";
        else if(!msg && it.qty>avail(it.pid)) msg="Not enough "+it.name+" in "+BR[sb.value]+". Only "+avail(it.pid)+" "+(UNIT[it.pid]||"")+" in stock.";
      });
      if(!msg && pay()==="CREDIT" && !cust.value.trim()) msg="Enter the customer name for a credit sale.";
      if(msg){ e.preventDefault(); showErr(msg); return; }
      btn.disabled=true; btn.textContent="Saving…";
    });
    summarize();
  })();
  </script>
  {% endif %}
</div>{% endif %}

{% if can_open_block('sales_list') %}<div class="grid" style="margin-bottom:16px">
  <div class="bcard"><b>Credit – to collect</b><span class="r" style="font-size:22px">{{ t.pend_qty|fmt }}</span><span>items · {{ t.pend_n }} unpaid sale(s)</span></div>
  <div class="bcard"><b>Credit – collected</b><span class="g" style="font-size:22px">{{ t.coll_qty|fmt }}</span><span>items · {{ t.coll_n }} sale(s)</span></div>
  <div class="bcard"><b>Free</b><span style="font-size:22px">{{ t.free_qty|fmt }}</span><span>items · {{ t.free_n }} sale(s)</span></div>
</div>
<p style="margin:-6px 0 14px"><small>Totals are for the sales listed below (they follow the filter).</small></p>{% endif %}

{% if debtors and can_open_block('sales_credit') %}
<div class="card">
  <h2>Customers who still owe items</h2>
  <div class="scroll"><table>
    <tr><th>Customer</th><th class="n">Unpaid sales</th><th class="n">Items</th><th></th></tr>
    {% for d in debtors %}
    <tr>
      <td>{{ d.customer }}</td><td class="n">{{ d.n }}</td><td class="n r">{{ d.qty|fmt }}</td>
      <td class="n">
        <form method="post" action="{{ url_for('settle_customer') }}" style="display:inline"
              onsubmit="return confirm('Mark all unpaid credit for {{ d.customer|e }} as paid?')">
          <input type="hidden" name="customer" value="{{ d.customer }}">
          <button class="mini" style="background:#16a34a">Mark all paid</button>
        </form>
      </td>
    </tr>
    {% endfor %}
  </table></div>
</div>
{% endif %}

{% if can_open_block('sales_list') %}<div class="card">
  <h2>Sales list</h2>
  <div class="chips">
    <a class="chip {{ 'on' if not f.pay }}" href="{{ url_for('sales_page', **dict(f, pay='')) }}">All</a>
    <a class="chip {{ 'on' if f.pay == 'FREE' }}" href="{{ url_for('sales_page', **dict(f, pay='FREE')) }}">Free</a>
    <a class="chip {{ 'on' if f.pay == 'CREDIT' }}" href="{{ url_for('sales_page', **dict(f, pay='CREDIT')) }}">Credit</a>
    <a class="chip {{ 'on' if f.pay == 'PENDING' }}" href="{{ url_for('sales_page', **dict(f, pay='PENDING')) }}">Credit not paid</a>
  </div>
  <details {{ 'open' if f.branch_id or f['from'] or f.to }}>
    <summary>Filter by branch or date</summary>
    <form method="get" class="row" style="margin-bottom:10px">
      <input type="hidden" name="pay" value="{{ f.pay }}">
      <select name="branch_id">
        <option value="">All branches</option>
        {% for b in branches %}<option value="{{ b.id }}" {{ 'selected' if f.branch_id == b.id|string }}>{{ b.name }}</option>{% endfor %}
      </select>
      <input type="date" name="from" value="{{ f['from'] }}">
      <input type="date" name="to" value="{{ f.to }}">
      <button>Apply</button>
    </form>
  </details>
  <div class="scroll"><table>
    <tr><th>When</th><th>Branch</th><th>Product</th><th class="n">Qty</th><th>Payment</th><th>Customer</th><th></th></tr>
    {% for r in rows[:300] %}
    <tr>
      <td>{{ r.created_at[5:16] }}</td>
      <td>{{ r.branch }}</td>
      <td>{{ r.name }}{% if r.note %}<br><small>{{ r.note }}</small>{% endif %}</td>
      <td class="n"><strong>{{ r.qty|fmt }}</strong> {{ r.unit }}</td>
      <td>
        <span class="tag {{ r.pay_type }}">{{ r.pay_type }}</span>
        {% if r.pay_type == 'CREDIT' %}<br><small>{{ 'paid on ' ~ r.settled_at[5:10] if r.settled_at else 'not paid yet' }}</small>{% endif %}
      </td>
      <td>{{ r.customer or '' }}</td>
      <td style="white-space:nowrap">
        {% if r.pay_type == 'CREDIT' and not r.settled_at %}
        <form method="post" action="{{ url_for('settle_sale', sid=r.id) }}" style="display:inline">
          <button class="mini" style="background:#16a34a">Mark paid</button>
        </form>
        {% endif %}
      </td>
    </tr>
    {% else %}
    <tr><td colspan="7">No sales yet. Record the first one above.</td></tr>
    {% endfor %}
  </table></div>
  {% if rows|length > 300 %}<small>Showing the latest 300 of {{ rows|length }}.</small><br>{% endif %}
  <a href="{{ url_for('export_sales', **f) }}">Download this list as CSV</a>
</div>{% endif %}

{% if can_open_block('sales_list') and branch_sales_list %}
<div class="card">
  <h2>&#128202; Sales by Branch</h2>
  <style>
    .bc-wrap{overflow-x:auto}
    .bc-tbl{width:100%;border-collapse:collapse;font-size:14px;min-width:480px}
    .bc-tbl th{padding:9px 10px;text-align:left;font-size:11px;font-weight:700;color:var(--muted,#64748b);text-transform:uppercase;letter-spacing:.05em;background:#f8fafc;border-bottom:2px solid var(--border,#e2e8f0)}
    .bc-tbl td{padding:10px 10px;border-bottom:1px solid var(--border,#e2e8f0);vertical-align:middle}
    .bc-tbl tr:last-child td{border-bottom:none}
    .bc-tbl tr:hover td{background:#fafbff}
    .bc-bar-bg{background:#e2e8f0;border-radius:99px;height:12px;min-width:60px;overflow:hidden}
    .bc-bar-fill{height:12px;border-radius:99px;background:linear-gradient(90deg,#2563eb,#7c3aed);transition:width .5s cubic-bezier(.4,0,.2,1)}
    .bc-dl{display:inline-flex;align-items:center;gap:5px;background:#eff6ff;color:#1d4ed8;border:1px solid #bfdbfe;border-radius:8px;padding:6px 12px;font-size:12px;font-weight:600;text-decoration:none;white-space:nowrap;transition:background .12s}
    .bc-dl:hover{background:#dbeafe}
    .bc-badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:600}
    .bc-badge.credit{background:#fee2e2;color:#991b1b}
    .bc-badge.free{background:#fef9c3;color:#713f12}
    .bc-rank{display:inline-flex;width:22px;height:22px;border-radius:50%;background:#f1f5f9;color:var(--muted,#64748b);font-size:11px;font-weight:700;align-items:center;justify-content:center}
    .bc-rank.top{background:#fef9c3;color:#713f12}
  </style>
  {% set max_qty = branch_sales_list[0].qty if branch_sales_list else 1 %}
  <div class="bc-wrap">
  <table class="bc-tbl">
    <thead>
      <tr>
        <th>#</th>
        <th>Branch</th>
        <th style="min-width:120px">Sales Volume</th>
        <th class="n">Qty Sold</th>
        <th class="n">Transactions</th>
        <th class="n">Credit Qty</th>
        <th class="n">Free Qty</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
    {% for bd in branch_sales_list %}
    <tr>
      <td><span class="bc-rank {{ 'top' if loop.index == 1 }}">{{ loop.index }}</span></td>
      <td><strong>{{ bd.name }}</strong></td>
      <td>
        <div style="display:flex;align-items:center;gap:8px">
          <div class="bc-bar-bg" style="flex:1">
            <div class="bc-bar-fill" style="width:{{ ((bd.qty / max_qty) * 100) | round }}%"></div>
          </div>
          <span style="font-size:12px;color:var(--muted,#64748b);white-space:nowrap">{{ "%.0f"|format((bd.qty / max_qty) * 100) }}%</span>
        </div>
      </td>
      <td class="n"><strong>{{ bd.qty | fmt }}</strong></td>
      <td class="n">{{ bd.n }}</td>
      <td class="n">{% if bd.credit_qty > 0 %}<span class="bc-badge credit">{{ bd.credit_qty | fmt }}</span>{% else %}&mdash;{% endif %}</td>
      <td class="n">{% if bd.free_qty > 0 %}<span class="bc-badge free">{{ bd.free_qty | fmt }}</span>{% else %}&mdash;{% endif %}</td>
      <td class="n">
        <a class="bc-dl" href="{{ url_for('export_sales', branch_id=bd.bid) }}">&#11015; Download CSV</a>
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
  <small style="display:block;margin-top:10px">&#8203;Percentages and volumes reflect the current filter (date range, payment type). Click Download CSV to get the full sales list for that branch.</small>
</div>
{% endif %}
"""


def sales_query():
    f = {k: request.args.get(k, "") for k in ("branch_id", "pay", "from", "to")}
    where, args = [], []
    if f["branch_id"]:
        where.append("s.branch_id = ?"); args.append(f["branch_id"])
    if f["pay"] in ("FREE", "CREDIT"):
        where.append("s.pay_type = ?"); args.append(f["pay"])
    elif f["pay"] == "PAID":
        where.append("s.pay_type = ?"); args.append(f["pay"])
    if f["pay"] == "PENDING":
        where.append("s.pay_type = 'CREDIT' AND s.settled_at IS NULL")
    if f["from"]:
        where.append("s.created_at >= ?"); args.append(f["from"] + " 00:00:00")
    if f["to"]:
        where.append("s.created_at <= ?"); args.append(f["to"] + " 23:59:59")
    sql = ("SELECT s.*, p.name, p.unit, b.name AS branch FROM sales s "
           "JOIN products p ON p.id = s.product_id JOIN branches b ON b.id = s.branch_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY s.id DESC"
    return f, get_db().execute(sql, args).fetchall()


CREDIT_BODY = """
{% if can_open_block('credit_pending') %}<div class="card">
  <h2>Pending credit sales</h2>
  <p style="margin-top:0"><small>Customer details are automatically saved from Loyverse whenever its sales are synced.</small></p>
  <p><a href="{{ url_for('export_credit') }}">Download pending credit as CSV</a></p>
  <div class="scroll"><table>
    <tr><th>When</th><th>Name</th><th>Place</th><th>Phone number</th><th>Product</th><th>Branch</th><th class="n">Qty</th><th></th></tr>
    {% for c in rows %}
    <tr>
      <td>{{ c.created_at[5:16] }}</td>
      <td><strong>{{ c.name }}</strong></td>
      <td>{{ c.place or '—' }}</td>
      <td>{{ c.phone or '—' }}</td>
      <td>{{ c.product }}</td>
      <td>{{ c.branch }}</td>
      <td class="n r">{{ c.qty|fmt }} {{ c.unit }}</td>
      <td class="n"><form method="post" action="{{ url_for('settle_sale', sid=c.id) }}" style="display:inline"><button class="mini" style="background:#16a34a">Mark paid</button></form></td>
    </tr>
    {% else %}
    <tr><td colspan="8">No unpaid credit sales.</td></tr>
    {% endfor %}
  </table></div>
</div>{% endif %}
"""


@app.route("/credit")
def credit_page():
    rows = get_db().execute(
        """SELECT s.id, s.customer AS name, c.place, c.phone, p.name AS product, p.unit,
                  b.name AS branch, s.qty, s.created_at
           FROM sales s
           JOIN products p ON p.id=s.product_id
           JOIN branches b ON b.id=s.branch_id
           LEFT JOIN (SELECT lower(name) AS customer_key, MAX(place) AS place, MAX(phone) AS phone
                      FROM lv_customers GROUP BY lower(name)) c ON c.customer_key=lower(s.customer)
           WHERE s.pay_type='CREDIT' AND s.settled_at IS NULL AND s.customer != ''
           ORDER BY s.created_at DESC, s.id DESC""").fetchall()
    return render(CREDIT_BODY, rows=rows)


@app.route("/credit.csv")
def export_credit():
    rows = get_db().execute(
        """SELECT s.customer AS name, c.place, c.phone, p.name AS product, p.unit,
                  b.name AS branch, s.qty, s.created_at
           FROM sales s JOIN products p ON p.id=s.product_id JOIN branches b ON b.id=s.branch_id
           LEFT JOIN (SELECT lower(name) AS customer_key, MAX(place) AS place, MAX(phone) AS phone
                      FROM lv_customers GROUP BY lower(name)) c ON c.customer_key=lower(s.customer)
           WHERE s.pay_type='CREDIT' AND s.settled_at IS NULL AND s.customer != ''
           ORDER BY s.created_at DESC, s.id DESC""").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "place", "phone_number", "product", "qty", "unit", "branch", "sale_time"])
    for r in rows:
        w.writerow([r["name"], r["place"] or "", r["phone"] or "", r["product"], r["qty"],
                    r["unit"], r["branch"], r["created_at"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=pending_credit.csv"})


@app.route("/sales")
def sales_page():
    db = get_db()
    f, rows = sales_query()
    t = {k: 0 for k in ("paid_qty", "paid_n", "pend_qty", "pend_n", "coll_qty", "coll_n", "free_qty", "free_n")}
    debtors = {}
    for r in rows:
        if r["pay_type"] == "PAID":
            t["paid_qty"] += r["qty"]; t["paid_n"] += 1
        elif r["pay_type"] == "FREE":
            t["free_qty"] += r["qty"]; t["free_n"] += 1
        elif r["settled_at"]:
            t["coll_qty"] += r["qty"]; t["coll_n"] += 1
        else:
            t["pend_qty"] += r["qty"]; t["pend_n"] += 1
            d = debtors.setdefault(r["customer"] or "(no name)", {"n": 0, "qty": 0})
            d["n"] += 1; d["qty"] += r["qty"]
    debtor_list = [{"customer": k, **v} for k, v in sorted(debtors.items(), key=lambda kv: -kv[1]["qty"])]

    branches = all_branches()
    products = all_products()
    stockmap = {}
    for r in db.execute("SELECT * FROM stock"):
        stockmap.setdefault(str(r["branch_id"]), {})[str(r["product_id"])] = fmt(r["qty"])
    customers = [r[0] for r in db.execute(
        "SELECT DISTINCT customer FROM sales WHERE customer != '' ORDER BY customer")]
    sel_branch = request.args.get("b", type=int) or (branches[0]["id"] if branches else None)
    sel_pay = request.args.get("p", "FREE")
    if sel_pay not in ("FREE", "CREDIT"):
        sel_pay = "FREE"
    # Per-branch breakdown for the Sales by Branch chart
    branch_sales = {}
    for r in rows:
        bid = r["branch_id"]
        if bid not in branch_sales:
            branch_sales[bid] = {"name": r["branch"], "bid": bid, "qty": 0, "n": 0,
                                  "credit_qty": 0, "free_qty": 0}
        branch_sales[bid]["qty"] += r["qty"]
        branch_sales[bid]["n"] += 1
        if r["pay_type"] == "CREDIT":
            branch_sales[bid]["credit_qty"] += r["qty"]
        elif r["pay_type"] == "FREE":
            branch_sales[bid]["free_qty"] += r["qty"]
    branch_sales_list = sorted(branch_sales.values(), key=lambda x: -x["qty"])

    return render(SALES_BODY, t=t, debtors=debtor_list, rows=rows, f=f, branches=branches,
                  products=products, sel_branch=sel_branch, sel_pay=sel_pay, stockmap=stockmap,
                  unitmap={str(p["id"]): p["unit"] for p in products},
                  productlist=picker_products(products),
                  branchmap={str(b["id"]): b["name"] for b in branches}, customers=customers,
                  branch_sales_list=branch_sales_list)


@app.route("/sales/add", methods=["POST"])
def add_sale():
    db = get_db()
    try:
        bid = int(request.form["branch_id"])
    except (ValueError, KeyError):
        flash("Choose a branch.", "error")
        return redirect(url_for("sales_page"))
    pay = request.form.get("pay_type", "FREE")
    customer = request.form.get("customer", "").strip()
    note = request.form.get("note", "").strip()
    if pay not in ("FREE", "CREDIT"):
        flash("Choose Free or Credit sale.", "error")
        return redirect(url_for("sales_page", b=bid))

    # collect the product lines (same product on two lines is added together)
    lines = {}
    for ps, qs in zip(request.form.getlist("product_id"), request.form.getlist("qty")):
        if not ps.strip() and not qs.strip():
            continue
        try:
            pid, q = int(ps), float(qs)
        except ValueError:
            flash("Choose a product and a quantity on every line.", "error")
            return redirect(url_for("sales_page", b=bid, p=pay))
        if q <= 0:
            flash("Quantities must be more than zero.", "error")
            return redirect(url_for("sales_page", b=bid, p=pay))
        lines[pid] = lines.get(pid, 0) + q
    if not lines:
        flash("Add at least one product with a quantity.", "error")
        return redirect(url_for("sales_page", b=bid, p=pay))
    if pay == "CREDIT" and not customer:
        flash("Enter the customer name for a credit sale.", "error")
        return redirect(url_for("sales_page", b=bid, p=pay))

    branch = db.execute("SELECT * FROM branches WHERE id=?", (bid,)).fetchone()
    products = {}
    for pid in lines:
        pr = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not pr:
            flash("A product was not found. Refresh the page and try again.", "error")
            return redirect(url_for("sales_page", b=bid, p=pay))
        products[pid] = pr
    if not branch:
        flash("Branch not found.", "error")
        return redirect(url_for("sales_page"))

    # all-or-nothing: lock, check every line, then save every line
    db.execute("BEGIN IMMEDIATE")
    short, current = [], {}
    for pid, q in lines.items():
        row = db.execute("SELECT qty FROM stock WHERE product_id=? AND branch_id=?",
                         (pid, bid)).fetchone()
        current[pid] = row["qty"] if row else 0
        if q > current[pid]:
            short.append(f"{products[pid]['name']} (only {fmt(current[pid])} {products[pid]['unit']} left)")
    if short:
        db.rollback()
        flash(f"Not enough stock in {branch['name']} for: " + ", ".join(short) +
              ". Nothing was saved.", "error")
        return redirect(url_for("sales_page", b=bid, p=pay))

    label = {"PAID": "Paid", "FREE": "Free", "CREDIT": "Credit"}[pay]
    stamp = now()
    for pid, q in lines.items():
        db.execute("UPDATE stock SET qty = qty - ? WHERE product_id=? AND branch_id=?", (q, pid, bid))
        db.execute(
            "INSERT INTO transactions (product_id, branch_id, kind, qty, note, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (pid, bid, "OUT", q, f"Sale ({label})" + (f" – {customer}" if customer else ""), stamp))
        db.execute(
            "INSERT INTO sales (product_id, branch_id, qty, price, total, pay_type, customer, note, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, bid, q, 0, 0, pay, customer, note, stamp))
    db.commit()
    items = ", ".join(f"{products[pid]['name']} {fmt(q)} {products[pid]['unit']}" for pid, q in lines.items())
    flash(f"Saved {label.lower()} sale from {branch['name']}"
          f"{' to ' + customer if customer else ''}: {items}.")
    return redirect(url_for("sales_page", b=bid, p=pay))


@app.route("/sales/settle/<int:sid>", methods=["POST"])
def settle_sale(sid):
    db = get_db()
    cur = db.execute("UPDATE sales SET settled_at=? WHERE id=? AND pay_type='CREDIT' AND settled_at IS NULL",
                     (now(), sid))
    db.commit()
    flash("Credit marked as paid." if cur.rowcount else "Nothing to update.",
          "ok" if cur.rowcount else "error")
    return redirect(request.referrer or url_for("sales_page"))


@app.route("/sales/settle_customer", methods=["POST"])
def settle_customer():
    db = get_db()
    name = request.form.get("customer", "")
    cur = db.execute("UPDATE sales SET settled_at=? WHERE customer=? AND pay_type='CREDIT' AND settled_at IS NULL",
                     (now(), name))
    db.commit()
    flash(f"All credit for {name or '(no name)'} marked as paid ({cur.rowcount} sale(s)).")
    return redirect(request.referrer or url_for("sales_page"))


@app.route("/sales.csv")
def export_sales():
    _f, rows = sales_query()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "branch", "product", "qty", "unit", "payment", "customer",
                "credit_paid_on", "note"])
    for r in reversed(rows):
        w.writerow([r["created_at"], r["branch"], r["name"], r["qty"], r["unit"],
                    r["pay_type"], r["customer"], r["settled_at"] or "", r["note"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=sales.csv"})


TRANSFER_BODY = """
<style>
  .step{font-size:12px;font-weight:700;color:#6b7280;text-transform:uppercase;letter-spacing:.04em;margin:14px 0 6px}
  .step:first-of-type{margin-top:0}
  .qtyrow{display:flex;gap:8px;align-items:stretch}
  .qtyrow button{flex:0 0 52px;font-size:22px;background:#e5e7eb;color:#111}
  .qtyrow input{flex:1;text-align:center;font-size:20px;font-weight:600;min-width:0}
  .hint{font-size:13px;margin-top:6px;color:#4b5563}
  .big{width:100%;padding:14px;font-size:17px;font-weight:600;margin-top:14px;background:#2563eb}
  .mini{padding:6px 10px;font-size:13px}
  .line{border:1px solid #d1d5db;border-radius:10px;padding:10px;margin-bottom:8px;background:#fafafa}
  .line select{width:100%;margin-bottom:8px}
  .line .qtyrow .rm{flex:0 0 44px;background:#fee2e2;color:#991b1b;font-size:16px}
  .addbtn{width:100%;background:#fff;color:#2563eb;border:2px dashed #93c5fd;font-weight:600;padding:12px}
  .errbox{background:#fee2e2;color:#991b1b;padding:10px;border-radius:8px;margin-top:10px;display:none;font-size:14px}
  .route{display:grid;grid-template-columns:1fr auto 1fr;gap:8px;align-items:end}
  .route .arrow{font-size:24px;color:#6b7280;padding-bottom:6px}
  .route label{display:block;font-size:12px;color:#6b7280;margin-bottom:4px}
  .route select{width:100%}
  .route>div{min-width:0}
</style>

{% if can_open_block('transfer_move') %}<div class="card">
  <h2>Move products between branches</h2>
  {% if not products %}
    <p>No products yet. <a href="{{ url_for('products_page') }}">Add your products first.</a></p>
  {% else %}
  <form method="post" action="{{ url_for('add_transfer') }}" id="tform" novalidate>
    <div class="step">1 · From which branch to which branch</div>
    <div class="route">
      <div><label for="fb">From</label>
        <select name="from_branch" id="fb">{% for b in branches %}<option value="{{ b.id }}" {{ 'selected' if b.id == sel_from }}>{{ b.name }}</option>{% endfor %}</select></div>
      <div class="arrow">→</div>
      <div><label for="tb">To</label>
        <select name="to_branch" id="tb">{% for b in branches %}<option value="{{ b.id }}" {{ 'selected' if b.id == sel_to }}>{{ b.name }}</option>{% endfor %}</select></div>
    </div>

    <div class="step">2 · Tap the products to move (tap again to add one more)</div>
    <input class="psearch" id="tsearch" type="search" placeholder="Search product…" autocomplete="off">
    <div class="pgrid" id="tgrid"></div>
    <div class="cart">
      <div class="cartempty" id="tempty">Tap a product above to add it to this transfer</div>
      <div id="tcart"></div>
    </div>

    <div class="step">Note (optional)</div>
    <input name="note" placeholder="Anything to remember" style="width:100%">

    <div class="errbox" id="terr"></div>
    <button class="big" id="tbtn">Move stock</button>
  </form>
  <script>
  (function(){
    var STOCK = {{ stockmap|tojson }}, UNIT = {{ unitmap|tojson }}, BR = {{ branchmap|tojson }},
        PRODUCTS = {{ productlist|tojson }};
    var fb=document.getElementById("fb"), tb=document.getElementById("tb"),
        form=document.getElementById("tform"), btn=document.getElementById("tbtn"), err=document.getElementById("terr");
    function avail(pid){ var b=STOCK[fb.value]||{}; return b[pid]||0; }
    function there(pid){ var b=STOCK[tb.value]||{}; return b[pid]||0; }
    function showErr(m){ err.textContent=m; err.style.display="block"; }
    var pk;
    function summarize(){
      err.style.display="none";
      var n = pk ? pk.items().length : 0;
      btn.textContent = "Move " + (n>1 ? n+" products" : "stock") + " to " + BR[tb.value];
    }
    pk = ProductPicker({
      grid: document.getElementById("tgrid"), search: document.getElementById("tsearch"),
      cart: document.getElementById("tcart"), empty: document.getElementById("tempty"),
      products: PRODUCTS, units: UNIT, avail: avail, allowZero: false,
      notify: showErr, onChange: summarize,
      rowHint: function(pid){ return BR[fb.value]+": "+avail(pid)+"  ·  "+BR[tb.value]+": "+there(pid)+" "+(UNIT[pid]||""); }
    });
    function fixRoute(changed){
      if(fb.value===tb.value){
        var target = changed==="from" ? tb : fb, keep = changed==="from" ? fb.value : tb.value;
        for(var i=0;i<target.options.length;i++){ if(target.options[i].value!==keep){ target.value=target.options[i].value; break; } }
      }
      pk.refresh(); summarize();
    }
    fb.addEventListener("change", function(){ fixRoute("from"); });
    tb.addEventListener("change", function(){ fixRoute("to"); });
    form.addEventListener("submit", function(e){
      var msg="", items=pk.items();
      if(fb.value===tb.value) msg="Choose two different branches.";
      if(!msg && !items.length) msg="Tap at least one product.";
      items.forEach(function(it){
        if(!msg && !(it.qty>0)) msg="Enter a quantity for "+it.name+", or remove it.";
        else if(!msg && it.qty>avail(it.pid)) msg=BR[fb.value]+" only has "+avail(it.pid)+" "+(UNIT[it.pid]||"")+" of "+it.name+".";
      });
      if(msg){ e.preventDefault(); showErr(msg); return; }
      btn.disabled=true; btn.textContent="Moving…";
    });
    fixRoute("from");
  })();
  </script>
  {% endif %}
</div>{% endif %}

{% if can_open_block('transfer_recent') %}<div class="card">
  <h2>Recent transfers</h2>
  <div class="scroll"><table>
    <tr><th>When</th><th>Product</th><th class="n">Qty</th><th>From → To</th><th>Note</th><th></th></tr>
    {% for r in rows %}
    <tr>
      <td>{{ r.created_at[5:16] }}</td>
      <td>{{ r.name }}</td>
      <td class="n"><strong>{{ r.qty|fmt }}</strong> {{ r.unit }}</td>
      <td>{{ r.from_name }} → {{ r.to_name }}</td>
      <td>{{ r.note or '' }}</td>
      <td>
        <form method="post" action="{{ url_for('cancel_transfer', tid=r.id) }}"
              onsubmit="return confirm('Undo this transfer and move the items back?')">
          <button class="del">Undo</button>
        </form>
      </td>
    </tr>
    {% else %}
    <tr><td colspan="6">No transfers yet.</td></tr>
    {% endfor %}
  </table></div>
</div>{% endif %}
"""


@app.route("/transfer")
def transfer_page():
    db = get_db()
    branches = all_branches()
    products = all_products()
    ids = [b["id"] for b in branches]
    sel_from = request.args.get("f", type=int)
    if sel_from not in ids:
        sel_from = ids[0] if ids else None
    sel_to = request.args.get("t", type=int)
    if sel_to not in ids or sel_to == sel_from:
        sel_to = next((i for i in ids if i != sel_from), sel_from)
    stockmap = {}
    for r in db.execute("SELECT * FROM stock"):
        stockmap.setdefault(str(r["branch_id"]), {})[str(r["product_id"])] = fmt(r["qty"])
    rows = db.execute(
        """SELECT t.*, p.name, p.unit, bf.name AS from_name, bt.name AS to_name
           FROM transfers t JOIN products p ON p.id = t.product_id
           JOIN branches bf ON bf.id = t.from_branch_id
           JOIN branches bt ON bt.id = t.to_branch_id
           ORDER BY t.id DESC LIMIT 100""").fetchall()
    return render(TRANSFER_BODY, branches=branches, products=products, rows=rows,
                  sel_from=sel_from, sel_to=sel_to, stockmap=stockmap,
                  unitmap={str(p["id"]): p["unit"] for p in products},
                  productlist=picker_products(products),
                  branchmap={str(b["id"]): b["name"] for b in branches})


@app.route("/transfer/add", methods=["POST"])
def add_transfer():
    db = get_db()
    try:
        fid = int(request.form["from_branch"])
        tid = int(request.form["to_branch"])
    except (ValueError, KeyError):
        flash("Choose both branches.", "error")
        return redirect(url_for("transfer_page"))
    note = request.form.get("note", "").strip()
    back = dict(f=fid, t=tid)
    if fid == tid:
        flash("Choose two different branches.", "error")
        return redirect(url_for("transfer_page", **back))

    lines = {}
    for ps, qs in zip(request.form.getlist("product_id"), request.form.getlist("qty")):
        if not ps.strip() and not qs.strip():
            continue
        try:
            pid, q = int(ps), float(qs)
        except ValueError:
            flash("Choose a product and a quantity on every line.", "error")
            return redirect(url_for("transfer_page", **back))
        if q <= 0:
            flash("Quantities must be more than zero.", "error")
            return redirect(url_for("transfer_page", **back))
        lines[pid] = lines.get(pid, 0) + q
    if not lines:
        flash("Add at least one product with a quantity.", "error")
        return redirect(url_for("transfer_page", **back))

    bf = db.execute("SELECT * FROM branches WHERE id=?", (fid,)).fetchone()
    bt = db.execute("SELECT * FROM branches WHERE id=?", (tid,)).fetchone()
    products = {}
    for pid in lines:
        pr = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not pr:
            flash("A product was not found. Refresh the page and try again.", "error")
            return redirect(url_for("transfer_page", **back))
        products[pid] = pr
    if not bf or not bt:
        flash("Branch not found.", "error")
        return redirect(url_for("transfer_page"))

    db.execute("BEGIN IMMEDIATE")
    short = []
    for pid, q in lines.items():
        row = db.execute("SELECT qty FROM stock WHERE product_id=? AND branch_id=?", (pid, fid)).fetchone()
        have = row["qty"] if row else 0
        if q > have:
            short.append(f"{products[pid]['name']} (only {fmt(have)} {products[pid]['unit']} left)")
    if short:
        db.rollback()
        flash(f"Not enough stock in {bf['name']} for: " + ", ".join(short) + ". Nothing was moved.", "error")
        return redirect(url_for("transfer_page", **back))

    stamp = now()
    for pid, q in lines.items():
        db.execute("UPDATE stock SET qty = qty - ? WHERE product_id=? AND branch_id=?", (q, pid, fid))
        db.execute(
            """INSERT INTO stock (product_id, branch_id, qty) VALUES (?,?,?)
               ON CONFLICT(product_id, branch_id) DO UPDATE SET qty = qty + excluded.qty""",
            (pid, tid, q))
        db.execute(
            "INSERT INTO transactions (product_id, branch_id, kind, qty, note, created_at) VALUES (?,?,?,?,?,?)",
            (pid, fid, "OUT", q, f"Transfer to {bt['name']}", stamp))
        db.execute(
            "INSERT INTO transactions (product_id, branch_id, kind, qty, note, created_at) VALUES (?,?,?,?,?,?)",
            (pid, tid, "IN", q, f"Transfer from {bf['name']}", stamp))
        db.execute(
            "INSERT INTO transfers (product_id, from_branch_id, to_branch_id, qty, note, created_at) "
            "VALUES (?,?,?,?,?,?)", (pid, fid, tid, q, note, stamp))
    db.commit()
    items = ", ".join(f"{products[pid]['name']} {fmt(q)} {products[pid]['unit']}" for pid, q in lines.items())
    flash(f"Moved from {bf['name']} to {bt['name']}: {items}.")
    return redirect(url_for("transfer_page", **back))


@app.route("/transfer/cancel/<int:tid>", methods=["POST"])
def cancel_transfer(tid):
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    t = db.execute("SELECT * FROM transfers WHERE id=?", (tid,)).fetchone()
    if not t:
        db.rollback()
        flash("Transfer not found.", "error")
        return redirect(url_for("transfer_page"))
    row = db.execute("SELECT qty FROM stock WHERE product_id=? AND branch_id=?",
                     (t["product_id"], t["to_branch_id"])).fetchone()
    have = row["qty"] if row else 0
    if have < t["qty"]:
        db.rollback()
        flash(f"Can't undo: the receiving branch has only {fmt(have)} left of these items "
              f"(it has already used some).", "error")
        return redirect(url_for("transfer_page", f=t["from_branch_id"], t=t["to_branch_id"]))
    stamp = now()
    db.execute("UPDATE stock SET qty = qty - ? WHERE product_id=? AND branch_id=?",
               (t["qty"], t["product_id"], t["to_branch_id"]))
    db.execute(
        """INSERT INTO stock (product_id, branch_id, qty) VALUES (?,?,?)
           ON CONFLICT(product_id, branch_id) DO UPDATE SET qty = qty + excluded.qty""",
        (t["product_id"], t["from_branch_id"], t["qty"]))
    db.execute("INSERT INTO transactions (product_id, branch_id, kind, qty, note, created_at) VALUES (?,?,?,?,?,?)",
               (t["product_id"], t["to_branch_id"], "OUT", t["qty"], f"Transfer #{tid} undone", stamp))
    db.execute("INSERT INTO transactions (product_id, branch_id, kind, qty, note, created_at) VALUES (?,?,?,?,?,?)",
               (t["product_id"], t["from_branch_id"], "IN", t["qty"], f"Transfer #{tid} undone", stamp))
    db.execute("DELETE FROM transfers WHERE id=?", (tid,))
    db.commit()
    flash("Transfer undone. The items are back in the first branch.")
    return redirect(url_for("transfer_page", f=t["from_branch_id"], t=t["to_branch_id"]))


RETURNS_BODY = """
<style>
  .step{font-size:12px;font-weight:700;color:#6b7280;text-transform:uppercase;letter-spacing:.04em;margin:14px 0 6px}
  .step:first-of-type{margin-top:0}
  .qtyrow{display:flex;gap:8px;align-items:stretch}
  .qtyrow button{flex:0 0 52px;font-size:22px;background:#e5e7eb;color:#111}
  .qtyrow input{flex:1;text-align:center;font-size:20px;font-weight:600;min-width:0}
  .hint{font-size:13px;margin-top:6px;color:#4b5563}
  .big{width:100%;padding:14px;font-size:17px;font-weight:600;margin-top:14px;background:#2563eb}
  .mini{padding:6px 10px;font-size:13px}
  .line{border:1px solid #d1d5db;border-radius:10px;padding:10px;margin-bottom:8px;background:#fafafa}
  .line select{width:100%;margin-bottom:8px}
  .line .qtyrow .rm{flex:0 0 44px;background:#fee2e2;color:#991b1b;font-size:16px}
  .addbtn{width:100%;background:#fff;color:#2563eb;border:2px dashed #93c5fd;font-weight:600;padding:12px}
  .errbox{background:#fee2e2;color:#991b1b;padding:10px;border-radius:8px;margin-top:10px;display:none;font-size:14px}
  .route{display:grid;grid-template-columns:1fr auto 1fr;gap:8px;align-items:end}
  .route .arrow{font-size:24px;color:#6b7280;padding-bottom:6px}
  .route label{display:block;font-size:12px;color:#6b7280;margin-bottom:4px}
  .route select{width:100%}
  .route>div{min-width:0}
</style>

{% if can_open_block('returns_move') %}<div class="card">
  <h2>Return products to another branch</h2>
  {% if not products %}
    <p>No products yet. <a href="{{ url_for('products_page') }}">Add your products first.</a></p>
  {% else %}
  <form method="post" action="{{ url_for('add_return') }}" id="rform" novalidate>
    <div class="step">1 · Which branch gives the product back, and to which branch</div>
    <div class="route">
      <div><label for="fb">Returning branch</label>
        <select name="from_branch" id="fb">{% for b in branches %}<option value="{{ b.id }}" {{ 'selected' if b.id == sel_from }}>{{ b.name }}</option>{% endfor %}</select></div>
      <div class="arrow">→</div>
      <div><label for="tb">Gets it back</label>
        <select name="to_branch" id="tb">{% for b in branches %}<option value="{{ b.id }}" {{ 'selected' if b.id == sel_to }}>{{ b.name }}</option>{% endfor %}</select></div>
    </div>

    <div class="step">2 · Tap the products being returned (tap again to add one more)</div>
    <input class="psearch" id="tsearch" type="search" placeholder="Search product…" autocomplete="off">
    <div class="pgrid" id="tgrid"></div>
    <div class="cart">
      <div class="cartempty" id="tempty">Tap a product above to add it to this return</div>
      <div id="tcart"></div>
    </div>

    <div class="step">3 · Why is it being returned? (required)</div>
    <input name="reason" id="rreason" list="rreasons" maxlength="200" required autocomplete="off" placeholder="Pick or type a reason" style="width:100%">
    <datalist id="rreasons"><option value="Excess stock"><option value="Damaged"><option value="Expired"><option value="Wrong item sent"><option value="Not selling"><option value="Branch closing"></datalist>
    <div class="step">Note (optional)</div>
    <input name="note" placeholder="Anything to remember" style="width:100%">

    <div class="errbox" id="terr"></div>
    <button class="big" id="tbtn">Return product</button>
  </form>
  <script>
  (function(){
    var STOCK = {{ stockmap|tojson }}, UNIT = {{ unitmap|tojson }}, BR = {{ branchmap|tojson }},
        PRODUCTS = {{ productlist|tojson }};
    var fb=document.getElementById("fb"), tb=document.getElementById("tb"),
        form=document.getElementById("rform"), btn=document.getElementById("tbtn"), err=document.getElementById("terr");
    function avail(pid){ var b=STOCK[fb.value]||{}; return b[pid]||0; }
    function there(pid){ var b=STOCK[tb.value]||{}; return b[pid]||0; }
    function showErr(m){ err.textContent=m; err.style.display="block"; }
    var pk;
    function summarize(){
      err.style.display="none";
      var n = pk ? pk.items().length : 0;
      btn.textContent = "Return " + (n>1 ? n+" products" : "product") + " to " + BR[tb.value];
    }
    pk = ProductPicker({
      grid: document.getElementById("tgrid"), search: document.getElementById("tsearch"),
      cart: document.getElementById("tcart"), empty: document.getElementById("tempty"),
      products: PRODUCTS, units: UNIT, avail: avail, allowZero: false,
      notify: showErr, onChange: summarize,
      rowHint: function(pid){ return BR[fb.value]+": "+avail(pid)+"  ·  "+BR[tb.value]+": "+there(pid)+" "+(UNIT[pid]||""); }
    });
    function fixRoute(changed){
      if(fb.value===tb.value){
        var target = changed==="from" ? tb : fb, keep = changed==="from" ? fb.value : tb.value;
        for(var i=0;i<target.options.length;i++){ if(target.options[i].value!==keep){ target.value=target.options[i].value; break; } }
      }
      pk.refresh(); summarize();
    }
    fb.addEventListener("change", function(){ fixRoute("from"); });
    tb.addEventListener("change", function(){ fixRoute("to"); });
    form.addEventListener("submit", function(e){
      var msg="", items=pk.items();
      if(fb.value===tb.value) msg="Choose two different branches.";
      if(!msg && !items.length) msg="Tap at least one product.";
      if(!msg && !document.getElementById("rreason").value.trim()) msg="Please write the reason for this return.";
      items.forEach(function(it){
        if(!msg && !(it.qty>0)) msg="Enter a quantity for "+it.name+", or remove it.";
        else if(!msg && it.qty>avail(it.pid)) msg=BR[fb.value]+" only has "+avail(it.pid)+" "+(UNIT[it.pid]||"")+" of "+it.name+".";
      });
      if(msg){ e.preventDefault(); showErr(msg); return; }
      btn.disabled=true; btn.textContent="Returning…";
    });
    fixRoute("from");
  })();
  </script>
  {% endif %}
</div>{% endif %}

{% if can_open_block('returns_recent') %}<div class="card">
  <h2>Recent returns</h2>
  <div class="scroll"><table>
    <tr><th>When</th><th>Product</th><th class="n">Qty</th><th>Returned by → To</th><th>Reason</th><th></th></tr>
    {% for r in rows %}
    <tr>
      <td>{{ r.created_at[5:16] }}</td>
      <td>{{ r.name }}</td>
      <td class="n"><strong>{{ r.qty|fmt }}</strong> {{ r.unit }}</td>
      <td>{{ r.from_name }} → {{ r.to_name }}</td>
      <td>{{ r.reason }}{% if r.note %}<br><small>{{ r.note }}</small>{% endif %}</td>
      <td>
        <form method="post" action="{{ url_for('cancel_return', rid=r.id) }}"
              onsubmit="return confirm('Undo this return and send the items back?')">
          <button class="del">Undo</button>
        </form>
      </td>
    </tr>
    {% else %}
    <tr><td colspan="6">No returns yet.</td></tr>
    {% endfor %}
  </table></div>
</div>{% endif %}
"""


@app.route("/returns")
def returns_page():
    db = get_db()
    branches = all_branches()
    products = all_products()
    ids = [b["id"] for b in branches]
    sel_from = request.args.get("f", type=int)
    if sel_from not in ids:
        sel_from = ids[0] if ids else None
    sel_to = request.args.get("t", type=int)
    if sel_to not in ids or sel_to == sel_from:
        sel_to = next((i for i in ids if i != sel_from), sel_from)
    stockmap = {}
    for r in db.execute("SELECT * FROM stock"):
        stockmap.setdefault(str(r["branch_id"]), {})[str(r["product_id"])] = fmt(r["qty"])
    rows = db.execute(
        """SELECT t.*, p.name, p.unit, bf.name AS from_name, bt.name AS to_name
           FROM returns t JOIN products p ON p.id = t.product_id
           JOIN branches bf ON bf.id = t.from_branch_id
           JOIN branches bt ON bt.id = t.to_branch_id
           ORDER BY t.id DESC LIMIT 100""").fetchall()
    return render(RETURNS_BODY, branches=branches, products=products, rows=rows,
                  sel_from=sel_from, sel_to=sel_to, stockmap=stockmap,
                  unitmap={str(p["id"]): p["unit"] for p in products},
                  productlist=picker_products(products),
                  branchmap={str(b["id"]): b["name"] for b in branches})


@app.route("/returns/add", methods=["POST"])
def add_return():
    db = get_db()
    try:
        fid = int(request.form["from_branch"])
        tid = int(request.form["to_branch"])
    except (ValueError, KeyError):
        flash("Choose both branches.", "error")
        return redirect(url_for("returns_page"))
    note = request.form.get("note", "").strip()
    reason = " ".join(request.form.get("reason", "").split())[:200]
    back = dict(f=fid, t=tid)
    if fid == tid:
        flash("Choose two different branches.", "error")
        return redirect(url_for("returns_page", **back))
    if not reason:
        flash("Please enter the reason for the return. Nothing was saved.", "error")
        return redirect(url_for("returns_page", **back))

    lines = {}
    for ps, qs in zip(request.form.getlist("product_id"), request.form.getlist("qty")):
        if not ps.strip() and not qs.strip():
            continue
        try:
            pid, q = int(ps), float(qs)
        except ValueError:
            flash("Choose a product and a quantity on every line.", "error")
            return redirect(url_for("returns_page", **back))
        if not q > 0:
            flash("Quantities must be more than zero.", "error")
            return redirect(url_for("returns_page", **back))
        lines[pid] = lines.get(pid, 0) + q
    if not lines:
        flash("Add at least one product with a quantity.", "error")
        return redirect(url_for("returns_page", **back))

    bf = db.execute("SELECT * FROM branches WHERE id=?", (fid,)).fetchone()
    bt = db.execute("SELECT * FROM branches WHERE id=?", (tid,)).fetchone()
    if not bf or not bt:
        flash("Branch not found.", "error")
        return redirect(url_for("returns_page"))
    products = {}
    for pid in lines:
        pr = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not pr:
            flash("A product was not found. Refresh the page and try again.", "error")
            return redirect(url_for("returns_page", **back))
        products[pid] = pr

    db.execute("BEGIN IMMEDIATE")
    short = []
    for pid, q in lines.items():
        row = db.execute("SELECT qty FROM stock WHERE product_id=? AND branch_id=?", (pid, fid)).fetchone()
        have = row["qty"] if row else 0
        if q > have:
            short.append(f"{products[pid]['name']} (only {fmt(have)} {products[pid]['unit']} left)")
    if short:
        db.rollback()
        flash(f"Not enough stock in {bf['name']} for: " + ", ".join(short) + ". Nothing was returned.", "error")
        return redirect(url_for("returns_page", **back))

    stamp = now()
    for pid, q in lines.items():
        db.execute("UPDATE stock SET qty = qty - ? WHERE product_id=? AND branch_id=?", (q, pid, fid))
        db.execute(
            """INSERT INTO stock (product_id, branch_id, qty) VALUES (?,?,?)
               ON CONFLICT(product_id, branch_id) DO UPDATE SET qty = qty + excluded.qty""",
            (pid, tid, q))
        db.execute(
            "INSERT INTO transactions (product_id, branch_id, kind, qty, note, created_at) VALUES (?,?,?,?,?,?)",
            (pid, fid, "OUT", q, f"Return to {bt['name']} — {reason}", stamp))
        db.execute(
            "INSERT INTO transactions (product_id, branch_id, kind, qty, note, created_at) VALUES (?,?,?,?,?,?)",
            (pid, tid, "IN", q, f"Return from {bf['name']} — {reason}", stamp))
        db.execute(
            "INSERT INTO returns (product_id, from_branch_id, to_branch_id, qty, reason, note, created_at) "
            "VALUES (?,?,?,?,?,?,?)", (pid, fid, tid, q, reason, note, stamp))
    db.commit()
    items = ", ".join(f"{products[pid]['name']} {fmt(q)} {products[pid]['unit']}" for pid, q in lines.items())
    flash(f"Returned from {bf['name']} to {bt['name']}: {items}.")
    return redirect(url_for("returns_page", **back))


@app.route("/returns/cancel/<int:rid>", methods=["POST"])
def cancel_return(rid):
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    t = db.execute("SELECT * FROM returns WHERE id=?", (rid,)).fetchone()
    if not t:
        db.rollback()
        flash("Return not found.", "error")
        return redirect(url_for("returns_page"))
    row = db.execute("SELECT qty FROM stock WHERE product_id=? AND branch_id=?",
                     (t["product_id"], t["to_branch_id"])).fetchone()
    have = row["qty"] if row else 0
    if have < t["qty"]:
        db.rollback()
        flash(f"Can't undo: the branch that received it has only {fmt(have)} left of these items "
              f"(it has already used some).", "error")
        return redirect(url_for("returns_page", f=t["from_branch_id"], t=t["to_branch_id"]))
    stamp = now()
    db.execute("UPDATE stock SET qty = qty - ? WHERE product_id=? AND branch_id=?",
               (t["qty"], t["product_id"], t["to_branch_id"]))
    db.execute(
        """INSERT INTO stock (product_id, branch_id, qty) VALUES (?,?,?)
           ON CONFLICT(product_id, branch_id) DO UPDATE SET qty = qty + excluded.qty""",
        (t["product_id"], t["from_branch_id"], t["qty"]))
    db.execute("INSERT INTO transactions (product_id, branch_id, kind, qty, note, created_at) VALUES (?,?,?,?,?,?)",
               (t["product_id"], t["to_branch_id"], "OUT", t["qty"], f"Return #{rid} undone", stamp))
    db.execute("INSERT INTO transactions (product_id, branch_id, kind, qty, note, created_at) VALUES (?,?,?,?,?,?)",
               (t["product_id"], t["from_branch_id"], "IN", t["qty"], f"Return #{rid} undone", stamp))
    db.execute("DELETE FROM returns WHERE id=?", (rid,))
    db.commit()
    flash("Return undone. The items are back in the returning branch.")
    return redirect(url_for("returns_page", f=t["from_branch_id"], t=t["to_branch_id"]))


BD_COLORS = (("#6366f1", "#8b5cf6"), ("#0ea5e9", "#2563eb"), ("#10b981", "#059669"), ("#f59e0b", "#ea580c"),
             ("#ec4899", "#db2777"), ("#14b8a6", "#0d9488"), ("#ef4444", "#dc2626"), ("#64748b", "#475569"))


def nice_when(stamp, with_time=True):
    try:
        dt = datetime.strptime((stamp or "")[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return stamp or ""
    today = datetime.now().date()
    if dt.date() == today:
        day = "Today"
    elif dt.date() == today - timedelta(days=1):
        day = "Yesterday"
    else:
        day = dt.strftime("%d %b") + ("" if dt.year == today.year else dt.strftime(" %Y"))
    return f"{day}, {dt.strftime('%H:%M')}" if with_time else day


def branch_moves(db, bid, names, other=None, limit=600):
    """Transfers and returns in or out of one branch, grouped into one event per movement, oldest first."""
    groups = {}
    for kind, table in (("transfer", "transfers"), ("return", "returns")):
        extra = "t.reason" if kind == "return" else "NULL AS reason"
        for r in db.execute(
                f"""SELECT t.*, {extra}, p.name, p.unit, p.photo FROM {table} t JOIN products p ON p.id = t.product_id
                    WHERE (t.from_branch_id=? OR t.to_branch_id=?) ORDER BY t.id DESC LIMIT ?""", (bid, bid, limit)):
            out = r["from_branch_id"] == bid
            peer = r["to_branch_id"] if out else r["from_branch_id"]
            if other and peer != other:
                continue
            g = groups.setdefault((kind, r["from_branch_id"], r["to_branch_id"], r["created_at"]), {
                "is_return": kind == "return", "out": out, "other": names.get(peer, "?"), "lines": [],
                "reason": r["reason"] or "", "note": r["note"] or "", "stamp": r["created_at"] or ""})
            g["lines"].append({"name": r["name"], "unit": r["unit"], "qty": r["qty"], "photo": photo_url(r["photo"])})
    return sorted(groups.values(), key=lambda g: g["stamp"])


def branch_summary(events, days=30):
    cut = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    sent = recv = ret = n = 0.0
    for e in events:
        if e["stamp"][:10] < cut:
            continue
        q = sum(l["qty"] for l in e["lines"])
        n += 1
        if e["out"]:
            sent += q
        else:
            recv += q
        if e["is_return"]:
            ret += 1
    return {"sent": sent, "recv": recv, "returns": int(ret), "moves": int(n)}


BRANCHCHAT_BODY = """
<style>
  .bdh{border-radius:18px;padding:18px 18px 16px;color:#fff;background:linear-gradient(135deg,#111827,#374151 60%,#4f46e5);margin-bottom:14px}
  .bdh h2{margin:0;font-size:22px;color:#fff}
  .bdh p{margin:2px 0 12px;color:#c7d2fe;font-size:13px}
  .bds{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px}
  .bds div{background:#ffffff1f;border-radius:12px;padding:9px 12px}
  .bds b{display:block;font-size:22px;line-height:1.1}
  .bds span{font-size:12px;color:#e0e7ff}
  .bdgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:14px}
  .bdc{background:#fff;border-radius:18px;box-shadow:0 2px 10px #0000001a;overflow:hidden;display:flex;flex-direction:column}
  .bdc-top{display:flex;gap:12px;align-items:center;padding:14px;color:#fff}
  .bdav{flex:0 0 46px;width:46px;height:46px;border-radius:14px;background:#ffffff33;display:flex;align-items:center;justify-content:center;font-size:21px;font-weight:700}
  .bdc-top h3{margin:0;font-size:17px;color:#fff}
  .bdc-top small{color:#ffffffcc}
  .bdstats{display:grid;grid-template-columns:repeat(3,1fr);border-bottom:1px solid #eef0f3}
  .bdstats div{padding:9px 6px;text-align:center}
  .bdstats b{display:block;font-size:17px}
  .bdstats span{font-size:11px;color:#6b7280}
  .bdstats .o b{color:#1d4ed8}.bdstats .i b{color:#15803d}.bdstats .r b{color:#b91c1c}
  .bdlist{padding:6px 10px;flex:1}
  .bdrow{display:flex;gap:10px;align-items:center;padding:8px 4px;border-bottom:1px solid #f3f4f6}
  .bdrow:last-child{border-bottom:0}
  .bdimg{flex:0 0 46px;width:46px;height:46px;border-radius:12px;object-fit:cover;background:#f3f4f6;display:flex;align-items:center;justify-content:center;font-size:20px}
  .bdmid{flex:1;min-width:0}
  .bdmid .nm{font-weight:600;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .bdmid .mt{font-size:12px;color:#6b7280;margin-top:1px}
  .pill{display:inline-block;font-size:11px;font-weight:700;border-radius:99px;padding:2px 8px;white-space:nowrap}
  .pill.out{background:#dbeafe;color:#1d4ed8}.pill.in{background:#dcfce7;color:#166534}.pill.ret{background:#fee2e2;color:#991b1b;margin-left:4px}
  .bdq{text-align:right;flex:0 0 auto}
  .bdq b{font-size:16px}.bdq span{display:block;font-size:11px;color:#6b7280}
  .bdempty{padding:26px 10px;text-align:center;color:#9ca3af;font-size:14px}
  .bdfoot{display:block;text-align:center;padding:11px;font-weight:600;font-size:14px;text-decoration:none;background:#f9fafb;color:#4338ca;border-top:1px solid #eef0f3}
  .bdfoot:hover{background:#eef2ff}
  .bctabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
  .bctabs a{padding:7px 14px;border-radius:99px;font-size:14px;text-decoration:none;background:#e5e7eb;color:#374151}
  .bctabs a.on{background:#1f2937;color:#fff}
  .chat{background:#f3f4f6;border-radius:16px;padding:12px;max-height:70vh;overflow-y:auto;display:flex;flex-direction:column;gap:10px}
  .day{align-self:center;font-size:12px;color:#6b7280;background:#e5e7eb;border-radius:99px;padding:3px 12px;margin:6px 0}
  .bub{max-width:86%;padding:10px 12px;border-radius:16px;font-size:14px;box-shadow:0 1px 2px #0002;word-wrap:break-word}
  .bub.out{align-self:flex-end;background:#dbeafe;border-bottom-right-radius:4px}
  .bub.in{align-self:flex-start;background:#fff;border-bottom-left-radius:4px}
  .bub .who{font-size:12px;font-weight:700;color:#4b5563;margin-bottom:6px}
  .bub .it{display:flex;gap:10px;align-items:center;padding:4px 0}
  .bub .it .bdimg{flex-basis:42px;width:42px;height:42px;border-radius:10px}
  .bub .it .nm{font-weight:600}
  .bub .it .q{font-size:12px;color:#4b5563}
  .bub .why{font-size:13px;color:#7f1d1d;margin-top:4px}
  .bub .nt{font-size:13px;color:#4b5563;margin-top:2px}
  .bub .when{font-size:11px;color:#6b7280;text-align:right;margin-top:4px}
  .chatempty{text-align:center;color:#6b7280;padding:30px 0}
</style>
{% if can_open_block('branchchat_view') %}
{% if not branches %}<div class="card"><p>No branches yet.</p></div>
{% elif bid is none %}
  <div class="bdh">
    <h2>Branch Dashboard</h2>
    <p>Every transfer and return, branch by branch, with the product photos · last 30 days</p>
    <div class="bds">
      <div><b>{{ totals.branches }}</b><span>Branches</span></div>
      <div><b>{{ totals.moves }}</b><span>Movements</span></div>
      <div><b>{{ totals.moved|fmt }}</b><span>Items moved</span></div>
      <div><b>{{ totals.returns }}</b><span>Returns</span></div>
    </div>
  </div>
  <div class="bdgrid">
    {% for c in cards %}
    <div class="bdc">
      <div class="bdc-top" style="background:linear-gradient(135deg,{{ c.c1 }},{{ c.c2 }})">
        <div class="bdav">{{ c.name[:1]|upper }}</div>
        <div><h3>{{ c.name }}</h3><small>{{ c.moves }} movement(s) in 30 days</small></div>
      </div>
      <div class="bdstats">
        <div class="o"><b>{{ c.sent|fmt }}</b><span>Sent out</span></div>
        <div class="i"><b>{{ c.recv|fmt }}</b><span>Received</span></div>
        <div class="r"><b>{{ c.returns }}</b><span>Returns</span></div>
      </div>
      <div class="bdlist">
        {% for r in c.rows %}
        <div class="bdrow">
          {% if r.photo %}<img class="bdimg" src="{{ r.photo }}" alt="" loading="lazy">{% else %}<div class="bdimg">📦</div>{% endif %}
          <div class="bdmid">
            <div class="nm" title="{{ r.name }}">{{ r.name }}</div>
            <div class="mt"><span class="pill {{ 'out' if r.out else 'in' }}">{{ ('→ ' if r.out else '← ') ~ r.other }}</span>{% if r.is_return %}<span class="pill ret">RETURN</span>{% endif %} · {{ r.when }}</div>
          </div>
          <div class="bdq"><b>{{ r.qty|fmt }}</b><span>{{ r.unit }}</span></div>
        </div>
        {% else %}<div class="bdempty">No transfers or returns yet</div>{% endfor %}
      </div>
      <a class="bdfoot" href="{{ url_for('branchchat_page', bid=c.id) }}">Open full history →</a>
    </div>
    {% endfor %}
  </div>
{% else %}
  <div class="bdh" style="background:linear-gradient(135deg,{{ hero.c1 }},{{ hero.c2 }})">
    <h2>{{ branch_name }}</h2>
    <p>All transfers and returns with product photos{% if other_name %} · with {{ other_name }} only{% endif %}</p>
    <div class="bds">
      <div><b>{{ hero.sent|fmt }}</b><span>Sent out (30 days)</span></div>
      <div><b>{{ hero.recv|fmt }}</b><span>Received (30 days)</span></div>
      <div><b>{{ hero.moves }}</b><span>Movements</span></div>
      <div><b>{{ hero.returns }}</b><span>Returns</span></div>
    </div>
  </div>
  <div class="card">
    <div class="bctabs"><a href="{{ url_for('branchchat_page') }}">← Dashboard</a>{% for b in branches %}<a href="{{ url_for('branchchat_page', bid=b.id) }}" class="{{ 'on' if b.id == bid }}">{{ b.name }}</a>{% endfor %}</div>
    <form method="get" action="{{ url_for('branchchat_page', bid=bid) }}" class="row" style="margin-bottom:10px">
      <select name="with" onchange="this.form.submit()"><option value="">All branches</option>{% for b in branches if b.id != bid %}<option value="{{ b.id }}" {{ 'selected' if b.id == other }}>{{ b.name }}</option>{% endfor %}</select>
    </form>
    <div class="chat" id="chat">
      {% for m in msgs %}
        {% if m.new_day %}<div class="day">{{ m.day }}</div>{% endif %}
        <div class="bub {{ 'out' if m.out else 'in' }}">
          <div class="who">{{ ('You sent to ' if m.out else 'Received from ') ~ m.other }}{% if m.is_return %}<span class="pill ret">RETURN</span>{% endif %}</div>
          {% for it in m.lines %}
          <div class="it">
            {% if it.photo %}<img class="bdimg" src="{{ it.photo }}" alt="" loading="lazy">{% else %}<div class="bdimg">📦</div>{% endif %}
            <div><div class="nm">{{ it.name }}</div><div class="q">{{ it.qty|fmt }} {{ it.unit }}</div></div>
          </div>
          {% endfor %}
          {% if m.reason %}<div class="why">Reason: {{ m.reason }}</div>{% endif %}
          {% if m.note %}<div class="nt">{{ m.note }}</div>{% endif %}
          <div class="when">{{ m.time }}</div>
        </div>
      {% else %}
        <div class="chatempty">No transfers or returns for this branch yet.</div>
      {% endfor %}
    </div>
    <small>Blue = stock this branch sent out · white = stock it received · newest at the bottom.</small>
    <script>(function(){var c=document.getElementById("chat"); if(c) c.scrollTop=c.scrollHeight;})();</script>
  </div>
{% endif %}
{% endif %}
"""


@app.route("/branch-chat", defaults={"bid": None})
@app.route("/branch-chat/<int:bid>")
def branchchat_page(bid):
    db = get_db()
    branches = all_branches()
    ids = [b["id"] for b in branches]
    names = {b["id"]: b["name"] for b in branches}
    colors = {b["id"]: BD_COLORS[i % len(BD_COLORS)] for i, b in enumerate(branches)}
    if bid is not None and bid not in ids:
        bid = None
    if bid is None:
        cards, totals = [], {"branches": len(branches), "moves": 0, "moved": 0.0, "returns": 0}
        for b in branches:
            ev = branch_moves(db, b["id"], names, limit=150)
            sm = branch_summary(ev)
            rows = []
            for e in reversed(ev):
                for l in e["lines"]:
                    rows.append({**l, "out": e["out"], "other": e["other"], "is_return": e["is_return"],
                                 "when": nice_when(e["stamp"])})
                if len(rows) >= 6:
                    break
            c1, c2 = colors[b["id"]]
            cards.append({"id": b["id"], "name": b["name"], "rows": rows[:6], "c1": c1, "c2": c2, **sm})
            totals["moves"] += sm["moves"]
            totals["moved"] += sm["sent"]
            totals["returns"] += sm["returns"]
        return render(BRANCHCHAT_BODY, branches=branches, bid=None, cards=cards, totals=totals)
    other = request.args.get("with", type=int)
    if other not in ids or other == bid:
        other = None
    events = branch_moves(db, bid, names, other)[-200:]
    last_day = None
    for g in events:
        day = g["stamp"][:10]
        g["new_day"], g["day"], g["time"] = day != last_day, nice_when(g["stamp"], with_time=False), g["stamp"][11:16]
        last_day = day
    hero = branch_summary(branch_moves(db, bid, names, other))
    hero["c1"], hero["c2"] = colors[bid]
    return render(BRANCHCHAT_BODY, branches=branches, bid=bid, other=other, msgs=events, hero=hero,
                  branch_name=names[bid], other_name=names.get(other, ""))


# =====================================================================
#  Loyverse connection (one-way: Loyverse sales/refunds -> this app)
#  NOTE: field names below follow Loyverse's public API as commonly
#  documented (receipts, stores, customers). If your account returns
#  something different, the "Test connection" button on the Loyverse
#  page shows what came back.
# =====================================================================
LV_CONFIG_PATH = os.path.join(BASE_DIR, "loyverse_config.json")
LV_LOCK = threading.Lock()


class LoyverseError(Exception):
    pass


def lv_configs():
    """Return configured Loyverse connections; supports old single-token config too."""
    try:
        with open(LV_CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return []
    except (ValueError, OSError):
        return {"error": "loyverse_config.json could not be read. Check that it is valid JSON."}
    if isinstance(raw.get("accounts"), list):
        accounts = raw.get("accounts") or []
    elif raw.get("token"):
        # Backward compatibility: existing one-account setup keeps its mappings/history.
        accounts = [{"id": "default", "name": str(raw.get("name") or "Loyverse account"), "token": raw.get("token"),
                     "base_url": raw.get("base_url"), "credit_words": raw.get("credit_words")}]
    else:
        return []
    try:
        interval = max(30, int(raw.get("interval_seconds", 60)))
    except (TypeError, ValueError):
        interval = 60
    out = []
    for i, a in enumerate(accounts[:MAX_LOYVERSE_ACCOUNTS]):
        token = str(a.get("token") or "").strip()
        if not token or token.startswith("PASTE"):
            continue
        words = a.get("credit_words") or raw.get("credit_words") or ["credit", "udhar", "udhaar", "due", "later"]
        out.append({"id": str(a.get("id") or ("acct" + str(i+1))),
                    "name": str(a.get("name") or ("Loyverse account " + str(i+1))),
                    "token": token,
                    "base_url": str(a.get("base_url") or raw.get("base_url") or "https://api.loyverse.com/v1.0").rstrip("/"),
                    "interval": interval,
                    "credit_words": [str(w).lower() for w in words]})
    return out


def lv_config():
    """Compatibility helper for existing UI/code that expects one connection."""
    accounts = lv_configs()
    if isinstance(accounts, dict) and accounts.get("error"):
        return accounts
    if not accounts:
        return None
    return accounts[0]


def lv_config_file():
    try:
        with open(LV_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"accounts": [], "interval_seconds": 60}
    except (ValueError, OSError):
        return {"accounts": [], "interval_seconds": 60}


def lv_save_accounts(accounts, old=None):
    old = old or lv_config_file()
    data = {"accounts": accounts[:MAX_LOYVERSE_ACCOUNTS],
            "interval_seconds": old.get("interval_seconds", 60),
            "base_url": old.get("base_url", "https://api.loyverse.com/v1.0"),
            "credit_words": old.get("credit_words", ["credit", "udhar", "udhaar", "due", "later"])}
    tmp = LV_CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, LV_CONFIG_PATH)


def lv_store_key(account_id, store_id):
    return str(store_id) if account_id == "default" else account_id + "::" + str(store_id)


def lv_get(cfg, path, params=None):
    url = cfg["base_url"] + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = "Loyverse is busy. Try again in a minute."
    for attempt in range(4):
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + cfg["token"], "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise LoyverseError(f"Loyverse refused the access token (HTTP {e.code}). "
                                    "Check the token in loyverse_config.json.")
            if e.code == 429 or e.code >= 500:      # temporary: wait and retry
                last = f"Loyverse returned a temporary error (HTTP {e.code})."
                try:
                    wait = int(e.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    wait = 0
                if attempt < 3:
                    time.sleep(min(wait or 2 ** (attempt + 1), 30))
                    continue
                break
            raise LoyverseError(f"Loyverse returned an error (HTTP {e.code}).")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = f"Could not reach Loyverse: {getattr(e, 'reason', e)}"
            if attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            break
        except (ValueError, OSError):
            raise LoyverseError("Loyverse sent a reply that could not be read.")
    raise LoyverseError(last)


def db_conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA synchronous = NORMAL")
    return c


def setting_get(db, key, default=""):
    r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r and r["value"] is not None else default


def setting_set(db, key, value):
    db.execute("INSERT INTO settings (key, value) VALUES (?,?) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def iso_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_iso(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def local_str(text):
    try:
        return parse_iso(text).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return now()


def lv_add_issue(db, kind, key, item_name, detail, payload=None):
    # Keep one active warning per issue kind + normalized product name; repeated sales refresh it.
    existing = db.execute("SELECT id, payload FROM lv_issues WHERE kind=? AND lower(trim(item_name))=lower(trim(?)) AND resolved=0 ORDER BY id LIMIT 1", (kind, item_name)).fetchone()
    if existing:
        saved_payload = payload
        # Keep every pending receipt line for an unmatched item while showing only one warning per item.
        if kind == "unmatched" and payload:
            try:
                prior = json.loads(existing["payload"] or "null")
            except (TypeError, ValueError):
                prior = None
            pending = prior if isinstance(prior, list) else ([prior] if isinstance(prior, dict) else [])
            pending.append(payload)
            saved_payload = pending
        db.execute("UPDATE lv_issues SET receipt_key=?, detail=?, payload=?, created_at=? WHERE id=?",
                   (key, detail, json.dumps(saved_payload) if saved_payload else None, now(), existing["id"]))
        return
    db.execute("INSERT INTO lv_issues (kind, receipt_key, item_name, detail, payload, created_at) "
               "VALUES (?,?,?,?,?,?)",
               (kind, key, item_name, detail, json.dumps(payload) if payload else None, now()))


def lv_norm(name):
    return " ".join(str(name or "").split()).lower()


def lv_is_ignored(db, name):
    return db.execute("SELECT 1 FROM lv_ignore WHERE lv_name=?", (lv_norm(name),)).fetchone() is not None


def lv_ignore_name(db, name):
    """Put a Loyverse item on the ignore list and clear its waiting warnings."""
    key = lv_norm(name)
    if not key:
        return 0
    db.execute("INSERT OR IGNORE INTO lv_ignore (lv_name, created_at) VALUES (?,?)", (key, now()))
    n = 0
    for it in db.execute("SELECT id, item_name FROM lv_issues WHERE kind='unmatched' AND resolved=0").fetchall():
        if lv_norm(it["item_name"]) == key:
            db.execute("UPDATE lv_issues SET resolved=1 WHERE id=?", (it["id"],))
            n += 1
    db.commit()
    return n


def lv_match_product(db, iname, vname):
    names = []
    if vname:
        names += [f"{iname} {vname}", f"{iname} - {vname}", f"{iname} ({vname})"]
    names.append(iname)
    for n in names:
        r = db.execute("SELECT product_id FROM lv_alias WHERE lower(lv_name)=lower(?)", (n.strip(),)).fetchone()
        if r:
            return r["product_id"]
    for n in names:
        r = db.execute("SELECT id FROM products WHERE lower(trim(name))=lower(trim(?))", (n,)).fetchone()
        if r:
            return r["id"]
    return None


def lv_apply_line(db, pl, pid):
    """Apply one receipt line. pl: receipt_key, receipt_type, branch_id, qty, when, pay_type, customer, item_name"""
    qty = float(pl["qty"])
    row = db.execute("SELECT qty FROM stock WHERE product_id=? AND branch_id=?",
                     (pid, pl["branch_id"])).fetchone()
    have = row["qty"] if row else 0
    refund = pl["receipt_type"] == "REFUND"
    if refund:
        delta, sale_qty, tag = qty, -qty, f"Loyverse refund #{pl['receipt_key']}"
    else:
        take = min(qty, have)
        delta, sale_qty, tag = -take, qty, f"Loyverse #{pl['receipt_key']}"
        if take < qty:
            lv_add_issue(db, "shortfall", pl["receipt_key"], pl["item_name"],
                         f"Loyverse sold {fmt(qty)} but this app only had {fmt(have)}. "
                         f"Stock was set to 0. Check the real count.")
    if delta:
        db.execute("""INSERT INTO stock (product_id, branch_id, qty) VALUES (?,?,?)
                      ON CONFLICT(product_id, branch_id) DO UPDATE SET qty = qty + excluded.qty""",
                   (pid, pl["branch_id"], delta))
        db.execute("INSERT INTO transactions (product_id, branch_id, kind, qty, note, created_at) "
                   "VALUES (?,?,?,?,?,?)",
                   (pid, pl["branch_id"], "IN" if delta > 0 else "OUT", abs(delta), tag, pl["when"]))
    cur = db.execute(
        "INSERT INTO sales (product_id, branch_id, qty, price, total, pay_type, customer, note, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (pid, pl["branch_id"], sale_qty, 0, 0, pl["pay_type"], pl["customer"], tag, pl["when"]))
    db.execute("INSERT INTO lv_lines (receipt_key, product_id, branch_id, delta, sale_id) VALUES (?,?,?,?,?)",
               (pl["receipt_key"], pid, pl["branch_id"], delta, cur.lastrowid))


def lv_reverse_receipt(db, key):
    """Loyverse cancelled a receipt we already applied: undo it (unless it was already cancelled here)."""
    for ln in db.execute("SELECT * FROM lv_lines WHERE receipt_key=?", (key,)).fetchall():
        sale = db.execute("SELECT id FROM sales WHERE id=?", (ln["sale_id"],)).fetchone()
        if not sale:
            continue
        if ln["delta"]:
            row = db.execute("SELECT qty FROM stock WHERE product_id=? AND branch_id=?",
                             (ln["product_id"], ln["branch_id"])).fetchone()
            back = max(0, (row["qty"] if row else 0) - ln["delta"])
            db.execute("""INSERT INTO stock (product_id, branch_id, qty) VALUES (?,?,?)
                          ON CONFLICT(product_id, branch_id) DO UPDATE SET qty = excluded.qty""",
                       (ln["product_id"], ln["branch_id"], back))
            db.execute("INSERT INTO transactions (product_id, branch_id, kind, qty, note, created_at) "
                       "VALUES (?,?,?,?,?,?)",
                       (ln["product_id"], ln["branch_id"], "IN" if -ln["delta"] > 0 else "OUT",
                        abs(ln["delta"]), f"Loyverse #{key} cancelled", now()))
        db.execute("DELETE FROM sales WHERE id=?", (ln["sale_id"],))
    db.execute("DELETE FROM lv_lines WHERE receipt_key=?", (key,))
    db.execute("UPDATE lv_receipts SET state='reversed', note='cancelled in Loyverse' WHERE receipt_key=?", (key,))


def lv_classify(r, cfg, getcust):
    total = float(r.get("total_money") or 0)
    pays = " ".join(f"{p.get('name') or ''} {p.get('type') or ''}" for p in (r.get("payments") or [])).lower()
    customer = getcust(r.get("customer_id")) if r.get("customer_id") else ""
    if str(r.get("receipt_type") or "SALE").upper() == "REFUND":
        return "PAID", customer
    # An explicit Loyverse free/no-charge tender takes priority over credit keywords.
    free_words = ("free", "no charge", "no-charge", "complimentary")
    if any(w in pays for w in free_words):
        return "FREE", customer
    if any(w in pays for w in cfg["credit_words"]):
        return "CREDIT", customer or "Walk-in (Loyverse)"
    if total == 0:
        return "FREE", customer
    return "PAID", customer


def lv_process_receipt(db, cfg, r, store_map, start_from, getcust, account_id="default"):
    raw_key = str(r.get("receipt_number") or r.get("id") or "").strip()
    key = raw_key if account_id == "default" else account_id + "::" + raw_key
    if not key:
        return 0
    old = db.execute("SELECT state FROM lv_receipts WHERE receipt_key=?", (key,)).fetchone()
    cancelled = bool(r.get("cancelled_at"))
    rtype = str(r.get("receipt_type") or "SALE").upper()
    created = r.get("receipt_date") or r.get("created_at")
    if old:
        if cancelled and old["state"] == "applied":
            db.execute("BEGIN IMMEDIATE")
            lv_reverse_receipt(db, key)
            db.commit()
            return 1
        return 0
    try:
        if start_from and parse_iso(created) < start_from:
            return 0
    except (ValueError, TypeError):
        pass
    bid = store_map.get(r.get("store_id"))
    state, note = "applied", ""
    if cancelled:
        state, note = "skipped", "cancelled"
    elif not bid:
        state, note = "skipped", "store not synced"
    db.execute("BEGIN IMMEDIATE")
    try:
        if state == "applied":
            pay_type, customer = lv_classify(r, cfg, getcust)
            when = local_str(created)
            for li in (r.get("line_items") or []):
                qty = abs(float(li.get("quantity") or 0))
                if qty <= 0:
                    continue
                iname = str(li.get("item_name") or "").strip()
                vname = str(li.get("variant_name") or "").strip()
                shown = f"{iname} {vname}".strip()
                pl = {"receipt_key": key, "receipt_type": "REFUND" if rtype == "REFUND" else "SALE",
                      "branch_id": bid, "qty": qty, "when": when, "pay_type": pay_type,
                      "customer": customer, "item_name": shown}
                # Items on the ignore list are not tracked in this app: no stock change, no warning.
                if lv_is_ignored(db, shown) or (vname and lv_is_ignored(db, iname)):
                    continue
                # A configured group/bundle expands one Loyverse item into its stock components.
                group_rows = db.execute(
                    "SELECT product_id, qty FROM lv_group_items WHERE lower(trim(lv_name))=lower(trim(?)) ORDER BY product_id",
                    (shown,)).fetchall()
                if not group_rows and vname:
                    group_rows = db.execute(
                        "SELECT product_id, qty FROM lv_group_items WHERE lower(trim(lv_name))=lower(trim(?)) ORDER BY product_id",
                        (iname,)).fetchall()
                if group_rows:
                    for component in group_rows:
                        component_pl = dict(pl)
                        component_pl["qty"] = qty * float(component["qty"])
                        component_pl["item_name"] = shown
                        lv_apply_line(db, component_pl, component["product_id"])
                else:
                    pid = lv_match_product(db, iname, vname)
                    if pid is None:
                        lv_add_issue(db, "unmatched", key, shown,
                                     f"Loyverse item “{shown}” is not mapped to a product in this app ({fmt(qty)} sold). Add it or match it below, or configure it on Product Groups.", pl)
                    else:
                        lv_apply_line(db, pl, pid)
        db.execute("INSERT INTO lv_receipts (receipt_key, receipt_type, store_id, state, note, created_at, applied_at) "
                   "VALUES (?,?,?,?,?,?,?)", (key, rtype, r.get("store_id"), state, note, local_str(created), now()))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return 1


def lv_refresh_pos(db, cfg, account_id, stores):
    """Load this account's POS devices (tills) from Loyverse and keep a local copy."""
    names = {st.get("id"): st.get("name") for st in (stores or [])}
    devices, cursor = [], None
    for _ in range(10):
        params = {"limit": 250}
        if cursor:
            params["cursor"] = cursor
        data = lv_get(cfg, "/pos_devices", params)
        devices += data.get("pos_devices") or []
        cursor = data.get("cursor")
        if not cursor:
            break
    db.execute("DELETE FROM lv_pos WHERE account_id=?", (account_id,))
    for d in devices:
        if not d.get("id") or d.get("deleted_at"):
            continue
        db.execute("INSERT OR REPLACE INTO lv_pos (account_id, pos_id, name, store_name, activated, synced_at) VALUES (?,?,?,?,?,?)",
                   (account_id, d["id"], str(d.get("name") or "POS"), names.get(d.get("store_id")) or "",
                    1 if d.get("activated", True) else 0, now()))
    db.commit()
    return db.execute("SELECT COUNT(*) FROM lv_pos WHERE account_id=?", (account_id,)).fetchone()[0]


def lv_refresh_stores(db, cfg, account_id="default"):
    data = lv_get(cfg, "/stores")
    for st in (data.get("stores") or []):
        sid = st.get("id")
        if not sid:
            continue
        map_key = lv_store_key(account_id, sid)
        raw_name = st.get("name") or sid
        shown_name = raw_name if account_id == "default" else f"{cfg.get('name', account_id)} — {raw_name}"
        db.execute("INSERT OR IGNORE INTO lv_store_map (lv_store_id, lv_store_name, branch_id) VALUES (?,?,NULL)",
                   (map_key, shown_name))
        db.execute("UPDATE lv_store_map SET lv_store_name=? WHERE lv_store_id=?", (shown_name, map_key))
    db.commit()
    return data.get("stores") or []


def lv_save_customer(db, customer_id, raw):
    """Keep the contact fields supplied by Loyverse for the Credit page."""
    name = str(raw.get("name") or "").strip()
    if not name:
        return ""
    place_parts = []
    for field in ("address", "city", "region", "country_code"):
        value = raw.get(field)
        if value and str(value).strip() and str(value).strip() not in place_parts:
            place_parts.append(str(value).strip())
    place = ", ".join(place_parts)
    phone = str(raw.get("phone_number") or raw.get("phone") or "").strip()
    db.execute(
        """INSERT INTO lv_customers(customer_id,name,place,phone,updated_at) VALUES(?,?,?,?,?)
           ON CONFLICT(customer_id) DO UPDATE SET name=excluded.name, place=excluded.place,
           phone=excluded.phone, updated_at=excluded.updated_at""",
        (str(customer_id), name, place, phone, now()))
    return name


def lv_sync_once():
    """Sync each configured account separately; receipt keys are namespaced to prevent collisions."""
    if not LV_LOCK.acquire(blocking=False):
        return False, "A sync is already running."
    db = None
    try:
        configs = lv_configs()
        if isinstance(configs, dict) and configs.get("error"):
            return False, configs["error"]
        if not configs:
            return False, "No Loyverse accounts configured. Add an account on the Loyverse page."
        db = db_conn()
        start_txt = setting_get(db, "lv_start_from")
        if not start_txt:
            raise LoyverseError("Press ‘Start syncing’ first.")
        start_from = parse_iso(start_txt)
        since = parse_iso(setting_get(db, "lv_last_sync") or start_txt)
        checked_at = datetime.now(timezone.utc)
        total_applied = 0
        account_errors = []
        for cfg in configs:
            account_id = cfg["id"]
            try:
                lv_refresh_stores(db, cfg, account_id)
                rows = db.execute("SELECT * FROM lv_store_map ORDER BY lv_store_name").fetchall()
                if account_id == "default":
                    account_rows = [m for m in rows if "::" not in m["lv_store_id"]]
                else:
                    prefix = account_id + "::"
                    account_rows = [m for m in rows if m["lv_store_id"].startswith(prefix)]
                if any(m["branch_id"] is None for m in account_rows):
                    account_errors.append(f"{cfg['name']}: map every store to a branch or ‘Don't sync’.")
                    continue
                if not account_rows:
                    account_errors.append(f"{cfg['name']}: no stores returned by Loyverse.")
                    continue
                store_map = {}
                for m in account_rows:
                    actual_id = m["lv_store_id"] if account_id == "default" else m["lv_store_id"][len(account_id)+2:]
                    store_map[actual_id] = m["branch_id"] or 0
                params = {"updated_at_min": iso_utc(since - timedelta(minutes=10)), "limit": 250}
                receipts = []
                for _ in range(40):
                    data = lv_get(cfg, "/receipts", params)
                    receipts += data.get("receipts") or []
                    if not data.get("cursor"):
                        break
                    params["cursor"] = data["cursor"]
                receipts.sort(key=lambda r: str(r.get("created_at") or ""))
                # Fetch customer details over the network BEFORE any database write lock is
                # taken. Doing HTTP calls inside the write transaction froze every page.
                raw_customers = {}
                for r in receipts:
                    cid = r.get("customer_id")
                    if not cid or cid in raw_customers or r.get("cancelled_at"):
                        continue
                    key = str(r.get("receipt_number") or r.get("id") or "").strip()
                    if not key:
                        continue
                    key = key if account_id == "default" else account_id + "::" + key
                    if db.execute("SELECT 1 FROM lv_receipts WHERE receipt_key=?", (key,)).fetchone():
                        continue   # already processed: customer not needed again
                    try:
                        raw_customers[cid] = lv_get(cfg, "/customers/" + urllib.parse.quote(str(cid))) or {}
                    except LoyverseError:
                        raw_customers[cid] = None
                saved_names = {}
                def getcust(cid, _raw=raw_customers, _saved=saved_names):
                    # Database-only: runs inside the short write transaction, no network.
                    if cid not in _saved:
                        _saved[cid] = lv_save_customer(db, cid, _raw[cid]) if _raw.get(cid) else ""
                    return _saved[cid]
                for receipt in receipts:
                    try:
                        total_applied += lv_process_receipt(db, cfg, receipt, store_map, start_from, getcust, account_id)
                    except LoyverseError:
                        raise
                    except Exception as e:     # isolate one bad receipt; the rest still sync
                        try:
                            db.rollback()
                        except Exception:
                            pass
                        print("Loyverse receipt problem:", receipt.get("receipt_number"), e)
                        account_errors.append(f"{cfg['name']}: receipt {receipt.get('receipt_number')} failed ({e})")
            except LoyverseError as e:
                account_errors.append(f"{cfg['name']}: {e}")
        # Do not advance the shared cursor if any account failed; successful accounts are
        # idempotent and can safely be checked again on the next pass.
        if not account_errors:
            setting_set(db, "lv_last_sync", iso_utc(checked_at))
        setting_set(db, "lv_last_ok", now())
        setting_set(db, "lv_last_error", " | ".join(account_errors))
        db.commit()
        if account_errors:
            return False, f"Checked {len(configs)} account(s); applied {total_applied} receipt(s). Issues: " + " | ".join(account_errors)
        return True, f"Checked {len(configs)} Loyverse account(s). {total_applied} new or changed receipt(s) applied."
    except Exception as e:
        try:
            if db is not None:
                db.rollback()
                setting_set(db, "lv_last_error", f"Unexpected problem: {e}")
                db.commit()
        except Exception:
            pass
        return False, f"Unexpected problem: {e}"
    finally:
        if db is not None:
            db.close()
        LV_LOCK.release()


def backup_db(keep=7):
    """Once a day, write a consistent copy of the database and keep the newest few."""
    folder = os.path.join(BASE_DIR, "backups")
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, "stock_data-" + datetime.now().strftime("%Y-%m-%d") + ".db")
    if os.path.exists(target):
        return
    src, dst = sqlite3.connect(DB_PATH, timeout=30), sqlite3.connect(target + ".tmp")
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    os.replace(target + ".tmp", target)
    for old in sorted(f for f in os.listdir(folder) if f.endswith(".db"))[:-keep]:
        os.remove(os.path.join(folder, old))


def backup_loop():
    while True:
        try:
            backup_db()
        except Exception as e:
            print("Backup problem:", e)
        time.sleep(3600)


def lv_loop():
    failures = 0
    while True:
        wait = 60
        try:
            cfgs = lv_configs()
            if cfgs and not (isinstance(cfgs, dict) and cfgs.get("error")):
                wait = cfgs[0]["interval"]
                db = db_conn()
                try:
                    on = setting_get(db, "lv_enabled") == "1"
                finally:
                    db.close()
                if on:
                    ok, _msg = lv_sync_once()
                    failures = 0 if ok else failures + 1
        except Exception as e:
            failures += 1
            print("Loyverse loop problem:", e)
        # Back off (up to 10 minutes) while Loyverse or the network keeps failing.
        time.sleep(min(wait * (2 ** min(failures, 4)) if failures else wait, 600))


LOYVERSE_BODY = """
<style>
  .kv{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;font-size:14px;margin:8px 0}
  .kv b{color:#6b7280;font-weight:600}
  pre{background:#f3f4f6;padding:10px;border-radius:8px;overflow-x:auto;font-size:13px}
  .okbox{background:#dcfce7;color:#166534;padding:8px 10px;border-radius:8px;font-size:14px}
  .bad{color:#991b1b}
  .inl{display:inline}
</style>

{% if config_error and can_open_block('loyverse_accounts') %}<div class="card bad"><strong>{{ config_error }}</strong></div>{% endif %}
{% if can_open_block('loyverse_accounts') %}<div class="card">
  <h2>Loyverse accounts ({{ account_rows|length }}/{{ max_accounts }})</h2>
  <p>Connect up to {{ max_accounts }} separate Loyverse accounts. Access tokens are stored on this server and never displayed again.</p>
  {% for a in account_rows %}
    <div class="row" style="align-items:center;border:1px solid #e5e7eb;border-radius:8px;padding:8px;margin:6px 0">
      <div style="flex:1"><strong>{{ a.name }}</strong><br><small>Token saved · ID {{ a.id }}</small>
        {% set plist = pos_by_account.get(a.id, []) %}
        <div style="margin-top:6px;font-size:13px">
          <b style="color:#6b7280">POS ({{ plist|length }})</b>
          {% for d in plist %}<div>{% if d.activated %}<span style="color:#15803d">●</span>{% else %}<span style="color:#9ca3af">○</span>{% endif %} {{ d.name }}{% if d.store_name %} <small style="color:#6b7280">· {{ d.store_name }}</small>{% endif %}{% if not d.activated %} <small style="color:#9ca3af">(not activated)</small>{% endif %}</div>
          {% else %}<div style="color:#6b7280">Not loaded yet — press “Test all account connections and load stores”.</div>{% endfor %}
        </div>
      </div>
      <form method="post" action="{{ url_for('lv_account_delete', account_id=a.id) }}" onsubmit="return confirm('Remove this account connection? Existing sales history will remain.')"><button class="del">Remove</button></form>
    </div>
  {% else %}
    <p><strong>No Loyverse accounts configured.</strong> Add the first account below.</p>
  {% endfor %}
  {% if account_rows|length < max_accounts %}
  <form method="post" action="{{ url_for('lv_account_add') }}" style="margin-top:12px">
    <label>Account name</label><input name="account_name" required maxlength="60" placeholder="e.g. Main Shop">
    <label style="margin-top:8px">Loyverse access token</label><input name="token" required autocomplete="off" placeholder="Paste access token">
    <button style="margin-top:10px">Add account and test later</button>
  </form>
  {% endif %}
  <form method="post" action="{{ url_for('lv_test') }}" style="margin-top:10px"><button>Test all account connections and load stores</button></form>
</div>{% endif %}

{% if account_rows and can_open_block('loyverse_map') %}
<div class="card">
  <h2>1 · Match Loyverse stores to your branches</h2>
  {% if not maps %}
    <p>Press <b>Test connection</b> above to load your Loyverse stores.</p>
  {% else %}
  <form method="post" action="{{ url_for('lv_map') }}">
    <div class="scroll"><table>
      <tr><th>Loyverse store</th><th>This app's branch</th></tr>
      {% for m in maps %}
      <tr>
        <td>{{ m.lv_store_name }}</td>
        <td><select name="map_{{ m.lv_store_id }}">
          <option value="" {{ 'selected' if m.branch_id is none }}>— choose —</option>
          <option value="0" {{ 'selected' if m.branch_id == 0 }}>Don't sync this store</option>
          {% for b in branches %}<option value="{{ b.id }}" {{ 'selected' if m.branch_id == b.id }}>{{ b.name }}</option>{% endfor %}
        </select></td>
      </tr>
      {% endfor %}
    </table></div>
    <button style="margin-top:10px">Save matching</button>
  </form>
  {% endif %}
</div>
{% endif %}

{% if account_rows and can_open_block('loyverse_sync') %}<div class="card">
  <h2>2 · Syncing</h2>
  <div class="kv">
    <b>Status</b><span>{% if enabled %}<strong style="color:#166534">ON</strong> – checking Loyverse every {{ cfg.interval }} seconds{% else %}<strong>OFF</strong>{% endif %}</span>
    <b>Counting sales from</b><span>{{ start_from or '—' }}</span>
    <b>Last check</b><span>{{ last_ok or '—' }}</span>
    {% if last_error %}<b>Problem</b><span class="bad">{{ last_error }}</span>{% endif %}
    <b>Receipts imported</b><span>{{ n_applied }}</span>
  </div>
  {% if not enabled %}
  <form method="post" action="{{ url_for('lv_start') }}" class="row" style="align-items:end">
    <div><label style="font-size:12px;color:#6b7280">Count Loyverse sales from (leave empty = from now)</label>
      <input type="date" name="from" style="width:100%"></div>
    <button class="in">Start syncing</button>
  </form>
  <small>Only sales after this moment change your stock. Set your opening stock in this app first.</small>
  {% else %}
  <div class="row">
    <form method="post" action="{{ url_for('lv_sync') }}"><button style="width:100%">Sync now</button></form>
    <form method="post" action="{{ url_for('lv_stop') }}"><button class="del" style="width:100%;padding:8px">Stop syncing</button></form>
  </div>
  {% endif %}
</div>{% endif %}

{% if account_rows and can_open_block('loyverse_issues') %}<div class="card">
  <h2>Needs your attention ({{ issues|length }})</h2>
  {% if issues|selectattr('kind', 'equalto', 'unmatched')|list %}
  <form method="post" action="{{ url_for('lv_add_products') }}" style="border:1px solid #fde68a;background:#fffbeb;border-radius:10px;padding:10px;margin-bottom:12px">
    <strong>Bulk add unknown Loyverse products</strong>
    <p style="font-size:13px">Select several missing items to create them in this app at once. They will start with zero stock; enter opening stock on the Products page. Pending sale lines will be processed and any stock shortfall will remain visible as a warning.</p>
    {% for i in issues if i.kind == 'unmatched' %}
      <label style="display:flex;gap:8px;align-items:center;margin:6px 0"><input type="checkbox" name="issue_ids" value="{{ i.id }}" style="width:auto"> <span>{{ i.item_name }} <small>— {{ i.detail }}</small></span></label>
    {% endfor %}
    <button class="in" type="submit">Add selected products</button>
  </form>
  {% endif %}
  {% for i in issues %}
    <div style="border:1px solid #fde68a;background:#fffbeb;border-radius:10px;padding:10px;margin-bottom:8px">
      <div><strong>{{ 'Product not found' if i.kind == 'unmatched' else 'Stock was too low' }}: {{ i.item_name }}</strong>
        <small> · receipt #{{ i.receipt_key }} · {{ i.created_at }}</small></div>
      <div style="font-size:13px;margin:4px 0 8px">{{ i.detail }}</div>
      {% if i.kind == 'unmatched' %}
      <form method="post" action="{{ url_for('lv_match', iid=i.id) }}" class="row">
        <select name="product_id" required>
          <option value="">Which product is this?</option>
          {% for p in products %}<option value="{{ p.id }}">{{ p.name }}</option>{% endfor %}
        </select>
        <button class="in">Match and apply</button>
      </form>
      {% endif %}
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px">
        <form method="post" action="{{ url_for('lv_dismiss', iid=i.id) }}"><button class="del">Dismiss</button></form>
        {% if i.kind == 'unmatched' %}<form method="post" action="{{ url_for('lv_ignore') }}" onsubmit="return confirm('Ignore {{ i.item_name|e }}? Its future Loyverse sales will not change stock here.')"><input type="hidden" name="lv_name" value="{{ i.item_name }}"><button class="del" title="Never count this Loyverse item in this app">Ignore this item</button></form>{% endif %}
      </div>
    </div>
  {% else %}
    <p style="margin:0;color:#6b7280">Nothing to fix.</p>
  {% endfor %}
  <small>When Loyverse sells a product this app doesn't know, match it once and future sales match automatically. <b>Dismiss</b> only clears this warning; <b>Ignore this item</b> also skips all its future sales.</small>
  {% if ignored %}
  <h3 style="margin:16px 0 6px;font-size:15px">Ignore list ({{ ignored|length }})</h3>
  {% for g in ignored %}
  <form method="post" action="{{ url_for('lv_unignore') }}" style="display:flex;gap:8px;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:1px solid #e5e7eb">
    <span>{{ g.lv_name }}</span><input type="hidden" name="lv_name" value="{{ g.lv_name }}"><button class="del">Stop ignoring</button>
  </form>
  {% endfor %}
  {% endif %}
</div>{% endif %}

{% if account_rows and can_open_block('loyverse_receipts') %}<div class="card">
  <h2>Recent Loyverse receipts</h2>
  <div class="scroll"><table>
    <tr><th>When</th><th>Receipt</th><th>Type</th><th>Result</th></tr>
    {% for r in recent %}
    <tr><td>{{ (r.created_at or '')[5:16] }}</td><td>#{{ r.receipt_key }}</td><td>{{ r.receipt_type }}</td>
        <td>{{ r.state }}{% if r.note %} · {{ r.note }}{% endif %}</td></tr>
    {% else %}<tr><td colspan="4">None yet.</td></tr>{% endfor %}
  </table></div>
</div>{% endif %}
"""


GROUPS_BODY = """
{% if can_open_block('groups_editor') %}<div class="card">
  <h2>Product Groups / Bundle Mapping</h2>
  <p>Use this page when one Loyverse item represents several separate products in this app. Example: Loyverse “Book and Pen” can deduct 1 Book and 1 Pen for every bundle sold. A bundle can contain different quantities of each component.</p>
  <form method="post" action="{{ url_for('lv_group_save') }}">
    <label>Loyverse item name (must match the sale item name)</label>
    <input name="lv_name" required maxlength="180" placeholder="e.g. Book and Pen">
    <h3>Choose components and quantities per bundle</h3>
    <div class="scroll"><table><tr><th>Include</th><th>Stock product</th><th>Qty per bundle</th></tr>
    {% for p in products %}<tr><td><input type="checkbox" name="product_ids" value="{{ p.id }}" style="width:auto"></td><td>{{ p.name }}</td><td><input type="number" name="qty_{{ p.id }}" min="0.01" step="0.01" value="1" style="max-width:120px"></td></tr>{% endfor %}
    </table></div>
    <button style="margin-top:10px">Save product group</button>
  </form>
</div>{% endif %}
{% if can_open_block('groups_saved') %}<div class="card"><h2>Saved groups ({{ groups|length }})</h2>
  {% for g in groups %}<div style="border:1px solid #e5e7eb;border-radius:8px;padding:10px;margin:8px 0">
    <strong>{{ g.lv_name }}</strong>
    <ul>{% for c in g.components %}<li>{{ c.name }} × {{ c.qty }}</li>{% endfor %}</ul>
    <form method="post" action="{{ url_for('lv_group_delete') }}"><input type="hidden" name="lv_name" value="{{ g.lv_name }}"><button class="del">Delete group</button></form>
  </div>{% else %}<p>No product groups configured yet.</p>{% endfor %}
</div>{% endif %}
"""

@app.route("/loyverse/groups")
def loyverse_groups_page():
    db = get_db()
    products = all_products()
    rows = db.execute("SELECT DISTINCT lv_name FROM lv_group_items ORDER BY lv_name COLLATE NOCASE").fetchall()
    groups = []
    for row in rows:
        components = db.execute("SELECT p.name, gi.qty FROM lv_group_items gi JOIN products p ON p.id=gi.product_id WHERE lower(trim(gi.lv_name))=lower(trim(?)) ORDER BY p.name", (row["lv_name"],)).fetchall()
        groups.append({"lv_name": row["lv_name"], "components": components})
    return render(GROUPS_BODY, products=products, groups=groups)

@app.route("/loyverse/groups/save", methods=["POST"])
def lv_group_save():
    db = get_db()
    name = request.form.get("lv_name", "").strip()
    ids = request.form.getlist("product_ids")
    if not name or not ids:
        flash("Enter the Loyverse item name and select at least one component product.", "error")
        return redirect(url_for("loyverse_groups_page"))
    components = []
    try:
        for raw_id in ids:
            pid = int(raw_id)
            qty = float(request.form.get(f"qty_{pid}", "1"))
            if qty <= 0 or not db.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone():
                raise ValueError
            components.append((pid, qty))
    except ValueError:
        flash("Component quantities must be greater than zero.", "error")
        return redirect(url_for("loyverse_groups_page"))
    db.execute("BEGIN IMMEDIATE")
    db.execute("DELETE FROM lv_group_items WHERE lower(trim(lv_name))=lower(trim(?))", (name,))
    for pid, qty in components:
        db.execute("INSERT INTO lv_group_items (lv_name, product_id, qty) VALUES (?,?,?)", (name, pid, qty))
    db.commit()
    flash(f"Saved group ‘{name}’ with {len(components)} component product(s). Future Loyverse sales will deduct each component from the mapped branch.")
    return redirect(url_for("loyverse_groups_page"))

@app.route("/loyverse/groups/delete", methods=["POST"])
def lv_group_delete():
    name = request.form.get("lv_name", "").strip()
    db = get_db()
    db.execute("DELETE FROM lv_group_items WHERE lower(trim(lv_name))=lower(trim(?))", (name,))
    db.commit()
    flash(f"Deleted product group ‘{name}’.")
    return redirect(url_for("loyverse_groups_page"))

@app.route("/loyverse/add-products", methods=["POST"])
def lv_add_products():
    db = get_db()
    selected = request.form.getlist("issue_ids")
    if not selected:
        flash("Select at least one unknown Loyverse product.", "error")
        return redirect(url_for("loyverse_page"))
    added = matched = 0
    db.execute("BEGIN IMMEDIATE")
    try:
        for raw_id in selected:
            try: iid = int(raw_id)
            except ValueError: continue
            issue = db.execute("SELECT * FROM lv_issues WHERE id=? AND kind='unmatched' AND resolved=0", (iid,)).fetchone()
            if not issue: continue
            name = str(issue["item_name"] or "").strip()
            if not name: continue
            product = db.execute("SELECT id FROM products WHERE lower(trim(name))=lower(trim(?))", (name,)).fetchone()
            if product:
                pid = product["id"]
                matched += 1
            else:
                try:
                    cur = db.execute("INSERT INTO products (name, unit, low_limit) VALUES (?,?,0)", (name, "pcs"))
                    pid = cur.lastrowid
                    added += 1
                except sqlite3.IntegrityError:
                    product = db.execute("SELECT id FROM products WHERE lower(trim(name))=lower(trim(?))", (name,)).fetchone()
                    if not product: continue
                    pid = product["id"]
                    matched += 1
            db.execute("INSERT INTO lv_alias (lv_name, product_id) VALUES (?,?) ON CONFLICT(lv_name) DO UPDATE SET product_id=excluded.product_id", (name.lower(), pid))
            pending = db.execute("SELECT * FROM lv_issues WHERE kind='unmatched' AND resolved=0 AND lower(trim(item_name))=lower(trim(?))", (name,)).fetchall()
            for it in pending:
                payloads = json.loads(it["payload"] or "null")
                payloads = payloads if isinstance(payloads, list) else ([payloads] if isinstance(payloads, dict) else [])
                for payload in payloads:
                    lv_apply_line(db, payload, pid)
                db.execute("UPDATE lv_issues SET resolved=1 WHERE id=?", (it["id"],))
        db.commit()
    except Exception:
        db.rollback()
        raise
    flash(f"Created {added} product(s); matched {matched} existing product(s). Pending Loyverse lines were processed. Check stock warnings and set opening stock for newly created products.", "ok")
    return redirect(url_for("loyverse_page"))


@app.route("/loyverse")
def loyverse_page():
    db = get_db()
    configs = lv_configs()
    config_error = configs.get("error") if isinstance(configs, dict) else None
    if isinstance(configs, dict):
        configs = []
    accounts = [{"id": a["id"], "name": a["name"]} for a in configs]
    maps = db.execute("SELECT * FROM lv_store_map ORDER BY lv_store_name").fetchall()
    pos_by_account = {}
    for r in db.execute("SELECT * FROM lv_pos ORDER BY store_name COLLATE NOCASE, name COLLATE NOCASE"):
        pos_by_account.setdefault(r["account_id"], []).append(r)
    return render(
        LOYVERSE_BODY, pos_by_account=pos_by_account, cfg={"interval": configs[0]["interval"]} if configs else None,
        account_rows=accounts, max_accounts=MAX_LOYVERSE_ACCOUNTS, config_error=config_error, branches=all_branches(), products=all_products(),
        maps=maps, enabled=setting_get(db, "lv_enabled") == "1",
        ignored=db.execute("SELECT lv_name FROM lv_ignore ORDER BY lv_name").fetchall(),
        start_from=local_str(setting_get(db, "lv_start_from")) if setting_get(db, "lv_start_from") else "",
        last_ok=setting_get(db, "lv_last_ok"), last_error=setting_get(db, "lv_last_error"),
        n_applied=db.execute("SELECT COUNT(*) FROM lv_receipts WHERE state='applied'").fetchone()[0],
        issues=db.execute("SELECT * FROM lv_issues WHERE resolved=0 ORDER BY id DESC LIMIT 100").fetchall(),
        recent=db.execute("SELECT * FROM lv_receipts ORDER BY rowid DESC LIMIT 15").fetchall())


@app.route("/loyverse/accounts/add", methods=["POST"])
def lv_account_add():
    name = request.form.get("account_name", "").strip()
    token = request.form.get("token", "").strip()
    if not name or not token:
        flash("Enter an account name and access token.", "error")
        return redirect(url_for("loyverse_page"))
    raw = lv_config_file()
    accounts = raw.get("accounts") if isinstance(raw.get("accounts"), list) else []
    if not accounts and raw.get("token"):
        accounts = [{"id": "default", "name": str(raw.get("name") or "Loyverse account"), "token": raw["token"],
                     "base_url": raw.get("base_url"), "credit_words": raw.get("credit_words")}]
    if len(accounts) >= MAX_LOYVERSE_ACCOUNTS:
        flash(f"A maximum of {MAX_LOYVERSE_ACCOUNTS} Loyverse accounts is supported.", "error")
        return redirect(url_for("loyverse_page"))
    if any(str(a.get("name", "")).casefold() == name.casefold() for a in accounts):
        flash("Use a unique account name so the stores are easy to identify.", "error")
        return redirect(url_for("loyverse_page"))
    accounts.append({"id": "acct" + secrets.token_hex(4), "name": name, "token": token})
    try:
        lv_save_accounts(accounts, raw)
    except OSError:
        flash("Could not save the account configuration. Check the app folder permissions.", "error")
        return redirect(url_for("loyverse_page"))
    flash(f"Added {name}. Press ‘Test all account connections and load stores’ next.")
    return redirect(url_for("loyverse_page"))


@app.route("/loyverse/accounts/delete/<account_id>", methods=["POST"])
def lv_account_delete(account_id):
    raw = lv_config_file()
    accounts = raw.get("accounts") if isinstance(raw.get("accounts"), list) else []
    if not accounts and raw.get("token"):
        accounts = [{"id": "default", "name": str(raw.get("name") or "Loyverse account"), "token": raw["token"],
                     "base_url": raw.get("base_url"), "credit_words": raw.get("credit_words")}]
    updated = [a for a in accounts if str(a.get("id")) != account_id]
    if len(updated) == len(accounts):
        flash("Account was not found.", "error")
    else:
        try:
            lv_save_accounts(updated, raw)
            # Remove only this account's store-to-branch mappings; historical receipts remain.
            db = get_db()
            db.execute("DELETE FROM lv_pos WHERE account_id=?", (account_id,))
            if account_id == "default":
                db.execute("DELETE FROM lv_store_map WHERE instr(lv_store_id, '::')=0")
            else:
                db.execute("DELETE FROM lv_store_map WHERE lv_store_id LIKE ?", (account_id + "::%",))
            db.commit()
            flash("Account connection removed. Existing sales history and stock records were kept.")
        except OSError:
            flash("Could not update account configuration.", "error")
    return redirect(url_for("loyverse_page"))


@app.route("/loyverse/test", methods=["POST"])
def lv_test():
    configs = lv_configs()
    if isinstance(configs, dict) and configs.get("error"):
        flash(configs["error"], "error")
        return redirect(url_for("loyverse_page"))
    if not configs:
        flash("Add at least one Loyverse account first.", "error")
        return redirect(url_for("loyverse_page"))
    db = get_db()
    results = []
    for cfg in configs:
        try:
            stores = lv_refresh_stores(db, cfg, cfg["id"])
            lv_get(cfg, "/receipts", {"limit": 1})
            try:
                npos = f"{lv_refresh_pos(db, cfg, cfg['id'], stores)} POS"
            except LoyverseError as e:
                npos = f"POS list not loaded ({e})"
            results.append(f"{cfg['name']}: connected, {len(stores)} store(s), {npos}")
        except LoyverseError as e:
            results.append(f"{cfg['name']}: ERROR — {e}")
    flash("; ".join(results), "error" if any("ERROR" in r for r in results) else "ok")
    return redirect(url_for("loyverse_page"))


@app.route("/loyverse/map", methods=["POST"])
def lv_map():
    db = get_db()
    for m in db.execute("SELECT lv_store_id FROM lv_store_map").fetchall():
        v = request.form.get("map_" + m["lv_store_id"], "")
        if v == "":
            db.execute("UPDATE lv_store_map SET branch_id=NULL WHERE lv_store_id=?", (m["lv_store_id"],))
        else:
            try:
                db.execute("UPDATE lv_store_map SET branch_id=? WHERE lv_store_id=?", (int(v), m["lv_store_id"]))
            except ValueError:
                pass
    db.commit()
    flash("Store matching saved.")
    return redirect(url_for("loyverse_page"))


@app.route("/loyverse/start", methods=["POST"])
def lv_start():
    db = get_db()
    maps = db.execute("SELECT * FROM lv_store_map").fetchall()
    if not maps or any(m["branch_id"] is None for m in maps):
        flash("First match every Loyverse store to a branch (or choose “Don't sync”).", "error")
        return redirect(url_for("loyverse_page"))
    txt = request.form.get("from", "").strip()
    try:
        start = datetime.strptime(txt, "%Y-%m-%d").astimezone() if txt else datetime.now().astimezone()
    except ValueError:
        flash("That date is not valid.", "error")
        return redirect(url_for("loyverse_page"))
    setting_set(db, "lv_start_from", iso_utc(start))
    setting_set(db, "lv_last_sync", "")
    setting_set(db, "lv_enabled", "1")
    setting_set(db, "lv_last_error", "")
    db.commit()
    flash("Syncing is on. New Loyverse sales will update stock here.")
    ok, msg = lv_sync_once()
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("loyverse_page"))


@app.route("/loyverse/stop", methods=["POST"])
def lv_stop():
    db = get_db()
    setting_set(db, "lv_enabled", "0")
    db.commit()
    flash("Syncing stopped. Nothing else changes until you start it again.")
    return redirect(url_for("loyverse_page"))


@app.route("/loyverse/sync", methods=["POST"])
def lv_sync():
    ok, msg = lv_sync_once()
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("loyverse_page"))


@app.route("/loyverse/match/<int:iid>", methods=["POST"])
def lv_match(iid):
    db = get_db()
    try:
        pid = int(request.form.get("product_id", ""))
    except ValueError:
        flash("Choose a product.", "error")
        return redirect(url_for("loyverse_page"))
    issue = db.execute("SELECT * FROM lv_issues WHERE id=? AND kind='unmatched' AND resolved=0", (iid,)).fetchone()
    if not issue or not db.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone():
        flash("That item was already handled.", "error")
        return redirect(url_for("loyverse_page"))
    db.execute("BEGIN IMMEDIATE")
    db.execute("INSERT INTO lv_alias (lv_name, product_id) VALUES (?,?) "
               "ON CONFLICT(lv_name) DO UPDATE SET product_id = excluded.product_id",
               (issue["item_name"].lower(), pid))
    n = 0
    for it in db.execute("SELECT * FROM lv_issues WHERE kind='unmatched' AND resolved=0 "
                         "AND lower(item_name)=lower(?)", (issue["item_name"],)).fetchall():
        pending = json.loads(it["payload"] or "null")
        pending = pending if isinstance(pending, list) else ([pending] if isinstance(pending, dict) else [])
        for payload in pending:
            lv_apply_line(db, payload, pid)
            n += 1
        db.execute("UPDATE lv_issues SET resolved=1 WHERE id=?", (it["id"],))
    db.commit()
    flash(f"Matched. {n} sale line(s) applied to stock. Future sales of this item match automatically.")
    return redirect(url_for("loyverse_page"))


@app.route("/loyverse/ignore", methods=["POST"])
def lv_ignore():
    name = " ".join(request.form.get("lv_name", "").split())
    if name:
        lv_ignore_name(get_db(), name)
        flash(f"Ignoring ‘{name}’. Its Loyverse sales will no longer change stock here.")
    return redirect(url_for("loyverse_page"))


@app.route("/loyverse/unignore", methods=["POST"])
def lv_unignore():
    name = lv_norm(request.form.get("lv_name", ""))
    db = get_db()
    db.execute("DELETE FROM lv_ignore WHERE lv_name=?", (name,))
    db.commit()
    flash(f"‘{name}’ is no longer ignored. Its next Loyverse sale will ask you to match it again.")
    return redirect(url_for("loyverse_page"))


@app.route("/loyverse/dismiss/<int:iid>", methods=["POST"])
def lv_dismiss(iid):
    db = get_db()
    db.execute("UPDATE lv_issues SET resolved=1 WHERE id=?", (iid,))
    db.commit()
    return redirect(url_for("loyverse_page"))


LVMATCH_BODY = """
{% if can_open_block('lvmatch_unmatched') %}<div class="card">
  <h2>Needs matching <small style="font-weight:400;color:#6b7280">· {{ unmatched|length }} Loyverse item(s)</small></h2>
  {% for u in unmatched %}
  <form method="post" action="{{ url_for('lvmatch_link') }}" class="row" style="padding:8px 0;border-bottom:1px solid #e5e7eb;align-items:center">
    <div style="flex:1 1 200px"><strong>{{ u.name }}</strong><br><small>{{ u.lines }} sale line(s) waiting · last {{ u.last }}</small></div>
    <input type="hidden" name="lv_name" value="{{ u.name }}">
    <select name="product_id" required style="flex:1 1 180px"><option value="">Choose app product…</option>{% for p in products %}<option value="{{ p.id }}">{{ p.name }}</option>{% endfor %}</select>
    <button>Match</button>
    <button class="del" type="submit" formaction="{{ url_for('lvmatch_ignore') }}" formnovalidate onclick="return confirm('Ignore this item? Its future Loyverse sales will not change stock here.')">Ignore</button>
  </form>
  {% else %}<p>✓ Every Loyverse item is matched.</p>{% endfor %}
  {% if ignored %}
  <h3 style="margin:16px 0 6px;font-size:15px">Ignore list ({{ ignored|length }})</h3>
  {% for g in ignored %}
  <form method="post" action="{{ url_for('lvmatch_unignore') }}" style="display:flex;gap:8px;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:1px solid #e5e7eb">
    <span>{{ g.lv_name }}</span><input type="hidden" name="lv_name" value="{{ g.lv_name }}"><button class="del">Stop ignoring</button>
  </form>
  {% endfor %}
  {% endif %}
  <small>Matching applies the waiting sales to stock, and future sales of this item match automatically.</small>
</div>{% endif %}
{% if can_open_block('lvmatch_matched') %}<div class="card">
  <h2>Loyverse item → App product</h2>
  <form method="get" class="row" style="margin-bottom:10px">
    <input name="q" value="{{ q }}" placeholder="Search Loyverse or app product name">
    <button>Search</button>
  </form>
  <div class="scroll"><table>
    <tr><th>Loyverse item</th><th>App product</th><th></th></tr>
    {% for m in matched %}
    <tr>
      <td>{{ m.lv_name }}</td>
      <td><strong>{{ m.product }}</strong> <small>{{ m.unit }}</small></td>
      <td><form method="post" action="{{ url_for('lvmatch_unlink') }}" onsubmit="return confirm('Remove this match? Past sales are not changed.')"><input type="hidden" name="lv_name" value="{{ m.lv_name }}"><button class="del">Unlink</button></form></td>
    </tr>
    {% endfor %}
    {% for g in groups %}
    <tr>
      <td>{{ g.lv_name }}</td>
      <td>{% for c in g.components %}{{ c.name }} × {{ c.qty|fmt }}{% if not loop.last %}<br>{% endif %}{% endfor %} <small style="color:#6b7280">(group)</small></td>
      <td></td>
    </tr>
    {% endfor %}
    {% if not matched and not groups %}<tr><td colspan="3">Nothing matched yet.</td></tr>{% endif %}
  </table></div>
  <h2 style="margin-top:16px;font-size:16px">Link a Loyverse item name by hand</h2>
  <form method="post" action="{{ url_for('lvmatch_link') }}" class="row">
    <input name="lv_name" placeholder="Loyverse item name, exactly as in Loyverse" required>
    <select name="product_id" required><option value="">Choose app product…</option>{% for p in products %}<option value="{{ p.id }}">{{ p.name }}</option>{% endfor %}</select>
    <button>Link</button>
  </form>
  <small>Loyverse items with exactly the same name as an app product match automatically and are not listed here. Groups are edited on the Product Groups page.</small>
</div>{% endif %}
"""


@app.route("/loyverse/match-list")
def lvmatch_page():
    db = get_db()
    q = request.args.get("q", "").strip()
    ql = q.lower()
    unmatched = {}
    for r in db.execute("SELECT item_name, payload, created_at FROM lv_issues "
                        "WHERE kind='unmatched' AND resolved=0 AND trim(COALESCE(item_name,''))<>'' ORDER BY id DESC"):
        try:
            pl = json.loads(r["payload"] or "null")
        except (TypeError, ValueError):
            pl = None
        n = len(pl) if isinstance(pl, list) else (1 if isinstance(pl, dict) else 0)
        u = unmatched.setdefault(r["item_name"].strip().lower(), {"name": r["item_name"].strip(), "lines": 0,
                                                                   "last": local_str(r["created_at"]) if r["created_at"] else ""})
        u["lines"] += n
    unmatched = list(unmatched.values())
    matched = [dict(r) for r in db.execute(
        "SELECT a.lv_name, p.name AS product, p.unit FROM lv_alias a JOIN products p ON p.id=a.product_id "
        "ORDER BY a.lv_name COLLATE NOCASE")]
    groups = []
    for row in db.execute("SELECT DISTINCT lv_name FROM lv_group_items ORDER BY lv_name COLLATE NOCASE").fetchall():
        comps = db.execute("SELECT p.name, gi.qty FROM lv_group_items gi JOIN products p ON p.id=gi.product_id "
                           "WHERE lower(trim(gi.lv_name))=lower(trim(?)) ORDER BY p.name", (row["lv_name"],)).fetchall()
        groups.append({"lv_name": row["lv_name"], "components": comps})
    if ql:
        matched = [m for m in matched if ql in m["lv_name"].lower() or ql in m["product"].lower()]
        groups = [g for g in groups if ql in g["lv_name"].lower() or any(ql in c["name"].lower() for c in g["components"])]
    return render(LVMATCH_BODY, unmatched=unmatched, matched=matched, groups=groups, products=all_products(), q=q,
                  ignored=db.execute("SELECT lv_name FROM lv_ignore ORDER BY lv_name").fetchall())


@app.route("/loyverse/match-list/link", methods=["POST"])
def lvmatch_link():
    db = get_db()
    name = " ".join(request.form.get("lv_name", "").split())
    try:
        pid = int(request.form.get("product_id", ""))
    except ValueError:
        pid = 0
    if not name or not db.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone():
        flash("Enter the Loyverse item name and choose an app product.", "error")
        return redirect(url_for("lvmatch_page"))
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("INSERT INTO lv_alias (lv_name, product_id) VALUES (?,?) "
                   "ON CONFLICT(lv_name) DO UPDATE SET product_id = excluded.product_id", (name.lower(), pid))
        n = 0
        for it in db.execute("SELECT * FROM lv_issues WHERE kind='unmatched' AND resolved=0 "
                             "AND lower(trim(item_name))=lower(?)", (name,)).fetchall():
            pending = json.loads(it["payload"] or "null")
            pending = pending if isinstance(pending, list) else ([pending] if isinstance(pending, dict) else [])
            for payload in pending:
                lv_apply_line(db, payload, pid)
                n += 1
            db.execute("UPDATE lv_issues SET resolved=1 WHERE id=?", (it["id"],))
        db.commit()
    except Exception:
        db.rollback()
        raise
    flash(f"Linked ‘{name}’. " + (f"{n} waiting sale line(s) applied to stock. " if n else "") + "Future sales of this item match automatically.")
    return redirect(url_for("lvmatch_page"))


@app.route("/loyverse/match-list/ignore", methods=["POST"])
def lvmatch_ignore():
    name = " ".join(request.form.get("lv_name", "").split())
    if name:
        lv_ignore_name(get_db(), name)
        flash(f"Ignoring ‘{name}’. Its Loyverse sales will no longer change stock here.")
    return redirect(url_for("lvmatch_page"))


@app.route("/loyverse/match-list/unignore", methods=["POST"])
def lvmatch_unignore():
    db = get_db()
    db.execute("DELETE FROM lv_ignore WHERE lv_name=?", (lv_norm(request.form.get("lv_name", "")),))
    db.commit()
    flash("Removed from the ignore list. Its next Loyverse sale will ask you to match it again.")
    return redirect(url_for("lvmatch_page"))


@app.route("/loyverse/match-list/unlink", methods=["POST"])
def lvmatch_unlink():
    db = get_db()
    name = request.form.get("lv_name", "")
    db.execute("DELETE FROM lv_alias WHERE lv_name=?", (name,))
    db.commit()
    flash(f"Removed the match for ‘{name}’. Past sales were not changed; new sales of it will need matching again.")
    return redirect(url_for("lvmatch_page"))


# =====================================================================
#  Passwords: one for the whole app, a separate one for Sales / Loyverse / Settings
# =====================================================================
SALES_ENDPOINTS = {"sales_page", "credit_page", "export_credit", "add_sale", "settle_sale", "settle_customer", "export_sales",
                   "loyverse_page", "loyverse_groups_page", "lv_group_save", "lv_group_delete", "lv_add_products",
                   "lv_test", "lv_map", "lv_start", "lv_stop", "lv_sync",
                   "lv_match", "lv_dismiss", "lv_account_add", "lv_account_delete", "settings_page", "settings_password",
                   "lvmatch_page", "lvmatch_link", "lvmatch_unlink", "lvmatch_ignore", "lvmatch_unignore",
                   "lv_ignore", "lv_unignore"}

ENDPOINT_PAGES = {
    "index": "stock", "move": "stock",
    "sales_page": "sales", "add_sale": "sales", "settle_sale": "sales", "settle_customer": "sales", "export_sales": "sales",
    "credit_page": "credit", "export_credit": "credit",
    "transfer_page": "transfer", "add_transfer": "transfer", "cancel_transfer": "transfer",
    "returns_page": "returns", "add_return": "returns", "cancel_return": "returns",
    "branchchat_page": "branchchat",
    "loyverse_page": "loyverse", "lv_add_products": "loyverse", "lv_test": "loyverse", "lv_map": "loyverse",
    "lv_start": "loyverse", "lv_stop": "loyverse", "lv_sync": "loyverse", "lv_match": "loyverse",
    "lv_dismiss": "loyverse", "lv_account_add": "loyverse", "lv_account_delete": "loyverse",
    "loyverse_groups_page": "groups", "lv_group_save": "groups", "lv_group_delete": "groups",
    "lvmatch_page": "lvmatch", "lvmatch_link": "lvmatch", "lvmatch_unlink": "lvmatch",
    "lvmatch_ignore": "lvmatch", "lvmatch_unignore": "lvmatch", "lv_ignore": "loyverse", "lv_unignore": "loyverse",
    "history": "history", "export_csv": "history",
    "products_page": "products", "add_product": "products", "product_photo_set": "products", "bulk_add": "products", "delete_product": "products",
    "branches_page": "branches", "add_branch": "branches", "delete_branch": "branches",
    "userlog_page": "userlog",
    "users_page": "users", "users_add": "users", "users_toggle": "users",
    "users_permissions": "users", "users_page_assignment": "users", "users_password": "users", "users_delete": "users",
    "settings_page": "settings", "settings_password": "settings", "lock_sales": "settings",
}

BLOCK_ENDPOINTS = {
    "move": "stock_move", "export_csv": "history_filter",
    "add_product": "products_add", "product_photo_set": "products_list", "bulk_add": "products_bulk", "delete_product": "products_list",
    "add_branch": "branches_add", "delete_branch": "branches_manage", "branches_page": "branches_manage",
    "add_sale": "sales_record", "settle_sale": "sales_credit", "settle_customer": "sales_credit", "export_sales": "sales_list",
    "export_credit": "credit_pending", "add_transfer": "transfer_move", "cancel_transfer": "transfer_recent",
    "add_return": "returns_move", "cancel_return": "returns_recent",
    "lv_account_add": "loyverse_accounts", "lv_account_delete": "loyverse_accounts", "lv_test": "loyverse_accounts", "lv_map": "loyverse_map",
    "lv_start": "loyverse_sync", "lv_stop": "loyverse_sync", "lv_sync": "loyverse_sync", "lv_match": "loyverse_issues", "lv_dismiss": "loyverse_issues",
    "lvmatch_link": "lvmatch_unmatched", "lvmatch_unlink": "lvmatch_matched",
    "lvmatch_ignore": "lvmatch_unmatched", "lvmatch_unignore": "lvmatch_unmatched",
    "lv_ignore": "loyverse_issues", "lv_unignore": "loyverse_issues",
    "lv_group_save": "groups_editor", "lv_group_delete": "groups_saved", "lv_add_products": "loyverse_issues",
    "users_add": "users_create", "users_toggle": "users_accounts", "users_permissions": "users_accounts",
    "users_page_assignment": "users_assign_pages", "users_block_assignment": "users_assign_blocks",
    "users_password": "users_accounts", "users_delete": "users_accounts",
    "lock_sales": "settings_lock",
}


def page_access_from_db(value):
    try:
        pages = json.loads(value)
    except (TypeError, ValueError):
        return None
    return pages if isinstance(pages, list) else None


def block_access_from_db(value):
    try:
        blocks = json.loads(value)
    except (TypeError, ValueError):
        return None
    return blocks if isinstance(blocks, list) else None


def first_allowed_page_url():
    routes = (("stock", "index"), ("sales", "sales_page"), ("credit", "credit_page"),
              ("transfer", "transfer_page"), ("returns", "returns_page"), ("branchchat", "branchchat_page"), ("loyverse", "loyverse_page"),
              ("groups", "loyverse_groups_page"), ("lvmatch", "lvmatch_page"), ("history", "history"),
              ("products", "products_page"), ("branches", "branches_page"),
              ("users", "users_page"), ("userlog", "userlog_page"), ("settings", "settings_page"))
    for page, endpoint in routes:
        if can_open_page(page):
            return url_for(endpoint)
    return url_for("logout")
LOGIN_FAILS = {}
MIN_PASSWORD = 6

AUTH_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ heading }}</title>
<style>
  *{box-sizing:border-box}
  body{font-family:system-ui,Arial,sans-serif;background:#f3f4f6;margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px}
  .box{background:#fff;border-radius:14px;box-shadow:0 2px 12px #0002;padding:24px;width:100%;max-width:380px}
  h1{font-size:20px;margin:0 0 6px}
  p{color:#4b5563;font-size:14px;margin:0 0 14px}
  label{display:block;font-size:13px;color:#374151;margin:12px 0 4px;font-weight:600}
  input{width:100%;font-size:16px;padding:12px;border:1px solid #d1d5db;border-radius:8px}
  button{width:100%;margin-top:18px;font-size:17px;font-weight:600;padding:13px;border:0;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer}
  .err{background:#fee2e2;color:#991b1b;padding:10px;border-radius:8px;font-size:14px;margin-bottom:10px}
  .note{background:#fef3c7;color:#92400e;padding:10px;border-radius:8px;font-size:13px;margin-bottom:12px}
  a{color:#2563eb;font-size:14px}
</style></head>
<body><div class="box">
  <h1>📦 {{ heading }}</h1>
  <p>{{ sub }}</p>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  {% if note %}<div class="note">{{ note }}</div>{% endif %}
  <form method="post" autocomplete="off">
    <input type="hidden" name="next" value="{{ next }}">
    {% for f in fields %}
      <label for="{{ f.name }}">{{ f.label }}</label>
      <input id="{{ f.name }}" name="{{ f.name }}" type="{{ f.type if f.type is defined else 'password' }}" autocomplete="{{ f.ac }}" required {{ 'autofocus' if loop.first }}>
    {% endfor %}
    <label style="display:flex;align-items:center;gap:8px;font-weight:400"><input id="showpw" type="checkbox" style="width:auto"> Show password</label>
    <script>document.addEventListener('DOMContentLoaded',function(){var c=document.getElementById('showpw');if(c)c.addEventListener('change',function(){document.querySelectorAll('input[type=password],input[data-was-password]').forEach(function(i){if(c.checked){i.dataset.wasPassword='1';i.type='text'}else{i.type='password';delete i.dataset.wasPassword}})});});</script>
    <button>{{ button }}</button>
  </form>
  {% if back %}<p style="margin:14px 0 0"><a href="{{ back }}">← Back</a></p>{% endif %}
</div></body></html>"""

SETTINGS_BODY = """
{% if can_open_block('settings_app') %}<div class="card">
  <h2>App password</h2>
  <p style="margin:0 0 10px;font-size:14px;color:#4b5563">Everyone who opens this app needs this password.</p>
  <form method="post" action="{{ url_for('settings_password') }}" class="row" style="flex-direction:column">
    <input type="hidden" name="which" value="app">
    <input type="password" name="current" placeholder="Current app password" autocomplete="current-password" required>
    <input type="password" name="new" placeholder="New password ({{ minlen }}+ characters)" autocomplete="new-password" required>
    <input type="password" name="confirm" placeholder="Type the new password again" autocomplete="new-password" required>
    <button>Change app password</button>
  </form>
</div>{% endif %}
{% if can_open_block('settings_sales') %}<div class="card">
  <h2>Sales password</h2>
  <p style="margin:0 0 10px;font-size:14px;color:#4b5563">Needed for the Sales, Loyverse and Settings pages only. Keep it to yourself.</p>
  <form method="post" action="{{ url_for('settings_password') }}" class="row" style="flex-direction:column">
    <input type="hidden" name="which" value="sales">
    <input type="password" name="current" placeholder="Current sales password" autocomplete="current-password" required>
    <input type="password" name="new" placeholder="New password ({{ minlen }}+ characters)" autocomplete="new-password" required>
    <input type="password" name="confirm" placeholder="Type the new password again" autocomplete="new-password" required>
    <button>Change sales password</button>
  </form>
  <small>Changing either password logs everyone else out.</small>
</div>{% endif %}
{% if can_open_block('settings_lock') %}<div class="card">
  <h2>Lock sales now</h2>
  <p style="margin:0 0 10px;font-size:14px;color:#4b5563">Sales, Loyverse and Settings lock by themselves after {{ minutes }} minutes without use.</p>
  <form method="post" action="{{ url_for('lock_sales') }}"><button class="del" style="padding:10px 14px">Lock now</button></form>
</div>{% endif %}
{% if can_open_block('settings_forgot') %}<div class="card">
  <h2>Forgot a password?</h2>
  <p style="margin:0;font-size:14px;color:#4b5563">On the computer or Raspberry Pi that runs the app, stop the app and run
  <code>python3 stock_app.py --reset-passwords</code>, then start it again and choose new passwords. Your stock data is not touched.</p>
</div>{% endif %}
"""


def pw_version(db):
    return setting_get(db, "pw_ver", "1")


def client_ip():
    ip = request.remote_addr or "?"
    if ip in ("127.0.0.1", "::1"):                 # behind the Cloudflare tunnel
        ip = request.headers.get("CF-Connecting-IP") or ip
    return ip


def locked_out(key):
    rec = LOGIN_FAILS.get(key)
    return bool(rec and rec[1] > time.time())


def note_failure(key):
    rec = LOGIN_FAILS.setdefault(key, [0, 0])
    rec[0] += 1
    if rec[0] >= 5:
        rec[0], rec[1] = 0, time.time() + 60


def safe_next(n):
    if n and n.startswith("/") and not n.startswith("//") and "\\" not in n:
        return n
    return url_for("index")


def sales_unlocked():
    return session.get("sales_until", 0) > time.time()


def start_session(db, sales=False):
    session.clear()
    session.permanent = True
    session["auth"] = True
    session["v"] = pw_version(db)
    session["role"] = "admin"
    if sales:
        session["sales_until"] = time.time() + SALES_MINUTES * 60


ONLINE_SECONDS = 20   # the open page checks in every 3 s; no check-in for 20 s = offline


def device_name():
    ua = request.headers.get("User-Agent", "")
    low = ua.lower()
    os_ = ("Android" if "android" in low else "iPhone" if "iphone" in low else "iPad" if "ipad" in low
           else "Windows" if "windows" in low else "Mac" if "mac os" in low else "Linux" if "linux" in low else "Other")
    br = ("Edge" if "edg/" in low else "Chrome" if "chrome" in low else "Firefox" if "firefox" in low
          else "Safari" if "safari" in low else "Browser")
    return f"{os_} · {br}"


def log_user_event(db, username, event):
    db.execute("INSERT INTO user_log (username, event, ip, device, created_at) VALUES (?,?,?,?,?)",
               (username, event, client_ip(), device_name(), now()))
    db.commit()


def touch_presence(db):
    """Heartbeat: remember that the signed-in user's page is open right now."""
    name = session.get("username")
    if not name:
        return
    t = time.time()
    if t - session.get("seen_at", 0) < 10:
        return
    session["seen_at"] = t
    db.execute("INSERT INTO user_presence (username, last_seen, logged_out, ip, device) VALUES (?,?,0,?,?) "
               "ON CONFLICT(username) DO UPDATE SET last_seen=excluded.last_seen, logged_out=0, "
               "ip=excluded.ip, device=excluded.device",
               (name, t, client_ip(), device_name()))
    db.commit()


@app.before_request
def gate():
    ep = request.endpoint
    if ep is None or ep == "static":
        return None
    db = get_db()
    if not setting_get(db, "pw_app") or not setting_get(db, "pw_sales"):
        return None if ep == "setup" else redirect(url_for("setup"))
    if ep == "setup":
        return redirect(url_for("index"))
    if not (session.get("auth") and session.get("v") == pw_version(db)):
        session.clear()
        if ep == "login":
            return None
        if ep == "version":
            return Response("login", status=401)
        nxt = request.full_path.rstrip("?") if request.method == "GET" else url_for("index")
        return redirect(url_for("login", next=nxt))
    touch_presence(db)
    if ep == "login":
        return redirect(url_for("sales_page" if session.get("role") == "sales_only" else "index"))
    role = session.get("role", "admin")
    custom_page_access = isinstance(session.get("page_access"), list)
    custom_block_access = isinstance(session.get("block_access"), list)
    requested_page = ENDPOINT_PAGES.get(ep)
    if custom_page_access and requested_page and not can_open_page(requested_page):
        flash("You do not have permission to open that page.", "error")
        return redirect(first_allowed_page_url())
    requested_block = BLOCK_ENDPOINTS.get(ep)
    if ep == "settings_password":
        requested_block = "settings_sales" if request.form.get("which") == "sales" else "settings_app"
    if custom_block_access and requested_block and not can_open_block(requested_block):
        flash("You do not have permission to use that section.", "error")
        return redirect(first_allowed_page_url())
    sales_only_allowed = {"index", "move", "sales_page", "add_sale", "settle_sale", "settle_customer", "export_sales", "logout", "version", "product_photo_file"}
    no_loyverse_branch = {"loyverse_page", "loyverse_groups_page", "lv_group_save", "lv_group_delete", "lv_add_products", "lv_test", "lv_map", "lv_start", "lv_stop", "lv_sync", "lv_match", "lv_dismiss", "lvmatch_page", "lvmatch_link", "lvmatch_unlink", "lvmatch_ignore", "lvmatch_unignore", "lv_ignore", "lv_unignore", "branches_page", "add_branch", "delete_branch"}
    branch_only = {"branches_page", "add_branch", "delete_branch"}
    if not custom_page_access and role == "sales_only" and ep not in sales_only_allowed:
        return redirect(url_for("sales_page"))
    if not custom_page_access and role == "no_loyverse_branch" and ep in no_loyverse_branch:
        flash("You do not have permission to open that page.", "error")
        return redirect(url_for("index"))
    if not custom_page_access and role == "no_branch" and ep in branch_only:
        flash("You do not have permission to manage branches.", "error")
        return redirect(url_for("index"))
    if not custom_page_access and role != "admin" and ep in {"userlog_page", "users_page", "users_add", "users_toggle", "users_permissions", "users_page_assignment", "users_password", "users_delete", "settings_page", "settings_password"}:
        flash("Only the software engineer account can manage users and settings.", "error")
        return redirect(url_for("sales_page" if role == "sales_only" else "index"))
    if role == "sales_only":
        return None
    if ep in SALES_ENDPOINTS and not session.get("user_id"):
        if not sales_unlocked():
            if request.method != "GET":
                flash(f"Sales locked itself after {SALES_MINUTES} minutes without use. "
                      "Enter the sales password, then do that again.", "error")
            return redirect(url_for("unlock", next=request.full_path.rstrip("?") if request.method == "GET" else url_for("sales_page")))
        session["sales_until"] = time.time() + SALES_MINUTES * 60
    return None


def check_pw(db, key, given):
    h = setting_get(db, key)
    return bool(h) and check_password_hash(h, given or "")


def make_hash(pw):
    return generate_password_hash(pw, method="pbkdf2:sha256")


@app.route("/setup", methods=["GET", "POST"])
def setup():
    db = get_db()
    error = ""
    if request.method == "POST":
        a, a2 = request.form.get("app_pw", ""), request.form.get("app_pw2", "")
        b, b2 = request.form.get("sales_pw", ""), request.form.get("sales_pw2", "")
        if len(a) < MIN_PASSWORD or len(b) < MIN_PASSWORD:
            error = f"Each password needs at least {MIN_PASSWORD} characters."
        elif a != a2 or b != b2:
            error = "A password and its confirmation don't match."
        elif a == b:
            error = "The sales password must be different from the app password."
        else:
            setting_set(db, "pw_app", make_hash(a))
            setting_set(db, "pw_sales", make_hash(b))
            setting_set(db, "pw_ver", str(int(pw_version(db)) + 1))
            db.commit()
            start_session(db, sales=True)
            flash("Passwords saved. You are signed in.")
            return redirect(url_for("index"))
    return render_template_string(
        AUTH_PAGE, heading="Set up passwords", button="Save passwords", error=error, next="", back="",
        sub="Choose two passwords before anyone else uses the app.",
        note="Do this now. Whoever opens this page first sets the passwords.",
        fields=[{"name": "app_pw", "label": "App password (for everyone who uses the app)", "ac": "new-password"},
                {"name": "app_pw2", "label": "Type the app password again", "ac": "new-password"},
                {"name": "sales_pw", "label": "Sales password (only for Sales, Loyverse, Settings)", "ac": "new-password"},
                {"name": "sales_pw2", "label": "Type the sales password again", "ac": "new-password"}])


@app.route("/login", methods=["GET", "POST"])
def login():
    db = get_db()
    nxt = request.values.get("next", "")
    error = ""
    if request.method == "POST":
        key = "app:" + client_ip()
        if locked_out(key):
            error = "Too many wrong tries. Wait a minute and try again."
        else:
            username = request.form.get("username", "").strip()
            user = db.execute("SELECT * FROM app_users WHERE lower(username)=lower(?) AND active=1", (username,)).fetchone() if username else None
            if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
                LOGIN_FAILS.pop(key, None)
                start_session(db)
                session["username"] = user["username"]
                session["role"] = user["role"]
                session["user_id"] = user["id"]
                pages = page_access_from_db(user["page_access"])
                if pages is not None:
                    session["page_access"] = pages
                blocks = block_access_from_db(user["block_access"])
                if blocks is not None:
                    session["block_access"] = blocks
                log_user_event(db, user["username"], "LOGIN")
                touch_presence(db)
                return redirect(safe_next(nxt))
            if not user and not db.execute("SELECT 1 FROM app_users LIMIT 1").fetchone() and check_pw(db, "pw_app", request.form.get("password")):
                LOGIN_FAILS.pop(key, None)
                start_session(db)
                session["username"] = "legacy-admin"
                session["role"] = "admin"
                log_user_event(db, "legacy-admin", "LOGIN")
                touch_presence(db)
                return redirect(safe_next(nxt))
            note_failure(key)
            if username:
                log_user_event(db, username[:40], "FAILED")
            error = "Username or password is incorrect."
    return render_template_string(
        AUTH_PAGE, heading="Stock Manager", sub="Enter the app password to continue.", button="Sign in",
        error=error, note="", next=nxt, back="",
        fields=[{"name": "username", "label": "Username", "ac": "username", "type": "text"}, {"name": "password", "label": "Password", "ac": "current-password", "type": "password"}])


@app.route("/logout")
def logout():
    name = session.get("username")
    if name:
        db = get_db()
        db.execute("UPDATE user_presence SET logged_out=1 WHERE username=?", (name,))
        log_user_event(db, name, "LOGOUT")
    session.clear()
    return redirect(url_for("login"))


@app.route("/unlock", methods=["GET", "POST"])
def unlock():
    db = get_db()
    nxt = request.values.get("next", "")
    error = ""
    if request.method == "POST":
        key = "sales:" + client_ip()
        if locked_out(key):
            error = "Too many wrong tries. Wait a minute and try again."
        elif check_pw(db, "pw_sales", request.form.get("password")):
            LOGIN_FAILS.pop(key, None)
            session["sales_until"] = time.time() + SALES_MINUTES * 60
            return redirect(safe_next(nxt))
        else:
            note_failure(key)
            error = "That password is not right."
    return render_template_string(
        AUTH_PAGE, heading="Sales is locked", sub="Enter the sales password to open this page.",
        button="Unlock", error=error, note="", next=nxt, back=url_for("index"),
        fields=[{"name": "password", "label": "Sales password", "ac": "current-password"}])


@app.route("/lock", methods=["POST"])
def lock_sales():
    session.pop("sales_until", None)
    flash("Sales is locked.")
    return redirect(url_for("index"))


USERS_BODY = """
{% if can_open_block('users_create') %}<div class="card"><h2>Create a user account</h2>
<form method="post" action="{{ url_for('users_add') }}">
<div class="row">
<input name="username" placeholder="Username" minlength="3" maxlength="40" required>
<input id="new-user-password" name="password" type="password" placeholder="Password (6+ characters)" minlength="6" required>
<label style="flex:0 0 auto;min-width:auto;display:flex;align-items:center;gap:5px;white-space:nowrap"><input id="show-new-user-password" type="checkbox" style="min-width:auto;flex:0;width:auto"> Show password</label>
<select name="role" required><option value="no_branch">Staff account — use selected pages</option><option value="admin">Software engineer — full control</option></select>
</div>
<p style="margin:12px 0 2px"><strong>Pages this staff user can open</strong></p>
<div class="page-grid">{% for key, label in page_options %}<label><input type="checkbox" name="pages" value="{{ key }}" {% if key in ('stock','sales') %}checked{% endif %}> {{ label }}</label>{% endfor %}</div>
<button>Create account</button></form>
<script>document.getElementById('show-new-user-password').addEventListener('change',function(){document.getElementById('new-user-password').type=this.checked?'text':'password';});</script>
<p><small>Choose any app pages now, or change them for any account below. Software engineer accounts always have full control.</small></p></div>{% endif %}
{% if can_open_block('users_accounts') %}<div class="card"><h2>Accounts</h2><div class="scroll"><table><tr><th>Username</th><th>Access</th><th>Status</th><th>Action</th></tr>
{% for u in users %}<tr><td>{{ u.username }}</td><td>{{ permission_labels(u) }}</td><td>{{ 'Active' if u.active else 'Disabled' }}</td><td><form method="post" action="{{ url_for('users_toggle', uid=u.id) }}"><button class="mini">{{ 'Disable' if u.active else 'Enable' }}</button></form></td></tr><tr><td colspan="4"><details><summary>Account options</summary><p><small>Saved passwords cannot be shown. Set a new password if it is forgotten.</small></p><form method="post" action="{{ url_for('users_permissions', uid=u.id) }}"><strong>Page access</strong><div class="page-grid">{% for key, label in page_options %}<label><input type="checkbox" name="pages" value="{{ key }}" {% if key in saved_pages(u) %}checked{% endif %}> {{ label }}</label>{% endfor %}</div><button class="mini">Save page access</button></form><form method="post" action="{{ url_for('users_password', uid=u.id) }}" class="row" style="margin-top:10px"><input name="password" type="password" placeholder="New password (6+ characters)" minlength="6" required><button class="mini">Set new password</button></form><form method="post" action="{{ url_for('users_delete', uid=u.id) }}" style="margin-top:10px" onsubmit="return confirm('Delete this account? This cannot be undone.');"><button class="del">Delete account</button></form></details></td></tr>{% else %}<tr><td colspan="4">No separate accounts yet. The current legacy password login has administrator access until accounts are created.</td></tr>{% endfor %}</table></div></div>{% endif %}
{% if can_open_block('users_assign_pages') %}<div class="card"><h2>Page assignments</h2><p><small>For each page, select all staff users who may open it. You can select more than one user.</small></p>{% for key, label in page_options %}<details style="margin:8px 0"><summary><strong>{{ label }}</strong> — choose users</summary><form method="post" action="{{ url_for('users_page_assignment') }}"><input type="hidden" name="page" value="{{ key }}"><div class="page-grid">{% for u in assignable_users %}<label><input type="checkbox" name="user_ids" value="{{ u.id }}" {% if key in saved_pages(u) %}checked{% endif %}> {{ u.username }}</label>{% else %}<small>Create a staff account first.</small>{% endfor %}</div><button class="mini">Save {{ label }} users</button></form></details>{% endfor %}</div>{% endif %}
{% if can_open_block('users_assign_blocks') %}<div class="card"><h2>Block assignments</h2><p><small>Assign individual sections inside each page. For example, allow one user to use Stock In / Out and another user to see Stock by Branch.</small></p>{% for key, label in block_options %}<details style="margin:8px 0"><summary><strong>{{ label }}</strong> — choose users</summary><form method="post" action="{{ url_for('users_block_assignment') }}"><input type="hidden" name="block" value="{{ key }}"><div class="page-grid">{% for u in assignable_users %}<label><input type="checkbox" name="user_ids" value="{{ u.id }}" {% if key in saved_blocks(u) %}checked{% endif %}> {{ u.username }}</label>{% else %}<small>Create a staff account first.</small>{% endfor %}</div><button class="mini">Save {{ label }} users</button></form></details>{% endfor %}</div>{% endif %}
"""

@app.route("/users")
def users_page():
    users = get_db().execute("SELECT * FROM app_users ORDER BY id").fetchall()
    return render(USERS_BODY, users=users, assignable_users=[u for u in users if u["role"] != "admin"],
                  page_options=USER_PAGES, block_options=BLOCKS, saved_pages=user_saved_pages,
                  saved_blocks=user_saved_blocks, permission_labels=user_permission_labels)


def user_saved_pages(user):
    pages = page_access_from_db(user["page_access"])
    if pages is not None:
        return set(pages) & USER_PAGE_KEYS
    return LEGACY_PAGE_ACCESS.get(user["role"], USER_PAGE_KEYS if user["role"] == "admin" else set())


def user_saved_blocks(user):
    blocks = block_access_from_db(user["block_access"])
    return (set(blocks) & BLOCK_KEYS) if blocks is not None else set(BLOCK_KEYS)


def user_permission_labels(user):
    if user["role"] == "admin":
        return "Full control"
    labels = dict(USER_PAGES)
    pages = user_saved_pages(user)
    return ", ".join(labels[key] for key, _label in USER_PAGES if key in pages) or "No pages"

USERLOG_BODY = """
{% if can_open_block('userlog_status') %}<div class="card">
  <h2>Who is online <small style="font-weight:400;color:#6b7280">· {{ n_online }} online of {{ people|length }}</small></h2>
  <div class="scroll"><table>
    <tr><th>User</th><th>Status</th><th>Last seen</th><th>Device</th></tr>
    {% for p in people %}
    <tr>
      <td><strong>{{ p.name }}</strong><br><small>{{ p.role }}</small></td>
      <td>{% if p.online %}<span style="color:#15803d;font-weight:700">● Online</span>{% else %}<span style="color:#6b7280">○ Offline</span>{% endif %}</td>
      <td>{{ p.seen }}</td>
      <td><small>{{ p.device }}{% if p.ip %}<br>{{ p.ip }}{% endif %}</small></td>
    </tr>
    {% else %}<tr><td colspan="4">No users yet.</td></tr>{% endfor %}
  </table></div>
  <small>Online = this user has the app open right now. Refreshes by itself.</small>
</div>{% endif %}
{% if can_open_block('userlog_history') %}<div class="card">
  <h2>Sign-in history</h2>
  <form method="get" class="row" style="margin-bottom:10px">
    <select name="user"><option value="">All users</option>{% for p in people %}<option value="{{ p.name }}" {{ 'selected' if p.name == fu }}>{{ p.name }}</option>{% endfor %}</select>
    <select name="event"><option value="">All events</option>{% for k, v in events %}<option value="{{ k }}" {{ 'selected' if k == fe }}>{{ v }}</option>{% endfor %}</select>
    <button>Filter</button>
  </form>
  <div class="scroll"><table>
    <tr><th>Time</th><th>User</th><th>Event</th><th>Device</th></tr>
    {% for r in log %}
    <tr>
      <td style="white-space:nowrap">{{ r.created_at }}</td>
      <td>{{ r.username }}</td>
      <td>{% if r.event == 'LOGIN' %}<span style="color:#15803d">Signed in</span>{% elif r.event == 'LOGOUT' %}<span style="color:#6b7280">Signed out</span>{% else %}<span style="color:#b91c1c">Wrong password</span>{% endif %}</td>
      <td><small>{{ r.device }}<br>{{ r.ip }}</small></td>
    </tr>
    {% else %}<tr><td colspan="4">No activity recorded yet.</td></tr>{% endfor %}
  </table></div>
  <small>Showing the latest {{ log|length }} events.</small>
</div>{% endif %}
<script>setTimeout(function(){ if(!document.activeElement || document.activeElement.tagName==="BODY") location.reload(); }, 15000);</script>
"""


def ago(t):
    d = int(time.time() - t)
    if d < 60: return "just now"
    if d < 3600: return f"{d // 60} min ago"
    if d < 86400: return f"{d // 3600} hr ago"
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")


@app.route("/user-log")
def userlog_page():
    db = get_db()
    pres = {r["username"]: r for r in db.execute("SELECT * FROM user_presence")}
    names = [(u["username"], u["role"]) for u in db.execute("SELECT username, role FROM app_users ORDER BY username")]
    known = {n for n, _ in names}
    names += [(n, "admin") for n in pres if n not in known]
    people, t = [], time.time()
    for name, role in names:
        r = pres.get(name)
        online = bool(r and not r["logged_out"] and t - r["last_seen"] <= ONLINE_SECONDS)
        people.append({"name": name, "role": "Software engineer" if role == "admin" else "Staff", "online": online,
                       "seen": "Now" if online else (ago(r["last_seen"]) if r else "Never"),
                       "device": r["device"] if r else "", "ip": r["ip"] if r else ""})
    people.sort(key=lambda p: (not p["online"], p["name"].lower()))
    events = [("LOGIN", "Signed in"), ("LOGOUT", "Signed out"), ("FAILED", "Wrong password")]
    fu, fe = request.args.get("user", ""), request.args.get("event", "")
    where, args = [], []
    if fu: where.append("username=?"); args.append(fu)
    if fe in {k for k, _ in events}: where.append("event=?"); args.append(fe)
    else: fe = ""
    sql = "SELECT * FROM user_log" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT 200"
    return render(USERLOG_BODY, people=people, n_online=sum(p["online"] for p in people),
                  log=db.execute(sql, args).fetchall(), events=events, fu=fu, fe=fe)


@app.route("/users/add", methods=["POST"])
def users_add():
    db = get_db()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "")
    pages = sorted(set(request.form.getlist("pages")) & USER_PAGE_KEYS)
    if len(username) < 3 or len(password) < MIN_PASSWORD or role not in ("no_branch", "admin"):
        flash("Enter a username (at least 3 characters), password (at least 6 characters), and valid role.", "error")
    elif role != "admin" and not pages:
        flash("Select at least one page for a staff account.", "error")
    else:
        try:
            access = None if role == "admin" else json.dumps(pages)
            db.execute("INSERT INTO app_users(username,password_hash,role,page_access,created_at) VALUES(?,?,?,?,?)", (username, make_hash(password), role, access, now()))
            db.commit(); flash(f"Account '{username}' created.")
        except sqlite3.IntegrityError:
            flash("That username already exists.", "error")
    return redirect(url_for("users_page"))


@app.route("/users/permissions/<int:uid>", methods=["POST"])
def users_permissions(uid):
    db = get_db()
    user = db.execute("SELECT * FROM app_users WHERE id=?", (uid,)).fetchone()
    pages = sorted(set(request.form.getlist("pages")) & USER_PAGE_KEYS)
    if not user:
        flash("Account not found.", "error")
    elif user["role"] == "admin":
        flash("Software engineer accounts always have full control.")
    elif not pages:
        flash("Select at least one page for this account.", "error")
    else:
        db.execute("UPDATE app_users SET page_access=? WHERE id=?", (json.dumps(pages), uid))
        db.commit()
        flash(f"Page access updated for '{user['username']}'.")
    return redirect(url_for("users_page"))


@app.route("/users/page-assignment", methods=["POST"])
def users_page_assignment():
    """Save the selected users for one page, while preserving their other pages."""
    db = get_db()
    page = request.form.get("page", "")
    selected = set(request.form.getlist("user_ids"))
    if page not in USER_PAGE_KEYS:
        flash("That page is not valid.", "error")
        return redirect(url_for("users_page"))
    users = db.execute("SELECT * FROM app_users WHERE role != 'admin'").fetchall()
    for user in users:
        pages = user_saved_pages(user)
        if str(user["id"]) in selected:
            pages.add(page)
        else:
            pages.discard(page)
        db.execute("UPDATE app_users SET page_access=? WHERE id=?", (json.dumps(sorted(pages)), user["id"]))
    db.commit()
    flash("Users assigned to the selected page.")
    return redirect(url_for("users_page"))


@app.route("/users/block-assignment", methods=["POST"])
def users_block_assignment():
    """Save the selected users for one block, preserving their other blocks."""
    db = get_db()
    block = request.form.get("block", "")
    selected = set(request.form.getlist("user_ids"))
    if block not in BLOCK_KEYS:
        flash("That block is not valid.", "error")
        return redirect(url_for("users_page"))
    users = db.execute("SELECT * FROM app_users WHERE role != 'admin'").fetchall()
    for user in users:
        blocks = user_saved_blocks(user)
        if str(user["id"]) in selected:
            blocks.add(block)
        else:
            blocks.discard(block)
        db.execute("UPDATE app_users SET block_access=? WHERE id=?", (json.dumps(sorted(blocks)), user["id"]))
    db.commit()
    flash("Users assigned to the selected block.")
    return redirect(url_for("users_page"))


@app.route("/users/password/<int:uid>", methods=["POST"])
def users_password(uid):
    db = get_db()
    user = db.execute("SELECT * FROM app_users WHERE id=?", (uid,)).fetchone()
    password = request.form.get("password", "")
    if not user:
        flash("Account not found.", "error")
    elif len(password) < MIN_PASSWORD:
        flash(f"The new password needs at least {MIN_PASSWORD} characters.", "error")
    else:
        db.execute("UPDATE app_users SET password_hash=? WHERE id=?", (make_hash(password), uid))
        db.commit()
        flash(f"Password changed for '{user['username']}'.")
    return redirect(url_for("users_page"))


@app.route("/users/delete/<int:uid>", methods=["POST"])
def users_delete(uid):
    db = get_db()
    user = db.execute("SELECT * FROM app_users WHERE id=?", (uid,)).fetchone()
    if not user:
        flash("Account not found.", "error")
    elif user["id"] == session.get("user_id"):
        flash("You cannot delete the account you are currently using.", "error")
    else:
        db.execute("DELETE FROM app_users WHERE id=?", (uid,))
        db.commit()
        flash(f"Account '{user['username']}' deleted.")
    return redirect(url_for("users_page"))


@app.route("/users/toggle/<int:uid>", methods=["POST"])
def users_toggle(uid):
    db = get_db()
    row = db.execute("SELECT * FROM app_users WHERE id=?", (uid,)).fetchone()
    if row:
        if row["id"] == session.get("user_id"):
            flash("You cannot disable your own account.", "error")
        else:
            db.execute("UPDATE app_users SET active=? WHERE id=?", (0 if row["active"] else 1, uid)); db.commit()
            flash("Account status updated.")
    return redirect(url_for("users_page"))

@app.route("/settings")
def settings_page():
    return render(SETTINGS_BODY, minlen=MIN_PASSWORD, minutes=SALES_MINUTES)


@app.route("/settings/password", methods=["POST"])
def settings_password():
    db = get_db()
    which = request.form.get("which")
    if which not in ("app", "sales"):
        return redirect(url_for("settings_page"))
    other = "pw_sales" if which == "app" else "pw_app"
    key = "pw_" + which
    new, confirm = request.form.get("new", ""), request.form.get("confirm", "")
    if not check_pw(db, key, request.form.get("current")):
        flash("The current password is not right.", "error")
    elif len(new) < MIN_PASSWORD:
        flash(f"The new password needs at least {MIN_PASSWORD} characters.", "error")
    elif new != confirm:
        flash("The new password and its confirmation don't match.", "error")
    elif check_pw(db, other, new):
        flash("The app password and the sales password must be different.", "error")
    else:
        setting_set(db, key, make_hash(new))
        setting_set(db, "pw_ver", str(int(pw_version(db)) + 1))
        db.commit()
        start_session(db, sales=True)
        flash(f"{'App' if which == 'app' else 'Sales'} password changed. Other devices must sign in again.")
    return redirect(url_for("settings_page"))


def history_query(limit=None):
    f = {k: request.args.get(k, "") for k in ("branch_id", "product_id", "from", "to")}
    for k in ("from", "to"):
        try:
            datetime.strptime(f[k], "%Y-%m-%d")
        except ValueError:
            f[k] = ""
    where, args = [], []
    if f["branch_id"]:
        where.append("t.branch_id = ?"); args.append(f["branch_id"])
    if f["product_id"]:
        where.append("t.product_id = ?"); args.append(f["product_id"])
    if f["from"]:
        where.append("t.created_at >= ?"); args.append(f["from"] + " 00:00:00")
    if f["to"]:
        where.append("t.created_at <= ?"); args.append(f["to"] + " 23:59:59")
    sql = ("SELECT t.*, p.name, p.unit, b.name AS branch FROM transactions t "
           "JOIN products p ON p.id = t.product_id JOIN branches b ON b.id = t.branch_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return f, get_db().execute(sql, args).fetchall()


@app.route("/history")
def history():
    f, allrows = history_query()
    tot_in = sum(r["qty"] for r in allrows if r["kind"] == "IN")
    tot_out = sum(r["qty"] for r in allrows if r["kind"] == "OUT")
    today = datetime.now().date()
    week = today - timedelta(days=today.weekday())
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    spans = [("Today", today, today), ("Yesterday", today - timedelta(days=1), today - timedelta(days=1)),
             ("This week", week, today), ("This month", first_this, today),
             ("Last month", last_prev.replace(day=1), last_prev), ("Last 30 days", today - timedelta(days=29), today)]
    chips = []
    for label, a, b in spans:
        args = dict(f, **{"from": a.isoformat(), "to": b.isoformat()})
        chips.append({"label": label, "url": url_for("history", **args),
                      "on": f["from"] == a.isoformat() and f["to"] == b.isoformat()})
    chips.append({"label": "All time", "url": url_for("history", **dict(f, **{"from": "", "to": ""})),
                  "on": not f["from"] and not f["to"]})
    return render(HISTORY_BODY, rows=allrows[:300], f=f, chips=chips, branches=all_branches(),
                  products=all_products(), tot_in=tot_in, tot_out=tot_out,
                  total_rows=len(allrows), shown=min(300, len(allrows)))


@app.route("/export.csv")
def export_csv():
    f, rows = history_query()
    buf = io.StringIO()
    buf.write("\ufeff")   # so Excel shows symbols correctly
    w = csv.writer(buf)
    w.writerow(["time", "branch", "product", "type", "qty", "unit", "note"])
    for r in reversed(rows):
        w.writerow([r["created_at"], r["branch"], r["name"],
                    r["kind"], r["qty"], r["unit"], r["note"]])
    span = f"{f['from'] or 'start'}_to_{f['to'] or 'today'}"
    return Response(buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=stock_history_{span}.csv"})


if __name__ == "__main__":
    init_db()
    if "--reset-passwords" in sys.argv:
        _db = db_conn()
        _db.execute("DELETE FROM settings WHERE key IN ('pw_app','pw_sales')")
        setting_set(_db, "pw_ver", str(int(pw_version(_db)) + 1))
        _db.commit()
        _db.close()
        print("Passwords cleared. Start the app and open it to choose new passwords.")
        sys.exit(0)
    threading.Thread(target=lv_loop, daemon=True).start()
    threading.Thread(target=backup_loop, daemon=True).start()
    app.run(host=os.environ.get("STOCK_HOST", "0.0.0.0"), port=5001, debug=False)
