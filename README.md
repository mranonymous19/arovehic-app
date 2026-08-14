# Shopify Purchase Tracker

A small Flask app that lists your Shopify order line items and lets you mark
each one **Pending / Purchased / In Stock / N/A**. Statuses are saved in a
Postgres database (free tier on Supabase), so they persist across deploys
and restarts.

Shopify itself is never called directly by this app — an n8n workflow does
that part (since you already have n8n on your VPS), and this app just calls
one n8n webhook to pull orders in.

The app is login-protected with three roles:

- **Owner** — full control: sync from Shopify, edit settings, update item
  status, manage user accounts.
- **Staff** — can update item status only.
- **Telecaller** — view-only.

First login uses a default account created automatically the first time the
app starts:

```
username: owner
password: changeme123
```

Log in, go to **Account** and change that password, then use **Manage
Users** (owner-only, top right) to add accounts for your team.

## 1. How data flows

```
Shopify  -->  n8n workflow (Shopify node + Webhook)  -->  Flask app (Render)  -->  Supabase Postgres
```

- n8n holds your Shopify credentials and does the actual API call.
- This app calls the n8n webhook, stores/updates orders and line items, and
  remembers the status you set on each item — re-syncing won't reset a
  status you already changed, it only adds new orders/items and refreshes
  titles/prices/quantities.

## 2. Set up the n8n workflow

1. In n8n, create a new workflow.
2. Add a **Webhook** node. Set the HTTP Method to `GET`, give it a path like
   `shopify-orders`, and add a **Respond to Webhook** node at the end of the
   workflow.
3. Add a **Shopify** node (or an **HTTP Request** node if you'd rather call
   the REST API directly — `GET /admin/api/2024-01/orders.json?status=any`).
   Connect it to your Shopify store: either use n8n's built-in Shopify
   credential type (store domain + Admin API access token) or, for the HTTP
   Request option, add the token as an `X-Shopify-Access-Token` header.
4. Add a **Code** node between the Shopify node and the Respond to Webhook
   node, to reshape the data into what this app expects:

   ```javascript
   return items.map(item => {
     const order = item.json;
     return {
       json: {
         id: order.id,
         name: order.name,
         customer_name: order.customer
           ? `${order.customer.first_name || ""} ${order.customer.last_name || ""}`.trim()
           : "",
         created_at: order.created_at,
         // Customer's shipping address — stored as-is so the app can pull
         // out address lines, city, state, and pincode. This is also what
         // will feed the invoice PDF's "ship to" block once that's added.
         shipping_address: order.shipping_address || {},
         // Fallback phone in case shipping_address has none (e.g. a
         // digital/no-shipping order) — the app tries shipping_address.phone
         // first, then this.
         phone: order.phone || (order.customer ? order.customer.phone : "") || "",
         line_items: (order.line_items || []).map(li => ({
           id: li.id,
           title: li.title,
           variant_title: li.variant_title,
           quantity: li.quantity,
           price: li.price,
           vendor: li.vendor,
         })),
       },
     };
   });
   ```

   `order.shipping_address` from Shopify already comes shaped as
   `{ address1, address2, city, province, zip, phone, ... }` — the app reads
   exactly those keys, so passing it straight through (rather than picking
   individual fields) is the simplest way to get the full address across.

5. In the **Respond to Webhook** node, set the response body to the Code
   node's output (as JSON, an array of orders).
6. Save and activate the workflow. Copy the **Production URL** shown on the
   Webhook node (it'll look like `https://n8n.yourdomain.com/webhook/shopify-orders`)
   — that's what you'll paste into this app's Settings panel later.
7. Test it by opening that URL directly in a browser; you should see a JSON
   array of orders.

If you'd rather I generate the actual n8n workflow JSON file (importable
directly), send me your Shopify store's domain and confirm you're using the
Shopify node vs. a raw HTTP Request, and I'll put that file together too.

**Already have this workflow set up?** Open your existing Code node, replace
it with the snippet above, save, and re-activate the workflow — then click
**Sync from Shopify** in the app again. Sync always refreshes an order's
details (including address) even for orders it's already seen, so this
backfills shipping addresses onto orders you synced before this change,
without duplicating anything or touching item statuses you've already set.

## 3. Create a free Supabase Postgres database

