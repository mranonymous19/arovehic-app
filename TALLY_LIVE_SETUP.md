# Tally live push - setup

Sales go to Tally when an invoice is first printed. Cancelled bills / refunded items that are
already in Tally go as **Credit Notes (Sales Return)** automatically.

## 1. Server (your VPS)
1. Copy `app.py`, `tally_export.py`, `tally_outbox.py` into the project (replace the old two).
2. Add to the app's `.env` (see `.env.example`): `TALLY_LIVE_PUSH=1`, `TALLY_BRIDGE_KEY` (make one with
   `openssl rand -hex 24`), `TALLY_COMPANY_NAME` (exactly as Tally shows it), `TALLY_CN_START`.
3. Rebuild/restart the container as you did for earlier updates. The `tally_outbox` table is created on start.
4. Log in as owner/accounts and open **/tally**. Set "Next credit note number" above your last CR number in Tally.

## 2. The Tally PC
1. Install Python 3.8+ (python.org, tick "Add python.exe to PATH").
2. Put `tally_bridge.py` and `start_tally_bridge.bat` in a folder; copy `tally_bridge.env.example` to
   `tally_bridge.env` and fill in the same key.
3. Open TallyPrime with the company loaded (server is already ON, port 9000).
4. `python tally_bridge.py --test` -> both lines should say OK. Then double-click `start_tally_bridge.bat`.
   To start with Windows: press Win+R, type `shell:startup`, put a shortcut to the .bat there.
5. /tally shows "Bridge on the Tally PC: running".

## 3. First test (use a test company first if you can: set TALLY_COMPANY_NAME to it)
Print one bill -> /tally shows a Sale, status Sent -> check Tally's Day Book. Cancel a test bill -> Credit Note CR/n/yy.

## 4. Fix bills that are cancelled but still in Sales
/tally -> "Check now". Review, untick any you already credited by hand (e.g. #9893 has CR/1/26), choose a
credit note date if needed, then "Create credit notes". Rows whose total no longer matches the printed invoice
are unticked for you to check first.

## Rules
| Event (bill already in Tally) | What happens |
|---|---|
| Bill / order cancelled | Credit Note for the whole bill (items, GST, delivery, round-off) |
| Item marked Refunded | Credit Note for that item only; the last one also reverses delivery + round-off |
| Cancelled before the bridge sent it | Sale is simply withdrawn - no sale, no credit note |
| Cancel undone before credit note was sent | Credit note withdrawn |
| Cancel undone after credit note was sent | Warning only - enter a fresh sale in Tally by hand |
| Tally rejects a voucher | Status Error with Tally's message on /tally; fix, then Retry |

## Not covered
Edits made to a bill after it was pushed are not sent to Tally (the activity log says so). Payment-type
number swaps and Repurchase (RPR) invoices are not pushed. Credit notes are dated today (IST) unless chosen.
Manual alternative to the bridge: /api/tally/outbox.xml (add ?mark=1 to mark rows sent).
