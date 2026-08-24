"""
Generates a GST Tax Invoice PDF that visually matches the seller's existing
Tally-style invoice template (see the sample PDF the format was built from).

Layout is hand-drawn with reportlab's low-level canvas (not platypus) so
every border, cell, and line lands at the same spot as the original —
platypus/table auto-layout does not reproduce a fixed pre-existing form
faithfully enough for this.
"""

import io
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth

# ---------------------------------------------------------------------------
# Fixed seller details (from the sample invoice this template was built from)
# ---------------------------------------------------------------------------

SELLER = {
    "name": "ARCADIA AUTOHUB PRIVATE LIMITED",
    "address_lines": [
        "THIRD FLOOR BUILDING N FLAT NO:-23",
        "ARIHANTH COMPLEX JC ROAD,",
        "KALASIPALYAM NEW EXTENTIOM",
        "JOURNALIST, COLONY BENGALURU-560002",
    ],
    "gstin": "29ABECA4544R1Z0",
    "state_name": "Karnataka",
    "state_code": "29",
    "contact": "+91-6360818919",
}

GST_RATE = 0.18  # always 18% IGST

# Indian GST state codes, used to render "State Name : X, Code : NN" for the
# buyer/consignee the same way the seller block shows its own.
GST_STATE_CODES = {
    "jammu and kashmir": "01", "jammu & kashmir": "01",
    "himachal pradesh": "02",
    "punjab": "03",
    "chandigarh": "04",
    "uttarakhand": "05",
    "haryana": "06",
    "delhi": "07", "new delhi": "07",
    "rajasthan": "08",
    "uttar pradesh": "09",
    "bihar": "10",
    "sikkim": "11",
    "arunachal pradesh": "12",
    "nagaland": "13",
    "manipur": "14",
    "mizoram": "15",
    "tripura": "16",
    "meghalaya": "17",
    "assam": "18",
    "west bengal": "19",
    "jharkhand": "20",
    "odisha": "21", "orissa": "21",
    "chhattisgarh": "22",
    "madhya pradesh": "23",
    "gujarat": "24",
    "dadra and nagar haveli and daman and diu": "26",
    "maharashtra": "27",
    "karnataka": "29",
    "goa": "30",
    "lakshadweep": "31",
    "kerala": "32",
    "tamil nadu": "33",
    "puducherry": "34", "pondicherry": "34",
    "andaman and nicobar islands": "35",
    "telangana": "36",
    "andhra pradesh": "37",
    "ladakh": "38",
}


def gst_state_code(state_name):
    if not state_name:
        return ""
    return GST_STATE_CODES.get(state_name.strip().lower(), "")


# ---------------------------------------------------------------------------
# Number -> Indian words (crore / lakh / thousand)
# ---------------------------------------------------------------------------

_ONES = [
    "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
    "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
    "Seventeen", "Eighteen", "Nineteen",
]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two_digits(n):
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + (" " + _ONES[ones] if ones else "")


