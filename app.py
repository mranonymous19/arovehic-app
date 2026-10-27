import os
import re
import socket
import time
import uuid
from datetime import datetime, timezone
from functools import wraps

from dotenv import load_dotenv

load_dotenv()  # reads a local .env file if present (no-op on Render)

import psycopg2
import psycopg2.extras
import psycopg2.pool
import requests
from flask import Flask, g, jsonify, redirect, render_template, request, session, send_file, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import io
from urllib.parse import quote
import json

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas as pdf_canvas

from invoice import build_invoice_pdf
import tally_export

DATABASE_URL = os.environ.get("DATABASE_URL", "")
VALID_STATUSES = {"pending", "purchased", "stock", "na", "refunded", "wait"}
VALID_ROLES = {"owner", "staff", "telecaller", "packer", "accounts"}

app = Flask(__name__)


@app.errorhandler(Exception)
def _api_json_errors(exc):
    """Return API crashes as JSON (with the real message) instead of an HTML
    500 page, so the UI banner can show what actually went wrong."""
    from werkzeug.exceptions import HTTPException
    if request.path.startswith("/api/"):
        app.logger.exception("API error on %s", request.path)
        if isinstance(exc, HTTPException):
            return jsonify({"error": exc.description}), exc.code
        return jsonify({"error": f"Server error: {type(exc).__name__}: {exc}"}), 500
    raise exc
# Needed for signed session cookies (login). Set a real SECRET_KEY env var
# in production (Render) — this fallback is only for local dev.
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")


# ---------------------------------------------------------------------------
# Database helpers (Postgres via DATABASE_URL, e.g. a Supabase connection
# string). Set DATABASE_URL as an environment variable wherever this runs.
#
# We keep a small pool of already-open connections instead of opening a
# brand new TCP+TLS connection on every request — reconnecting each time is
# the single biggest source of latency when the database is a cloud service
# (e.g. Supabase) rather than a local file.
# ---------------------------------------------------------------------------

_pool = None


def get_pool():
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. Add it as an environment variable "
                "(see README) — it should be your Supabase Postgres connection string."
            )
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL)
    return _pool


def get_db():
    if "db" not in g:
        pool = get_pool()
        conn = pool.getconn()
        # Supabase's connection pooler (Supavisor/PgBouncer) can close an
        # idle connection on its end at any time. Our pool doesn't know
        # that until something tries to use it and gets
        # "server closed the connection unexpectedly". So do a cheap
        # liveness check here and discard+replace a dead connection
        # before it ever reaches a real query.
        try:
            with conn.cursor() as probe:
                probe.execute("SELECT 1")
        except psycopg2.Error:
            pool.putconn(conn, close=True)
            conn = pool.getconn()
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        if exception is not None:
            try:
                db.rollback()
            except psycopg2.Error:
                # Connection is actually dead (this is usually the real
                # cause of the request failing in the first place) —
                # close it instead of returning a broken connection to
                # the pool for the next request to trip over.
                get_pool().putconn(db, close=True)
                return
        get_pool().putconn(db)


def init_db():
    if not DATABASE_URL:
        return
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS orders (
            shopify_order_id TEXT PRIMARY KEY,
            order_name TEXT,
            customer_name TEXT,
            shipping_address TEXT,
            shipping_address1 TEXT,
            shipping_address2 TEXT,
            shipping_city TEXT,
            shipping_state TEXT,
            shipping_pincode TEXT,
            customer_phone TEXT,
            created_at TEXT,
            synced_at TEXT
        );

        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            shopify_order_id TEXT NOT NULL REFERENCES orders (shopify_order_id),
            title TEXT,
            variant_title TEXT,
            quantity INTEGER,
            price TEXT,
            vendor TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            sort_order SERIAL,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('owner', 'staff', 'telecaller', 'packer', 'accounts')),
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS activity_log (
            id SERIAL PRIMARY KEY,
            item_id TEXT,
            item_name TEXT,
            order_id TEXT,
            user_name TEXT NOT NULL,
            action TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL
        );

        -- Repurchase: items re-bought and re-sent after damage. Deliberately its
        -- own table, not tied to orders/invoices/Tally — the original bill is
        -- already in Tally and must never be duplicated or changed by this.
        CREATE TABLE IF NOT EXISTS repurchases (
            id SERIAL PRIMARY KEY,
            shopify_order_id TEXT,
            order_name TEXT,
            customer_name TEXT,
            customer_phone TEXT,
            item_id TEXT,
            title TEXT,
            variant_title TEXT,
            vendor TEXT,
            quantity INTEGER NOT NULL DEFAULT 1,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_by TEXT,
            created_at TEXT,
            updated_by TEXT,
            updated_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_items_order_id ON items (shopify_order_id);
        CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders (created_at DESC);
        """
    )
    # Migration: add address/phone columns if this table was created before they existed.
    cur.execute(
        """
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_address TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_address1 TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_address2 TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_city TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_state TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_pincode TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_phone TEXT;
        ALTER TABLE items ADD COLUMN IF NOT EXISTS purchase_amount NUMERIC NOT NULL DEFAULT 0;
        ALTER TABLE items ADD COLUMN IF NOT EXISTS packed BOOLEAN NOT NULL DEFAULT false;
        ALTER TABLE items ADD COLUMN IF NOT EXISTS packed_by TEXT;
        ALTER TABLE items ADD COLUMN IF NOT EXISTS packed_at TEXT;
        ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS order_id TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_amount NUMERIC;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS balance_due NUMERIC;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS amount_to_receive NUMERIC;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS invoice_total NUMERIC;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_type TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS deleted_at TEXT;
        CREATE INDEX IF NOT EXISTS idx_orders_deleted_at ON orders (deleted_at);
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS packed BOOLEAN NOT NULL DEFAULT false;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS packed_by TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS packed_at TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS invoice_number TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS invoice_printed_by TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS invoice_date TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS tally_exported_at TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS cancelled BOOLEAN NOT NULL DEFAULT false;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS cancelled_reason TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS cancelled_by TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS cancelled_at TEXT;
        ALTER TABLE items ADD COLUMN IF NOT EXISTS original_price TEXT;
        ALTER TABLE items ADD COLUMN IF NOT EXISTS price_edited BOOLEAN NOT NULL DEFAULT false;
        ALTER TABLE items ADD COLUMN IF NOT EXISTS price_edited_by TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS bill_cancelled BOOLEAN NOT NULL DEFAULT false;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS bill_cancelled_reason TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS bill_cancelled_by TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS bill_cancelled_at TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS order_edited BOOLEAN NOT NULL DEFAULT false;
        ALTER TABLE items ADD COLUMN IF NOT EXISTS item_edited BOOLEAN NOT NULL DEFAULT false;
        ALTER TABLE repurchases ADD COLUMN IF NOT EXISTS batch TEXT;
        ALTER TABLE repurchases ADD COLUMN IF NOT EXISTS price NUMERIC;
        ALTER TABLE repurchases ADD COLUMN IF NOT EXISTS invoice_number TEXT;
        ALTER TABLE repurchases ADD COLUMN IF NOT EXISTS invoice_date TEXT;
        ALTER TABLE repurchases ADD COLUMN IF NOT EXISTS invoice_printed_by TEXT;
        """
    )
    # Migration: tables created before the 'packer'/'accounts' roles existed
    # have a CHECK constraint that would reject them — widen it so those
    # accounts can be created on older databases too.
    cur.execute(
        """
        ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
        ALTER TABLE users ADD CONSTRAINT users_role_check
            CHECK (role IN ('owner', 'staff', 'telecaller', 'packer', 'accounts'));
        """
    )
    # Seed a default owner account on first run, so there's always a way in.
    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        cur.execute(
            "INSERT INTO users (name, password_hash, role, created_at) VALUES (%s, %s, %s, %s)",
            ("owner", generate_password_hash("changeme123"), "owner", datetime.now(timezone.utc).isoformat()),
        )
        print("\nCreated default login -> username: owner   password: changeme123")
        print("Log in and change this password, then add accounts for your team.\n")
    conn.commit()
    cur.close()
    conn.close()


# ---------------------------------------------------------------------------
# Settings (n8n webhook URL is stored here so it can be set from the UI
# instead of editing environment variables)
# ---------------------------------------------------------------------------

def get_setting(key, default=None):
    # Load all settings in ONE query and cache them for the rest of this
    # request, instead of one database round-trip per setting.
    cache = g.get("_settings_cache")
    if cache is None:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT key, value FROM settings")
        cache = {r["key"]: r["value"] for r in cur.fetchall()}
        cur.close()
        g._settings_cache = cache
    val = cache.get(key)
    return val if val is not None else default


def set_setting(key, value):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO settings (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value),
    )
    db.commit()
    cur.close()
    g.pop("_settings_cache", None)


def log_activity(cur, item_id, item_name, action, details="", order_id=None, user_name=None):
    """Record one activity log entry using the already-open cursor `cur`,
    so it lands in the same transaction as whatever it's logging — does
    NOT call db.commit() itself, the caller's existing commit covers it."""
    cur.execute(
        "INSERT INTO activity_log (item_id, item_name, order_id, user_name, action, details, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (item_id, item_name, order_id, user_name or session.get("name", "unknown"), action, details,
         datetime.now(timezone.utc).isoformat()),
    )


# ---------------------------------------------------------------------------
# Manual order entry — see api_add_manual_order below. Telecallers fill in
# a structured form (order id, customer, address fields, payment type,
# delivery charge, and a repeatable list of items) instead of typing text
# in a specific shape, so there's no format for them to get wrong.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Auth helpers
#
# Five roles:
#   owner      - full control: sync, settings, status updates, manage users.
#   staff      - can update item status only; only sees orders currently
#                assigned to them (see /api/orders below). Can also print
#                (but not reprint) the invoice for their own orders once
#                an order reaches Billing — reprinting an already-printed
#                invoice stays owner/accounts only.
#   telecaller - view-only for orders/items (including Trash, Refunded,
#                and Cancelled — same visibility as owner, just nothing is
#                editable), plus can add manual orders (they're the ones
#                taking these calls).
#   packer     - can toggle the packed flag, or mark an order cancelled
#                (with a reason) instead.
#   accounts   - view-only, restricted to the Billing view: which orders
#                are ready to invoice and whether each has been printed yet.
# ---------------------------------------------------------------------------

def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, name, role FROM users WHERE id = %s", (session["user_id"],))
    user = cur.fetchone()
    cur.close()
    return user


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"error": "Please log in"}), 401
            return redirect(url_for("login"))
        # Keep session role in sync in case the owner changed it since login.
        session["role"] = user["role"]
        session["name"] = user["name"]
        return f(*args, **kwargs)

    return wrapper


def owner_required(f):
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if session.get("role") != "owner":
            return jsonify({"error": "Only the owner account can do that"}), 403
        return f(*args, **kwargs)

    return wrapper


def staff_or_owner_required(f):
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if session.get("role") not in ("owner", "staff"):
            return jsonify({"error": "Telecaller accounts are view-only"}), 403
        return f(*args, **kwargs)

    return wrapper


def telecaller_or_owner_required(f):
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if session.get("role") not in ("owner", "telecaller"):
            return jsonify({"error": "Only telecallers and the owner can add orders"}), 403
        return f(*args, **kwargs)

    return wrapper


def repurchase_required(f):
    """Owner (admin) and telecallers only."""
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if session.get("role") not in ("owner", "telecaller"):
            return jsonify({"error": "Only the owner and telecallers can use Repurchase"}), 403
        return f(*args, **kwargs)

    return wrapper


def repurchase_view_required(f):
    """Any logged-in role can open the Repurchase list; what each role can DO
    in it (set Purchased / set Sent / remove / print) is checked per endpoint."""
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        return f(*args, **kwargs)

    return wrapper


def packer_or_owner_required(f):
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if session.get("role") not in ("owner", "packer"):
            return jsonify({"error": "Only packer or owner accounts can update packed status"}), 403
        return f(*args, **kwargs)

    return wrapper


def accounts_or_owner_required(f):
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if session.get("role") not in ("owner", "accounts"):
            return jsonify({"error": "Only accounts or owner accounts can print invoices"}), 403
        return f(*args, **kwargs)

    return wrapper


