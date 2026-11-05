"""
Tally outbox -- the queue between this app and TallyPrime.

Every voucher that has to reach Tally (a sale when an invoice is first printed, a
credit note when a bill is cancelled / an item is refunded) is written to the
`tally_outbox` table together with a SNAPSHOT of exactly what was sent. A small
program on the Tally PC (tally_bridge.py) fetches pending rows, posts them to
TallyPrime's local XML server (port 9000) and acknowledges each one.

Why a snapshot: a credit note must reverse what Tally actually has, to the paisa,
even if the order was edited afterwards.

All functions take an open DB cursor (RealDictCursor style rows) and never commit --
the caller commits, so the outbox row and the change that caused it are one
transaction.

Row status:  pending -> sent            (Tally accepted it)
             pending -> error -> pending (Tally rejected it; fix + press Retry)
             pending/error -> void       (cancelled before Tally ever received it)
"""

import json
import os
from datetime import datetime, timedelta, timezone

import tally_export

IST = timezone(timedelta(hours=5, minutes=30))
ACTIVE = ("pending", "sent")
CN_COUNTER_KEY = "tally_cn_seq_next"
BRIDGE_SEEN_KEY = "tally_bridge_last_seen"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def today_ist():
    n = datetime.now(IST)
    return datetime(n.year, n.month, n.day)


def _row(r):
    """Normalise a DB row to a plain dict."""
    return dict(r) if r is not None else None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def order_rows(cur, order_id):
    cur.execute(
        "SELECT id, kind, status, voucher_number, payload, reason FROM tally_outbox "
        "WHERE order_id = %s ORDER BY id",
        (order_id,),
    )
    rows = [_row(r) for r in cur.fetchall()]
    for r in rows:
        r["data"] = json.loads(r["payload"])
    return rows


def has_sale(cur, order_id):
    cur.execute(
        "SELECT 1 AS x FROM tally_outbox WHERE order_id = %s AND kind = 'sale' AND status <> 'void' LIMIT 1",
        (order_id,),
    )
    return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _insert(cur, order_id, kind, rec, reason, company, status="pending", with_xml=True):
    masters_xml = voucher_xml = None
    if with_xml:
        masters_xml = tally_export.build_tally_xml([rec], company, masters=True, vouchers=False)
        voucher_xml = tally_export.build_tally_xml([rec], company, masters=False, vouchers=True)
    cur.execute(
        "INSERT INTO tally_outbox (order_id, kind, voucher_number, reason, payload, masters_xml, "
        "voucher_xml, status, attempts, created_at, sent_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s) RETURNING id",
        (
            order_id, kind, rec["invoice_number"], reason,
            json.dumps(tally_export.record_to_payload(rec)),
            masters_xml, voucher_xml, status, _now_iso(),
            _now_iso() if status == "sent" else None,
        ),
    )
    return cur.fetchone()["id"]


def enqueue_sale(cur, order, items, payment_type, company=""):
    """Queue the sales voucher for a freshly printed invoice. `order` must already carry
    invoice_number and invoice_date. Does nothing if this order already has a live sale."""
    order_id = order["shopify_order_id"]
    if has_sale(cur, order_id):
        return {"action": "none", "message": "already queued"}
    rec = tally_export.build_records([(order, items, payment_type)])[0]
    row_id = _insert(cur, order_id, "sale", rec, "invoice printed", company)
    # Keep the manual XML export from picking this bill up a second time.
    cur.execute(
        "UPDATE orders SET tally_exported_at = %s WHERE shopify_order_id = %s", (_now_iso(), order_id)
    )
    return {"action": "sale_queued", "id": row_id, "voucher_number": rec["invoice_number"],
            "amount": rec["grand_total"]}


def next_cn_number(cur, cn_dt):
    """CR/<running no>/<yy>. The counter row is locked so two requests can't clash."""
    start = os.environ.get("TALLY_CN_START", "2")
    start = int(start) if str(start).isdigit() else 2
    cur.execute(
        "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
        (CN_COUNTER_KEY, str(start)),
    )
    cur.execute("SELECT value FROM settings WHERE key = %s FOR UPDATE", (CN_COUNTER_KEY,))
    seq = int(cur.fetchone()["value"])
    cur.execute("UPDATE settings SET value = %s WHERE key = %s", (str(seq + 1), CN_COUNTER_KEY))
    return f"CR/{seq}/{cn_dt.year % 100:02d}"


def set_cn_next(cur, number):
    cur.execute(
        "INSERT INTO settings (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (CN_COUNTER_KEY, str(int(number))),
    )


def _void(cur, row_ids):
    for rid in row_ids:
        cur.execute(
            "UPDATE tally_outbox SET status = 'void', last_error = NULL WHERE id = %s", (rid,)
        )


