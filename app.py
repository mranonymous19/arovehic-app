import os
import re
import socket
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

from invoice import build_invoice_pdf

DATABASE_URL = os.environ.get("DATABASE_URL", "")
VALID_STATUSES = {"pending", "purchased", "stock", "na"}
VALID_ROLES = {"owner", "staff", "telecaller", "packer", "accounts"}

app = Flask(__name__)
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

        CREATE INDEX IF NOT EXISTS idx_items_order_id ON items (shopify_order_id);
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
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_type TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS deleted_at TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS invoice_number TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS invoice_date TEXT;
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
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
    row = cur.fetchone()
    cur.close()
    return row["value"] if row else default


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


def log_activity(cur, item_id, item_name, action, details="", order_id=None):
    """Record one activity log entry using the already-open cursor `cur`,
    so it lands in the same transaction as whatever it's logging — does
    NOT call db.commit() itself, the caller's existing commit covers it."""
    cur.execute(
        "INSERT INTO activity_log (item_id, item_name, order_id, user_name, action, details, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (item_id, item_name, order_id, session.get("name", "unknown"), action, details,
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
#                assigned to them (see /api/orders below).
#   telecaller - view-only for orders/items, plus can add manual orders
#                (they're the ones taking these calls).
#   packer     - can toggle the packed flag only.
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


def packer_or_owner_required(f):
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if session.get("role") not in ("owner", "packer"):
            return jsonify({"error": "Only packer or owner accounts can update packed status"}), 403
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
        "cod_staff_id": get_setting("cod_staff_id", ""),
        "prepaid_staff_id": get_setting("prepaid_staff_id", ""),
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
# member is on COD duty vs Prepaid duty, is saved here — assignment is a
# standing rule ("X handles all COD orders"), not something set per order,
# so every order (past and future) always reflects the current rule.
# ---------------------------------------------------------------------------

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
        "cod_staff_id": get_setting("cod_staff_id", ""),
        "prepaid_staff_id": get_setting("prepaid_staff_id", ""),
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

    cod_staff_id = str(data.get("cod_staff_id") or "").strip()
    prepaid_staff_id = str(data.get("prepaid_staff_id") or "").strip()

    set_setting("cod_shipping_threshold", str(threshold))
    set_setting("cod_staff_id", cod_staff_id)
    set_setting("prepaid_staff_id", prepaid_staff_id)

    db = get_db()
    cur = db.cursor()
    log_activity(cur, None, "schedule", "settings",
                 f"{session['name']} updated COD/Prepaid staff scheduling")
    db.commit()
    cur.close()
    return jsonify({
        "ok": True,
        "cod_shipping_threshold": str(threshold),
        "cod_staff_id": cod_staff_id,
        "prepaid_staff_id": prepaid_staff_id,
    })


# ---------------------------------------------------------------------------
# Routes - sync from n8n (n8n talks to Shopify, this app talks to n8n)
# ---------------------------------------------------------------------------

@app.route("/api/sync", methods=["POST"])
@owner_required
def api_sync():
    webhook_url = get_setting("n8n_webhook_url")
    if not webhook_url:
        return jsonify({"error": "No n8n webhook URL configured. Set it in Settings first."}), 400

    try:
        resp = requests.get(webhook_url, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        return jsonify({"error": f"Could not reach n8n webhook: {exc}"}), 502

    try:
        payload = resp.json()
    except ValueError:
        # requests.Response.json() raises a JSONDecodeError that is ALSO a
        # RequestException subclass — split into its own try/except so this
        # doesn't get mislabeled as a connectivity failure above. A common
        # cause: the n8n workflow's "Respond to Webhook" node returned a
        # completely empty body (e.g. zero orders matched, with
        # "allIncomingItems" mode) instead of valid JSON like `[]`.
        body_preview = (resp.text or "")[:200]
        return jsonify({
            "error": "n8n webhook reached, but didn't return valid JSON "
                     f"(got: {body_preview!r}). Check the Respond to Webhook "
                     "node in n8n — it should always emit valid JSON, even "
                     "an empty [] when there's nothing to sync."
        }), 502

    # Expected payload shape (see README): a list of orders, each with a
    # list of line items. Adjust here if your n8n Code node outputs
    # differently.
    orders = payload if isinstance(payload, list) else payload.get("orders", [])

    db = get_db()
    cur = db.cursor()
    now = datetime.now(timezone.utc).isoformat()
    order_count = 0
    item_count = 0

    for order in orders:
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
            "order_name = EXCLUDED.order_name, customer_name = EXCLUDED.customer_name, "
            "shipping_address = EXCLUDED.shipping_address, "
            "shipping_address1 = EXCLUDED.shipping_address1, "
            "shipping_address2 = EXCLUDED.shipping_address2, "
            "shipping_city = EXCLUDED.shipping_city, "
            "shipping_state = EXCLUDED.shipping_state, "
            "shipping_pincode = EXCLUDED.shipping_pincode, "
            "customer_phone = EXCLUDED.customer_phone, "
            "shipping_amount = EXCLUDED.shipping_amount, "
            "balance_due = EXCLUDED.balance_due, "
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
                    "UPDATE items SET shopify_order_id=%s, title=%s, variant_title=%s, "
                    "quantity=%s, price=%s, vendor=%s, updated_at=%s WHERE id=%s",
                    (order_id, title, variant_title, quantity, price, vendor, now, item_id),
                )
            else:
                cur.execute(
                    "INSERT INTO items (id, shopify_order_id, title, variant_title, "
                    "quantity, price, vendor, status, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s)",
                    (item_id, order_id, title, variant_title, quantity, price, vendor, now),
                )
            item_count += 1

    log_activity(cur, None, "sync", "sync", f"{session['name']} synced {order_count} orders / {item_count} items from Shopify")
    db.commit()
    cur.close()
    return jsonify({"ok": True, "orders_synced": order_count, "items_synced": item_count})


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

    if status_filter == "trash" and session.get("role") != "owner":
        return jsonify({"error": "Only the owner can view Trash"}), 403

    # created_at is stored as ISO-8601 text (as it comes from Shopify), so
    # cast it to a date for the range comparison rather than a string match.
    query = "SELECT * FROM orders WHERE 1=1"
    params = []
    query += " AND deleted_at IS NOT NULL" if status_filter == "trash" else " AND deleted_at IS NULL"
    if date_from:
        # NULLIF guards against a blank/unparseable created_at (e.g. odd
        # sync data) throwing a cast error — it just won't match instead.
        query += " AND NULLIF(created_at, '')::date >= %s::date"
        params.append(date_from)
    if date_to:
        query += " AND NULLIF(created_at, '')::date <= %s::date"
        params.append(date_to)
    query += " ORDER BY created_at DESC"
    cur.execute(query, params)
    orders = cur.fetchall()

    # COD/Prepaid is derived from each order's shipping charge against the
    # threshold set in Settings -> Schedule, not stored per order — so
    # changing the threshold or the staff assignment always applies
    # retroactively to every order, past and future.
    cod_threshold = float(get_setting("cod_shipping_threshold", "140") or 140)
    cod_staff_id = get_setting("cod_staff_id", "")
    prepaid_staff_id = get_setting("prepaid_staff_id", "")
    staff_names = {}
    staff_ids = [sid for sid in (cod_staff_id, prepaid_staff_id) if sid]
    if staff_ids:
        cur.execute("SELECT id, name FROM users WHERE id = ANY(%s)", ([int(s) for s in staff_ids],))
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
            assigned_staff_id = None
            assigned_to = None
        else:
            payment_type = resolve_payment_type(order, cod_threshold)
            assigned_staff_id = cod_staff_id if payment_type == "cod" else prepaid_staff_id
            assigned_to = staff_names.get(str(assigned_staff_id)) if assigned_staff_id else None

        # Staff accounts only see orders currently assigned to them (their
        # COD/Prepaid duty from Settings -> Schedule) — everyone else
        # (owner, telecaller, packer, accounts) sees the full list, subject
        # to whatever other filters apply below.
        if session.get("role") == "staff":
            if not assigned_staff_id or str(assigned_staff_id) != str(session.get("user_id")):
                continue

        if payment_filter in ("cod", "prepaid") and payment_type != payment_filter:
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
        elif status_filter:
            items = [i for i in items if i["status"] == status_filter]
            if not items:
                continue

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
                "closed": closed,
                "payment_type": payment_type,
                "assigned_to": assigned_to,
                "invoice_number": order["invoice_number"],
                "deleted_at": order["deleted_at"],
                "items": [dict(i) for i in items],
            }
        )

    cur.close()
    return jsonify(result)