1. Go to [supabase.com](https://supabase.com), sign up, and create a new
   project (pick any name/region, set a database password and save it).
2. Once the project is ready, go to **Project Settings -> Database**.
3. Under **Connection string**, copy the **URI** — use the **Transaction
   pooler** connection string (port 6543), which works better with
   short-lived connections like this app makes. It looks like:
   ```
   postgresql://postgres.xxxxxxxx:[YOUR-PASSWORD]@aws-0-xx-xxxx-1.pooler.supabase.com:6543/postgres
   ```
4. Replace `[YOUR-PASSWORD]` with the database password you set in step 1.
   This full string is your `DATABASE_URL`.

You don't need to create any tables yourself — the app creates `settings`,
`orders`, `items`, and `users` tables automatically the first time it starts.

## 4. Run it locally first (optional but recommended)

The easiest way is the launcher script — it installs dependencies and
starts the app for you, and prints a link you can open on your phone too
(as long as the phone is on the **same WiFi** as this computer):

1. Copy `.env.example` to `.env` and fill in your Supabase `DATABASE_URL`
   and a `SECRET_KEY` (any long random string — it just signs login
   session cookies).
2. **Windows:** double-click `start_windows.bat`.
   **Mac/Linux:** run `./start_mac_linux.sh` (or `bash start_mac_linux.sh`).
3. You'll see something like:

   ```
   ============================================================
     Shopify Purchase Tracker is running!
   ============================================================
     On THIS PC, open:       http://127.0.0.1:5050
     On phones (same WiFi):  http://192.168.1.5:5050
   ============================================================
   ```

   - On this computer: open the first (`127.0.0.1`) link in a browser.
   - On your phone: connect to the **same WiFi network** as this computer,
     open the second (`192.168.x.x`) link, and bookmark it.
4. Log in with the default owner account (see above), click **Settings**,
   paste in your n8n webhook URL, save, then click **Sync from Shopify**.

To stop the server, close the terminal window or press `Ctrl+C`.

Prefer doing it by hand instead of the script? That works too:

```bash
cd shopify-tracker
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL="postgresql://...your Supabase URI..."
export SECRET_KEY="any-long-random-string"   # signs login session cookies
python app.py
```

Note: since `DATABASE_URL` points at your cloud Supabase database (not a
local file), the app works the same whether you're running it locally via
the `.bat`/`.sh` script or deployed on Render — your data is shared
between both.

## 5. Deploy on Render

1. Push this folder to a GitHub repo (Render deploys from a repo, or you can
   drag-and-drop via their dashboard if you don't want to use git).
2. Go to [render.com](https://render.com), sign up, click **New -> Web
   Service**, and connect the repo.
3. Render should auto-detect the `render.yaml` in this folder and pre-fill
   the build command (`pip install -r requirements.txt`) and start command
   (`gunicorn app:app`). It will also generate a random `SECRET_KEY` for
   you automatically. If it doesn't pick up `render.yaml` automatically,
   set the build/start commands manually and choose the **Free** plan.
4. Under **Environment**, add an environment variable:
   - Key: `DATABASE_URL`
   - Value: your Supabase connection string from step 3 above
5. Click **Create Web Service**. Render will build and deploy — first
   deploy takes a couple of minutes.
6. Open the URL Render gives you (something like
   `https://shopify-purchase-tracker.onrender.com`).
7. In the app, go to **Settings**, paste your n8n webhook URL, save, and
   click **Sync from Shopify**.

Note: Render's free tier spins the service down after 15 minutes of no
traffic, so the first request after idle time takes 30–60 seconds to wake
up — that's normal, just a free-tier trade-off. Your data is safe either
way since it lives in Supabase, not on Render's disk.

## 6. Day-to-day use

- **Sync from Shopify** (owner only) pulls in any new orders/items from n8n.
  It never overwrites a status you've already set on an existing item.
- Click a status pill (Pending / Purchased / In Stock / N/A) on any line
  item to change it — saved immediately. Owner and Staff accounts can do
  this; Telecaller accounts see the pills but can't change them.
- Use the filter bar at the top to show only orders containing items of a
  given status (handy for "show me everything still Pending").
- **Manage Users** (owner only, top right) adds/removes accounts and resets
  passwords. **Account** (everyone) changes your own password.

## 7. Files

- `app.py` — Flask backend + Postgres storage (via `DATABASE_URL`) +
  login/role-based access control
- `templates/index.html`, `templates/login.html`, `static/app.js`,
  `static/style.css` — frontend
- `requirements.txt` — Python dependencies
- `render.yaml` — Render deployment config
- `start_windows.bat`, `start_mac_linux.sh` — one-click local launchers
  that install dependencies, start the app, and print a phone-friendly
  LAN link (see section 4)
- `.env.example` — template for the `.env` file used by the launchers