def reverse(cur, order_id, item_ids=None, reason="", company="", cn_dt=None):
    """Undo (all or part of) this order's sale in Tally.

    item_ids=None  -> the whole bill is cancelled.
    item_ids=[..]  -> only those items were refunded (partial credit note).

    * Sale never reached Tally (pending / error) and the whole bill is cancelled ->
      the queued sale is simply voided: no sale, no credit note, nothing to file.
    * Sale already in Tally -> a credit note is queued for what is not yet reversed.
    """
    rows = order_rows(cur, order_id)
    sales = [r for r in rows if r["kind"] == "sale" and r["status"] != "void"]
    if not sales:
        return {"action": "none", "message": "bill was never sent to Tally"}
    sale = sales[-1]
    cns = [r for r in rows if r["kind"] == "credit_note" and r["status"] in ACTIVE]

    if item_ids is None and sale["status"] != "sent":
        _void(cur, [sale["id"]] + [c["id"] for c in cns if c["status"] in ("pending", "error")])
        return {"action": "voided", "message":
                f"{sale['voucher_number']} had not reached Tally yet, so it was cancelled there too "
                "(no credit note needed)"}
    if sale["status"] == "error":
        return {"action": "none", "message": "sale was rejected by Tally; nothing to reverse"}

    reversed_ids = set()
    prior_igst = 0.0
    for c in cns:
        reversed_ids.update(c["data"].get("reversed_item_ids") or [])
        prior_igst += c["data"].get("igst") or 0.0

    sale_dt = datetime.fromisoformat(sale["data"]["dt"])
    when = cn_dt or today_ist()
    if when < sale_dt:  # a credit note can't pre-date the sale it reverses
        when = sale_dt

    plan = tally_export.plan_credit_note(
        sale["data"], "", when, reason, item_ids=item_ids,
        reversed_ids=reversed_ids, prior_igst=prior_igst,
    )
    if plan is None:
        return {"action": "none", "message": "nothing left to reverse in Tally"}
    rec, _ids, is_final = plan
    rec["invoice_number"] = next_cn_number(cur, when)
    row_id = _insert(cur, order_id, "credit_note", rec, reason, company)
    return {"action": "credit_note_queued", "id": row_id, "voucher_number": rec["invoice_number"],
            "amount": rec["grand_total"], "full": is_final,
            "message": f"credit note {rec['invoice_number']} (Rs {rec['grand_total']:.2f}) queued for "
                       f"invoice {sale['voucher_number']}"}


def restore(cur, order_id, requeue_sale=None):
    """The bill / order was un-cancelled. Undo what cancelling queued, if Tally hasn't seen it.

    requeue_sale: zero-arg callable that re-queues the sale from the order as it is now
    (only used when the sale was voided and never reached Tally).
    """
    rows = order_rows(cur, order_id)
    sales = [r for r in rows if r["kind"] == "sale" and r["status"] != "void"]
    cns = [r for r in rows if r["kind"] == "credit_note" and r["status"] in ACTIVE]
    if not sales:
        voided = [r for r in rows if r["kind"] == "sale" and r["status"] == "void"]
        if voided and requeue_sale:
            return requeue_sale()
        return {"action": "none"}
    pending_cn = [c for c in cns if c["status"] in ("pending", "error") and c["data"].get("is_final")]
    sent_cn = [c for c in cns if c["status"] == "sent" and c["data"].get("is_final")]
    if pending_cn and not sent_cn:
        _void(cur, [c["id"] for c in pending_cn])
        return {"action": "credit_note_voided", "message":
                "credit note had not reached Tally yet, so it was cancelled; the sale stays as it was"}
    if sent_cn:
        return {"action": "warn", "message":
                f"Credit note {sent_cn[-1]['voucher_number']} is already in Tally for this bill, and the "
                "original sale stays reversed there. Restoring the bill here does NOT create a new sale "
                "in Tally - enter a fresh sale for it in Tally if it is going ahead."}
    return {"action": "none"}


def unrefund(cur, order_id, item_id):
    """An item's Refunded status was undone."""
    rows = order_rows(cur, order_id)
    for c in rows:
        if c["kind"] != "credit_note" or c["status"] not in ACTIVE:
            continue
        ids = c["data"].get("reversed_item_ids") or []
        if item_id in ids:
            if c["status"] in ("pending", "error") and ids == [item_id]:
                _void(cur, [c["id"]])
                return {"action": "credit_note_voided", "message":
                        f"credit note {c['voucher_number']} had not reached Tally yet, so it was cancelled"}
            return {"action": "warn", "message":
                    f"Credit note {c['voucher_number']} for this item is already in Tally (or covers more "
                    "than this item) and is NOT undone there. Fix it in Tally if the refund was a mistake."}
    return {"action": "none"}


# ---------------------------------------------------------------------------
# Bridge side (the program on the Tally PC)
# ---------------------------------------------------------------------------