def can_print_invoice_required(f):
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        # Staff can print their own orders' invoices too (a purchase staff
        # member finishing an order is exactly who needs to hand over a
        # printed invoice), but only the first, unprinted copy — see the
        # invoice_number check inside api_order_invoice for the reprint
        # restriction.
        if session.get("role") not in ("owner", "accounts", "staff"):
            return jsonify({"error": "Only accounts, staff, or owner accounts can print invoices"}), 403
        return f(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Routes - pages
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if "user_id" in session:
            return redirect(url_for("index"))
        return render_template("login.html")

    name = (request.form.get("name") or "").strip()
    password = request.form.get("password") or ""

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM users WHERE name = %s", (name,))
    user = cur.fetchone()
    cur.close()
    if not user or not check_password_hash(user["password_hash"], password):
        return render_template("login.html", error="Wrong name or password"), 401

    session["user_id"] = user["id"]
    session["name"] = user["name"]
    session["role"] = user["role"]
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Routes - current account
# ---------------------------------------------------------------------------

@app.route("/api/me", methods=["GET"])
@login_required
def api_me():
    return jsonify({"name": session["name"], "role": session["role"]})


@app.route("/api/me/password", methods=["POST"])
@login_required
def api_change_own_password():
    data = request.get_json(force=True) or {}
    current = data.get("current_password") or ""
    new = data.get("new_password") or ""
    if len(new) < 4:
        return jsonify({"error": "New password must be at least 4 characters"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM users WHERE id = %s", (session["user_id"],))
    user = cur.fetchone()
    if not check_password_hash(user["password_hash"], current):
        cur.close()
        return jsonify({"error": "Current password is incorrect"}), 400

    cur.execute(
        "UPDATE users SET password_hash = %s WHERE id = %s",
        (generate_password_hash(new), session["user_id"]),
    )
    db.commit()
    cur.close()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Routes - user management (owner only)
# ---------------------------------------------------------------------------

@app.route("/api/users", methods=["GET"])
@owner_required
def api_list_users():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, name, role, created_at FROM users ORDER BY role, name")
    users = cur.fetchall()
    cur.close()
    return jsonify([dict(u) for u in users])


@app.route("/api/users", methods=["POST"])
@owner_required
def api_create_user():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    password = data.get("password") or ""
    role = data.get("role") or "staff"
    if role not in VALID_ROLES:
        return jsonify({"error": f"role must be one of {sorted(VALID_ROLES)}"}), 400
    if not name or len(password) < 4:
        return jsonify({"error": "Name and a password (4+ characters) are required"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM users WHERE name = %s", (name,))
    if cur.fetchone():
        cur.close()
        return jsonify({"error": "That name is already taken"}), 400

    now = datetime.now(timezone.utc).isoformat()
    cur.execute(
        "INSERT INTO users (name, password_hash, role, created_at) VALUES (%s, %s, %s, %s) "
        "RETURNING id, name, role, created_at",
        (name, generate_password_hash(password), role, now),
    )
    row = cur.fetchone()
    log_activity(cur, None, name, "create_user", f"{session['name']} added {name} as {role}")
    db.commit()
    cur.close()
    return jsonify(dict(row)), 201


@app.route("/api/users/<int:user_id>/reset-password", methods=["POST"])
@owner_required
def api_reset_user_password(user_id):
    data = request.get_json(force=True) or {}
    new_password = data.get("new_password") or ""
    if len(new_password) < 4:
        return jsonify({"error": "New password must be at least 4 characters"}), 400
    if user_id == session["user_id"]:
        return jsonify({"error": "Use Account -> Change password to change your own password"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE users SET password_hash = %s WHERE id = %s RETURNING name",
        (generate_password_hash(new_password), user_id),
    )
    row = cur.fetchone()
    if row:
        log_activity(cur, None, row["name"], "reset_password", f"{session['name']} reset the password for {row['name']}")
    db.commit()
    cur.close()
    if row is None:
        return jsonify({"error": "User not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/users/<int:user_id>", methods=["DELETE"])
@owner_required
def api_delete_user(user_id):
    if user_id == session["user_id"]:
        return jsonify({"error": "You can't remove your own account while logged in as it"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT name, role FROM users WHERE id = %s", (user_id,))
    target = cur.fetchone()
    if not target:
        cur.close()
        return jsonify({"error": "User not found"}), 404

    if target["role"] == "owner":
        cur.execute("SELECT COUNT(*) AS c FROM users WHERE role = 'owner'")
        if cur.fetchone()["c"] <= 1:
            cur.close()
            return jsonify({"error": "There must be at least one owner account"}), 400

    cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
    log_activity(cur, None, target["name"], "delete_user", f"{session['name']} removed {target['name']} ({target['role']})")
    db.commit()
    cur.close()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Routes - settings
# ---------------------------------------------------------------------------

@app.route("/api/settings", methods=["GET"])
@owner_required
def api_get_settings():
    return jsonify({
        "n8n_webhook_url": get_setting("n8n_webhook_url", ""),
        # COD/Prepaid scheduling — see the "Routes - COD/Prepaid scheduling"
        # section below for how these are used.
        "cod_shipping_threshold": get_setting("cod_shipping_threshold", "140"),
        "cod_staff_ids": _parse_staff_ids(get_setting("cod_staff_id", "")),
        "prepaid_staff_ids": _parse_staff_ids(get_setting("prepaid_staff_id", "")),
    })


@app.route("/api/settings", methods=["POST"])
@owner_required
def api_save_settings():
    data = request.get_json(force=True) or {}
    url = (data.get("n8n_webhook_url") or "").strip()
    set_setting("n8n_webhook_url", url)
    db = get_db()
    cur = db.cursor()
    log_activity(cur, None, "settings", "settings", f"{session['name']} updated the n8n webhook URL")
    db.commit()
    cur.close()
    return jsonify({"ok": True, "n8n_webhook_url": url})


# ---------------------------------------------------------------------------
# Routes - COD/Prepaid scheduling (owner only)
#
# Payment type isn't a separate field synced from Shopify — the owner told
# us it's implied by the shipping charge on the order (e.g. Rs 140 shipping
# means COD, Rs 70/75 means Prepaid). So instead of hardcoding that, we
# store a single threshold: any order with shipping_amount >= threshold is
# COD, everything below it is Prepaid. That threshold, plus which staff
# member(s) are on COD duty vs Prepaid duty, is saved here — assignment is
# a standing rule ("X and Y handle all COD orders"), not something set per
# order, so every order (past and future) always reflects the current rule.
# Multiple staff can be assigned to the same category, and the same staff
# member can be assigned to both — stored as a comma-separated list of user
# ids in a single TEXT setting.
# ---------------------------------------------------------------------------

def _parse_staff_ids(raw):
    return [s for s in (raw or "").split(",") if s]


@app.route("/api/settings/schedule", methods=["GET"])
@owner_required
def api_get_schedule_settings():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, name FROM users WHERE role = 'staff' ORDER BY name")
    staff = [dict(r) for r in cur.fetchall()]
    cur.close()
    return jsonify({
        "cod_shipping_threshold": get_setting("cod_shipping_threshold", "140"),
        "cod_staff_ids": _parse_staff_ids(get_setting("cod_staff_id", "")),
        "prepaid_staff_ids": _parse_staff_ids(get_setting("prepaid_staff_id", "")),
        "staff": staff,
    })


@app.route("/api/settings/schedule", methods=["POST"])
@owner_required
def api_save_schedule_settings():
    data = request.get_json(force=True) or {}
    threshold_raw = data.get("cod_shipping_threshold", "140")
    try:
        threshold = float(threshold_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "COD shipping threshold must be a number"}), 400
    if threshold < 0:
        return jsonify({"error": "COD shipping threshold can't be negative"}), 400

    # Multiple staff can be assigned to the same category, and the same
    # staff member can be assigned to both COD and Prepaid — stored as a
    # comma-separated list of user ids (still a single TEXT setting value).
    cod_staff_ids = [str(i).strip() for i in (data.get("cod_staff_ids") or []) if str(i).strip()]
    prepaid_staff_ids = [str(i).strip() for i in (data.get("prepaid_staff_ids") or []) if str(i).strip()]

    set_setting("cod_shipping_threshold", str(threshold))
    set_setting("cod_staff_id", ",".join(cod_staff_ids))
    set_setting("prepaid_staff_id", ",".join(prepaid_staff_ids))

    db = get_db()
    cur = db.cursor()
    log_activity(cur, None, "schedule", "settings",
                 f"{session['name']} updated COD/Prepaid staff scheduling")
    db.commit()
    cur.close()
    return jsonify({
        "ok": True,
        "cod_shipping_threshold": str(threshold),
        "cod_staff_ids": cod_staff_ids,
        "prepaid_staff_ids": prepaid_staff_ids,
    })


# ---------------------------------------------------------------------------
# Routes - sync from n8n (n8n talks to Shopify, this app talks to n8n)
# ---------------------------------------------------------------------------

SYNC_HISTORY_MAX = 30


def record_sync_event(ok, latency_ms, attempts):
    """Keep the last N sync attempts (success, webhook latency) in settings."""
    import json
    try:
        history = json.loads(get_setting("sync_history", "[]") or "[]")
    except ValueError:
        history = []
    history.append({
        "t": datetime.now(timezone.utc).isoformat(),
        "ok": bool(ok),
        "ms": int(latency_ms) if latency_ms is not None else None,
        "tries": attempts,
    })
    set_setting("sync_history", json.dumps(history[-SYNC_HISTORY_MAX:]))


@app.route("/api/sync/health")
@staff_or_owner_required
def api_sync_health():
    import json
    try:
        history = json.loads(get_setting("sync_history", "[]") or "[]")
    except ValueError:
        history = []
    if not history:
        return jsonify({"samples": 0, "status": "No data"})
    ok_runs = [h for h in history if h.get("ok") and h.get("ms") is not None]
    last = history[-1]
    avg_ms = round(sum(h["ms"] for h in ok_runs) / len(ok_runs)) if ok_runs else None
    integrity = round(100 * sum(1 for h in history if h.get("ok")) / len(history))
    return jsonify({
        "samples": len(history),
        "integrity_pct": integrity,
        "last_ok": last.get("ok"),
        "last_ms": last.get("ms"),
        "avg_ms": avg_ms,
        "last_at": last.get("t"),
        "status": "Live" if last.get("ok") else "Down",
    })


SYNC_PROGRESS_KEY = "sync_progress"


def _write_progress(state):
    """Best-effort progress write on its OWN connection, so it never commits
    the sync's own transaction midway."""
    import json
    pool = get_pool()
    conn = pool.getconn()
    broken = False
    try:
        with conn.cursor() as c:
            c.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (SYNC_PROGRESS_KEY, json.dumps(state)),
            )
        conn.commit()
    except Exception:
        broken = True
    finally:
        pool.putconn(conn, close=broken)


def _read_progress():
    import json
    cur = get_db().cursor()
    cur.execute("SELECT value FROM settings WHERE key = %s", (SYNC_PROGRESS_KEY,))
    row = cur.fetchone()
    cur.close()
    try:
        return json.loads(row["value"]) if row and row["value"] else None
    except ValueError:
        return None


def _do_sync(webhook_url, user_name, progress):
    """Runs inside a background thread (app context). Returns (dict, status)."""

    # The first call to the n8n webhook after some idle time has reliably
    # failed here (both connection errors and an HTML error page instead of
    # JSON), while a retry after a short pause succeeds — likely n8n
    # "waking up" the webhook registration. Rather than surface that to the
    # owner every time, retry automatically (with an increasing pause)
    # before giving up.
    last_error = None
    payload = None
    latency_ms = None
    attempts_used = 0
    delays = [0, 3, 6]
    for attempt, delay in enumerate(delays):
        attempts_used = attempt + 1
        if delay:
            time.sleep(delay)
        t0 = time.perf_counter()
        try:
            resp = requests.get(webhook_url, timeout=180)
            resp.raise_for_status()
        except requests.exceptions.RequestException as exc:
            last_error = f"Could not reach n8n webhook: {exc}"
            continue
        latency_ms = (time.perf_counter() - t0) * 1000

        try:
            payload = resp.json()
            last_error = None
            break
        except ValueError:
            # requests.Response.json() raises a JSONDecodeError that is
            # ALSO a RequestException subclass — handled in its own except
            # so it isn't mislabeled as a connectivity failure above. A
            # common cause: n8n's "Respond to Webhook" node returned an
            # empty body or an HTML error page instead of JSON.
            body_preview = (resp.text or "")[:200]
            last_error = (
                "n8n webhook reached, but didn't return valid JSON "
                f"(got: {body_preview!r}). Check the Respond to Webhook "
                "node in n8n — it should always emit valid JSON, even "
                "an empty [] when there's nothing to sync."
            )

    if last_error is not None:
        record_sync_event(False, None, attempts_used)
        return {"error": last_error}, 502
    record_sync_event(True, latency_ms, attempts_used)

    # Expected payload shape (see README): a list of orders, each with a
    # list of line items. Adjust here if your n8n Code node outputs
    # differently.
    orders = payload if isinstance(payload, list) else payload.get("orders", [])

    progress.update(phase="saving", total=len(orders), done=0)
    _write_progress(progress)

    db = get_db()
    cur = db.cursor()
    now = datetime.now(timezone.utc).isoformat()
    order_count = 0
    item_count = 0

    for idx, order in enumerate(orders):
        if idx and idx % 3 == 0:
            progress["done"] = idx
            _write_progress(progress)
        order_id = str(order.get("id") or order.get("order_id") or "").strip()
        if not order_id:
            continue
        order_name = order.get("name") or order.get("order_name") or order_id
        customer_name = order.get("customer_name") or order.get("customer") or ""
        created_at = order.get("created_at") or ""

        addr = order.get("shipping_address") or {}
        address1 = addr.get("address1") or ""
        address2 = addr.get("address2") or ""
        city = addr.get("city") or ""
        state = addr.get("province") or addr.get("state") or ""
        pincode = addr.get("zip") or ""
        full_address = ", ".join(p for p in [address1, address2, city, state, pincode] if p)
        customer_phone = addr.get("phone") or order.get("phone") or ""

        # Shipping charge — this is what COD vs Prepaid is derived from (see
        # Settings -> Schedule). Accept a couple of shapes since it depends
        # on how the n8n Code node reshapes the Shopify order.
        shipping_amount_raw = (
            order.get("shipping_amount")
            or order.get("total_shipping_price")
            or ""
        )
        try:
            shipping_amount = float(shipping_amount_raw) if shipping_amount_raw != "" else None
        except (TypeError, ValueError):
            shipping_amount = None

        # Outstanding balance on the order (Shopify's total_outstanding) —
        # used on COD invoices so "Amount to be Received" reflects what's
        # actually still owed on a partially-paid order, not the full
        # order total. None when not synced, e.g. a manually-pasted order —
        # the invoice falls back to the full grand total in that case.
        balance_due_raw = order.get("balance_due")
        try:
            balance_due = float(balance_due_raw) if balance_due_raw not in (None, "") else None
        except (TypeError, ValueError):
            balance_due = None

        cur.execute(
            "INSERT INTO orders (shopify_order_id, order_name, customer_name, "
            "shipping_address, shipping_address1, shipping_address2, shipping_city, "
            "shipping_state, shipping_pincode, customer_phone, shipping_amount, "
            "balance_due, created_at, synced_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (shopify_order_id) DO UPDATE SET "
            "order_name = EXCLUDED.order_name, "
            "customer_name = CASE WHEN orders.order_edited THEN orders.customer_name ELSE EXCLUDED.customer_name END, "
            "shipping_address = CASE WHEN orders.order_edited THEN orders.shipping_address ELSE EXCLUDED.shipping_address END, "
            "shipping_address1 = CASE WHEN orders.order_edited THEN orders.shipping_address1 ELSE EXCLUDED.shipping_address1 END, "
            "shipping_address2 = CASE WHEN orders.order_edited THEN orders.shipping_address2 ELSE EXCLUDED.shipping_address2 END, "
            "shipping_city = CASE WHEN orders.order_edited THEN orders.shipping_city ELSE EXCLUDED.shipping_city END, "
            "shipping_state = CASE WHEN orders.order_edited THEN orders.shipping_state ELSE EXCLUDED.shipping_state END, "
            "shipping_pincode = CASE WHEN orders.order_edited THEN orders.shipping_pincode ELSE EXCLUDED.shipping_pincode END, "
            "customer_phone = CASE WHEN orders.order_edited THEN orders.customer_phone ELSE EXCLUDED.customer_phone END, "
            "shipping_amount = CASE WHEN orders.order_edited THEN orders.shipping_amount ELSE EXCLUDED.shipping_amount END, "
            "balance_due = CASE WHEN orders.order_edited THEN orders.balance_due ELSE EXCLUDED.balance_due END, "
            "created_at = EXCLUDED.created_at, synced_at = EXCLUDED.synced_at",
            (order_id, order_name, customer_name, full_address, address1, address2,
             city, state, pincode, customer_phone, shipping_amount, balance_due, created_at, now),
        )
        order_count += 1

        line_items = order.get("line_items", []) or []
        for li in line_items:
            item_id = str(li.get("id") or f"{order_id}-{li.get('title', '')}")
            title = li.get("title") or li.get("name") or ""
            variant_title = li.get("variant_title") or ""
            quantity = li.get("quantity") or 1
            price = str(li.get("price") or "")
            vendor = li.get("vendor") or ""

            cur.execute("SELECT status FROM items WHERE id = %s", (item_id,))
            existing = cur.fetchone()
            if existing:
                # Keep whatever status was already set; just refresh details.
                cur.execute(
                    "UPDATE items SET shopify_order_id=%s, updated_at=%s, "
                    "title = CASE WHEN item_edited THEN title ELSE %s END, "
                    "variant_title = CASE WHEN item_edited THEN variant_title ELSE %s END, "
                    "quantity = CASE WHEN item_edited THEN quantity ELSE %s END, "
                    "vendor = CASE WHEN item_edited THEN vendor ELSE %s END, "
                    "price = CASE WHEN price_edited OR item_edited THEN price ELSE %s END, "
                    "original_price = CASE WHEN price_edited OR item_edited THEN %s ELSE original_price END "
                    "WHERE id=%s",
                    (order_id, now, title, variant_title, quantity, vendor, price, price, item_id),
                )
            else:
                cur.execute(
                    "INSERT INTO items (id, shopify_order_id, title, variant_title, "
                    "quantity, price, vendor, status, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s)",
                    (item_id, order_id, title, variant_title, quantity, price, vendor, now),
                )
            item_count += 1

    log_activity(cur, None, "sync", "sync", f"{user_name} synced {order_count} orders / {item_count} items from Shopify", user_name=user_name)
    db.commit()
    cur.close()
    return {"ok": True, "orders_synced": order_count, "items_synced": item_count}, 200


def _run_sync_job(state, webhook_url, user_name):
    import threading  # noqa: F401
    with app.app_context():
        try:
            result, status = _do_sync(webhook_url, user_name, state)
        except Exception as exc:  # never leave the UI stuck on "running"
            result, status = {"error": f"Sync crashed: {type(exc).__name__}: {exc}"}, 500
        if status == 200:
            state.update(state="done", phase="done", orders_synced=result["orders_synced"],
                         items_synced=result["items_synced"], done=state.get("total", 0))
        else:
            state.update(state="error", message=result.get("error", "Sync failed."))
        state["finished"] = time.time()
        _write_progress(state)


@app.route("/api/sync", methods=["POST"])
@staff_or_owner_required
def api_sync():
    import json
    import threading
    webhook_url = get_setting("n8n_webhook_url")
    if not webhook_url:
        return jsonify({"error": "No n8n webhook URL configured. Set it in Settings first."}), 400

    current = _read_progress()
    if current and current.get("state") == "running" and time.time() - current.get("started", 0) < 720:
        return jsonify({"error": "A sync is already running."}), 409

    try:
        history = json.loads(get_setting("sync_history", "[]") or "[]")
    except ValueError:
        history = []
    times = [h["ms"] for h in history if h.get("ok") and h.get("ms")]
    expected_ms = max(2000, sum(times) / len(times)) if times else 8000

    state = {"id": uuid.uuid4().hex, "state": "running", "phase": "fetching",
             "started": time.time(), "expected_ms": expected_ms, "done": 0, "total": 0}
    _write_progress(state)
    threading.Thread(target=_run_sync_job, args=(state, webhook_url, session["name"]), daemon=True).start()
    return jsonify({"ok": True, "id": state["id"]}), 202


@app.route("/api/sync/progress")
@staff_or_owner_required
def api_sync_progress():
    st = _read_progress()
    if not st:
        return jsonify({"state": "idle", "percent": 0})
    now = time.time()
    if st.get("state") == "running" and now - st.get("started", now) > 720:
        st.update(state="error", message="Sync was interrupted (server restarted?). Try again.")
    if st.get("state") == "running":
        if st.get("phase") == "fetching":
            elapsed = (now - st["started"]) * 1000
            percent = int(min(38, 38 * elapsed / max(st.get("expected_ms", 8000), 1)))
        else:
            percent = min(99, 40 + int(60 * st.get("done", 0) / max(st.get("total", 1), 1)))
    elif st.get("state") == "done":
        percent = 100
    else:
        percent = 0
    return jsonify({**st, "percent": percent})


@app.route("/api/orders/manual", methods=["POST"])
@telecaller_or_owner_required
def api_add_manual_order():
    data = request.get_json(force=True) or {}

    raw_order_id = (data.get("order_id") or "").strip()
    customer_name = (data.get("customer_name") or "").strip()
    phone = re.sub(r"\D", "", data.get("phone") or "")
    address1 = (data.get("address1") or "").strip()
    address2 = (data.get("address2") or "").strip()
    city = (data.get("city") or "").strip()
    state = (data.get("state") or "").strip()
    pincode = (data.get("pincode") or "").strip()
    payment_type = data.get("payment_type")
    items_in = data.get("items") or []

    errors = []
    if not raw_order_id:
        errors.append("Order ID is required")
    if not customer_name:
        errors.append("Customer name is required")
    if not address1:
        errors.append("Address is required")
    if not city:
        errors.append("City is required")
    if not state:
        errors.append("State is required")
    if not pincode:
        errors.append("Pincode is required")
    if payment_type not in ("cod", "prepaid"):
        errors.append("Payment type must be COD or Prepaid")

    def _num(value, field, allow_none=False):
        if value in (None, ""):
            if allow_none:
                return None
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            errors.append(f"{field} must be a number")
            return 0.0

    shipping_amount = _num(data.get("shipping_amount"), "Delivery charge")
    balance_due = _num(data.get("balance_due"), "Cash to be collected", allow_none=True)
    if balance_due is not None and balance_due < 0:
        errors.append("Cash to be collected can't be negative")
    if shipping_amount < 0:
        errors.append("Delivery charge can't be negative")

    parsed_items = []
    for idx, raw_item in enumerate(items_in, start=1):
        title = (raw_item.get("title") or "").strip()
        vendor = (raw_item.get("vendor") or "").strip()
        if not title:
            continue  # a fully-blank item row (e.g. added then not filled in) is just skipped
        try:
            quantity = int(raw_item.get("quantity") or 1)
        except (TypeError, ValueError):
            errors.append(f"Item {idx}: quantity must be a whole number")
            quantity = 1
        if quantity < 1:
            errors.append(f"Item {idx}: quantity must be at least 1")
            quantity = 1
        try:
            price = float(raw_item.get("price") or 0)
        except (TypeError, ValueError):
            errors.append(f"Item {idx}: price must be a number")
            price = 0.0
        if price < 0:
            errors.append(f"Item {idx}: price can't be negative")
        parsed_items.append({"title": title, "vendor": vendor, "quantity": quantity, "price": price})

    if not parsed_items:
        errors.append("At least one item is required")

    if errors:
        return jsonify({"error": "; ".join(errors)}), 400

    order_id = "manual-" + re.sub(r"\s+", "-", raw_order_id).lower()
    full_address = ", ".join(p for p in [address1, address2, city, state, pincode] if p)

    db = get_db()
    cur = db.cursor()
    now = datetime.now(timezone.utc).isoformat()

    cur.execute("SELECT shopify_order_id FROM orders WHERE shopify_order_id = %s", (order_id,))
    if cur.fetchone():
        cur.close()
        return jsonify({"error": f"Order {raw_order_id} already exists"}), 400

    cur.execute(
        "INSERT INTO orders (shopify_order_id, order_name, customer_name, customer_phone, "
        "shipping_address, shipping_address1, shipping_address2, shipping_city, shipping_state, "
        "shipping_pincode, shipping_amount, balance_due, payment_type, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (order_id, raw_order_id, customer_name, phone, full_address, address1, address2, city,
         state, pincode, shipping_amount, balance_due, payment_type, now),
    )

    for item in parsed_items:
        item_id = f"{order_id}-item-{uuid.uuid4().hex[:8]}"
        cur.execute(
            "INSERT INTO items (id, shopify_order_id, title, variant_title, quantity, price, "
            "vendor, status, updated_at) "
            "VALUES (%s, %s, %s, '', %s, %s, %s, 'pending', %s)",
            (item_id, order_id, item["title"], item["quantity"], str(item["price"]), item["vendor"], now),
        )
        log_activity(cur, item_id, item["title"], "manual_add",
                     f"{session['name']} added this item manually",
                     order_id=order_id)

    db.commit()
    cur.close()
    return jsonify({"ok": True, "order_id": order_id, "items_added": len(parsed_items)})


# ---------------------------------------------------------------------------
# Routes - orders / items
# ---------------------------------------------------------------------------

def resolve_payment_type(order_row, cod_threshold):
    """COD vs Prepaid for one order. An explicit orders.payment_type (set
    when a telecaller enters the order manually — they know this directly,
    no need to infer it) always wins. Otherwise, for Shopify-synced orders,
    it's derived from the shipping charge against the threshold in
    Settings -> Schedule, same as before — so changing that threshold still
    applies retroactively to every synced order, past and future."""
    explicit = order_row.get("payment_type") if hasattr(order_row, "get") else order_row["payment_type"]
    if explicit in ("cod", "prepaid"):
        return explicit
    shipping_amount = order_row["shipping_amount"]
    if shipping_amount is None:
        return None
    return "cod" if float(shipping_amount) >= cod_threshold else "prepaid"


def invoice_number_for_payment(invoice_number, payment_type):
    """Swap the SHP/COD prefix of an existing invoice number to match the
    payment type (COD -> "COD/...", anything else -> "SHP/..."). The running
    sequence and year are kept. Numbers with any other format are returned
    unchanged."""
    if not invoice_number or "/" not in invoice_number:
        return invoice_number
    prefix, rest = invoice_number.split("/", 1)
    if prefix not in ("SHP", "COD"):
        return invoice_number
    return ("COD" if payment_type == "cod" else "SHP") + "/" + rest


def price_edit_delta(billing_items):
    """How much the manually edited item prices changed the order total vs
    what Shopify billed (sum of (new - original) x qty). Shopify's
    balance_due still reflects the original prices, so COD "Amount to be
    Received" is adjusted by this."""
    delta = 0.0
    for i in billing_items:
        if i.get("price_edited") and i.get("original_price") not in (None, ""):
            try:
                delta += (float(i["price"] or 0) - float(i["original_price"])) * (i["quantity"] or 0)
            except (TypeError, ValueError):
                pass
    return delta


@app.route("/api/orders", methods=["GET"])
@login_required
def api_orders():
    db = get_db()
    cur = db.cursor()
    status_filter = request.args.get("status")
    payment_filter = request.args.get("payment")  # 'cod' or 'prepaid'
    date_from = request.args.get("date_from")  # 'YYYY-MM-DD', inclusive
    date_to = request.args.get("date_to")      # 'YYYY-MM-DD', inclusive

    # Accounts is a Billing-only role — enforced here, not just hidden in
    # the UI, so it can't be bypassed by calling the API directly.
    if session.get("role") == "accounts":
        status_filter = "billing"

    # Packer only ever needs Billing orders whose invoice has already been
    # printed — that's the signal packing is ready to start. Force both,
    # server-side, same reasoning as Accounts above.
    invoice_filter = request.args.get("invoice")  # 'printed' or 'not_printed'
    if session.get("role") == "packer":
        status_filter = "billing"
        invoice_filter = "printed"

    # Same idea as invoice_filter above, but for whether an order's
    # invoice has already gone out in a Tally export — independent of
    # printed status (an invoice can be printed but not yet exported, or
    # in principle re-exported later).
    tally_filter = request.args.get("tally")  # 'exported' or 'not_exported'

    # Purchase staff work item-by-item off a single unified list — the
    # Billing/Closed groupings (and the Invoice/Tally sub-filters that only
    # apply within Billing) aren't part of their workflow, so collapse
    # those back to "all orders" for them, enforced here too and not just
    # hidden in the UI.
    if session.get("role") == "staff" and status_filter in ("billing", "closed"):
        status_filter = None
        invoice_filter = None
        tally_filter = None

    # Trash, Refunded, and Cancelled are otherwise-hidden views. Owner gets
    # full access; telecaller can look but never edit (enforced by the
    # role checks on every write endpoint below), so they can check any
    # order's full history when helping a customer on the phone.
    if status_filter == "trash" and session.get("role") not in ("owner", "telecaller"):
        return jsonify({"error": "Only the owner can view Trash"}), 403

    if status_filter == "refunded" and session.get("role") not in ("owner", "telecaller"):
        return jsonify({"error": "Only the owner can view Refunded items"}), 403

    # Cancelled orders are their own view, same reasoning as Trash/Refunded
    # above.
    if status_filter == "cancelled" and session.get("role") not in ("owner", "telecaller"):
        return jsonify({"error": "Only the owner can view Cancelled orders"}), 403

    # created_at is stored as ISO-8601 text (as it comes from Shopify), so
    # cast it to a date for the range comparison rather than a string match.
    query = "SELECT * FROM orders WHERE 1=1"
    params = []
    query += " AND deleted_at IS NOT NULL" if status_filter == "trash" else " AND deleted_at IS NULL"
    if status_filter == "cancelled":
        query += " AND cancelled = true"
    if date_from:
        # NULLIF guards against a blank/unparseable created_at (e.g. odd
        # sync data) throwing a cast error — it just won't match instead.
        query += " AND NULLIF(created_at, '')::date >= %s::date"
        params.append(date_from)
    if date_to:
        query += " AND NULLIF(created_at, '')::date <= %s::date"
        params.append(date_to)
    # Exact order number (e.g. "#7257") — used by "Open" on the Risk page and the
    # Summary chips to show that one order instantly, before the full list arrives.
    order_name_exact = (request.args.get("order_name") or "").strip()
    if order_name_exact:
        query += " AND order_name = %s"
        params.append(order_name_exact)
    query += " ORDER BY created_at DESC"
    _limit_n = request.args.get("limit", type=int)
    if _limit_n and _limit_n > 0:
        _limit_n = min(_limit_n, 1000)
        query += " LIMIT %s"
        params.append(_limit_n)
    else:
        _limit_n = None
    cur.execute(query, params)
    orders = cur.fetchall()

    # COD/Prepaid is derived from each order's shipping charge against the
    # threshold set in Settings -> Schedule, not stored per order — so
    # changing the threshold or the staff assignment always applies
    # retroactively to every order, past and future.
    cod_threshold = float(get_setting("cod_shipping_threshold", "140") or 140)
    cod_staff_ids = _parse_staff_ids(get_setting("cod_staff_id", ""))
    prepaid_staff_ids = _parse_staff_ids(get_setting("prepaid_staff_id", ""))
    staff_names = {}
    all_staff_ids = set(cod_staff_ids) | set(prepaid_staff_ids)
    if all_staff_ids:
        cur.execute("SELECT id, name FROM users WHERE id = ANY(%s)", ([int(s) for s in all_staff_ids],))
        staff_names = {str(r["id"]): r["name"] for r in cur.fetchall()}

    # Fetch items for ALL orders in a single query instead of one query per
    # order (N+1), then group them in Python. This turns "1 + (1 per order)"
    # round-trips to the database into just 2, total — the main cost when
    # the database is a remote service like Supabase rather than a local file.
    order_ids = [order["shopify_order_id"] for order in orders]
    items_by_order = {oid: [] for oid in order_ids}
    if order_ids:
        cur.execute(
            "SELECT * FROM items WHERE shopify_order_id = ANY(%s) ORDER BY shopify_order_id, sort_order",
            (order_ids,),
        )
        for item in cur.fetchall():
            items_by_order.setdefault(item["shopify_order_id"], []).append(item)

    result = []
    for order in orders:
        items = items_by_order.get(order["shopify_order_id"], [])
        all_order_items = items  # unfiltered — status_filter below may narrow `items`
        # An order is "Closed" once every item in it has a final status —
        # purchased / stock / na — with none left pending (1, 2, or more
        # items, doesn't matter). This is computed fresh on every request
        # instead of stored, so it's always in sync with the items'
        # actual statuses and needs no separate "reopen" bookkeeping: if
        # an item goes back to pending, the order just stops being closed.
        closed = bool(items) and all(i["status"] != "pending" for i in items)

        shipping_amount = order["shipping_amount"]
        if shipping_amount is None and order.get("payment_type") not in ("cod", "prepaid"):
            # No shipping charge synced and no explicit payment type set
            # (e.g. an old manually-pasted order from before this field
            # existed) — payment type can't be determined, so leave it
            # unset rather than guessing.
            payment_type = None
            assigned_staff_ids = []
            assigned_to = None
        else:
            payment_type = resolve_payment_type(order, cod_threshold)
            assigned_staff_ids = cod_staff_ids if payment_type == "cod" else prepaid_staff_ids
            names = [staff_names[sid] for sid in assigned_staff_ids if sid in staff_names]
            assigned_to = ", ".join(names) if names else None

        # Staff accounts only see orders currently assigned to them (their
        # COD/Prepaid duty from Settings -> Schedule) — everyone else
        # (owner, telecaller, packer, accounts) sees the full list, subject
        # to whatever other filters apply below.
        if session.get("role") == "staff":
            if str(session.get("user_id")) not in assigned_staff_ids:
                continue

        if payment_filter in ("cod", "prepaid") and payment_type != payment_filter:
            continue

        if invoice_filter == "printed" and not order["invoice_number"]:
            continue
        if invoice_filter == "not_printed" and order["invoice_number"]:
            continue
        if invoice_filter == "bill_cancelled" and not order["bill_cancelled"]:
            continue

        if tally_filter == "exported" and not order["tally_exported_at"]:
            continue
        if tally_filter == "not_exported" and order["tally_exported_at"]:
            continue

        if status_filter == "closed":
            # Every item in the order, as long as the order itself is closed.
            if not closed:
                continue
        elif status_filter == "billing":
            # Only the purchased/in-stock items from Closed orders — an
            # N/A item never shows here, and if a Closed order has no
            # purchased/in-stock items at all it's left out entirely.
            if not closed:
                continue
            items = [i for i in items if i["status"] in ("purchased", "stock")]
            if not items:
                continue
        elif status_filter == "trash":
            pass  # every deleted order shows as-is, items unfiltered
        elif status_filter == "cancelled":
            pass  # every cancelled order shows as-is, items unfiltered
        elif status_filter:
            items = [i for i in items if i["status"] == status_filter]
            if not items:
                continue

        # Same "Amount to be Received" the invoice itself shows — computed
        # here too so it can be displayed right on the order card, without
        # needing to open/print the PDF. Only meaningful for COD orders
        # that have at least one purchased/in-stock item (i.e. reached
        # Billing); mirrors invoice.py's grand_total and N/A-item
        # deduction from balance_due exactly, so the two never disagree.
        billing_items_for_amount = [i for i in all_order_items if i["status"] in ("purchased", "stock")]
        amount_to_receive = None
        order_total = None
        if billing_items_for_amount:
            # Invoice grand total (purchased/in-stock items + shipping) —
            # shown for every order in Billing, COD and Prepaid alike.
            total_incl = sum(
                float(i["price"] or 0) * (i["quantity"] or 0) for i in billing_items_for_amount
            )
            grand_total = round(total_incl + float(shipping_amount or 0), 2)
            order_total = grand_total
        if payment_type == "cod" and billing_items_for_amount:
            balance_due = order["balance_due"]
            if balance_due is not None:
                na_items = [i for i in all_order_items if i["status"] == "na"]
                na_value = sum(float(i["price"] or 0) * (i["quantity"] or 0) for i in na_items)
                amount_to_receive = round(max(0.0, float(balance_due) - na_value
                                              + price_edit_delta([dict(i) for i in billing_items_for_amount])), 2)
            else:
                amount_to_receive = grand_total

        # When this order most recently became Billing-eligible — the
        # latest status-change timestamp among its purchased/in-stock
        # items (whichever one a staff member marked last is effectively
        # when the order was ready to bill).
        billed_at = None
        billed_timestamps = [i["updated_at"] for i in billing_items_for_amount if i["updated_at"]]
        if billed_timestamps:
            billed_at = max(billed_timestamps)

        result.append(
            {
                "order_id": order["shopify_order_id"],
                "order_name": order["order_name"],
                "customer_name": order["customer_name"],
                "customer_phone": order["customer_phone"],
                "shipping_address": order["shipping_address"],
                "shipping_address1": order["shipping_address1"],
                "shipping_address2": order["shipping_address2"],
                "shipping_city": order["shipping_city"],
                "shipping_state": order["shipping_state"],
                "shipping_pincode": order["shipping_pincode"],
                "created_at": order["created_at"],
                "shipping_amount": shipping_amount,
                "closed": closed,
                "payment_type": payment_type,
                "amount_to_receive": amount_to_receive,
                "order_total": order_total,
                "billed_at": billed_at,
                "packed": order["packed"],
                "packed_by": order["packed_by"],
                "packed_at": order["packed_at"],
                "cancelled": order["cancelled"],
                "cancelled_reason": order["cancelled_reason"],
                "cancelled_by": order["cancelled_by"],
                "cancelled_at": order["cancelled_at"],
                "assigned_to": assigned_to,
                "bill_cancelled": order["bill_cancelled"],
                "bill_cancelled_reason": order["bill_cancelled_reason"],
                "bill_cancelled_by": order["bill_cancelled_by"],
                "bill_cancelled_at": order["bill_cancelled_at"],
                "invoice_number": order["invoice_number"],
                "invoice_printed_by": order["invoice_printed_by"],
                "tally_exported_at": order["tally_exported_at"],
                "deleted_at": order["deleted_at"],
                "items": [dict(i) for i in items],
            }
        )

    cur.close()
    resp = jsonify(result)
    # Uncompressed size, so the browser can show a real download progress even
    # when a proxy (Caddy) gzips the response and hides Content-Length.
    resp.headers["X-Raw-Length"] = str(len(resp.get_data()))
    resp.headers["X-Truncated"] = "1" if (_limit_n and len(orders) >= _limit_n) else "0"
    return resp


# ---------------------------------------------------------------------------
# Risk (Menu -> Risk): orders running late
#   Bill risk: arrived more than 48 h ago and still not in Billing (any item
#              statuses); all of the order's items are listed.
#   Pack risk: in Billing for more than 24 h and still not packed.
# Same Billing rule as /api/orders: an order is in Billing once no item is
# Pending and at least one item is Purchased / In Stock. "In Billing since" is
# the same time as the order card's "Billed" badge (the last Purchased / In
# Stock item's change). Trashed and cancelled orders are never at risk.
# ---------------------------------------------------------------------------
RISK_HOURS = 48  # Risk: arrived this many hours ago and still not closed


def _parse_ts(value):
    """ISO-8601 text (Shopify or our own) -> aware datetime, or None."""
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


@app.route("/api/risk", methods=["GET"])
@login_required
def api_risk():
    if session.get("role") not in ("owner", "telecaller"):
        return jsonify({"error": "Only the owner or telecaller can view Risk"}), 403
    db = get_db()
    cur = db.cursor()
    # Let the database do the narrowing (much faster over a remote database like
    # Supabase): only orders that can be Risk (still has a Pending item) or Danger
    # (no Pending item, has a Purchased/In Stock item, not packed, bill not
    # cancelled), and only the columns the cards use. The exact 48-hour rule and
    # the Delay Days are then worked out below, same as before.
    cur.execute(
        "SELECT o.shopify_order_id, o.order_name, o.customer_name, o.created_at, "
        "       o.shipping_amount, o.payment_type, o.invoice_number, o.packed, o.bill_cancelled "
        "FROM orders o "
        "JOIN (SELECT shopify_order_id, "
        "             bool_or(status = 'pending') AS has_pending, "
        "             bool_or(status <> 'pending') AS has_done, "
        "             bool_or(status IN ('purchased', 'stock')) AS has_billable "
        "      FROM items GROUP BY shopify_order_id) a ON a.shopify_order_id = o.shopify_order_id "
        "WHERE o.deleted_at IS NULL AND NOT COALESCE(o.cancelled, false) "
        "  AND ((a.has_pending AND a.has_done) "
        "       OR (a.has_billable AND NOT COALESCE(o.packed, false) AND NOT COALESCE(o.bill_cancelled, false))) "
        "ORDER BY o.created_at ASC"
    )
    orders = cur.fetchall()

    cod_threshold = float(get_setting("cod_shipping_threshold", "140") or 140)
    cod_staff_ids = _parse_staff_ids(get_setting("cod_staff_id", ""))
    prepaid_staff_ids = _parse_staff_ids(get_setting("prepaid_staff_id", ""))
    staff_names = {}
    all_staff_ids = set(cod_staff_ids) | set(prepaid_staff_ids)
    if all_staff_ids:
        cur.execute("SELECT id, name FROM users WHERE id = ANY(%s)", ([int(x) for x in all_staff_ids],))
        staff_names = {str(r["id"]): r["name"] for r in cur.fetchall()}

    order_ids = [o["shopify_order_id"] for o in orders]
    items_by_order = {oid: [] for oid in order_ids}
    if order_ids:
        cur.execute(
            "SELECT shopify_order_id, title, variant_title, quantity, price, vendor, status, updated_at "
            "FROM items WHERE shopify_order_id = ANY(%s) ORDER BY shopify_order_id, sort_order",
            (order_ids,),
        )
        for item in cur.fetchall():
            items_by_order.setdefault(item["shopify_order_id"], []).append(item)
    cur.close()

    now = datetime.now(timezone.utc)

    def card(order, items, since, hours):
        payment_type = None
        assigned_to = None
        if not (order["shipping_amount"] is None and order.get("payment_type") not in ("cod", "prepaid")):
            payment_type = resolve_payment_type(order, cod_threshold)
            ids = cod_staff_ids if payment_type == "cod" else prepaid_staff_ids
            names = [staff_names[i] for i in ids if i in staff_names]
            assigned_to = ", ".join(names) if names else None
        return {
            "order_id": order["shopify_order_id"],
            "order_name": order["order_name"],
            "customer_name": order["customer_name"],
            "payment_type": payment_type,
            "assigned_to": assigned_to,
            "created_at": order["created_at"],
            "since": since.isoformat(),
            "delay_hours": round(hours, 1),
            "delay_days": int(hours // 24),
            "invoice_number": order["invoice_number"],
            "items": [
                {"title": i["title"], "variant_title": i["variant_title"], "quantity": i["quantity"],
                 "price": i["price"], "vendor": i["vendor"], "status": i["status"]}
                for i in items
            ],
        }

    # Same "Closed" rule as the order list: every item has a final status, none Pending.
    #   Risk:   partly done — at least one item already moved on (Purchased, In Stock,
    #           N/A, ...) but at least one still Pending — and arrived RISK_HOURS+ hours
    #           ago. Orders where every item is still Pending are NOT Risk.
    #           Delay Days = full days since the order arrived.
    #   Danger: closed, has something to pack (a Purchased / In Stock item), and the
    #           order is not packed (bill-cancelled orders are left out: nothing to pack).
    #           Delay Days = full days since it closed (when its last Purchased /
    #           In Stock item was marked — the "Billed" date on the order card).
    # Every item of the order is listed on its card. Cancelled and deleted orders
    # are never included (see the query above).
    risk, danger = [], []
    for order in orders:
        items = items_by_order.get(order["shopify_order_id"], [])
        if not items:
            continue
        closed = all(i["status"] != "pending" for i in items)
        partly_done = any(i["status"] != "pending" for i in items)
        billing_items = [i for i in items if i["status"] in ("purchased", "stock")]
        if not closed:
            if not partly_done:
                continue  # every item still Pending: not Risk
            since = _parse_ts(order["created_at"])
            if not since:
                continue
            hours = (now - since).total_seconds() / 3600
            if hours >= RISK_HOURS:
                risk.append(card(order, items, since, hours))
        elif billing_items and not order["packed"] and not order["bill_cancelled"]:
            stamps = [t for t in (_parse_ts(i["updated_at"]) for i in billing_items) if t]
            since = max(stamps) if stamps else _parse_ts(order["created_at"])
            if not since:
                continue
            hours = max(0.0, (now - since).total_seconds() / 3600)
            danger.append(card(order, items, since, hours))

    risk.sort(key=lambda c: -c["delay_hours"])
    danger.sort(key=lambda c: -c["delay_hours"])
    return jsonify({"risk_hours": RISK_HOURS, "risk": risk, "danger": danger})


@app.route("/api/orders/summary", methods=["GET"])
@login_required
def api_orders_summary():
    """Order-count summary for a date range and/or an Order ID range: how
    many orders arrived, how many of those have been moved through to
    Billing (purchased/in-stock, nothing left pending), and how many are
    still pending.

    date_from/date_to ('YYYY-MM-DD', inclusive, from the browser's local
    date so "today" matches what the person looking at the screen
    considers today) filter by when the order came in. order_id_from/
    order_id_to filter by the order's visible number (order_name, e.g.
    "#1041") — handy for someone picking up a batch mid-day rather than a
    full day's orders; only the digits are compared, so "#" or other
    formatting doesn't matter. Any combination of the two can be given;
    with nothing at all, defaults to today.
    """
    db = get_db()
    cur = db.cursor()
    today_str = datetime.now(timezone.utc).date().isoformat()
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    order_id_from_raw = request.args.get("order_id_from")
    order_id_to_raw = request.args.get("order_id_to")
    order_id_from = re.sub(r"\D", "", order_id_from_raw) if order_id_from_raw else ""
    order_id_to = re.sub(r"\D", "", order_id_to_raw) if order_id_to_raw else ""

    if not date_from and not date_to and not order_id_from and not order_id_to:
        date_from = date_to = today_str

    query = "SELECT * FROM orders WHERE deleted_at IS NULL"
    params = []
    if date_from:
        query += " AND NULLIF(created_at, '')::date >= %s::date"
        params.append(date_from)
    if date_to:
        query += " AND NULLIF(created_at, '')::date <= %s::date"
        params.append(date_to)
    if order_id_from:
        query += " AND NULLIF(regexp_replace(order_name, '\\D', '', 'g'), '')::bigint >= %s"
        params.append(int(order_id_from))
    if order_id_to:
        query += " AND NULLIF(regexp_replace(order_name, '\\D', '', 'g'), '')::bigint <= %s"
        params.append(int(order_id_to))

    cur.execute(query, params)
    orders = cur.fetchall()

    order_ids = [order["shopify_order_id"] for order in orders]
    items_by_order = {oid: [] for oid in order_ids}
    if order_ids:
        cur.execute(
            "SELECT * FROM items WHERE shopify_order_id = ANY(%s)",
            (order_ids,),
        )
        for item in cur.fetchall():
            items_by_order.setdefault(item["shopify_order_id"], []).append(item)

    # Same "which orders can this role see" rule as /api/orders: a staff
    # account only ever sees orders currently assigned to their COD/Prepaid
    # duty, so their summary should only count those, not every order in
    # the range.
    cod_threshold = float(get_setting("cod_shipping_threshold", "140") or 140)
    cod_staff_ids = _parse_staff_ids(get_setting("cod_staff_id", ""))
    prepaid_staff_ids = _parse_staff_ids(get_setting("prepaid_staff_id", ""))

    arrived = 0
    billing = 0
    pending = 0
    arrived_orders = []
    billing_orders = []
    pending_orders = []
    for order in orders:
        if session.get("role") == "staff":
            shipping_amount = order["shipping_amount"]
            if shipping_amount is None and order.get("payment_type") not in ("cod", "prepaid"):
                continue
            payment_type = resolve_payment_type(order, cod_threshold)
            assigned_staff_ids = cod_staff_ids if payment_type == "cod" else prepaid_staff_ids
            if str(session.get("user_id")) not in assigned_staff_ids:
                continue

        items = items_by_order.get(order["shopify_order_id"], [])
        arrived += 1
        order_name = order["order_name"] or order["shopify_order_id"]
        arrived_orders.append(order_name)

        # Same "closed" + "has billable items" test used for the Billing
        # view in /api/orders — an order counts as billed once every item
        # has a final status and at least one of them is purchased/in-stock
        # (an order that's closed but entirely N/A never reaches Billing).
        closed = bool(items) and all(i["status"] != "pending" for i in items)
        billing_items = [i for i in items if i["status"] in ("purchased", "stock")]
        if closed and billing_items:
            billing += 1
            billing_orders.append(order_name)
        else:
            pending += 1
            pending_orders.append(order_name)

    # Sorted by the numeric part of the order number where there is one
    # (e.g. "#9474" before "#9480"), so the lists read in the same order
    # someone picking through a batch would expect — anything without a
    # clean number (a manual order's free-text ID) sorts after, by name.
    def sort_key(name):
        digits = re.sub(r"\D", "", name or "")
        return (0, int(digits)) if digits else (1, name or "")

    arrived_orders.sort(key=sort_key)
    billing_orders.sort(key=sort_key)
    pending_orders.sort(key=sort_key)

    cur.close()
    return jsonify(
        {
            "date_from": date_from or None,
            "date_to": date_to or None,
            "order_id_from": order_id_from or None,
            "order_id_to": order_id_to or None,
            "arrived": arrived,
            "billing": billing,
            "pending": pending,
            "arrived_orders": arrived_orders,
            "billing_orders": billing_orders,
            "pending_orders": pending_orders,
        }
    )


@app.route("/api/orders/export/pending.xlsx", methods=["GET"])
@login_required
def api_export_pending_orders():
    """Excel export of every Pending item, split into a COD sheet and a
    Prepaid sheet. Honors the same date_from/date_to range as the main
    order list (omit both to export every Pending order regardless of
    date). One row per pending item — Order ID, the item exactly as shown
    in the app, and a blank Vendor column for the purchase team to fill in.
    """
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font

    db = get_db()
    cur = db.cursor()
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")

    query = "SELECT * FROM orders WHERE deleted_at IS NULL"
    params = []
    if date_from:
        query += " AND NULLIF(created_at, '')::date >= %s::date"
        params.append(date_from)
    if date_to:
        query += " AND NULLIF(created_at, '')::date <= %s::date"
        params.append(date_to)
    query += " ORDER BY created_at DESC"
    cur.execute(query, params)
    orders = cur.fetchall()

    order_ids = [o["shopify_order_id"] for o in orders]
    items_by_order = {oid: [] for oid in order_ids}
    if order_ids:
        cur.execute("SELECT * FROM items WHERE shopify_order_id = ANY(%s)", (order_ids,))
        for item in cur.fetchall():
            items_by_order.setdefault(item["shopify_order_id"], []).append(item)

    cod_threshold = float(get_setting("cod_shipping_threshold", "140") or 140)
    cur.close()

    rows = {"cod": [], "prepaid": []}
    for order in orders:
        pending_items = [i for i in items_by_order.get(order["shopify_order_id"], []) if i["status"] == "pending"]
        if not pending_items:
            continue
        payment_type = resolve_payment_type(order, cod_threshold)
        if payment_type not in ("cod", "prepaid"):
            continue  # can't tell which sheet it belongs on — leave it out rather than guess
        for item in pending_items:
            item_text = item["title"] or ""
            if item["variant_title"]:
                item_text += f" — {item['variant_title']}"
            rows[payment_type].append([order["order_name"], item_text, ""])

    wb = Workbook()
    wb.remove(wb.active)
    for sheet_key, sheet_name in (("cod", "COD"), ("prepaid", "Prepaid")):
        ws = wb.create_sheet(sheet_name)
        ws.append(["Order ID", "Item", "Vendor"])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for row in rows[sheet_key]:
            ws.append(row)
        ws.column_dimensions["A"].width = 16
        ws.column_dimensions["B"].width = 60
        ws.column_dimensions["C"].width = 20

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = "pending_orders.xlsx"
    if date_from or date_to:
        filename = f"pending_orders_{date_from or 'start'}_to_{date_to or 'now'}.xlsx"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


class InvoiceError(Exception):
    """An order that can't be invoiced right now (message + HTTP status)."""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _issue_invoice(db, order_id, cod_threshold):
    """Everything one invoice print does, shared by the single Print Invoice
    route and the bulk PDF route so the two can never behave differently:
    checks, invoice number (first print) or reprint, activity log, saved
    totals. Returns (order_dict, billing_items, invoice_number, invoice_date,
    is_first_print). Raises InvoiceError if this order can't be invoiced."""
    cur = db.cursor()
    cur.execute("SELECT * FROM orders WHERE shopify_order_id = %s", (order_id,))
    order = cur.fetchone()
    if not order:
        cur.close()
        raise InvoiceError("Order not found", 404)

    if order["bill_cancelled"]:
        cur.close()
        raise InvoiceError("This bill is marked Cancelled — restore it first to print")

    # Staff can print the first copy of an invoice, but reprinting one
    # that's already been printed (a new invoice number is never issued
    # for it — see below) is an accounts/owner action only.
    if session.get("role") == "staff" and order["invoice_number"]:
        cur.close()
        raise InvoiceError("This invoice has already been printed — ask accounts or the owner to reprint it", 403)

    cur.execute(
        "SELECT * FROM items WHERE shopify_order_id = %s ORDER BY sort_order",
        (order_id,),
    )
    all_items = cur.fetchall()

    # Same "Billing" rule used by /api/orders: the order must be fully
    # closed (no item left pending) and have at least one purchased/in-stock
    # item — that's what actually appears on the invoice.
    closed = bool(all_items) and all(i["status"] != "pending" for i in all_items)
    billing_items = [i for i in all_items if i["status"] in ("purchased", "stock")]
    if not closed or not billing_items:
        cur.close()
        raise InvoiceError("This order isn't in Billing yet.")

    # Same COD/Prepaid rule used by /api/orders — an explicit payment_type
    # (manually-entered orders) wins, otherwise derived from the shipping
    # charge against the threshold in Settings -> Schedule. Computed here,
    # before the invoice number, so COD orders get a COD/... number and
    # everything else keeps the SHP/... prefix.
    payment_type = resolve_payment_type(order, cod_threshold) or "prepaid"
    invoice_prefix = "COD" if payment_type == "cod" else "SHP"

    is_first_print = not order["invoice_number"]
    if is_first_print:
        # Assign the next number from a continuous counter (starts at 2501)
        # the first time this order is printed, and lock the settings row
        # while doing it so two people printing at once can't collide on
        # the same number. Once set, it's stored on the order so reprinting
        # later always returns the same invoice number.
        cur.execute("SELECT value FROM settings WHERE key = %s FOR UPDATE", ("invoice_seq_next",))
        row = cur.fetchone()
        seq = int(row["value"]) if row else 2501
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            ("invoice_seq_next", str(seq + 1)),
        )
        now = datetime.now()
        invoice_date = f"{now.day}-{now.strftime('%b-%y')}"  # same as %-d-%b-%y, but works on Windows too
        invoice_number = f"{invoice_prefix}/{seq}/{now.year}"
        cur.execute(
            "UPDATE orders SET invoice_number = %s, invoice_date = %s, invoice_printed_by = %s "
            "WHERE shopify_order_id = %s",
            (invoice_number, invoice_date, session["name"], order_id),
        )
    else:
        invoice_number = invoice_number_for_payment(order["invoice_number"], payment_type)
        invoice_date = order["invoice_date"]
        if invoice_number != order["invoice_number"]:
            # Payment type changed after first print (or before this fix) —
            # bring the stored number in line with it.
            cur.execute(
                "UPDATE orders SET invoice_number = %s WHERE shopify_order_id = %s",
                (invoice_number, order_id),
            )
        # Reprinting updates who last touched it — "who gave the print" is
        # answered by whoever most recently printed or reprinted it, not
        # necessarily whoever first issued the invoice number.
        cur.execute(
            "UPDATE orders SET invoice_printed_by = %s WHERE shopify_order_id = %s",
            (session["name"], order_id),
        )

    # Log who printed (or reprinted) this invoice — the Billing view only
    # shows a Printed/Not-Printed badge, this is what answers "who printed
    # it" when that's asked later.
    log_activity(
        cur, None, order["order_name"] or order_id,
        "invoice_print" if is_first_print else "invoice_reprint",
        f"{session['name']} {'printed' if is_first_print else 'reprinted'} invoice {invoice_number}",
        order_id=order_id,
    )
    db.commit()
    cur.close()

    order_dict = dict(order)
    order_dict["payment_type"] = payment_type

    # Shopify's balance_due reflects the order's original full total — it
    # has no idea an item was later marked N/A (couldn't be sourced, not
    # being delivered) here in the app. Subtract those items' value so
    # "Amount to be Received" matches what's actually being handed to the
    # customer, not what Shopify originally billed for.
    na_items = [i for i in all_items if i["status"] == "na"]
    if na_items and order_dict.get("balance_due") is not None:
        na_value = 0.0
        for i in na_items:
            try:
                na_value += float(i["price"] or 0) * (i["quantity"] or 0)
            except (TypeError, ValueError):
                pass
        order_dict["balance_due"] = max(0.0, float(order_dict["balance_due"]) - na_value)
    if order_dict.get("balance_due") is not None:
        order_dict["balance_due"] = max(
            0.0, float(order_dict["balance_due"]) + price_edit_delta([dict(i) for i in billing_items])
        )

    # Persist the final calculated Amount to be Received into Supabase at
    # the moment it's locked in for the invoice — same reasoning as the
    # invoice number itself being locked in on first print. COD orders get
    # the real figure; everything else (already paid) stores nothing.
    # Invoice total (purchased/in-stock items + shipping) — the same grand
    # total printed on the PDF. Saved for EVERY order, COD and prepaid, on
    # every print/reprint, so it always matches the latest printed invoice.
    total_incl_all = sum(float(i["price"] or 0) * (i["quantity"] or 0) for i in billing_items)
    final_invoice_total = round(total_incl_all + float(order_dict.get("shipping_amount") or 0), 2)

    final_amount_to_receive = None
    if order_dict.get("payment_type") == "cod":
        if order_dict.get("balance_due") is not None:
            final_amount_to_receive = float(order_dict["balance_due"])
        else:
            final_amount_to_receive = final_invoice_total
    save_cur = db.cursor()
    save_cur.execute(
        "UPDATE orders SET invoice_total = %s, amount_to_receive = %s WHERE shopify_order_id = %s",
        (final_invoice_total, final_amount_to_receive, order_id),
    )
    db.commit()
    save_cur.close()

    return order_dict, [dict(i) for i in billing_items], invoice_number, invoice_date, is_first_print


@app.route("/api/orders/<order_id>/invoice.pdf", methods=["GET"])
@can_print_invoice_required
def api_order_invoice(order_id):
    db = get_db()
    cod_threshold = float(get_setting("cod_shipping_threshold", "140") or 140)
    try:
        order_dict, billing_items, invoice_number, invoice_date, _ = _issue_invoice(db, order_id, cod_threshold)
    except InvoiceError as e:
        return jsonify({"error": e.message}), e.status

    pdf_buf = build_invoice_pdf(order_dict, billing_items, invoice_number, invoice_date)
    filename = invoice_number.replace("/", "-") + ".pdf"
    response = send_file(pdf_buf, mimetype="application/pdf", as_attachment=False, download_name=filename)
    # So the frontend can flip the Printed/Not-Printed badge right away,
    # from this same request/response, instead of needing a page refresh
    # or a second round-trip to find out what number got assigned.
    response.headers["X-Invoice-Number"] = invoice_number
    response.headers["X-Invoice-Date"] = invoice_date
    response.headers["X-Invoice-Printed-By"] = session["name"]
    response.headers["Access-Control-Expose-Headers"] = "X-Invoice-Number, X-Invoice-Date, X-Invoice-Printed-By"
    return response


BULK_INVOICE_LIMIT = 200


@app.route("/api/invoices/bulk.pdf", methods=["POST"])
@accounts_or_owner_required
def api_bulk_invoices():
    """Many invoices in one PDF (Billing -> select orders -> PDF). Each order
    goes through exactly the same steps as Print Invoice (_issue_invoice):
    never-printed orders get their invoice number now, already-printed ones
    are reprinted with their existing number. Orders that can't be invoiced
    (bill cancelled, not in Billing...) are skipped and reported back."""
    body = request.get_json(silent=True) or {}
    raw_ids = body.get("order_ids") or []
    if not isinstance(raw_ids, list):
        return jsonify({"error": "order_ids must be a list"}), 400
    order_ids = []
    for oid in raw_ids:
        oid = str(oid).strip()
        if oid and oid not in order_ids:
            order_ids.append(oid)
    if not order_ids:
        return jsonify({"error": "Select at least one order."}), 400
    if len(order_ids) > BULK_INVOICE_LIMIT:
        return jsonify({"error": f"Max {BULK_INVOICE_LIMIT} invoices per PDF — you selected {len(order_ids)}. "
                                 f"Narrow it with the date or Invoice filter."}), 400

    db = get_db()
    # Oldest order first, so new invoice numbers follow the order sequence
    # (same as printing them one by one from the oldest).
    cur = db.cursor()
    cur.execute(
        "SELECT shopify_order_id FROM orders WHERE shopify_order_id = ANY(%s) "
        "ORDER BY created_at ASC NULLS LAST, order_name ASC",
        (order_ids,),
    )
    ordered = [r["shopify_order_id"] for r in cur.fetchall()]
    cur.close()
    ordered += [oid for oid in order_ids if oid not in ordered]  # unknown ids -> reported as skipped

    cod_threshold = float(get_setting("cod_shipping_threshold", "140") or 140)
    buf = io.BytesIO()
    pdf = pdf_canvas.Canvas(buf, pagesize=A4)
    printed, new_count, skipped = 0, 0, []
    results = []  # [order_id, invoice_number] for each printed order -> screen updates instantly
    for oid in ordered:
        try:
            order_dict, billing_items, invoice_number, invoice_date, is_first = _issue_invoice(db, oid, cod_threshold)
        except InvoiceError as e:
            skipped.append({"order_id": oid, "reason": e.message})
            continue
        build_invoice_pdf(order_dict, billing_items, invoice_number, invoice_date, c=pdf)
        results.append([oid, invoice_number])
        printed += 1
        new_count += 1 if is_first else 0

    if not printed:
        reason = skipped[0]["reason"] if skipped else "Nothing to print."
        return jsonify({"error": f"No invoice could be printed. {reason}", "skipped": skipped}), 400

    pdf.save()
    buf.seek(0)
    filename = f"invoices-{datetime.now().strftime('%d-%b-%Y-%H%M')}-{printed}.pdf"
    response = send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=filename)
    response.headers["X-Invoice-Count"] = str(printed)
    response.headers["X-Invoice-New"] = str(new_count)
    response.headers["X-Invoice-Skipped"] = str(len(skipped))
    response.headers["X-Invoice-Results"] = quote(json.dumps(results, separators=(",", ":")))
    response.headers["X-Invoice-Printed-By"] = quote(session["name"])
    if skipped:
        # Short readable summary for the toast (order names, first few only)
        names = []
        cur = db.cursor()
        cur.execute("SELECT shopify_order_id, order_name FROM orders WHERE shopify_order_id = ANY(%s)",
                    ([s["order_id"] for s in skipped],))
        by_id = {r["shopify_order_id"]: r["order_name"] for r in cur.fetchall()}
        cur.close()
        for s in skipped[:5]:
            names.append(f"{by_id.get(s['order_id']) or s['order_id']}: {s['reason']}")
        response.headers["X-Invoice-Skipped-Info"] = quote(" | ".join(names))
    response.headers["Access-Control-Expose-Headers"] = "X-Invoice-Count, X-Invoice-New, X-Invoice-Skipped, X-Invoice-Skipped-Info, X-Invoice-Results, X-Invoice-Printed-By"
    return response


# ---------------------------------------------------------------------------
# Tally Prime export — invoices in a date range, as Tally XML or Excel
# ---------------------------------------------------------------------------

def _collect_invoices_for_tally(date_from, date_to, only_new):
    """Orders that already have an invoice number, filtered by INVOICE date
    (stored as text like '14-Aug-26', so parsed here), returned as
    [(order_dict, billing_items, payment_type)] sorted by invoice date/number."""
    db = get_db()
    cur = db.cursor()
    query = "SELECT * FROM orders WHERE deleted_at IS NULL AND invoice_number IS NOT NULL AND invoice_number <> ''"
    # Cancelled bills were re-created directly in Tally, so exporting them
    # would duplicate the voucher there.
    query += " AND bill_cancelled = false"
    if only_new:
        query += " AND (tally_exported_at IS NULL OR tally_exported_at = '')"
    cur.execute(query)
    orders = cur.fetchall()

    d_from = datetime.strptime(date_from, "%Y-%m-%d") if date_from else None
    d_to = datetime.strptime(date_to, "%Y-%m-%d") if date_to else None
    picked = []
    for o in orders:
        dt = tally_export.parse_invoice_date(o["invoice_date"])
        if dt is None:
            continue
        if d_from and dt < d_from:
            continue
        if d_to and dt > d_to:
            continue
        picked.append((dt, o))

    ids = [o["shopify_order_id"] for _, o in picked]
    items_by_order = {oid: [] for oid in ids}
    if ids:
        cur.execute(
            "SELECT * FROM items WHERE shopify_order_id = ANY(%s) ORDER BY sort_order", (ids,)
        )
        for it in cur.fetchall():
            items_by_order[it["shopify_order_id"]].append(it)
    cur.close()

    cod_threshold = float(get_setting("cod_shipping_threshold", "140") or 140)
    picked.sort(key=lambda p: (p[0], p[1]["invoice_number"]))
    result = []
    for dt, o in picked:
        billing_items = [dict(i) for i in items_by_order.get(o["shopify_order_id"], [])
                         if i["status"] in ("purchased", "stock")]
        if not billing_items:
            continue
        od = dict(o)
        ptype = resolve_payment_type(o, cod_threshold) or "prepaid"
        # An order printed before the SHP/COD fix (or edited since) may carry the
        # wrong prefix; export the number that matches its payment type.
        fixed_no = invoice_number_for_payment(od["invoice_number"], ptype)
        if fixed_no != od["invoice_number"]:
            fix_cur = db.cursor()
            fix_cur.execute(
                "UPDATE orders SET invoice_number = %s WHERE shopify_order_id = %s",
                (fixed_no, od["shopify_order_id"]),
            )
            db.commit()
            fix_cur.close()
            od["invoice_number"] = fixed_no
        result.append((od, billing_items, ptype))
    return result


def _tally_export_args():
    date_from = request.args.get("date_from") or ""
    date_to = request.args.get("date_to") or ""
    only_new = request.args.get("only_new") == "1"
    company = (request.args.get("company") or "").strip()
    try:
        invoices = _collect_invoices_for_tally(date_from, date_to, only_new)
    except ValueError:
        return None, None, None, (jsonify({"error": "Dates must be YYYY-MM-DD"}), 400)
    if not invoices:
        msg = "No invoices found in that date range."
        if only_new:
            msg = "No new invoices in that range (already exported ones are skipped)."
        return None, None, None, (jsonify({"error": msg}), 404)
    return tally_export.build_records(invoices), only_new, company, None


def _mark_tally_exported(records):
    ids = [r["order"]["shopify_order_id"] for r in records]
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE orders SET tally_exported_at = %s WHERE shopify_order_id = ANY(%s)",
        (datetime.now(timezone.utc).isoformat(), ids),
    )
    db.commit()
    cur.close()


def _tally_filename(ext):
    date_from = request.args.get("date_from") or "start"
    date_to = request.args.get("date_to") or "now"
    return f"tally_invoices_{date_from}_to_{date_to}.{ext}"


@app.route("/api/invoices/export/tally.xml", methods=["GET"])
@accounts_or_owner_required
def api_export_tally_xml():
    records, only_new, company, err = _tally_export_args()
    if err:
        return err
    xml = tally_export.build_tally_xml(records, company)
    if only_new:
        _mark_tally_exported(records)
    resp = app.response_class(xml.encode("utf-8"), mimetype="application/xml")
    resp.headers["Content-Disposition"] = f'attachment; filename="{_tally_filename("xml")}"'
    resp.headers["X-Invoice-Count"] = str(len(records))
    resp.headers["Access-Control-Expose-Headers"] = "X-Invoice-Count"
    return resp


@app.route("/api/invoices/export/tally.xlsx", methods=["GET"])
@accounts_or_owner_required
def api_export_tally_xlsx():
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font

    records, only_new, _company, err = _tally_export_args()
    if err:
        return err
    wb = Workbook()
    ws = wb.active
    ws.title = "Invoices"
    ws.append(tally_export.EXCEL_HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in tally_export.excel_rows(records):
        ws.append(row)
    # Mobile numbers must stay text, or Excel turns them into 9.8E+09.
    for row in ws.iter_rows(min_row=2, min_col=5, max_col=5):
        for cell in row:
            cell.number_format = "@"
    for col, w in zip("ABCDEFGHIJKLMNOPQ", (12, 18, 12, 12, 16, 22, 40, 16, 16, 10, 12, 50, 14, 12, 14, 10, 14)):
        ws.column_dimensions[col].width = w
    if only_new:
        _mark_tally_exported(records)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=_tally_filename("xlsx"),
    )
    resp.headers["X-Invoice-Count"] = str(len(records))
    resp.headers["Access-Control-Expose-Headers"] = "X-Invoice-Count"
    return resp


@app.route("/api/orders/<order_id>/bill-cancelled", methods=["POST"])
@accounts_or_owner_required
def api_update_bill_cancelled(order_id):
    """Accounts/owner. Marks an order's BILL (printed or not yet printed) as
    cancelled — e.g. item rates went up and the bill is re-created directly
    in Tally. The order itself stays active. Cancelled bills are skipped by
    the Tally XML/Excel export so they don't get duplicated in Tally."""
    data = request.get_json(force=True) or {}
    if "cancelled" not in data:
        return jsonify({"error": "cancelled is required"}), 400
    cancelled = bool(data.get("cancelled"))
    reason = (data.get("reason") or "").strip()
    if cancelled and not reason:
        return jsonify({"error": "A reason is required to cancel a bill"}), 400

    who = session.get("name", "")
    now = datetime.now(timezone.utc).isoformat()
    vals = (cancelled, reason if cancelled else None, who if cancelled else None, now if cancelled else None)

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE orders SET bill_cancelled = %s, bill_cancelled_reason = %s, "
        "bill_cancelled_by = %s, bill_cancelled_at = %s "
        "WHERE shopify_order_id = %s RETURNING order_name, invoice_number",
        vals + (order_id,),
    )
    row = cur.fetchone()
    if row is None:
        cur.close()
        return jsonify({"error": "Order not found"}), 404
    label = row["invoice_number"] or "(not printed)"
    log_activity(
        cur, None, row["order_name"] or order_id, "bill_cancelled_update",
        f"{who} marked bill {label} of order {row['order_name'] or order_id} as "
        f"{'cancelled (' + reason + ')' if cancelled else 'active again'}",
        order_id=order_id,
    )
    db.commit()
    cur.close()
    return jsonify({
        "ok": True, "order_id": order_id, "bill_cancelled": cancelled,
        "bill_cancelled_reason": vals[1], "bill_cancelled_by": vals[2], "bill_cancelled_at": vals[3],
    })


@app.route("/api/orders/<order_id>", methods=["DELETE"])
@owner_required
def api_delete_order(order_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT order_name, deleted_at FROM orders WHERE shopify_order_id = %s", (order_id,))
    order = cur.fetchone()
    if not order:
        cur.close()
        return jsonify({"error": "Order not found"}), 404
    if order["deleted_at"]:
        cur.close()
        return jsonify({"error": "Order is already in Trash"}), 400

    # Soft delete: mark it deleted rather than removing the row, so it can
    # be restored from Trash — items and history are left untouched.
    now = datetime.now(timezone.utc).isoformat()
    cur.execute("UPDATE orders SET deleted_at = %s WHERE shopify_order_id = %s", (now, order_id))
    log_activity(cur, None, order["order_name"] or order_id, "order_delete",
                 f"{session['name']} moved order {order['order_name'] or order_id} to Trash",
                 order_id=order_id)

    db.commit()
    cur.close()
    return jsonify({"ok": True})


@app.route("/api/orders/<order_id>/restore", methods=["POST"])
@owner_required
def api_restore_order(order_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT order_name, deleted_at FROM orders WHERE shopify_order_id = %s", (order_id,))
    order = cur.fetchone()
    if not order:
        cur.close()
        return jsonify({"error": "Order not found"}), 404
    if not order["deleted_at"]:
        cur.close()
        return jsonify({"error": "Order isn't in Trash"}), 400

    cur.execute("UPDATE orders SET deleted_at = NULL WHERE shopify_order_id = %s", (order_id,))
    log_activity(cur, None, order["order_name"] or order_id, "order_restore",
                 f"{session['name']} restored order {order['order_name'] or order_id} from Trash",
                 order_id=order_id)

    db.commit()
    cur.close()
    return jsonify({"ok": True})


def order_assigned_to_user(cur, order_id, user_id):
    """True if `order_id`'s current COD/Prepaid duty staff includes `user_id`.

    Mirrors the assignment logic in /api/orders — used to stop a staff
    account from editing items on an order that isn't (or is no longer)
    assigned to them, even via a direct API call.
    """
    cur.execute("SELECT shipping_amount, payment_type FROM orders WHERE shopify_order_id = %s", (order_id,))
    order = cur.fetchone()
    if not order:
        return False
    cod_threshold = float(get_setting("cod_shipping_threshold", "140") or 140)
    payment_type = resolve_payment_type(order, cod_threshold)
    if payment_type is None:
        return False
    assigned_staff_ids = _parse_staff_ids(
        get_setting("cod_staff_id" if payment_type == "cod" else "prepaid_staff_id", "")
    )
    return str(user_id) in assigned_staff_ids


@app.route("/api/items/<item_id>/status", methods=["POST"])
@staff_or_owner_required
def api_update_item_status(item_id):
    data = request.get_json(force=True) or {}
    status = data.get("status")
    if status not in VALID_STATUSES:
        return jsonify({"error": f"status must be one of {sorted(VALID_STATUSES)}"}), 400

    if status == "refunded" and session.get("role") != "owner":
        return jsonify({"error": "Only the owner can mark an item as Refunded"}), 403

    db = get_db()
    cur = db.cursor()

    if session.get("role") == "staff":
        cur.execute("SELECT shopify_order_id FROM items WHERE id = %s", (item_id,))
        item_row = cur.fetchone()
        if not item_row or not order_assigned_to_user(cur, item_row["shopify_order_id"], session["user_id"]):
            cur.close()
            return jsonify({"error": "This order isn't assigned to you"}), 403

    now = datetime.now(timezone.utc).isoformat()
    # Read the previous value and write the new one in a single round-trip
    # (self-join against the pre-update row) instead of a separate SELECT
    # then UPDATE — halves the database latency this endpoint pays on
    # every click, which matters most when the database is a remote
    # service like Supabase rather than a local file.
    cur.execute(
        "UPDATE items i SET status = %s, updated_at = %s "
        "FROM items old WHERE i.id = %s AND old.id = i.id "
        "RETURNING i.title, old.status AS old_status, i.shopify_order_id",
        (status, now, item_id),
    )
    row = cur.fetchone()
    if row is None:
        db.rollback()
        cur.close()
        return jsonify({"error": "item not found"}), 404

    if status != row["old_status"]:
        log_activity(cur, item_id, row["title"], "status_update",
                     f"{session['name']} changed status: {row['old_status']} -> {status}",
                     order_id=row["shopify_order_id"])
    db.commit()
    cur.close()
    return jsonify({"ok": True, "id": item_id, "status": status})


@app.route("/api/items/<item_id>/purchase-amount", methods=["POST"])
@staff_or_owner_required
def api_update_item_purchase_amount(item_id):
    data = request.get_json(force=True) or {}
    try:
        purchase_amount = float(data.get("purchase_amount") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "purchase_amount must be a number"}), 400
    if purchase_amount < 0:
        return jsonify({"error": "purchase_amount can't be negative"}), 400

    db = get_db()
    cur = db.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cur.execute(
        "UPDATE items i SET purchase_amount = %s, updated_at = %s "
        "FROM items old WHERE i.id = %s AND old.id = i.id "
        "RETURNING i.title, old.purchase_amount AS old_amount, i.shopify_order_id",
        (purchase_amount, now, item_id),
    )
    row = cur.fetchone()
    if row is None:
        db.rollback()
        cur.close()
        return jsonify({"error": "item not found"}), 404

    old_amount = float(row["old_amount"] or 0)
    if abs(purchase_amount - old_amount) > 0.001:
        log_activity(cur, item_id, row["title"], "purchase_amount_update",
                     f"{session['name']} changed purchase amount: {old_amount} -> {purchase_amount}",
                     order_id=row["shopify_order_id"])
    db.commit()
    cur.close()
    return jsonify({"ok": True, "id": item_id, "purchase_amount": purchase_amount})


@app.route("/api/items/<item_id>/price", methods=["POST"])
@accounts_or_owner_required
def api_update_item_price(item_id):
    """Accounts/owner. Edit an item's selling price (per unit, GST-inclusive)
    when the rate goes up after the order. The edited price is what the
    invoice PDF, the COD amount and the Tally export use — not Shopify's.
    Entering the original Shopify price again clears the edit."""
    data = request.get_json(force=True) or {}
    try:
        new_price = round(float(data.get("price")), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "price must be a number"}), 400
    if new_price < 0:
        return jsonify({"error": "price can't be negative"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM items WHERE id = %s", (item_id,))
    it = cur.fetchone()
    if it is None:
        cur.close()
        return jsonify({"error": "item not found"}), 404

    original = it["original_price"] if it["original_price"] not in (None, "") else it["price"]
    try:
        is_edited = abs(new_price - float(original or 0)) > 0.001
    except (TypeError, ValueError):
        is_edited = True
    now = datetime.now(timezone.utc).isoformat()
    cur.execute(
        "UPDATE items SET price = %s, original_price = %s, price_edited = %s, "
        "price_edited_by = %s, updated_at = %s WHERE id = %s",
        (f"{new_price:.2f}", original, is_edited, session["name"] if is_edited else None, now, item_id),
    )
    cur.execute("SELECT order_name, invoice_number, tally_exported_at FROM orders WHERE shopify_order_id = %s",
                (it["shopify_order_id"],))
    o = cur.fetchone() or {}
    log_activity(cur, item_id, it["title"], "price_update",
                 f"{session['name']} changed price: {it['price']} -> {new_price:.2f} (Shopify price {original})",
                 order_id=it["shopify_order_id"])
    db.commit()
    cur.close()
    return jsonify({
        "ok": True, "id": item_id, "price": f"{new_price:.2f}", "original_price": original,
        "price_edited": is_edited,
        "already_exported": bool(o.get("tally_exported_at")) if o else False,
    })


# ---------------------------------------------------------------------------
# Edit order (accounts/owner) — change customer/address, delivery charge, and
# every item (name, qty, price), or add more items, in one go.
# ---------------------------------------------------------------------------

def _item_is_removable(order_id, item_id):
    """Items added by hand (manual orders, or items added via Edit Order) can
    be deleted. Shopify-synced items can't — a sync would just bring them
    back — so those are dropped by marking them N/A instead."""
    return str(item_id).startswith(f"{order_id}-item-")


def _money(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _effective_balance(stored_balance, items, shipping_amount=None):
    """Returns (stored - N/A value + price-edit delta), i.e. the COD amount
    the invoice would show (before the max(0) clamp), plus the pieces the
    edit needs to keep it consistent."""
    na_value = sum(_money(i["price"]) * (i["quantity"] or 0) for i in items if i["status"] == "na")
    billing = [dict(i) for i in items if i["status"] in ("purchased", "stock")]
    billing_total = sum(_money(i["price"]) * (i["quantity"] or 0) for i in billing)
    pd = price_edit_delta(billing)
    eff = None if stored_balance is None else float(stored_balance) - na_value + pd
    return eff, na_value, pd, billing_total


@app.route("/api/orders/<order_id>/edit-data", methods=["GET"])
@accounts_or_owner_required
def api_order_edit_data(order_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM orders WHERE shopify_order_id = %s", (order_id,))
    o = cur.fetchone()
    if o is None:
        cur.close()
        return jsonify({"error": "order not found"}), 404
    cur.execute("SELECT * FROM items WHERE shopify_order_id = %s ORDER BY sort_order", (order_id,))
    items = cur.fetchall()
    cur.close()
    cod_threshold = float(get_setting("cod_shipping_threshold", "140") or 140)
    pay_now = resolve_payment_type(o, cod_threshold)
    cash_now = None
    if pay_now == "cod" and o["balance_due"] is not None:
        _e, _n, _p, _b = _effective_balance(o["balance_due"], [dict(i) for i in items])
        cash_now = round(max(0.0, _e), 2)
    return jsonify({
        "cash_to_collect": cash_now,
        "payment_type": pay_now,
        "order_id": o["shopify_order_id"],
        "order_name": o["order_name"],
        "customer_name": o["customer_name"] or "",
        "phone": o["customer_phone"] or "",
        "address1": o["shipping_address1"] or "",
        "address2": o["shipping_address2"] or "",
        "city": o["shipping_city"] or "",
        "state": o["shipping_state"] or "",
        "pincode": o["shipping_pincode"] or "",
        "shipping_amount": float(o["shipping_amount"]) if o["shipping_amount"] is not None else 0,
        "invoice_number": o["invoice_number"],
        "tally_exported_at": o["tally_exported_at"],
        "items": [
            {
                "id": i["id"], "title": i["title"] or "", "variant_title": i["variant_title"] or "",
                "vendor": i["vendor"] or "", "quantity": i["quantity"] or 1,
                "price": i["price"] or "0", "status": i["status"],
                "removable": _item_is_removable(order_id, i["id"]),
            }
            for i in items
        ],
    })


@app.route("/api/orders/<order_id>/edit", methods=["POST"])
@accounts_or_owner_required
def api_edit_order(order_id):
    data = request.get_json(force=True) or {}
    errors = []

    customer_name = (data.get("customer_name") or "").strip()
    phone = re.sub(r"\D", "", data.get("phone") or "")
    address1 = (data.get("address1") or "").strip()
    address2 = (data.get("address2") or "").strip()
    city = (data.get("city") or "").strip()
    state = (data.get("state") or "").strip()
    pincode = (data.get("pincode") or "").strip()
    if not customer_name:
        errors.append("Customer name is required")

    try:
        new_shipping = round(float(data.get("shipping_amount") or 0), 2)
    except (TypeError, ValueError):
        errors.append("Delivery charge must be a number")
        new_shipping = 0.0
    if new_shipping < 0:
        errors.append("Delivery charge can't be negative")

    # Optional: switch the order between Prepaid and COD (postpaid).
    requested_payment = data.get("payment_type") or None
    if requested_payment not in (None, "cod", "prepaid"):
        errors.append("Payment type must be COD or Prepaid")

    cash_raw = data.get("cash_to_collect")
    cash_to_collect = None
    if cash_raw not in (None, ""):
        try:
            cash_to_collect = round(float(cash_raw), 2)
        except (TypeError, ValueError):
            errors.append("Cash to be collected must be a number")
        if cash_to_collect is not None and cash_to_collect < 0:
            errors.append("Cash to be collected can't be negative")

    items_in = data.get("items") or []
    remove_ids = set(data.get("remove_ids") or [])
    parsed = []
    for idx, raw in enumerate(items_in, start=1):
        title = (raw.get("title") or "").strip()
        if not title:
            if raw.get("id"):
                errors.append(f"Item {idx}: name can't be empty")
            continue  # blank new row — skip
        try:
            quantity = int(raw.get("quantity") or 1)
        except (TypeError, ValueError):
            errors.append(f"Item {idx}: quantity must be a whole number")
            quantity = 1
        if quantity < 1:
            errors.append(f"Item {idx}: quantity must be at least 1")
            quantity = 1
        try:
            price = round(float(raw.get("price") or 0), 2)
        except (TypeError, ValueError):
            errors.append(f"Item {idx}: price must be a number")
            price = 0.0
        if price < 0:
            errors.append(f"Item {idx}: price can't be negative")
        status = raw.get("status") or "stock"
        if status not in VALID_STATUSES or status == "refunded":
            status = "stock"
        parsed.append({
            "id": raw.get("id") or None, "title": title,
            "vendor": (raw.get("vendor") or "").strip(),
            "quantity": quantity, "price": price, "status": status,
        })
    if errors:
        return jsonify({"error": "; ".join(errors)}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM orders WHERE shopify_order_id = %s", (order_id,))
    order = cur.fetchone()
    if order is None:
        cur.close()
        return jsonify({"error": "order not found"}), 404
    cur.execute("SELECT * FROM items WHERE shopify_order_id = %s", (order_id,))
    old_items = {i["id"]: dict(i) for i in cur.fetchall()}

    for it in parsed:
        if it["id"] and it["id"] not in old_items:
            cur.close()
            return jsonify({"error": "An item in this edit doesn't belong to this order — reload and try again"}), 400
    for rid in remove_ids:
        if rid not in old_items or not _item_is_removable(order_id, rid):
            cur.close()
            return jsonify({"error": "Only items added by hand can be removed — mark Shopify items as N/A instead"}), 400
    remaining = [i for i in old_items if i not in remove_ids] + [p for p in parsed if not p["id"]]
    if not remaining:
        cur.close()
        return jsonify({"error": "An order needs at least one item"}), 400

    # ---- "before" figures, so the COD amount can be kept consistent ----
    old_shipping = _money(order["shipping_amount"])
    eff_old, _na_old, _pd_old, billing_old = _effective_balance(order["balance_due"], list(old_items.values()))

    now = datetime.now(timezone.utc).isoformat()
    who = session["name"]
    changes = []

    # ---- apply item changes ----
    new_state = {k: dict(v) for k, v in old_items.items()}
    for rid in remove_ids:
        it = old_items[rid]
        cur.execute("DELETE FROM items WHERE id = %s", (rid,))
        new_state.pop(rid, None)
        changes.append(f"removed '{it['title']}'")
        log_activity(cur, rid, it["title"], "order_edit", f"{who} removed this item", order_id=order_id)

    for p in parsed:
        if p["id"]:
            old = old_items[p["id"]]
            if p["id"] in remove_ids:
                continue
            diffs = []
            if (old["title"] or "") != p["title"]:
                diffs.append(f"name '{old['title']}' -> '{p['title']}'")
            if (old["vendor"] or "") != p["vendor"]:
                diffs.append(f"vendor '{old['vendor'] or ''}' -> '{p['vendor']}'")
            if (old["quantity"] or 0) != p["quantity"]:
                diffs.append(f"qty {old['quantity']} -> {p['quantity']}")
            price_changed = abs(_money(old["price"]) - p["price"]) > 0.001
            if price_changed:
                diffs.append(f"price {old['price']} -> {p['price']:.2f}")
            if not diffs:
                continue
            original = old["original_price"] if old["original_price"] not in (None, "") else old["price"]
            is_price_edited = abs(p["price"] - _money(original)) > 0.001
            cur.execute(
                "UPDATE items SET title=%s, vendor=%s, quantity=%s, price=%s, original_price=%s, "
                "price_edited=%s, price_edited_by=%s, item_edited=true, updated_at=%s WHERE id=%s",
                (p["title"], p["vendor"], p["quantity"], f"{p['price']:.2f}", original,
                 is_price_edited, who if is_price_edited else None, now, p["id"]),
            )
            new_state[p["id"]].update(
                title=p["title"], vendor=p["vendor"], quantity=p["quantity"],
                price=f"{p['price']:.2f}", original_price=original, price_edited=is_price_edited,
            )
            changes.append(f"'{p['title']}': " + ", ".join(diffs))
            log_activity(cur, p["id"], p["title"], "order_edit", f"{who} edited: " + ", ".join(diffs), order_id=order_id)
        else:
            new_id = f"{order_id}-item-{uuid.uuid4().hex[:8]}"
            cur.execute(
                "INSERT INTO items (id, shopify_order_id, title, variant_title, quantity, price, vendor, "
                "status, item_edited, updated_at) VALUES (%s, %s, %s, '', %s, %s, %s, %s, true, %s)",
                (new_id, order_id, p["title"], p["quantity"], f"{p['price']:.2f}", p["vendor"], p["status"], now),
            )
            new_state[new_id] = {
                "id": new_id, "title": p["title"], "quantity": p["quantity"], "price": f"{p['price']:.2f}",
                "status": p["status"], "price_edited": False, "original_price": None,
            }
            changes.append(f"added '{p['title']}' x{p['quantity']} @ {p['price']:.2f}")
            log_activity(cur, new_id, p["title"], "order_edit",
                         f"{who} added this item: qty {p['quantity']} @ {p['price']:.2f} ({p['status']})",
                         order_id=order_id)

    # ---- order-level fields ----
    full_address = ", ".join(x for x in [address1, address2, city, state, pincode] if x)
    old_full = order["shipping_address"] or ""
    if (order["customer_name"] or "") != customer_name:
        changes.append(f"customer '{order['customer_name'] or ''}' -> '{customer_name}'")
    if (order["customer_phone"] or "") != phone:
        changes.append("phone changed")
    if old_full != full_address:
        changes.append("address changed")

    shipping_changed = abs(new_shipping - old_shipping) > 0.001 or order["shipping_amount"] is None
    set_payment_type = order["payment_type"]
    if shipping_changed and set_payment_type not in ("cod", "prepaid"):
        # COD vs Prepaid is derived from the delivery charge for Shopify orders;
        # lock in what it is today so editing the charge can't flip it.
        cod_threshold = float(get_setting("cod_shipping_threshold", "140") or 140)
        set_payment_type = resolve_payment_type(order, cod_threshold)
    if abs(new_shipping - old_shipping) > 0.001:
        changes.append(f"delivery charge {old_shipping:.2f} -> {new_shipping:.2f}")

    # ---- explicit Prepaid <-> COD switch ----
    cod_threshold_now = float(get_setting("cod_shipping_threshold", "140") or 140)
    current_payment = resolve_payment_type(order, cod_threshold_now)
    payment_switched = bool(requested_payment) and requested_payment != current_payment
    if payment_switched:
        set_payment_type = requested_payment
        changes.append(
            f"payment type {(current_payment or 'unknown').upper()} -> {requested_payment.upper()}"
        )

    # ---- keep COD "balance_due" consistent with the new item list ----
    new_balance = order["balance_due"]
    final_payment = set_payment_type if set_payment_type in ("cod", "prepaid") else current_payment
    _eff_cur = None if eff_old is None else max(0.0, eff_old)
    cash_changed = (
        final_payment == "cod" and cash_to_collect is not None
        and (payment_switched or _eff_cur is None or abs(cash_to_collect - _eff_cur) > 0.001)
    )
    if cash_changed:
        _eff, na_new, pd_new, _bn = _effective_balance(None, list(new_state.values()))
        new_balance = round(cash_to_collect + na_new - pd_new, 2)
        changes.append(f"cash to be collected set to {cash_to_collect:.2f}")
    elif payment_switched and requested_payment == "cod":
        # Shopify's outstanding balance on a prepaid order is 0 (or stale), which
        # would make "Amount to be Received" show 0. Clear it so the COD amount
        # is the full total: billed items + delivery charge.
        new_balance = None
    elif order["balance_due"] is not None:
        _eff, na_new, pd_new, billing_new = _effective_balance(None, list(new_state.values()))
        target_eff = eff_old + (billing_new - billing_old) + (new_shipping - old_shipping)
        new_balance = round(target_eff + na_new - pd_new, 2)

    cur.execute(
        "UPDATE orders SET customer_name=%s, customer_phone=%s, shipping_address=%s, shipping_address1=%s, "
        "shipping_address2=%s, shipping_city=%s, shipping_state=%s, shipping_pincode=%s, "
        "shipping_amount=%s, balance_due=%s, payment_type=%s, order_edited=true WHERE shopify_order_id=%s",
        (customer_name, phone, full_address, address1, address2, city, state, pincode,
         new_shipping, new_balance, set_payment_type, order_id),
    )
    if payment_switched or cash_changed:
        # amount_to_receive is locked in at invoice print time; it's recomputed on the
        # next (re)print, so drop the stale figure now.
        cur.execute("UPDATE orders SET amount_to_receive = NULL WHERE shopify_order_id = %s", (order_id,))
    # An already-printed invoice keeps its number, but the SHP/ vs COD/ prefix
    # must follow the payment type — otherwise a Prepaid -> COD edit leaves a
    # COD order carrying an SHP/... number (and the reverse).
    if payment_switched and order["invoice_number"]:
        new_inv = invoice_number_for_payment(order["invoice_number"], set_payment_type)
        if new_inv != order["invoice_number"]:
            cur.execute("UPDATE orders SET invoice_number = %s WHERE shopify_order_id = %s", (new_inv, order_id))
            changes.append(f"invoice no. {order['invoice_number']} -> {new_inv}")
    # The Tally XML is built live from the order/items, so it always carries the
    # edited figures — no invoice reprint needed. If this order was already
    # exported, clear that mark so it shows up again in the next "Only new" XML.
    was_exported = bool(order["tally_exported_at"])
    if changes and was_exported:
        cur.execute("UPDATE orders SET tally_exported_at = NULL WHERE shopify_order_id = %s", (order_id,))
        changes.append("marked for Tally re-export")
    if changes:
        log_activity(cur, None, order["order_name"] or order_id, "order_edit",
                     f"{who} edited order: " + "; ".join(changes), order_id=order_id)
    db.commit()
    cur.close()
    return jsonify({
        "ok": True, "changes": len(changes),
        "has_invoice": bool(order["invoice_number"]),
        "re_export": bool(changes and was_exported),
    })


@app.route("/api/items/<item_id>/packed", methods=["POST"])
@packer_or_owner_required
def api_update_item_packed(item_id):
    data = request.get_json(force=True) or {}
    if "packed" not in data:
        return jsonify({"error": "packed is required"}), 400
    packed = bool(data.get("packed"))

    who = session.get("name", "")
    now = datetime.now(timezone.utc).isoformat()
    # Track who packed it (and when) separately from updated_at, so a later
    # unrelated edit by someone else doesn't erase which packer actually
    # packed this item.
    packed_by = who if packed else None
    packed_at = now if packed else None

    db = get_db()
    cur = db.cursor()
    # Packing only makes sense once an item is actually in hand — bought in
    # (purchased) or already had it (stock). Owners can override; packer
    # accounts are held to this the same way the old app enforced it. The
    # status check is enforced right in the WHERE clause so the common
    # (allowed) case is a single round-trip; only the blocked/not-found
    # cases pay for a follow-up SELECT to explain what happened.
    packer_restricted = session.get("role") == "packer"
    cur.execute(
        "UPDATE items SET packed = %s, packed_by = %s, packed_at = %s, updated_at = %s "
        "WHERE id = %s AND (%s = false OR status IN ('purchased', 'stock')) "
        "RETURNING title, shopify_order_id",
        (packed, packed_by, packed_at, now, item_id, packer_restricted),
    )
    row = cur.fetchone()
    if row is None:
        cur.execute("SELECT status FROM items WHERE id = %s", (item_id,))
        existing = cur.fetchone()
        db.rollback()
        cur.close()
        if existing is None:
            return jsonify({"error": "item not found"}), 404
        return jsonify({"error": "Only purchased or in-stock items can be marked as packed"}), 400

    log_activity(cur, item_id, row["title"], "packed_update",
                 f"{who} marked as {'packed' if packed else 'not packed'}",
                 order_id=row["shopify_order_id"])
    db.commit()
    cur.close()
    return jsonify({"ok": True, "id": item_id, "packed": packed, "packed_by": packed_by, "packed_at": packed_at})


@app.route("/api/orders/<order_id>/packed", methods=["POST"])
@packer_or_owner_required
def api_update_order_packed(order_id):
    data = request.get_json(force=True) or {}
    if "packed" not in data:
        return jsonify({"error": "packed is required"}), 400
    packed = bool(data.get("packed"))

    who = session.get("name", "")
    now = datetime.now(timezone.utc).isoformat()
    packed_by = who if packed else None
    packed_at = now if packed else None

    db = get_db()
    cur = db.cursor()
    # Packing only makes sense once the order actually has something in
    # hand to pack — at least one purchased/in-stock item (i.e. it's
    # reached Billing). Checked here rather than trusting the frontend,
    # since packer accounts can call this directly.
    cur.execute(
        "SELECT 1 FROM items WHERE shopify_order_id = %s AND status IN ('purchased', 'stock') LIMIT 1",
        (order_id,),
    )
    if packed and cur.fetchone() is None:
        cur.close()
        return jsonify({"error": "Order has no purchased or in-stock items to pack"}), 400

    cur.execute(
        "UPDATE orders SET packed = %s, packed_by = %s, packed_at = %s WHERE shopify_order_id = %s "
        "RETURNING order_name",
        (packed, packed_by, packed_at, order_id),
    )
    row = cur.fetchone()
    if row is None:
        cur.close()
        return jsonify({"error": "Order not found"}), 404

    log_activity(cur, None, row["order_name"] or order_id, "packed_update",
                 f"{who} marked order {row['order_name'] or order_id} as {'packed' if packed else 'not packed'}",
                 order_id=order_id)
    db.commit()
    cur.close()
    return jsonify({"ok": True, "order_id": order_id, "packed": packed, "packed_by": packed_by, "packed_at": packed_at})


# ---------------------------------------------------------------------------
# Repurchase (owner + telecaller only). Completely separate from the order /
# invoice / Tally flow: nothing here touches orders, items or invoice numbers.
# ---------------------------------------------------------------------------
REPURCHASE_STATUSES = ("pending", "purchased", "resent")


@app.route("/api/orders/<order_id>/repurchase-items")
@repurchase_required
def api_repurchase_items(order_id):
    """All items of an order (for the 'which items need repurchase' popup),
    with how many of each are already in Repurchase."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT order_name, customer_name FROM orders WHERE shopify_order_id = %s", (order_id,))
    order = cur.fetchone()
    if not order:
        cur.close()
        return jsonify({"error": "Order not found"}), 404
    cur.execute(
        "SELECT id, title, variant_title, quantity FROM items WHERE shopify_order_id = %s ORDER BY sort_order",
        (order_id,),
    )
    items = cur.fetchall()
    cur.execute(
        "SELECT item_id, COALESCE(SUM(quantity), 0) AS qty FROM repurchases "
        "WHERE shopify_order_id = %s GROUP BY item_id",
        (order_id,),
    )
    already = {r["item_id"]: int(r["qty"]) for r in cur.fetchall()}
    cur.close()
    return jsonify({
        "order_id": order_id,
        "order_name": order["order_name"],
        "customer_name": order["customer_name"],
        "items": [
            {
                "id": i["id"],
                "title": i["title"],
                "variant_title": i["variant_title"],
                "quantity": i["quantity"] or 1,
                "already_repurchased": already.get(i["id"], 0),
            }
            for i in items
        ],
    })


@app.route("/api/orders/<order_id>/repurchase", methods=["POST"])
@repurchase_required
def api_add_repurchase(order_id):
    data = request.get_json(force=True) or {}
    picked = data.get("items") or []
    reason = (data.get("reason") or "").strip()
    if not picked:
        return jsonify({"error": "Select at least one item"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT order_name, customer_name, customer_phone FROM orders WHERE shopify_order_id = %s",
        (order_id,),
    )
    order = cur.fetchone()
    if not order:
        cur.close()
        return jsonify({"error": "Order not found"}), 404
    cur.execute(
        "SELECT id, title, variant_title, vendor, quantity, price FROM items WHERE shopify_order_id = %s",
        (order_id,),
    )
    order_items = {i["id"]: i for i in cur.fetchall()}
    batch = uuid.uuid4().hex  # one repurchase bill per "Add to Repurchase" action

    cur.execute(
        "SELECT item_id, COALESCE(SUM(quantity), 0) AS qty FROM repurchases "
        "WHERE shopify_order_id = %s GROUP BY item_id",
        (order_id,),
    )
    already = {r["item_id"]: int(r["qty"]) for r in cur.fetchall()}

    who = session.get("name", "")
    now = datetime.now(timezone.utc).isoformat()
    added = []
    for p in picked:
        if not isinstance(p, dict):
            cur.close()
            return jsonify({"error": "Invalid item"}), 400
        item = order_items.get(p.get("id"))
        if not item:
            cur.close()
            return jsonify({"error": "An item doesn't belong to this order"}), 400
        try:
            qty = int(p.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 0
        max_qty = item["quantity"] or 1
        if qty < 1 or qty > max_qty:
            cur.close()
            return jsonify({"error": f"Quantity for {item['title']} must be between 1 and {max_qty}"}), 400
        room = max_qty - already.get(item["id"], 0)
        if qty > room:
            cur.close()
            msg = (f"{item['title']} is already fully in Repurchase" if room <= 0
                   else f"Only {room} more of {item['title']} can be added to Repurchase")
            return jsonify({"error": msg}), 400
        already[item["id"]] = already.get(item["id"], 0) + qty
        cur.execute(
            "INSERT INTO repurchases (shopify_order_id, order_name, customer_name, customer_phone, item_id, "
            "title, variant_title, vendor, quantity, reason, status, created_by, created_at, batch, price) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s)",
            (order_id, order["order_name"], order["customer_name"], order["customer_phone"], item["id"],
             item["title"], item["variant_title"], item["vendor"], qty, reason or None, who, now,
             batch, _to_float(item["price"])),
        )
        added.append(f"{item['title']} x{qty}")

    log_activity(
        cur, None, order["order_name"] or order_id, "repurchase_add",
        f"{who} added to Repurchase from {order['order_name'] or order_id}: " + ", ".join(added),
        order_id=order_id,
    )
    db.commit()
    cur.close()
    return jsonify({"ok": True, "added": len(added)})


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Who may move a repurchase to which status. Owner/telecaller: any. Staff buy
# the item (Pending <-> Purchased). Packers send it (Purchased <-> Sent) and
# only once it has been purchased. Accounts only view/print.
def _repurchase_status_allowed(role, current, new):
    if role in ("owner", "telecaller"):
        return True
    if role == "staff":
        return new in ("pending", "purchased") and current in ("pending", "purchased")
    if role == "packer":
        return (new == "resent" and current == "purchased") or (new == "purchased" and current == "resent")
    return False


@app.route("/api/repurchases")
@repurchase_view_required
def api_list_repurchases():
    status = request.args.get("status")
    role = session.get("role")
    db = get_db()
    cur = db.cursor()
    where, params = [], []
    if status in REPURCHASE_STATUSES:
        where.append("status = %s")
        params.append(status)
    if role == "packer":
        # Packers only deal with what is already bought and waiting to be sent.
        where.append("status IN ('purchased', 'resent')")
    sql = "SELECT *, COALESCE(batch, 'r' || id::text) AS batch_key FROM repurchases"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    out = []
    for r in rows:
        d = dict(r)
        d["price"] = _to_float(d.get("price"))
        out.append(d)
    return jsonify({"role": role, "rows": out})


@app.route("/api/repurchases/<int:rid>/status", methods=["POST"])
@repurchase_view_required
def api_repurchase_status(rid):
    data = request.get_json(force=True) or {}
    status = data.get("status")
    if status not in REPURCHASE_STATUSES:
        return jsonify({"error": "Invalid status"}), 400
    who = session.get("name", "")
    role = session.get("role")
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT status FROM repurchases WHERE id = %s FOR UPDATE", (rid,))
    existing = cur.fetchone()
    if not existing:
        cur.close()
        return jsonify({"error": "Not found"}), 404
    if not _repurchase_status_allowed(role, existing["status"], status):
        cur.close()
        msg = {
            "staff": "Staff can only mark items Pending or Purchased",
            "packer": "Packers can only mark a purchased item as Sent",
        }.get(role, "You can't change Repurchase status")
        return jsonify({"error": msg}), 403
    cur.execute(
        "UPDATE repurchases SET status = %s, updated_by = %s, updated_at = %s WHERE id = %s "
        "RETURNING order_name, shopify_order_id, title",
        (status, who, datetime.now(timezone.utc).isoformat(), rid),
    )
    row = cur.fetchone()
    log_activity(cur, None, row["order_name"] or row["shopify_order_id"], "repurchase_status",
                 f"{who} set repurchase of {row['title']} ({row['order_name']}) to {status}",
                 order_id=row["shopify_order_id"])
    db.commit()
    cur.close()
    return jsonify({"ok": True, "status": status})


def repurchase_invoice_required(f):
    """Owner, telecaller, accounts, staff may print a Repurchase bill. Staff
    only the first copy (same rule as normal invoices) — checked in the route."""
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if session.get("role") not in ("owner", "telecaller", "accounts", "staff"):
            return jsonify({"error": "You can't print Repurchase invoices"}), 403
        return f(*args, **kwargs)

    return wrapper


@app.route("/api/repurchases/batch/<batch_key>/invoice.pdf")
@repurchase_invoice_required
def api_repurchase_invoice(batch_key):
    """Separate RPR/<n>/<year> bill for one repurchase batch. Own counter, own
    columns — never reads or writes the original order's invoice number,
    invoice date, or Tally flag, and is not part of the Tally export."""
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT * FROM repurchases WHERE COALESCE(batch, 'r' || id::text) = %s ORDER BY id FOR UPDATE",
        (batch_key,),
    )
    rows = cur.fetchall()
    if not rows:
        cur.close()
        return jsonify({"error": "Repurchase entry not found"}), 404
    if any(r["status"] == "pending" for r in rows):
        cur.close()
        return jsonify({"error": "Mark every item in this repurchase as Purchased before printing the invoice"}), 400

    existing_no = next((r["invoice_number"] for r in rows if r["invoice_number"]), None)
    if session.get("role") == "staff" and existing_no:
        cur.close()
        return jsonify({"error": "This invoice has already been printed — ask accounts or the owner to reprint it"}), 403

    # Read-only look at the original order for the customer/address block.
    cur.execute("SELECT * FROM orders WHERE shopify_order_id = %s", (rows[0]["shopify_order_id"],))
    order = cur.fetchone()
    cur.execute(
        "SELECT id, price FROM items WHERE shopify_order_id = %s", (rows[0]["shopify_order_id"],)
    )
    item_price = {i["id"]: _to_float(i["price"]) for i in cur.fetchall()}

    is_first_print = not existing_no
    if is_first_print:
        cur.execute("SELECT value FROM settings WHERE key = %s FOR UPDATE", ("rpr_seq_next",))
        srow = cur.fetchone()
        seq = int(srow["value"]) if srow else 1
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            ("rpr_seq_next", str(seq + 1)),
        )
        now = datetime.now()
        invoice_number = f"RPR/{seq}/{now.year}"
        invoice_date = f"{now.day}-{now.strftime('%b-%y')}"  # same as %-d-%b-%y, but works on Windows too
        cur.execute(
            "UPDATE repurchases SET invoice_number = %s, invoice_date = %s, invoice_printed_by = %s "
            "WHERE COALESCE(batch, 'r' || id::text) = %s",
            (invoice_number, invoice_date, session["name"], batch_key),
        )
    else:
        invoice_number = existing_no
        invoice_date = next((r["invoice_date"] for r in rows if r["invoice_date"]), "")
        cur.execute(
            "UPDATE repurchases SET invoice_printed_by = %s WHERE COALESCE(batch, 'r' || id::text) = %s",
            (session["name"], batch_key),
        )

    log_activity(
        cur, None, rows[0]["order_name"] or rows[0]["shopify_order_id"],
        "repurchase_invoice",
        f"{session['name']} {'printed' if is_first_print else 'reprinted'} repurchase invoice {invoice_number}",
        order_id=rows[0]["shopify_order_id"],
    )
    db.commit()
    cur.close()

    order_dict = dict(order) if order else {}
    order_dict.update({
        "customer_name": order_dict.get("customer_name") or rows[0]["customer_name"],
        "customer_phone": order_dict.get("customer_phone") or rows[0]["customer_phone"],
        "order_name": rows[0]["order_name"],
        "shipping_amount": 0,        # no delivery charge on a repurchase bill
        "payment_type": "prepaid",   # shows "Amount is Received" — nothing to collect
        "balance_due": None,
    })
    items = []
    for r in rows:
        price = _to_float(r["price"])
        if price is None:
            price = item_price.get(r["item_id"]) or 0
        items.append({
            "title": r["title"], "variant_title": r["variant_title"],
            "quantity": r["quantity"], "price": price,
        })

    pdf_buf = build_invoice_pdf(order_dict, items, invoice_number, invoice_date)
    response = send_file(pdf_buf, mimetype="application/pdf", as_attachment=False,
                         download_name=invoice_number.replace("/", "-") + ".pdf")
    response.headers["X-Invoice-Number"] = invoice_number
    response.headers["Access-Control-Expose-Headers"] = "X-Invoice-Number"
    return response


@app.route("/api/repurchases/<int:rid>", methods=["DELETE"])
@repurchase_required
def api_delete_repurchase(rid):
    who = session.get("name", "")
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM repurchases WHERE id = %s RETURNING order_name, shopify_order_id, title", (rid,))
    row = cur.fetchone()
    if not row:
        cur.close()
        return jsonify({"error": "Not found"}), 404
    log_activity(cur, None, row["order_name"] or row["shopify_order_id"], "repurchase_delete",
                 f"{who} removed {row['title']} ({row['order_name']}) from Repurchase",
                 order_id=row["shopify_order_id"])
    db.commit()
    cur.close()
    return jsonify({"ok": True})


@app.route("/api/orders/<order_id>/cancelled", methods=["POST"])
@packer_or_owner_required
def api_update_order_cancelled(order_id):
    """Packer/owner-only. An alternative to packing — a packer who can't
    fulfil an order marks it cancelled instead, with a reason, rather than
    just leaving it stuck. Uncancelling (packed=False here) clears the
    reason and who/when, same as unpacking clears packed_by/packed_at."""
    data = request.get_json(force=True) or {}
    if "cancelled" not in data:
        return jsonify({"error": "cancelled is required"}), 400
    cancelled = bool(data.get("cancelled"))
    reason = (data.get("reason") or "").strip()

    if cancelled and not reason:
        return jsonify({"error": "A cancellation reason is required"}), 400

    who = session.get("name", "")
    now = datetime.now(timezone.utc).isoformat()
    cancelled_by = who if cancelled else None
    cancelled_at = now if cancelled else None
    cancelled_reason = reason if cancelled else None

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE orders SET cancelled = %s, cancelled_reason = %s, cancelled_by = %s, cancelled_at = %s "
        "WHERE shopify_order_id = %s RETURNING order_name",
        (cancelled, cancelled_reason, cancelled_by, cancelled_at, order_id),
    )
    row = cur.fetchone()
    if row is None:
        cur.close()
        return jsonify({"error": "Order not found"}), 404

    log_activity(
        cur, None, row["order_name"] or order_id, "cancelled_update",
        f"{who} marked order {row['order_name'] or order_id} as "
        f"{'cancelled (' + reason + ')' if cancelled else 'not cancelled'}",
        order_id=order_id,
    )
    db.commit()
    cur.close()
    return jsonify({
        "ok": True,
        "order_id": order_id,
        "cancelled": cancelled,
        "cancelled_reason": cancelled_reason,
        "cancelled_by": cancelled_by,
        "cancelled_at": cancelled_at,
    })


@app.route("/api/activity-log", methods=["GET"])
@login_required
def api_activity_log():
    # Read-only: owner and telecaller can view the log (telecaller needs the
    # full order history while on a call); nobody else.
    if session.get("role") not in ("owner", "telecaller"):
        return jsonify({"error": "Only the owner and telecallers can view the activity log"}), 403
    search = request.args.get("search", "").strip()
    order_id = request.args.get("order_id", "").strip()

    db = get_db()
    cur = db.cursor()
    query = """
        SELECT activity_log.*, orders.order_name AS order_name
        FROM activity_log
        LEFT JOIN orders ON orders.shopify_order_id = activity_log.order_id
        WHERE 1=1
    """
    params = []
    if order_id:
        # Exact match — used by the "History" link on a specific order card.
        query += " AND activity_log.order_id = %s"
        params.append(order_id)
    if search:
        # Search across item name, the change details, who made the change,
        # and the order itself (name or Shopify order id) — so an item
        # title, an order number, or a staff name all work here, letting
        # you "track" everything that happened on one order.
        like = f"%{search}%"
        query += (
            " AND (activity_log.item_name ILIKE %s OR activity_log.details ILIKE %s "
            "OR activity_log.user_name ILIKE %s OR orders.order_name ILIKE %s "
            "OR activity_log.order_id ILIKE %s)"
        )
        params += [like, like, like, like, like]
    query += " ORDER BY activity_log.id DESC LIMIT 300"

    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()
    return jsonify([dict(r) for r in rows])


# Run table creation once at import time (Render/gunicorn imports this
# module rather than calling `python app.py` directly).
init_db()


def get_local_ip():
    """Best-effort LAN IP, so we can print a phone-friendly URL when running
    locally. Doesn't actually send any traffic to 8.8.8.8 — just uses that
    address to ask the OS which local interface/IP would be used."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


if __name__ == "__main__":
    ip = get_local_ip()
    port = int(os.environ.get("PORT", 5050))
    print("\n" + "=" * 60)
    print("  Shopify Purchase Tracker is running!")
    print("=" * 60)
    print(f"  On THIS PC, open:       http://127.0.0.1:{port}")
    print(f"  On phones (same WiFi):  http://{ip}:{port}")
    print("=" * 60 + "\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
