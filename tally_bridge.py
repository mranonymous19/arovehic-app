"""
Tally bridge -- runs on the PC where TallyPrime is open.

Every few seconds it asks the tracker app (tracker.arovehic.com) for vouchers waiting
for Tally (new sales, credit notes for cancelled / refunded bills), posts each one to
TallyPrime's local XML server (default http://localhost:9000) and reports Tally's answer
back to the app. Nothing on this PC is exposed to the internet: the bridge only makes
outgoing calls.

Needs only Python 3.8+ (no extra packages).

Settings (environment variables, or a file named tally_bridge.env next to this script):
    APP_URL            https://tracker.arovehic.com
    TALLY_BRIDGE_KEY   the same secret you set on the server
    TALLY_URL          http://localhost:9000          (default)
    POLL_SECONDS       15                             (default)

Run:   python tally_bridge.py            (keeps running)
       python tally_bridge.py --test     (checks the app and Tally, then exits)
       python tally_bridge.py --once     (one pass, then exits)
"""

import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from logging.handlers import RotatingFileHandler

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "tally_bridge_state.json")

# Tally answers like this when the company isn't open / the request can't be processed at all.
# That is a "try again later" problem, not a problem with the voucher, so we don't mark the
# voucher as failed.
TRANSIENT_HINTS = ("svcurrentcompany", "company is not loaded", "no company", "unknown request",
                   "could not set", "not open")