def fetch_pending(cur, limit=20):
    cur.execute(
        "SELECT o.id, o.order_id, o.kind, o.voucher_number, o.masters_xml, o.voucher_xml "
        "FROM tally_outbox o WHERE o.status = 'pending' AND o.voucher_xml IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM tally_outbox e WHERE e.order_id = o.order_id "
        "                AND e.id < o.id AND e.status = 'error') "
        "ORDER BY o.id LIMIT %s",
        (int(limit),),
    )
    return [_row(r) for r in cur.fetchall()]


def ack(cur, row_id, ok, message=""):
    """Record Tally's answer. Returns {order_id, kind, was_void} or None if unknown id."""
    cur.execute("SELECT id, order_id, kind, status FROM tally_outbox WHERE id = %s", (row_id,))
    r = cur.fetchone()
    if r is None:
        return None
    r = _row(r)
    if r["status"] == "sent":
        return {"order_id": r["order_id"], "kind": r["kind"], "was_void": False, "duplicate": True}
    if ok:
        cur.execute(
            "UPDATE tally_outbox SET status = 'sent', sent_at = %s, last_error = NULL, "
            "attempts = attempts + 1 WHERE id = %s",
            (_now_iso(), row_id),
        )
    else:
        cur.execute(
            "UPDATE tally_outbox SET status = 'error', last_error = %s, attempts = attempts + 1 "
            "WHERE id = %s",
            ((message or "unknown error")[:2000], row_id),
        )
    return {"order_id": r["order_id"], "kind": r["kind"], "was_void": r["status"] == "void",
            "duplicate": False}


def retry(cur, row_id):
    cur.execute(
        "UPDATE tally_outbox SET status = 'pending', last_error = NULL "
        "WHERE id = %s AND status = 'error'",
        (row_id,),
    )
    return cur.rowcount == 1


def list_rows(cur, limit=150):
    cur.execute(
        "SELECT id, order_id, kind, voucher_number, reason, status, attempts, last_error, payload, "
        "created_at, sent_at FROM tally_outbox ORDER BY id DESC LIMIT %s",
        (int(limit),),
    )
    out = []
    for r in cur.fetchall():
        r = _row(r)
        data = json.loads(r.pop("payload"))
        r["order_name"] = data.get("order_name")
        r["customer"] = data.get("customer_name")
        r["amount"] = data.get("grand_total")
        r["orig_invoice"] = data.get("orig_invoice_number")
        out.append(r)
    cur.execute("SELECT status, COUNT(*) AS n FROM tally_outbox GROUP BY status")
    counts = {r["status"]: r["n"] for r in cur.fetchall()}
    return out, counts


def pending_records(cur, ids=None):
    """Records of pending rows (for a manual XML download when the bridge isn't used)."""
    cur.execute(
        "SELECT id, payload FROM tally_outbox WHERE status = 'pending' AND voucher_xml IS NOT NULL ORDER BY id"
    )
    rows = [_row(r) for r in cur.fetchall()]
    if ids is not None:
        rows = [r for r in rows if r["id"] in set(ids)]
    return [r["id"] for r in rows], [tally_export.payload_to_record(json.loads(r["payload"])) for r in rows]


def mark_sent(cur, ids):
    for rid in ids:
        cur.execute(
            "UPDATE tally_outbox SET status = 'sent', sent_at = %s, last_error = NULL WHERE id = %s",
            (_now_iso(), rid),
        )


# ---------------------------------------------------------------------------
# One-off: bills that were exported to Tally by XML file earlier and cancelled since
# ---------------------------------------------------------------------------

def legacy_candidates(cur):
    """Invoiced orders that were already exported to Tally (old manual XML), are cancelled /
    bill-cancelled now, and have no outbox history -> their sale is still sitting in Sales."""
    cur.execute(
        "SELECT * FROM orders o WHERE o.deleted_at IS NULL AND o.invoice_number IS NOT NULL "
        "AND o.invoice_number <> '' AND o.tally_exported_at IS NOT NULL AND o.tally_exported_at <> '' "
        "AND (o.bill_cancelled = true OR o.cancelled = true) "
        "AND NOT EXISTS (SELECT 1 FROM tally_outbox t WHERE t.order_id = o.shopify_order_id) "
        "ORDER BY o.shopify_order_id"
    )
    return [_row(r) for r in cur.fetchall()]


def enqueue_legacy_credit_note(cur, order, items, payment_type, reason, company="", cn_dt=None):
    """Record the earlier manual export as an already-'sent' sale (no XML, never re-sent), then
    reverse it with a normal credit note."""
    rec = tally_export.build_records([(order, items, payment_type)])[0]
    _insert(cur, order["shopify_order_id"], "sale", rec,
            "exported earlier by XML file (legacy)", company, status="sent", with_xml=False)
    return reverse(cur, order["shopify_order_id"], None, reason, company, cn_dt)