@app.route("/api/orders/<order_id>/invoice.pdf", methods=["GET"])
@login_required
def api_order_invoice(order_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM orders WHERE shopify_order_id = %s", (order_id,))
    order = cur.fetchone()
    if not order:
        cur.close()
        return jsonify({"error": "Order not found"}), 404

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
        return jsonify({"error": "This order isn't in Billing yet."}), 400

    if not order["invoice_number"]:
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
        invoice_date = now.strftime("%-d-%b-%y")
        invoice_number = f"SHP/{seq}/{now.year}"
        cur.execute(
            "UPDATE orders SET invoice_number = %s, invoice_date = %s WHERE shopify_order_id = %s",
            (invoice_number, invoice_date, order_id),
        )
        db.commit()
    else:
        invoice_number = order["invoice_number"]
        invoice_date = order["invoice_date"]

    cur.close()

    # Same COD/Prepaid rule used by /api/orders — an explicit payment_type
    # (manually-entered orders) wins, otherwise derived from the shipping
    # charge against the threshold in Settings -> Schedule.
    order_dict = dict(order)
    cod_threshold = float(get_setting("cod_shipping_threshold", "140") or 140)
    order_dict["payment_type"] = resolve_payment_type(order_dict, cod_threshold) or "prepaid"
    # order_dict["balance_due"] already carries through from `dict(order)` —
    # invoice.py uses it (when set) instead of the full grand total for the
    # "Amount to be Received" line on partially-paid COD orders.

    pdf_buf = build_invoice_pdf(order_dict, [dict(i) for i in billing_items], invoice_number, invoice_date)
    filename = invoice_number.replace("/", "-") + ".pdf"
    response = send_file(pdf_buf, mimetype="application/pdf", as_attachment=False, download_name=filename)
    # So the frontend can flip the Printed/Not-Printed badge right away,
    # from this same request/response, instead of needing a page refresh
    # or a second round-trip to find out what number got assigned.
    response.headers["X-Invoice-Number"] = invoice_number
    response.headers["X-Invoice-Date"] = invoice_date
    response.headers["Access-Control-Expose-Headers"] = "X-Invoice-Number, X-Invoice-Date"
    return response


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
    """True if `order_id`'s current COD/Prepaid duty staff is `user_id`.

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
    assigned_staff_id = get_setting("cod_staff_id" if payment_type == "cod" else "prepaid_staff_id", "")
    return bool(assigned_staff_id) and str(assigned_staff_id) == str(user_id)


@app.route("/api/items/<item_id>/status", methods=["POST"])
@staff_or_owner_required
def api_update_item_status(item_id):
    data = request.get_json(force=True) or {}
    status = data.get("status")
    if status not in VALID_STATUSES:
        return jsonify({"error": f"status must be one of {sorted(VALID_STATUSES)}"}), 400

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


@app.route("/api/activity-log", methods=["GET"])
@owner_required
def api_activity_log():
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