def _three_digits(n):
    if n >= 100:
        rest = n % 100
        return _ONES[n // 100] + " Hundred" + (" " + _two_digits(rest) if rest else "")
    return _two_digits(n)


def number_to_words_indian(amount):
    """770 -> 'Seven Hundred Seventy'. Indian grouping (crore/lakh/thousand)."""
    n = int(round(amount))
    if n == 0:
        return "Zero"
    parts = []
    crore, n = divmod(n, 10000000)
    lakh, n = divmod(n, 100000)
    thousand, n = divmod(n, 1000)
    hundred = n
    if crore:
        parts.append(_three_digits(crore) + " Crore")
    if lakh:
        parts.append(_three_digits(lakh) + " Lakh")
    if thousand:
        parts.append(_three_digits(thousand) + " Thousand")
    if hundred:
        parts.append(_three_digits(hundred))
    return " ".join(parts)


def amount_in_words(amount):
    return f"INR {number_to_words_indian(amount)} Only"


# ---------------------------------------------------------------------------
# Small drawing helpers
# ---------------------------------------------------------------------------

def _text(c, x, y, text, font="Helvetica", size=8, align="left"):
    c.setFont(font, size)
    if align == "left":
        c.drawString(x, y, text)
    elif align == "center":
        c.drawCentredString(x, y, text)
    elif align == "right":
        c.drawRightString(x, y, text)


def _wrap(text, font, size, max_width):
    """Greedy word-wrap for a single string into lines that fit max_width."""
    if not text:
        return [""]
    words = text.split()
    lines = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        if stringWidth(trial, font, size) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def fmt_amount(n):
    return f"{n:,.2f}"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_invoice_pdf(order, items, invoice_number, invoice_date_str):
    """
    order: dict with customer_name, shipping_address1/2, shipping_city,
           shipping_state, shipping_pincode, customer_phone, shipping_amount
    items: list of dicts with title, variant_title, quantity, price
    invoice_number: e.g. "SHP/2501/2026"
    invoice_date_str: e.g. "14-Aug-26"
    Returns a BytesIO containing the PDF.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4

    margin = 30
    x0 = margin
    x1 = page_w - margin

    # ---- computed figures -------------------------------------------------
    line_items = []
    total_base = 0.0
    total_incl = 0.0
    total_qty = 0
    for it in items:
        qty = int(it.get("quantity") or 1)
        unit_price_incl = float(it.get("price") or 0)
        item_total_incl = unit_price_incl * qty
        item_base = item_total_incl / (1 + GST_RATE)
        unit_rate_excl = item_base / qty if qty else 0
        title = it.get("title") or ""
        variant = it.get("variant_title") or ""
        desc = f"{title} {variant}".strip() if variant else title
        line_items.append(
            {
                "desc": desc,
                "qty": qty,
                "unit_rate_excl": unit_rate_excl,
                "amount": item_base,
            }
        )
        total_base += item_base
        total_incl += item_total_incl
        total_qty += qty

    igst_amount = round(total_base * GST_RATE, 2)
    delivery_amount = float(order.get("shipping_amount") or 0)
    grand_total = round(total_incl + delivery_amount, 2)

    # ---- title --------------------------------------------------------
    y = page_h - margin
    _text(c, page_w / 2, y, "Tax Invoice", font="Helvetica-Bold", size=13, align="center")
    y -= 18

    box_top = y
    LEFT_W = 340
    x_mid = x0 + LEFT_W
    RIGHT_HALF = (x1 - x_mid) / 2
    x_r_mid = x_mid + RIGHT_HALF

    ROW_H = 26
    N_LABEL_ROWS = 6
    right_rows_h = ROW_H * N_LABEL_ROWS

    SELLER_H = right_rows_h
    CONSIGNEE_H = 70
    BUYER_H = 70
    LEFT_H = SELLER_H + CONSIGNEE_H + BUYER_H
    TERMS_H = LEFT_H - right_rows_h

    box_bottom = box_top - LEFT_H

    c.setLineWidth(0.8)
    c.rect(x0, box_bottom, x1 - x0, box_top - box_bottom)
    c.line(x_mid, box_bottom, x_mid, box_top)

    # ---- seller block (left, top) ----
    sy = box_top - 11
    _text(c, x0 + 4, sy, SELLER["name"], font="Helvetica-Bold", size=9.5)
    sy -= 12
    for line in SELLER["address_lines"]:
        _text(c, x0 + 4, sy, line, font="Helvetica", size=8)
        sy -= 10.5
    _text(c, x0 + 4, sy, f"GSTIN/UIN: {SELLER['gstin']}", font="Helvetica", size=8)
    sy -= 10.5
    _text(c, x0 + 4, sy, f"State Name : {SELLER['state_name']}, Code : {SELLER['state_code']}",
          font="Helvetica", size=8)
    sy -= 10.5
    _text(c, x0 + 4, sy, f"Contact : {SELLER['contact']}", font="Helvetica", size=8)

    c.line(x0, box_top - SELLER_H, x_mid, box_top - SELLER_H)

    # ---- consignee block ----
    consignee_top = box_top - SELLER_H
    cy = consignee_top - 10
    _text(c, x0 + 4, cy, "Consignee (Ship to)", font="Helvetica", size=7.5)
    cy -= 11
    _text(c, x0 + 4, cy, order.get("customer_name") or "", font="Helvetica-Bold", size=8.5)
    cy -= 11
    addr_parts = [order.get("shipping_address1"), order.get("shipping_address2"),
                  order.get("shipping_city"), order.get("shipping_pincode")]
    addr_line = ", ".join(p for p in addr_parts if p)
    for wline in _wrap(addr_line, "Helvetica", 8, LEFT_W - 8)[:2]:
        _text(c, x0 + 4, cy, wline, font="Helvetica", size=8)
        cy -= 10.5
    if order.get("customer_phone"):
        _text(c, x0 + 4, cy, f"Mob: {order['customer_phone']}", font="Helvetica", size=8)
        cy -= 10.5
    state = order.get("shipping_state") or ""
    code = gst_state_code(state)
    _text(c, x0 + 4, cy, f"State Name : {state}, Code : {code}", font="Helvetica", size=8)

    c.line(x0, box_top - SELLER_H - CONSIGNEE_H, x_mid, box_top - SELLER_H - CONSIGNEE_H)

    # ---- buyer block (same details — Bill to == Ship to in this app) ----
    buyer_top = box_top - SELLER_H - CONSIGNEE_H
    by = buyer_top - 10
    _text(c, x0 + 4, by, "Buyer (Bill to)", font="Helvetica", size=7.5)
    by -= 11
    _text(c, x0 + 4, by, order.get("customer_name") or "", font="Helvetica-Bold", size=8.5)
    by -= 11
    for wline in _wrap(addr_line, "Helvetica", 8, LEFT_W - 8)[:2]:
        _text(c, x0 + 4, by, wline, font="Helvetica", size=8)
        by -= 10.5
    if order.get("customer_phone"):
        _text(c, x0 + 4, by, f"Mob: {order['customer_phone']}", font="Helvetica", size=8)
        by -= 10.5
    _text(c, x0 + 4, by, f"State Name : {state}, Code : {code}", font="Helvetica", size=8)

    # ---- right meta grid ----
    right_rows = [
        ("Invoice No.", invoice_number, "Dated", invoice_date_str),
        ("Delivery Note", "", "Mode/Terms of Payment", ""),
        ("Reference No. & Date.", "", "Other References", ""),
        ("Buyer's Order No.", "", "Dated", ""),
        ("Dispatch Doc No.", "", "Delivery Note Date", ""),
        ("Dispatched through", "", "Destination", ""),
    ]
    for i, (l1, v1, l2, v2) in enumerate(right_rows):
        row_top = box_top - i * ROW_H
        row_bottom = row_top - ROW_H
        if i > 0:
            c.line(x_mid, row_top, x1, row_top)
        c.line(x_r_mid, row_bottom, x_r_mid, row_top)
        _text(c, x_mid + 4, row_top - 10, l1, font="Helvetica", size=7.5)
        _text(c, x_mid + 4, row_top - 21, v1, font="Helvetica-Bold", size=8.5)
        _text(c, x_r_mid + 4, row_top - 10, l2, font="Helvetica", size=7.5)
        _text(c, x_r_mid + 4, row_top - 21, v2, font="Helvetica-Bold", size=8.5)

    terms_top = box_top - right_rows_h
    c.line(x_mid, terms_top, x1, terms_top)
    _text(c, x_mid + 4, terms_top - 10, "Terms of Delivery", font="Helvetica", size=7.5)

    # payment_type is passed in by app.py, derived from the order's shipping
    # charge against the COD threshold in Settings -> Schedule (same rule
    # used everywhere else in the app). Falls back to that same 140 default
    # here too, in case this is ever called without it set.
    payment_type = order.get("payment_type")
    if payment_type not in ("cod", "prepaid"):
        cod_threshold = float(order.get("cod_shipping_threshold") or 140)
        payment_type = "cod" if delivery_amount >= cod_threshold else "prepaid"
    if payment_type == "cod":
        payment_text = f"Rs. {fmt_amount(grand_total)} Amount to be Received"
    else:
        payment_text = "Amount is Received"
    _text(c, x_mid + 4, terms_top - 25, payment_text, font="Helvetica-Bold", size=9)

    y = box_bottom

    # ---- item table ---------------------------------------------------
    cols = [
        ("Sl\nNo.", 25),
        ("Description of Goods", 300),
        ("Quantity", 55),
        ("Rate", 55),
        ("Amount", 100),
    ]
    col_x = [x0]
    for _, w in cols:
        col_x.append(col_x[-1] + w)

    HEADER_H = 24
    header_top = y
    header_bottom = header_top - HEADER_H
    for i, (label, w) in enumerate(cols):
        cx = col_x[i]
        lines = label.split("\n")
        if len(lines) == 1:
            _text(c, cx + w / 2, header_bottom + 8, lines[0], font="Helvetica-Bold", size=8, align="center")
        else:
            _text(c, cx + w / 2, header_bottom + 14, lines[0], font="Helvetica-Bold", size=8, align="center")
            _text(c, cx + w / 2, header_bottom + 4, lines[1], font="Helvetica-Bold", size=8, align="center")

    # body rows: one per line item (height grows if the description wraps to
    # 2 lines), then an IGST summary row, then a delivery row if any.
    BASE_ROW_H = 16
    desc_wrap_cache = []
    rows_height = 0
    for li in line_items:
        wrapped = _wrap(li["desc"], "Helvetica-Bold", 8, cols[1][1] - 6)[:2]
        desc_wrap_cache.append(wrapped)
        row_h = BASE_ROW_H if len(wrapped) <= 1 else BASE_ROW_H + 9
        rows_height += row_h
    rows_height += BASE_ROW_H  # IGST row
    if delivery_amount:
        rows_height += BASE_ROW_H  # delivery row

    MIN_BODY_H = 230  # keep a tall table like the sample even for few items
    body_h = max(rows_height, MIN_BODY_H)
    body_top = header_bottom
    body_bottom = body_top - body_h

    ry = body_top
    for idx, li in enumerate(line_items, start=1):
        wrapped = desc_wrap_cache[idx - 1]
        row_h = BASE_ROW_H if len(wrapped) <= 1 else BASE_ROW_H + 9
        _text(c, col_x[0] + cols[0][1] / 2, ry - 12, str(idx), size=8, align="center")
        for wline_i, wline in enumerate(wrapped):
            _text(c, col_x[1] + 3, ry - 12 - wline_i * 9, wline, font="Helvetica-Bold", size=8)
        _text(c, col_x[2] + cols[2][1] - 4, ry - 12, f"{li['qty']} nos", size=8, align="right")
        _text(c, col_x[3] + cols[3][1] - 4, ry - 12, fmt_amount(li["unit_rate_excl"]), size=8, align="right")
        _text(c, col_x[4] + cols[4][1] - 4, ry - 12, fmt_amount(li["amount"]), font="Helvetica-Bold", size=8, align="right")
        ry -= row_h

    # GST/IGST is applied once here, on the invoice total — not per item.
    # The rate is stated in the description text itself, so no separate
    # GST-rate column is needed.
    _text(c, col_x[1] + 3, ry - 12, f"Output Igst {int(GST_RATE*100)}%", font="Helvetica-Oblique", size=8)
    _text(c, col_x[4] + cols[4][1] - 4, ry - 12, fmt_amount(igst_amount), font="Helvetica-Bold", size=8, align="right")
    ry -= BASE_ROW_H

    if delivery_amount:
        _text(c, col_x[1] + 3, ry - 12, "DELIVERY CHARGE", font="Helvetica-Oblique", size=8)
        _text(c, col_x[4] + cols[4][1] - 4, ry - 12, fmt_amount(delivery_amount), font="Helvetica-Bold", size=8, align="right")
        ry -= BASE_ROW_H

    # table borders: outer rect (header+body), column separators, header/body divider
    c.rect(x0, body_bottom, x1 - x0, header_top - body_bottom)
    c.line(x0, header_bottom, x1, header_bottom)
    for cx in col_x[1:-1]:
        c.line(cx, body_bottom, cx, header_top)

    # total row
    TOTAL_ROW_H = 22
    total_top = body_bottom
    total_bottom = total_top - TOTAL_ROW_H
    c.rect(x0, total_bottom, x1 - x0, TOTAL_ROW_H)
    c.line(col_x[2], total_bottom, col_x[2], total_top)  # divider before Quantity total
    c.line(col_x[4], total_bottom, col_x[4], total_top)  # divider before Amount total
    _text(c, col_x[2] - 4, total_bottom + 7, "Total", font="Helvetica-Bold", size=9, align="right")
    _text(c, col_x[2] + cols[2][1] - 4, total_bottom + 7, f"{total_qty} nos", font="Helvetica-Bold", size=8.5, align="right")
    _text(c, col_x[4] + cols[4][1] - 4, total_bottom + 7, f"Rs. {fmt_amount(grand_total)}", font="Helvetica-Bold", size=9.5, align="right")

    y = total_bottom

    # ---- amount in words row ----
    WORDS_ROW_H = 34
    words_bottom = y - WORDS_ROW_H
    c.rect(x0, words_bottom, x1 - x0, WORDS_ROW_H)
    _text(c, x0 + 4, y - 11, "Amount Chargeable (in words)", font="Helvetica", size=8)
    _text(c, x1 - 4, y - 11, "E. & O.E", font="Helvetica-Oblique", size=8, align="right")
    _text(c, x0 + 4, y - 25, amount_in_words(grand_total), font="Helvetica-Bold", size=9.5)
    y = words_bottom

    # ---- declaration / signatory row ----
    DECL_H = 75
    decl_bottom = y - DECL_H
    c.rect(x0, decl_bottom, x1 - x0, DECL_H)
    decl_split = x0 + 320
    c.line(decl_split, decl_bottom, decl_split, y)

    dy = y - 11
    _text(c, x0 + 4, dy, "Declaration", font="Helvetica", size=8)
    c.line(x0 + 4, dy - 2, x0 + 4 + stringWidth("Declaration", "Helvetica", 8), dy - 2)
    dy -= 13
    decl_text = (
        "We declare that this invoice shows the actual price of the "
        "goods described and that all particulars are true and correct."
    )
    for wline in _wrap(decl_text, "Helvetica", 8, decl_split - x0 - 8):
        _text(c, x0 + 4, dy, wline, font="Helvetica", size=8)
        dy -= 10.5

    _text(c, x1 - 4, y - 11, f"for {SELLER['name']}", font="Helvetica-Bold", size=8.5, align="right")
    _text(c, x1 - 4, decl_bottom + 16, "Authorised Signatory", font="Helvetica", size=8, align="right")

    y = decl_bottom - 16
    _text(c, page_w / 2, y, "This is a Computer Generated Invoice", font="Helvetica", size=8, align="center")

    c.showPage()
    c.save()
    buf.seek(0)
    return buf