log = logging.getLogger("tally_bridge")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    cfg = {}
    path = os.path.join(HERE, "tally_bridge.env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("APP_URL", "TALLY_BRIDGE_KEY", "TALLY_URL", "POLL_SECONDS"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    cfg.setdefault("TALLY_URL", "http://localhost:9000")
    cfg.setdefault("POLL_SECONDS", "15")
    cfg["APP_URL"] = cfg.get("APP_URL", "").rstrip("/")
    return cfg


def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = RotatingFileHandler(os.path.join(HERE, "tally_bridge.log"), maxBytes=1_000_000,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(sh)
    log.addHandler(fh)


# ---------------------------------------------------------------------------
# State: acknowledgements that Tally accepted but the app didn't receive yet.
# They are re-sent to the app WITHOUT posting the voucher to Tally again (no duplicates).
# ---------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
            st.setdefault("unacked", {})
            return st
    except (OSError, ValueError):
        return {"unacked": {}}


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def app_call(cfg, method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        cfg["APP_URL"] + path, data=data, method=method,
        headers={"X-Tally-Key": cfg.get("TALLY_BRIDGE_KEY", ""), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def decode(raw):
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8", errors="replace")


def tally_post(cfg, xml):
    req = urllib.request.Request(
        cfg["TALLY_URL"], data=xml.encode("utf-8"), method="POST",
        headers={"Content-Type": "text/xml; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return decode(r.read())


def tally_alive(cfg):
    try:
        with urllib.request.urlopen(cfg["TALLY_URL"], timeout=8) as r:
            return "tally" in decode(r.read()).lower()
    except (urllib.error.URLError, OSError):
        return False


def _num(text, tag):
    m = re.search(rf"<{tag}>\s*(\d+)\s*</{tag}>", text)
    return int(m.group(1)) if m else 0


def judge(response):
    """Decide what Tally's answer means -> ('ok'|'error'|'transient', message)."""
    errors = [re.sub(r"\s+", " ", e).strip() for e in re.findall(r"<LINEERROR>(.*?)</LINEERROR>", response, re.S)]
    if "<RESPONSE>" not in response and "<CREATED>" not in response:
        snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", response)).strip()[:300]
        return "transient", f"Unexpected answer from Tally: {snippet or '(empty)'}"
    created, altered = _num(response, "CREATED"), _num(response, "ALTERED")
    n_err, ignored = _num(response, "ERRORS"), _num(response, "IGNORED")
    exceptions = _num(response, "EXCEPTIONS")
    if n_err == 0 and exceptions == 0 and (created + altered) >= 1:
        return "ok", f"created {created}"
    detail = "; ".join(errors) if errors else (
        "Tally ignored the voucher (it may already exist)" if ignored else "Tally did not create the voucher")
    if any(h in detail.lower() for h in TRANSIENT_HINTS):
        return "transient", detail
    return "error", detail[:1500]


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------

def send_pending_acks(cfg, st):
    for rid, res in list(st["unacked"].items()):
        try:
            app_call(cfg, "POST", "/api/tally/ack", {"id": int(rid), **res})
            del st["unacked"][rid]
            save_state(st)
            log.info("#%s: acknowledged to the app (retry)", rid)
        except urllib.error.HTTPError as e:
            if e.code == 404:  # app doesn't know that row any more
                del st["unacked"][rid]
                save_state(st)
            else:
                log.warning("#%s: app still not reachable for acknowledgement (%s)", rid, e)
                return False
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("#%s: app still not reachable for acknowledgement (%s)", rid, e)
            return False
    return True


def run_once(cfg):
    st = load_state()
    if not send_pending_acks(cfg, st):
        return
    try:
        items = app_call(cfg, "GET", "/api/tally/pending?limit=20")["items"]
    except urllib.error.HTTPError as e:
        log.error("App refused the request (%s). Check APP_URL and TALLY_BRIDGE_KEY.", e)
        return
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        log.warning("Cannot reach the app: %s", e)
        return
    if not items:
        return
    if not tally_alive(cfg):
        log.warning("%d voucher(s) waiting, but TallyPrime is not reachable at %s. Open TallyPrime, load the "
                    "company and check F1 > Settings > Connectivity (Server, port 9000).", len(items), cfg["TALLY_URL"])
        return

    blocked_orders = set()
    for it in items:
        rid = str(it["id"])
        if it.get("order_id") in blocked_orders:
            continue  # an earlier voucher of this bill failed; keep the order of events
        if rid in st["unacked"]:
            continue
        label = f"#{rid} {it['kind']} {it['voucher_number']}"
        try:
            # Masters (customer ledger, stock items, tax ledgers). Already-existing ones are simply
            # ignored by Tally; only a vanished connection matters here.
            if it.get("masters_xml"):
                tally_post(cfg, it["masters_xml"])
            answer = tally_post(cfg, it["voucher_xml"])
        except (urllib.error.URLError, OSError) as e:
            log.warning("%s: lost contact with Tally (%s); will retry", label, e)
            return
        verdict, msg = judge(answer)
        if verdict == "transient":
            log.warning("%s: Tally could not take it right now (%s); will retry", label, msg)
            return
        result = {"ok": verdict == "ok", "message": msg}
        log.info("%s: %s%s", label, "SENT" if result["ok"] else "REJECTED", "" if result["ok"] else " - " + msg)
        if not result["ok"]:
            blocked_orders.add(it.get("order_id"))
        st["unacked"][rid] = result  # remembered until the app confirms, so it is never posted twice
        save_state(st)
        try:
            app_call(cfg, "POST", "/api/tally/ack", {"id": it["id"], **result})
            del st["unacked"][rid]
            save_state(st)
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("%s: Tally took it, but the app did not get the acknowledgement yet (%s); "
                        "will tell it on the next pass without re-posting", label, e)
            return


def self_test(cfg):
    ok = True
    if not cfg["APP_URL"] or not cfg.get("TALLY_BRIDGE_KEY"):
        print("Set APP_URL and TALLY_BRIDGE_KEY (see tally_bridge.env.example).")
        return 1
    try:
        n = len(app_call(cfg, "GET", "/api/tally/pending?limit=1")["items"])
        print(f"App: OK ({cfg['APP_URL']}), {n} voucher(s) waiting")
    except Exception as e:
        ok = False
        print(f"App: FAILED - {e}")
    if tally_alive(cfg):
        print(f"Tally: OK ({cfg['TALLY_URL']})")
    else:
        ok = False
        print(f"Tally: NOT reachable at {cfg['TALLY_URL']} - open TallyPrime with the company loaded and "
              "enable the server (F1 > Settings > Connectivity > TallyPrime acts as Server, port 9000).")
    return 0 if ok else 1


def main():
    setup_logging()
    cfg = load_config()
    if "--test" in sys.argv:
        sys.exit(self_test(cfg))
    if not cfg["APP_URL"] or not cfg.get("TALLY_BRIDGE_KEY"):
        log.error("APP_URL and TALLY_BRIDGE_KEY are not set (see tally_bridge.env.example).")
        sys.exit(1)
    if "--once" in sys.argv:
        run_once(cfg)
        return
    log.info("Tally bridge started: %s  ->  %s", cfg["APP_URL"], cfg["TALLY_URL"])
    poll = max(3, int(cfg["POLL_SECONDS"]))
    while True:
        try:
            run_once(cfg)
        except Exception:  # never let one bad pass kill the bridge
            log.exception("Unexpected error in bridge pass")
        time.sleep(poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
