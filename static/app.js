const STATUS_LABELS = {
  pending: "Pending",
  purchased: "Purchased",
  stock: "In Stock",
  na: "N/A",
  refunded: "Refunded",
  wait: "Wait",
};

const ROLE_LABELS = {
  owner: "Owner",
  staff: "Staff",
  telecaller: "Telecaller",
  packer: "Packer",
  accounts: "Accounts",
};

let currentFilter = "";
let currentPaymentFilter = "";
let currentInvoiceFilter = "";
let currentTallyFilter = "";
let currentDateFrom = "";
let currentDateTo = "";
let currentRole = null;
let currentName = "";
let lastLoadedOrders = [];
let orderTrackQuery = "";
// Billing bulk PDF: order_ids ticked for the combined invoice PDF
const bulkSelected = new Set();
// true while "Select all" is ticked: every filter view (Printed, Not printed...)
// then selects all of its own bills.
let bulkAllMode = false;
let lastRenderedOrders = [];

const ordersContainer = document.getElementById("ordersContainer");
const orderTrackInput = document.getElementById("orderTrackInput");
const syncBtn = document.getElementById("syncBtn");
const syncMsg = document.getElementById("syncMsg");
const settingsBtn = document.getElementById("settingsBtn");
const settingsModal = document.getElementById("settingsModal");
const webhookInput = document.getElementById("webhookInput");
const saveSettingsBtn = document.getElementById("saveSettingsBtn");
const closeSettingsBtn = document.getElementById("closeSettingsBtn");
const statusFilterSelect = document.getElementById("statusFilterSelect");
const paymentFilterButtons = document.querySelectorAll(".filter-btn[data-payment]");
const invoiceFilterRow = document.getElementById("invoiceFilterRow");
const invoiceFilterSelect = document.getElementById("invoiceFilterSelect");
const tallyFilterSelect = document.getElementById("tallyFilterSelect");
const billingResultCount = document.getElementById("billingResultCount");
const dateFromInput = document.getElementById("dateFromInput");
const dateToInput = document.getElementById("dateToInput");
const clearDateFilterBtn = document.getElementById("clearDateFilterBtn");
const exportPendingBtn = document.getElementById("exportPendingBtn");
const tallyExportGroup = document.getElementById("tallyExportGroup");
const exportTallyXmlBtn = document.getElementById("exportTallyXmlBtn");
const tallyOnlyNew = document.getElementById("tallyOnlyNew");

const codThresholdInput = document.getElementById("codThresholdInput");
const codStaffList = document.getElementById("codStaffList");
const prepaidStaffList = document.getElementById("prepaidStaffList");
const scheduleError = document.getElementById("scheduleError");
const saveScheduleBtn = document.getElementById("saveScheduleBtn");

const pasteOrderBtn = document.getElementById("pasteOrderBtn");
const pasteOrderModal = document.getElementById("pasteOrderModal");
const pasteOrderError = document.getElementById("pasteOrderError");
const submitPasteOrderBtn = document.getElementById("submitPasteOrderBtn");
const closePasteOrderBtn = document.getElementById("closePasteOrderBtn");
const moOrderId = document.getElementById("moOrderId");
const moCustomerName = document.getElementById("moCustomerName");
const moPhone = document.getElementById("moPhone");
const moPaymentType = document.getElementById("moPaymentType");
const moAddress1 = document.getElementById("moAddress1");
const moAddress2 = document.getElementById("moAddress2");
const moCity = document.getElementById("moCity");
const moState = document.getElementById("moState");
const moPincode = document.getElementById("moPincode");
const moShippingAmount = document.getElementById("moShippingAmount");
const moBalanceDue = document.getElementById("moBalanceDue");
const moItemsList = document.getElementById("moItemsList");
const moAddItemBtn = document.getElementById("moAddItemBtn");
const moTotalDisplay = document.getElementById("moTotalDisplay");

const userBadge = document.getElementById("userBadge");
const accountBtn = document.getElementById("accountBtn");
const accountModal = document.getElementById("accountModal");
const accountName = document.getElementById("accountName");
const accountRole = document.getElementById("accountRole");
const currentPasswordInput = document.getElementById("currentPasswordInput");
const newPasswordInput = document.getElementById("newPasswordInput");
const accountError = document.getElementById("accountError");
const savePasswordBtn = document.getElementById("savePasswordBtn");
const closeAccountBtn = document.getElementById("closeAccountBtn");

const usersBtn = document.getElementById("usersBtn");
const trashBtn = document.getElementById("trashBtn");
const repurchaseBtn = document.getElementById("repurchaseBtn");
const cancelledBtn = document.getElementById("cancelledBtn");
const refundedFilterOption = document.getElementById("refundedFilterOption");
const usersModal = document.getElementById("usersModal");
const usersTableBody = document.getElementById("usersTableBody");
const newUserName = document.getElementById("newUserName");
const newUserPassword = document.getElementById("newUserPassword");
const newUserRole = document.getElementById("newUserRole");
const usersError = document.getElementById("usersError");
const addUserBtn = document.getElementById("addUserBtn");
const closeUsersBtn = document.getElementById("closeUsersBtn");

const activityLogBtn = document.getElementById("activityLogBtn");
const activityLogModal = document.getElementById("activityLogModal");
const activityLogSearch = document.getElementById("activityLogSearch");
const activityLogList = document.getElementById("activityLogList");
const closeActivityLogBtn = document.getElementById("closeActivityLogBtn");

function showMessage(text, isError = false) {
  syncMsg.textContent = text;
  syncMsg.hidden = false;
  syncMsg.classList.toggle("error", isError);
  setTimeout(() => { syncMsg.hidden = true; }, 5000);
}

// ---------------------------------------------------------------------------
// Current user / role-based UI
// ---------------------------------------------------------------------------

async function loadMe() {
  const res = await fetch("/api/me");
  if (!res.ok) return; // not logged in — the page itself redirects to /login
  const me = await res.json();
  currentRole = me.role;
  currentName = me.name;

  userBadge.textContent = `${currentName} · ${ROLE_LABELS[currentRole] || currentRole}`;
  accountName.textContent = currentName;
  accountRole.textContent = ROLE_LABELS[currentRole] || currentRole;

  const isOwner = currentRole === "owner";
  syncBtn.hidden = !(isOwner || currentRole === "staff");
  loadSyncHealth();
  pasteOrderBtn.hidden = !(isOwner || currentRole === "telecaller");
  settingsBtn.hidden = !isOwner;
  usersBtn.hidden = !isOwner;
  activityLogBtn.hidden = !(isOwner || currentRole === "telecaller");
  trashBtn.hidden = !(isOwner || currentRole === "telecaller");
  repurchaseBtn.hidden = false; // every role can open it; what they can do inside is role-limited
  cancelledBtn.hidden = !(isOwner || currentRole === "telecaller");
  // Accounts/Packer are locked to a single Billing-only view already — an
  // arrived/pending breakdown isn't meaningful there, so the Summary
  // button is only shown to the roles that see the full order flow.
  summaryBtn.hidden = !(isOwner || currentRole === "staff" || currentRole === "telecaller");
  // Telecaller is otherwise view-only everywhere (every edit endpoint is
  // gated server-side too), but gets the same visibility as the owner so
  // they can pull up any order's full status while on a call.
  refundedFilterOption.hidden = !(isOwner || currentRole === "telecaller");

  if (currentRole === "accounts") {
    // Accounts only ever needs the Billing view (what's ready to invoice,
    // and whether it's been printed yet) — lock the dropdown to Billing
    // and disable it, enforced server-side too in /api/orders.
    currentFilter = "billing";
    statusFilterSelect.value = "billing";
    statusFilterSelect.disabled = true;
    invoiceFilterRow.hidden = false;
  }

  if (currentRole === "staff") {
    // Purchase staff work item-by-item off a single unified list (each
    // item has its own status control right in the order row) — none of
    // the Status/Invoice/Tally dropdowns are part of that workflow, so
    // hide all three and just show every order, enforced server-side too
    // in /api/orders (status=billing/closed is dropped for this role).
    document.querySelector('label[for="statusFilterSelect"]').hidden = true;
    statusFilterSelect.hidden = true;
    statusFilterSelect.disabled = true;
    currentFilter = "";
    statusFilterSelect.value = "";
  }

  updateTallyExportVisibility();

  if (currentRole === "packer") {
    // A packer only ever needs Billing orders whose invoice has already
    // been printed — that's the signal packing can start. Lock the
    // dropdown to Billing + Printed and hide the sub-filter entirely,
    // enforced server-side too in /api/orders so it can't be bypassed.
    currentFilter = "billing";
    currentInvoiceFilter = "printed";
    statusFilterSelect.value = "billing";
    statusFilterSelect.disabled = true;
    invoiceFilterSelect.value = "printed";
    invoiceFilterRow.hidden = true;
  }
}

function canEditStatus() {
  if (currentFilter === "trash") return false;
  return currentRole === "owner" || currentRole === "staff";
}

function canTogglePacked() {
  if (currentFilter === "trash") return false;
  return currentRole === "owner" || currentRole === "packer";
}

// ---------------------------------------------------------------------------
// Orders
// ---------------------------------------------------------------------------

let loadOrdersSeq = 0;
const FIRST_PAGE = 100;

async function fetchOrders(extra) {
  const params = new URLSearchParams();
  if (currentFilter) params.set("status", currentFilter);
  if (currentPaymentFilter) params.set("payment", currentPaymentFilter);
  if (currentDateFrom) params.set("date_from", currentDateFrom);
  if (currentDateTo) params.set("date_to", currentDateTo);
  if (currentFilter === "billing" && currentInvoiceFilter) params.set("invoice", currentInvoiceFilter);
  if (currentFilter === "billing" && currentTallyFilter) params.set("tally", currentTallyFilter);
  for (const [k, v] of Object.entries(extra || {})) params.set(k, v);
  const qs = params.toString();
  const res = await fetch(qs ? `/api/orders?${qs}` : "/api/orders");
  const data = await res.json();
  return { data, truncated: res.headers.get("X-Truncated") === "1" };
}

async function loadOrders() {
  const seq = ++loadOrdersSeq;
  const first = await fetchOrders({ limit: FIRST_PAGE });
  if (seq !== loadOrdersSeq) return;
  lastLoadedOrders = first.data;
  renderOrders(filterByTrackQuery(first.data));
  if (!first.truncated) return;
  fetchOrders().then((all) => {
    if (seq !== loadOrdersSeq) return;
    lastLoadedOrders = all.data;
    renderOrders(filterByTrackQuery(all.data));
  });
}

// ---------------------------------------------------------------------------
// Summary modal (order counts, date-range filterable)
// ---------------------------------------------------------------------------

const summaryBtn = document.getElementById("summaryBtn");
const summaryModal = document.getElementById("summaryModal");
const closeSummaryBtn = document.getElementById("closeSummaryBtn");
const summaryRangeButtons = document.querySelectorAll(".filter-btn[data-range]");
const summaryCustomRange = document.getElementById("summaryCustomRange");
const summaryFromInput = document.getElementById("summaryFromInput");
const summaryToInput = document.getElementById("summaryToInput");
const summaryOrderIdFromInput = document.getElementById("summaryOrderIdFromInput");
const summaryOrderIdToInput = document.getElementById("summaryOrderIdToInput");
const summaryApplyCustomBtn = document.getElementById("summaryApplyCustomBtn");
const summaryRangeLabel = document.getElementById("summaryRangeLabel");
const summaryArrivedVal = document.getElementById("summaryArrivedVal");
const summaryBillingVal = document.getElementById("summaryBillingVal");
const summaryPendingVal = document.getElementById("summaryPendingVal");
const summaryOrderListEls = {
  arrived: document.getElementById("summaryArrivedList"),
  billing: document.getElementById("summaryBillingList"),
  pending: document.getElementById("summaryPendingList"),
};
const summaryViewOrdersButtons = document.querySelectorAll(".summary-view-orders-btn");
let lastSummaryOrders = { arrived: [], billing: [], pending: [] };

let currentSummaryRange = "today";

function localDateStr(date) {
  // Local (browser) date, not UTC — so "today"/"yesterday" match the
  // calendar date for whoever is looking at the screen.
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function summaryRangeDates(range) {
  const today = new Date();
  if (range === "yesterday") {
    const y = new Date(today);
    y.setDate(y.getDate() - 1);
    return { from: localDateStr(y), to: localDateStr(y) };
  }
  if (range === "7days") {
    const from = new Date(today);
    from.setDate(from.getDate() - 6); // last 7 days inclusive of today
    return { from: localDateStr(from), to: localDateStr(today) };
  }
  if (range === "custom") {
    return {
      from: summaryFromInput.value,
      to: summaryToInput.value,
      orderIdFrom: summaryOrderIdFromInput.value.trim(),
      orderIdTo: summaryOrderIdToInput.value.trim(),
    };
  }
  // "today"
  return { from: localDateStr(today), to: localDateStr(today) };
}

function summaryRangeLabelText(range, { from, to, orderIdFrom, orderIdTo }) {
  if (range === "today") return "Today";
  if (range === "yesterday") return "Yesterday";
  if (range === "7days") return "Last 7 days";
  // custom
  const parts = [];
  if (from && to) parts.push(from === to ? from : `${from} to ${to}`);
  if (orderIdFrom && orderIdTo) parts.push(`Order #${orderIdFrom}–#${orderIdTo}`);
  return parts.length ? parts.join(" · ") : "Pick a date range and/or an Order ID range.";
}

async function loadSummary(range) {
  const { from, to, orderIdFrom, orderIdTo } = summaryRangeDates(range);

  if (range === "custom") {
    const hasDateRange = !!(from || to);
    const hasOrderIdRange = !!(orderIdFrom || orderIdTo);
    if (hasDateRange && (!from || !to)) {
      summaryRangeLabel.textContent = "Fill in both a from and to date, or leave both blank.";
      return;
    }
    if (hasOrderIdRange && (!orderIdFrom || !orderIdTo)) {
      summaryRangeLabel.textContent = "Fill in both an Order ID from and to, or leave both blank.";
      return;
    }
    if (!hasDateRange && !hasOrderIdRange) {
      summaryRangeLabel.textContent = "Enter a date range and/or an Order ID range.";
      return;
    }
  }

  summaryRangeLabel.textContent = summaryRangeLabelText(range, { from, to, orderIdFrom, orderIdTo });

  const params = new URLSearchParams();
  if (from) params.set("date_from", from);
  if (to) params.set("date_to", to);
  if (orderIdFrom) params.set("order_id_from", orderIdFrom);
  if (orderIdTo) params.set("order_id_to", orderIdTo);

  try {
    const res = await fetch(`/api/orders/summary?${params.toString()}`);
    if (!res.ok) return;
    const data = await res.json();
    summaryArrivedVal.textContent = data.arrived;
    summaryBillingVal.textContent = data.billing;
    summaryPendingVal.textContent = data.pending;
    lastSummaryOrders = {
      arrived: data.arrived_orders || [],
      billing: data.billing_orders || [],
      pending: data.pending_orders || [],
    };
    // Re-render any order list that's currently open (or collapse+relabel
    // it) so it doesn't keep showing stale numbers from the previous
    // range after Apply/quick-filter is clicked again.
    Object.keys(summaryOrderListEls).forEach((bucket) => {
      const listEl = summaryOrderListEls[bucket];
      if (!listEl.hidden) renderSummaryOrderList(bucket);
    });
  } catch (err) {
    summaryRangeLabel.textContent = "Could not load the summary.";
  }
}

function renderSummaryOrderList(bucket) {
  const listEl = summaryOrderListEls[bucket];
  const orders = lastSummaryOrders[bucket] || [];
  listEl.innerHTML = orders.length
    ? orders.map((name) => `<button type="button" class="summary-order-chip" data-order-name="${escapeHtml(name)}">${escapeHtml(name)}</button>`).join("")
    : `<span class="summary-order-list-empty">None in this range.</span>`;
}

// Clicking an order number inside the summary breakdown (arrived/billing/
// pending) jumps straight to that order in the main list below, instead of
// making someone copy the number and paste it into the search box
// themselves. One delegated listener covers all three lists since they're
// re-rendered on every open/range change.
Object.values(summaryOrderListEls).forEach((listEl) => {
  listEl.addEventListener("click", (e) => {
    const chip = e.target.closest(".summary-order-chip");
    if (!chip) return;
    goToOrder(chip.dataset.orderName);
  });
});

function goToOrder(orderName) {
  if (!orderName) return;

  // Jumping to a specific order means seeing it regardless of which
  // status/date/payment filter happens to be active, so clear anything
  // that could hide it — then search for just this order number.
  summaryModal.hidden = true;
  currentFilter = "";
  statusFilterSelect.value = "";
  statusFilterSelect.disabled = false;
  invoiceFilterRow.hidden = true;
  currentInvoiceFilter = "";
  currentPaymentFilter = "";
  paymentFilterButtons.forEach((b) => b.classList.toggle("active", b.dataset.payment === ""));
  currentDateFrom = "";
  currentDateTo = "";
  dateFromInput.value = "";
  dateToInput.value = "";
  updateClearDateFilterVisibility();
  exportPendingBtn.hidden = true;
  updateTallyExportVisibility();

  orderTrackQuery = orderName;
  orderTrackInput.value = orderName;

  loadOrders().then(() => {
    const card = ordersContainer.querySelector(
      `.order-card[data-order-name="${CSS.escape(orderName)}"]`
    );
    if (card) {
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      card.classList.add("order-card-jumped");
      setTimeout(() => card.classList.remove("order-card-jumped"), 2000);
    }
  });
}

summaryViewOrdersButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    const bucket = btn.dataset.bucket;
    const listEl = summaryOrderListEls[bucket];
    const opening = listEl.hidden;
    if (opening) renderSummaryOrderList(bucket);
    listEl.hidden = !opening;
    btn.textContent = opening ? "Hide orders" : "View orders";
  });
});

function collapseSummaryOrderLists() {
  summaryViewOrdersButtons.forEach((btn) => {
    summaryOrderListEls[btn.dataset.bucket].hidden = true;
    btn.textContent = "View orders";
  });
}

summaryBtn.addEventListener("click", () => {
  summaryModal.hidden = false;
  collapseSummaryOrderLists();
  loadSummary(currentSummaryRange);
});
closeSummaryBtn.addEventListener("click", () => { summaryModal.hidden = true; });

summaryRangeButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    summaryRangeButtons.forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    currentSummaryRange = btn.dataset.range;
    summaryCustomRange.hidden = currentSummaryRange !== "custom";
    collapseSummaryOrderLists();
    if (currentSummaryRange !== "custom") loadSummary(currentSummaryRange);
  });
});

summaryApplyCustomBtn.addEventListener("click", () => {
  collapseSummaryOrderLists();
  loadSummary("custom");
});

function filterByTrackQuery(orders) {
  const q = orderTrackQuery.trim().toLowerCase();
  let result = orders;
  if (q) {
    result = result.filter((order) => {
      return (
        (order.order_name || "").toLowerCase().includes(q) ||
        (order.customer_name || "").toLowerCase().includes(q) ||
        (order.order_id || "").toLowerCase().includes(q)
      );
    });
  }
  if (currentFilter === "billing" && currentInvoiceFilter) {
    result = result.filter((order) =>
      currentInvoiceFilter === "printed" ? !!order.invoice_number
        : currentInvoiceFilter === "bill_cancelled" ? !!order.bill_cancelled
        : !order.invoice_number
    );
  }
  if (currentFilter === "billing" && currentTallyFilter) {
    result = result.filter((order) =>
      currentTallyFilter === "exported" ? !!order.tally_exported_at : !order.tally_exported_at
    );
  }
  return result;
}

let orderTrackDebounce;
orderTrackInput.addEventListener("input", () => {
  clearTimeout(orderTrackDebounce);
  orderTrackDebounce = setTimeout(() => {
    orderTrackQuery = orderTrackInput.value;
    renderOrders(filterByTrackQuery(lastLoadedOrders));
  }, 200);
});

async function deleteOrder(orderId, orderName) {
  const ok = confirm(`Move order ${orderName || orderId} to Trash? You can restore it later from Trash if needed.`);
  if (!ok) return;
  const res = await fetch(`/api/orders/${encodeURIComponent(orderId)}`, { method: "DELETE" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    showMessage(data.error || "Could not delete the order.", true);
    return;
  }
  lastLoadedOrders = lastLoadedOrders.filter((o) => o.order_id !== orderId);
  renderOrders(filterByTrackQuery(lastLoadedOrders));
  showMessage(`Moved ${orderName || orderId} to Trash.`);
}

async function restoreOrder(orderId, orderName) {
  const res = await fetch(`/api/orders/${encodeURIComponent(orderId)}/restore`, { method: "POST" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    showMessage(data.error || "Could not restore the order.", true);
    return;
  }
  lastLoadedOrders = lastLoadedOrders.filter((o) => o.order_id !== orderId);
  renderOrders(filterByTrackQuery(lastLoadedOrders));
  showMessage(`Restored ${orderName || orderId}.`);
}

async function printInvoice(orderId, buttonEl, preOpenedTab) {
  // Open the tab synchronously, inside the click handler, so browsers don't
  // treat it as an unrequested popup — we fill in its location once the
  // fetch below resolves.
  const tab = preOpenedTab || window.open("", "_blank");
  const originalLabel = buttonEl.textContent;
  buttonEl.disabled = true;
  buttonEl.textContent = "Printing…";
  try {
    const res = await fetch(`/api/orders/${encodeURIComponent(orderId)}/invoice.pdf`);
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      if (tab) tab.close();
      showMessage(data.error || "Could not generate the invoice.", true);
      return;
    }
    const invoiceNumber = res.headers.get("X-Invoice-Number");
    const invoicePrintedBy = res.headers.get("X-Invoice-Printed-By");
    const blob = await res.blob();
    const blobUrl = URL.createObjectURL(blob);
    if (tab) tab.location = blobUrl;

    // Update this order's badge/button right now, from what this same
    // request just told us — no page refresh, no waiting for the next
    // full order-list reload.
    if (invoiceNumber) {
      const order = lastLoadedOrders.find((o) => o.order_id === orderId);
      if (order) {
        order.invoice_number = invoiceNumber;
        if (invoicePrintedBy) order.invoice_printed_by = invoicePrintedBy;
      }
      renderOrders(filterByTrackQuery(lastLoadedOrders));
    }
  } catch (err) {
    if (tab) tab.close();
    showMessage("Could not reach the server to print the invoice.", true);
  } finally {
    buttonEl.disabled = false;
    buttonEl.textContent = originalLabel;
  }
}

// The list shows ORDERS_PAGE orders at first and adds the next ORDERS_PAGE as you
// scroll down. Showing all 2000+ cards at once made the page huge (300k+ elements),
// which can crash a Chrome tab. Search, filters, counts, Select all and PDF still
// work on ALL orders — only the drawing on screen is done in steps.
const ORDERS_PAGE = 100;
let renderState = { orders: [], shown: 0 };
let ordersMoreEl = null;
let ordersMoreObserver = null;

function renderOrders(orders) {
  // Re-drawing the same list while scrolled down (e.g. after Print Invoice) keeps
  // as many cards as before, so the page doesn't jump back up.
  const keepShown = window.scrollY > ordersContainer.offsetTop ? renderState.shown : 0;
  // Remember the order card at the top of the screen, to put it back in the same spot.
  let anchor = null;
  if (keepShown) {
    for (const c of ordersContainer.children) {
      if (!c.classList.contains("order-card")) continue;
      const r = c.getBoundingClientRect();
      if (r.bottom > 80) { anchor = { name: c.dataset.orderName, top: r.top }; break; }
    }
  }
  renderState = { orders: [], shown: 0 };
  ordersContainer.innerHTML = "";
  lastRenderedOrders = orders;
  if (bulkAllMode) {
    bulkSelected.clear();
    for (const o of orders) if (!o.bill_cancelled) bulkSelected.add(o.order_id);
  } else if (bulkSelected.size) {
    // Selection follows what's on the page: when a filter (Invoice, Tally, search,
    // payment...) hides an order, it is unticked too, so PDF (n) = ticked orders shown.
    const shownIds = new Set(orders.map((o) => o.order_id));
    for (const id of [...bulkSelected]) if (!shownIds.has(id)) bulkSelected.delete(id);
  }

  // Running count of whatever's currently in view in Billing — most useful
  // with the Invoice filter set to "Not printed" (or Tally set to "Not
  // exported"): print/export them one by one and watch this count run down
  // to 0, so you can tell for certain when you've caught every last one.
  if (currentFilter === "billing") {
    billingResultCount.textContent = orders.length === 1 ? "1 order" : `${orders.length} orders`;
  } else {
    billingResultCount.textContent = "";
  }

  if (!orders.length) {
    updateBulkUI();
    if (orderTrackQuery.trim()) {
      ordersContainer.innerHTML = `
        <div class="empty-state empty-search">
          <div class="glyph">No match</div>
          <p>No order matches "${escapeHtml(orderTrackQuery.trim())}".</p>
        </div>`;
      return;
    }
    ordersContainer.innerHTML = `
      <div class="empty-state">
        <div class="glyph">All caught up</div>
        <p>${currentFilter === "trash" ? "Trash is empty." : currentFilter === "cancelled" ? "No cancelled orders." : `No orders yet.${currentRole === "owner" ? " Set your n8n webhook in Settings, then hit Sync from Shopify." : " Ask an owner to sync from Shopify."}`}</p>
      </div>`;
    return;
  }

  renderState = { orders, shown: 0 };
  appendOrderCards(Math.max(ORDERS_PAGE, Math.min(keepShown, orders.length)));
  if (anchor && anchor.name) {
    const same = ordersContainer.querySelector(`.order-card[data-order-name="${CSS.escape(anchor.name)}"]`);
    if (same) {
      const fix = () => { if (same.isConnected) window.scrollBy(0, same.getBoundingClientRect().top - anchor.top); };
      fix();
      // nearby cards settle to their real height over the next frames: correct again
      requestAnimationFrame(() => { fix(); requestAnimationFrame(fix); });
    }
  }
  updateBulkUI();
}

// Add the next `count` orders of the current list to the page.
function appendOrderCards(count) {
  const { orders } = renderState;
  const end = Math.min(orders.length, renderState.shown + count);
  const frag = document.createDocumentFragment();
  for (let i = renderState.shown; i < end; i++) frag.appendChild(buildOrderCard(orders[i]));
  if (ordersMoreEl && ordersMoreEl.parentNode === ordersContainer) ordersContainer.insertBefore(frag, ordersMoreEl);
  else ordersContainer.appendChild(frag);
  renderState.shown = end;
  updateOrdersMore();
}

// "Showing 100 of 2783" line at the bottom; loads the next batch when it scrolls near.
function updateOrdersMore() {
  const total = renderState.orders.length, shown = renderState.shown;
  if (shown >= total) {
    if (ordersMoreEl) ordersMoreEl.remove();
    return;
  }
  if (!ordersMoreEl) {
    ordersMoreEl = document.createElement("div");
    ordersMoreEl.className = "orders-more";
    ordersMoreEl.innerHTML = '<span class="orders-more-text"></span><button type="button" class="btn btn-ghost btn-small orders-more-btn">Show more</button>';
    ordersMoreEl.querySelector(".orders-more-btn").addEventListener("click", () => appendOrderCards(ORDERS_PAGE));
    if ("IntersectionObserver" in window) {
      ordersMoreObserver = new IntersectionObserver((entries) => {
        if (entries.some((en) => en.isIntersecting)) appendOrderCards(ORDERS_PAGE);
      }, { rootMargin: "1200px 0px" });
    }
  }
  ordersMoreEl.querySelector(".orders-more-text").textContent = `Showing ${shown} of ${total} orders`;
  if (ordersMoreEl.parentNode !== ordersContainer) ordersContainer.appendChild(ordersMoreEl);
  if (ordersMoreObserver) { ordersMoreObserver.unobserve(ordersMoreEl); ordersMoreObserver.observe(ordersMoreEl); }
}

function buildOrderCard(order) {
    const card = document.createElement("div");
    card.className = "order-card" + (bulkSelected.has(order.order_id) ? " is-picked" : "");
    card.dataset.orderName = order.order_name || "";

    const head = document.createElement("div");
    head.className = "order-head";
    const paymentBadge = order.payment_type
      ? `<span class="payment-badge payment-badge-${order.payment_type}">${order.payment_type === "cod" ? "COD" : "Prepaid"}</span>`
      : "";
    const assignedTo = order.assigned_to
      ? `<span class="assigned-to">Assigned: ${escapeHtml(order.assigned_to)}</span>`
      : "";
    const showInvoiceInfo = currentFilter === "billing";
    // Bulk PDF tick box (Billing, owner/accounts — same people as Tally export)
    const canBulkPick = showInvoiceInfo && (currentRole === "owner" || currentRole === "accounts");
    const bulkPick = canBulkPick
      ? `<label class="bulk-pick" title="${order.bill_cancelled ? "Bill cancelled — can't be printed" : "Select for PDF"}"><input type="checkbox" class="bulk-pick-input" data-order-id="${escapeHtml(order.order_id)}" ${bulkSelected.has(order.order_id) ? "checked" : ""} ${order.bill_cancelled ? "disabled" : ""} aria-label="Select ${escapeHtml(order.order_name || order.order_id)} for PDF"></label>`
      : "";
    // Staff can only print the first, not-yet-printed copy of an invoice
    // (for their own assigned orders — /api/orders already scopes what
    // they see); reprinting an already-printed one stays accounts/owner
    // only, same as the server enforces in api_order_invoice.
    const canPrintInvoice = showInvoiceInfo && (
      currentRole === "owner" || currentRole === "accounts" ||
      (currentRole === "staff" && !order.invoice_number)
    );
    const invoiceBadge = showInvoiceInfo
      ? (order.invoice_number
          ? `<span class="invoice-badge invoice-badge-printed">Invoice Printed · ${escapeHtml(order.invoice_number)}${order.invoice_printed_by ? ` · by ${escapeHtml(order.invoice_printed_by)}` : ""}</span>`
          : `<span class="invoice-badge invoice-badge-not-printed">Not Printed</span>`)
      : "";
    const totalBadge = (showInvoiceInfo && order.order_total != null)
      ? `<span class="invoice-badge order-total-badge">Total · ₹${Number(order.order_total).toFixed(2)}</span>`
      : "";
    const amountBadge = (showInvoiceInfo && order.payment_type === "cod" && order.amount_to_receive != null)
      ? `<span class="invoice-badge amount-to-receive-badge">To Receive · ₹${Number(order.amount_to_receive).toFixed(2)}</span>`
      : "";
    const billedDateBadge = (showInvoiceInfo && order.billed_at)
      ? `<span class="invoice-badge billed-date-badge">Billed · ${new Date(order.billed_at).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" })}</span>`
      : "";
    const tallyBadge = (showInvoiceInfo && order.invoice_number)
      ? (order.tally_exported_at
          ? `<span class="invoice-badge billed-date-badge">Exported · ${new Date(order.tally_exported_at).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" })}</span>`
          : `<span class="invoice-badge invoice-badge-not-printed">Not exported</span>`)
      : "";
    // Date/time the order arrived on Shopify (created_at), shown in IST
    // on every view except Billing (already crowded with badges). Skipped silently if the value is blank/unparseable.
    let arrivedBadge = "";
    if (order.created_at && currentFilter !== "billing") {
      const d = new Date(order.created_at);
      if (!isNaN(d)) {
        const txt = d.toLocaleString("en-IN", {
          timeZone: "Asia/Kolkata", day: "numeric", month: "short", year: "numeric",
          hour: "numeric", minute: "2-digit", hour12: true,
        });
        arrivedBadge = `<span class="invoice-badge arrived-date-badge" title="Order arrived on Shopify">Arrived · ${escapeHtml(txt)}</span>`;
      }
    }
    const billBadge = (showInvoiceInfo && order.bill_cancelled)
      ? `<span class="invoice-badge cancelled-badge" title="${escapeHtml(order.bill_cancelled_reason || "")}">Bill Cancelled${order.bill_cancelled_reason ? ": " + escapeHtml(order.bill_cancelled_reason) : ""}</span>`
      : "";
    const canCancelBill = showInvoiceInfo && (currentRole === "owner" || currentRole === "accounts");
    const billingEligible = (order.items || []).some((i) => i.status === "purchased" || i.status === "stock");
    // Cancelling is the packer/owner-only alternative to packing — same
    // permission and eligibility as the Packed checkbox, so it shows up
    // in exactly the same place. Once an order is cancelled, the Packed
    // checkbox itself is hidden (packing a cancelled order makes no
    // sense) but the Cancelled checkbox stays so it can be un-cancelled.
    const canManageCancel = canTogglePacked() && billingEligible;
    const canPack = canManageCancel && !order.cancelled;
    const packedReadonlyBadge = (!canTogglePacked() && order.packed)
      ? `<span class="invoice-badge packed-yes-badge">Packed</span>`
      : "";
    // Cancellation info (that it happened, and why) is shown to owner,
    // packer, and telecaller — telecaller can't act on it, but needs to
    // see it to answer a customer's call about their order.
    const canViewCancelInfo = canTogglePacked() || currentRole === "telecaller";
    const cancelledBadge = (canViewCancelInfo && order.cancelled)
      ? `<span class="invoice-badge cancelled-badge" title="${escapeHtml(order.cancelled_reason || "")}">Cancelled${order.cancelled_reason ? ": " + escapeHtml(order.cancelled_reason) : ""}</span>`
      : "";
    head.innerHTML = `
      <span class="order-head-left">
        ${bulkPick}
        <span class="order-name">${escapeHtml(order.order_name)}</span>
        <span class="customer">${escapeHtml(order.customer_name || "")}</span>
        ${order.closed ? `<span class="order-closed-badge">Closed</span>` : ""}
        ${paymentBadge}
        ${arrivedBadge}
        ${invoiceBadge}
        ${totalBadge}
        ${amountBadge}
        ${billedDateBadge}
        ${tallyBadge}
        ${packedReadonlyBadge}
        ${cancelledBadge}
        ${billBadge}
        ${assignedTo}
      </span>
      <span class="order-head-actions">
        ${canPack ? `<label class="order-packed-checkbox"><input type="checkbox" class="order-packed-input" ${order.packed ? "checked" : ""} /> Packed</label>` : ""}
        ${canManageCancel ? `<label class="order-cancelled-checkbox"><input type="checkbox" class="order-cancelled-input" ${order.cancelled ? "checked" : ""} /> Cancelled</label>` : ""}
        ${canCancelBill ? `<button type="button" class="btn btn-ghost btn-small btn-danger order-bill-cancel-link">${order.bill_cancelled ? "Restore Bill" : "Cancel Bill"}</button>` : ""}
        ${canPrintInvoice && !order.bill_cancelled ? `<button type="button" class="btn btn-ghost btn-small order-invoice-link" data-order-id="${escapeHtml(order.order_id)}">${order.invoice_number ? "Reprint Invoice" : "Print Invoice"}</button>` : ""}
        ${(currentRole === "owner" || currentRole === "telecaller") && currentFilter !== "trash" ? `<button type="button" class="btn btn-ghost btn-small order-repurchase-link" data-order-id="${escapeHtml(order.order_id)}">Repurchase</button>` : ""}
        ${(currentRole === "owner" || currentRole === "telecaller") ? `<button type="button" class="order-history-link" data-order-id="${escapeHtml(order.order_id)}">History</button>` : ""}
        ${currentRole === "owner" && currentFilter === "trash" ? `<button type="button" class="btn btn-primary btn-small order-restore-link" data-order-id="${escapeHtml(order.order_id)}">Restore</button>` : ""}
        ${currentRole === "owner" && currentFilter !== "trash" ? `<button type="button" class="btn btn-ghost btn-small btn-danger order-delete-link" data-order-id="${escapeHtml(order.order_id)}">Delete</button>` : ""}
      </span>
    `;
    if (canPack) {
      head.querySelector(".order-packed-input").addEventListener("change", (e) => {
        updateOrderPacked(order.order_id, e.target.checked, e.target);
      });
    }
    if (canManageCancel) {
      head.querySelector(".order-cancelled-input").addEventListener("change", (e) => {
        if (e.target.checked) {
          const reason = prompt(`Reason for cancelling order ${order.order_name || order.order_id}:`);
          if (!reason || !reason.trim()) {
            e.target.checked = false;
            return;
          }
          updateOrderCancelled(order.order_id, true, reason.trim(), e.target);
        } else {
          if (!confirm("Remove the cancelled status from this order?")) {
            e.target.checked = true;
            return;
          }
          updateOrderCancelled(order.order_id, false, "", e.target);
        }
      });
    }
    const rpLink = head.querySelector(".order-repurchase-link");
    if (rpLink) rpLink.addEventListener("click", () => openRepurchasePicker(order.order_id));
    if (canCancelBill) {
      head.querySelector(".order-bill-cancel-link").addEventListener("click", () => {
        const label = order.invoice_number ? `bill ${order.invoice_number}` : `the bill of ${order.order_name || order.order_id}`;
        if (order.bill_cancelled) {
          if (!confirm(`Restore ${label}? It will be included in Tally exports again.`)) return;
          updateBillCancelled(order.order_id, false, "");
        } else {
          const reason = prompt(`Reason for cancelling ${label} (it will be skipped in Tally export):`);
          if (!reason || !reason.trim()) return;
          updateBillCancelled(order.order_id, true, reason.trim());
        }
      });
    }
    if (canPrintInvoice && !order.bill_cancelled) {
      head.querySelector(".order-invoice-link").addEventListener("click", (e) => {
        printInvoice(order.order_id, e.currentTarget);
      });
    }
    if (currentRole === "owner" || currentRole === "telecaller") {
      head.querySelector(".order-history-link").addEventListener("click", () => openActivityLogForOrder(order.order_id));
    }
    if (currentRole === "owner") {
      if (currentFilter === "trash") {
        head.querySelector(".order-restore-link").addEventListener("click", () => restoreOrder(order.order_id, order.order_name));
      } else {
        head.querySelector(".order-delete-link").addEventListener("click", () => deleteOrder(order.order_id, order.order_name));
      }
    }
    card.appendChild(head);

    for (const item of order.items) {
      card.appendChild(renderItemRow(item));
    }

    return card;
}

// Only accounts/owner, and only inside Billing — that's where invoices are
// made and the figures that go to Tally are locked in.
function canEditPrice(item) {
  // Any item that has proceeded to billing (Purchased / In Stock), wherever it
  // is shown — Closed, All, Billing, etc. — not just inside the Billing filter.
  return (currentRole === "owner" || currentRole === "accounts")
    && currentFilter !== "trash"
    && !!item && (item.status === "purchased" || item.status === "stock");
}

async function updateItemPrice(itemId, value) {
  const res = await fetch(`/api/items/${encodeURIComponent(itemId)}/price`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ price: value }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    showMessage(err.error || "Could not update price.", true);
    return;
  }
  const data = await res.json();
  await loadOrders();
  showMessage(
    data.already_exported
      ? "Price updated — NOTE: this order was already exported to Tally with the old price."
      : "Price updated — invoice and Tally export will use it.",
    !!data.already_exported
  );
}

// ---------------------------------------------------------------------------
// Edit order (accounts/owner, Billing): customer/address, delivery charge,
// every item's name/qty/price, and adding more items — all in one popup.
// ---------------------------------------------------------------------------
const editOrderModal = document.getElementById("editOrderModal");
const eoTitle = document.getElementById("eoTitle");
const eoCustomerName = document.getElementById("eoCustomerName");
const eoPhone = document.getElementById("eoPhone");
const eoAddress1 = document.getElementById("eoAddress1");
const eoAddress2 = document.getElementById("eoAddress2");
const eoCity = document.getElementById("eoCity");
const eoState = document.getElementById("eoState");
const eoPincode = document.getElementById("eoPincode");
const eoShipping = document.getElementById("eoShipping");
const eoPaymentType = document.getElementById("eoPaymentType");
const eoCashWrap = document.getElementById("eoCashWrap");
const eoCash = document.getElementById("eoCash");
function eoSyncCash() {
  const isCod = eoPaymentType.value === "cod";
  eoCashWrap.hidden = !isCod;
  if (!isCod) eoCash.value = "";
}
eoPaymentType.addEventListener("change", eoSyncCash);
const eoItemsList = document.getElementById("eoItemsList");
const eoAddItemBtn = document.getElementById("eoAddItemBtn");
const eoTotalDisplay = document.getElementById("eoTotalDisplay");
const eoWarn = document.getElementById("eoWarn");
const eoError = document.getElementById("eoError");
const eoSaveBtn = document.getElementById("eoSaveBtn");
const eoSaveReprintBtn = document.getElementById("eoSaveReprintBtn");
const eoCancelBtn = document.getElementById("eoCancelBtn");
let eoOrderId = null;

function eoUpdateTotal() {
  let total = 0;
  eoItemsList.querySelectorAll(".eo-item-row").forEach((row) => {
    if (row.classList.contains("eo-removed")) return;
    const st = row.dataset.status;
    if (st === "na" || st === "pending" || st === "wait") return; // not on the invoice
    total += (parseFloat(row.querySelector(".eo-qty").value) || 0) * (parseFloat(row.querySelector(".eo-price").value) || 0);
  });
  total += parseFloat(eoShipping.value) || 0;
  eoTotalDisplay.textContent = `₹${total.toFixed(2)}`;
}
eoShipping.addEventListener("input", eoUpdateTotal);

function eoAddRow(item) {
  const isNew = !item;
  const row = document.createElement("div");
  row.className = "eo-item-row" + (isNew ? " eo-new" : "");
  row.dataset.itemId = item ? item.id : "";
  row.dataset.removable = item && item.removable ? "1" : "";
  row.dataset.status = item ? item.status : "stock";
  const nameVal = item ? item.title + (item.variant_title ? ` — ${item.variant_title}` : "") : "";
  row.innerHTML = `
    <input type="text" class="eo-vendor" placeholder="Vendor / Model" value="${escapeHtml(item?.vendor || "")}">
    <input type="text" class="eo-title" placeholder="Item name" value="${escapeHtml(item ? item.title : "")}" ${item && item.variant_title ? `title="${escapeHtml(nameVal)}"` : ""}>
    <input type="number" class="eo-qty" min="1" step="1" value="${item ? item.quantity : 1}" placeholder="Qty">
    <input type="number" class="eo-price" min="0" step="0.01" value="${item ? Number(item.price) : ""}" placeholder="Price ₹">
    ${isNew
      ? `<select class="eo-status"><option value="stock">In Stock</option><option value="purchased">Purchased</option></select>`
      : `<span class="eo-status-label">${escapeHtml(STATUS_LABELS[item.status] || item.status)}</span>`}
    <button type="button" class="mo-item-remove" ${isNew || item.removable ? "" : "disabled"} title="${isNew || item.removable ? "Remove item" : "Shopify items can't be removed — mark them N/A instead"}">✕</button>
  `;
  eoItemsList.appendChild(row);
  row.querySelectorAll(".eo-qty, .eo-price").forEach((el) => el.addEventListener("input", eoUpdateTotal));
  const statusSel = row.querySelector(".eo-status");
  if (statusSel) statusSel.addEventListener("change", () => { row.dataset.status = statusSel.value; eoUpdateTotal(); });
  row.querySelector(".mo-item-remove").addEventListener("click", () => {
    if (isNew) {
      row.remove();
    } else {
      row.classList.toggle("eo-removed");
    }
    eoUpdateTotal();
  });
  eoUpdateTotal();
}

eoAddItemBtn.addEventListener("click", () => eoAddRow(null));
eoCancelBtn.addEventListener("click", () => { editOrderModal.hidden = true; });

async function openEditOrder(orderId) {
  eoOrderId = orderId;
  eoError.hidden = true;
  eoWarn.hidden = true;
  let data;
  try {
    const res = await fetch(`/api/orders/${encodeURIComponent(orderId)}/edit-data`);
    data = await res.json();
    if (!res.ok) { showMessage(data.error || "Could not open order.", true); return; }
  } catch (err) {
    showMessage("Could not open order: " + err.message, true);
    return;
  }
  eoTitle.textContent = `Edit Order ${data.order_name || ""}`.trim();
  eoCustomerName.value = data.customer_name;
  eoPhone.value = data.phone;
  eoAddress1.value = data.address1;
  eoAddress2.value = data.address2;
  eoCity.value = data.city;
  eoPincode.value = data.pincode;
  eoShipping.value = data.shipping_amount;
  eoPaymentType.value = data.payment_type || "";
  eoCash.value = data.cash_to_collect != null ? data.cash_to_collect : "";
  eoSyncCash();
  if (data.payment_type !== "cod") eoCash.value = "";
  const states = INDIA_STATES.includes(data.state) || !data.state ? INDIA_STATES : [data.state, ...INDIA_STATES];
  eoState.innerHTML = `<option value="">Select…</option>` +
    states.map((st) => `<option value="${escapeHtml(st)}">${escapeHtml(st)}</option>`).join("");
  eoState.value = data.state;
  eoItemsList.innerHTML = `<div class="eo-cols"><span>Vendor</span><span>Item</span><span>Qty</span><span>Price ₹</span><span>Status</span><span></span></div>`;
  data.items.forEach((it) => eoAddRow(it));
  if (data.tally_exported_at) {
    eoWarn.textContent = "This order was already exported to Tally. After saving it goes back into \"Only new\" so the next Tally XML includes the updated figures — delete the old voucher in Tally before importing it again.";
    eoWarn.hidden = false;
  }
  eoSaveReprintBtn.hidden = !data.invoice_number;
  eoUpdateTotal();
  editOrderModal.hidden = false;
}

async function eoSave(reprintTab) {
  eoError.hidden = true;
  const items = [];
  const remove_ids = [];
  eoItemsList.querySelectorAll(".eo-item-row").forEach((row) => {
    const id = row.dataset.itemId;
    if (row.classList.contains("eo-removed")) { if (id) remove_ids.push(id); return; }
    items.push({
      id: id || null,
      vendor: row.querySelector(".eo-vendor").value.trim(),
      title: row.querySelector(".eo-title").value.trim(),
      quantity: row.querySelector(".eo-qty").value || 1,
      price: row.querySelector(".eo-price").value || 0,
      status: id ? undefined : row.querySelector(".eo-status").value,
    });
  });
  const payload = {
    customer_name: eoCustomerName.value.trim(),
    phone: eoPhone.value.trim(),
    address1: eoAddress1.value.trim(),
    address2: eoAddress2.value.trim(),
    city: eoCity.value.trim(),
    state: eoState.value,
    pincode: eoPincode.value.trim(),
    shipping_amount: eoShipping.value || 0,
    payment_type: eoPaymentType.value || null,
    cash_to_collect: eoPaymentType.value === "cod" ? eoCash.value : "",
    items,
    remove_ids,
  };
  eoSaveBtn.disabled = true;
  eoSaveReprintBtn.disabled = true;
  eoSaveBtn.textContent = "Saving…";
  try {
    const res = await fetch(`/api/orders/${encodeURIComponent(eoOrderId)}/edit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      eoError.textContent = data.error || "Could not save changes.";
      eoError.hidden = false;
      if (reprintTab) reprintTab.close();
      return;
    }
    editOrderModal.hidden = true;
    await loadOrders();
    if (reprintTab !== undefined) {
      if (data.has_invoice) {
        await printInvoice(eoOrderId, eoSaveBtn, reprintTab);
      } else if (reprintTab) {
        reprintTab.close();
      }
    }
    showMessage(
      !data.changes ? "No changes to save."
        : data.re_export ? "Order updated — download the Tally XML (Only new) to get the updated voucher."
        : "Order updated — the Tally XML will use the new figures.",
      false
    );
  } catch (err) {
    eoError.textContent = "Could not save changes: " + err.message;
    eoError.hidden = false;
    if (reprintTab) reprintTab.close();
  } finally {
    eoSaveBtn.disabled = false;
    eoSaveBtn.textContent = "Save Changes";
    eoSaveReprintBtn.disabled = false;
  }
}

eoSaveBtn.addEventListener("click", () => eoSave());
eoSaveReprintBtn.addEventListener("click", () => {
  // Open the tab right here, inside the click, so the browser allows it.
  const tab = window.open("", "_blank");
  eoSave(tab);
});

function renderItemRow(item) {
  const row = document.createElement("div");
  row.className = "item-row";

  const titleBlock = document.createElement("div");
  titleBlock.innerHTML = `
    <div class="item-title">${escapeHtml(item.title)}${item.variant_title ? ` — ${escapeHtml(item.variant_title)}` : ""}</div>
    <div class="item-meta">qty ${item.quantity} · ${escapeHtml(item.price || "")}${item.price_edited ? ` <span class="price-edited-badge" title="Shopify price: ${escapeHtml(item.original_price || "")}">edited (was ${escapeHtml(item.original_price || "")})</span>` : ""}${item.vendor ? " · " + escapeHtml(item.vendor) : ""}${canEditPrice(item) ? ` · <button type="button" class="item-price-edit-btn">Edit order</button>` : ""}</div>
  `;
  const priceBtn = titleBlock.querySelector(".item-price-edit-btn");
  if (priceBtn) {
    priceBtn.addEventListener("click", () => openEditOrder(item.order_id || item.shopify_order_id));
  }

  const editable = canEditStatus();

  const purchaseBlock = document.createElement("div");
  purchaseBlock.className = "purchase-amount-block";
  if (editable) {
    purchaseBlock.innerHTML = `<input type="number" step="0.01" min="0" class="purchase-amount-input" placeholder="Amount ₹" value="${item.purchase_amount ? Number(item.purchase_amount) : ""}" />`;
    const input = purchaseBlock.querySelector("input");
    input.addEventListener("blur", () => updatePurchaseAmount(item.id, input.value, input));
  } else {
    const amount = item.purchase_amount ? "₹" + Number(item.purchase_amount) : "—";
    purchaseBlock.innerHTML = `<div class="purchase-amount-readonly">${amount}</div>`;
  }

  const pills = document.createElement("div");
  pills.className = "status-pills";
  for (const status of Object.keys(STATUS_LABELS)) {
    if (status === "refunded" && currentRole !== "owner" && currentRole !== "telecaller") continue;
    const pill = document.createElement("button");
    pill.className = "pill";
    pill.dataset.status = status;
    pill.textContent = STATUS_LABELS[status];
    if (item.status === status) pill.classList.add("active");
    if (editable) {
      pill.addEventListener("click", () => updateStatus(item.id, status, row));
    } else {
      pill.disabled = true;
      pill.classList.add("pill-readonly");
    }
    pills.appendChild(pill);
  }

  row.appendChild(titleBlock);
  row.appendChild(purchaseBlock);
  row.appendChild(pills);
  return row;
}

async function updateOrderPacked(orderId, packed, checkboxEl) {
  const res = await fetch(`/api/orders/${encodeURIComponent(orderId)}/packed`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ packed }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    showMessage(err.error || "Could not update packed status.", true);
    if (checkboxEl) checkboxEl.checked = !packed;
    return;
  }
  const order = lastLoadedOrders.find((o) => o.order_id === orderId);
  if (order) order.packed = packed;
  showMessage(packed ? "Order marked as packed" : "Order marked as not packed");
}

async function updateOrderCancelled(orderId, cancelled, reason, checkboxEl) {
  const res = await fetch(`/api/orders/${encodeURIComponent(orderId)}/cancelled`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cancelled, reason }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    showMessage(err.error || "Could not update cancelled status.", true);
    if (checkboxEl) checkboxEl.checked = !cancelled;
    return;
  }
  const data = await res.json();
  const order = lastLoadedOrders.find((o) => o.order_id === orderId);
  if (order) {
    order.cancelled = data.cancelled;
    order.cancelled_reason = data.cancelled_reason;
    order.cancelled_by = data.cancelled_by;
    order.cancelled_at = data.cancelled_at;
  }
  // Cancelling/un-cancelling changes whether the Packed checkbox should be
  // showing at all (see canPack above), so re-render rather than just
  // mutating the one field updateOrderPacked does.
  renderOrders(filterByTrackQuery(lastLoadedOrders));
  showMessage(cancelled ? "Order marked as cancelled" : "Cancelled status removed");
}

async function updateBillCancelled(orderId, cancelled, reason) {
  const res = await fetch(`/api/orders/${encodeURIComponent(orderId)}/bill-cancelled`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cancelled, reason }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    showMessage(err.error || "Could not update bill status.", true);
    return;
  }
  const data = await res.json();
  const order = lastLoadedOrders.find((o) => o.order_id === orderId);
  if (order) {
    order.bill_cancelled = data.bill_cancelled;
    order.bill_cancelled_reason = data.bill_cancelled_reason;
    order.bill_cancelled_by = data.bill_cancelled_by;
    order.bill_cancelled_at = data.bill_cancelled_at;
  }
  renderOrders(filterByTrackQuery(lastLoadedOrders));
  showMessage(cancelled ? "Bill cancelled — it won't be in the Tally export" : "Bill restored");
}

async function updatePurchaseAmount(itemId, value, inputEl) {
  const res = await fetch(`/api/items/${encodeURIComponent(itemId)}/purchase-amount`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ purchase_amount: value || 0 }),
  });
  if (!res.ok) {
    showMessage("Could not save purchase amount.", true);
    return;
  }
  const data = await res.json();
  inputEl.value = data.purchase_amount ? Number(data.purchase_amount) : "";
}

async function updateStatus(itemId, status, rowEl) {
  const res = await fetch(`/api/items/${encodeURIComponent(itemId)}/status`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    showMessage(`${err.error || "Could not update status."} (HTTP ${res.status})`, true);
    return;
  }
  rowEl.querySelectorAll(".pill").forEach((p) => {
    p.classList.toggle("active", p.dataset.status === status);
  });
  // If a filter is active and the item no longer matches it, refresh the
  // list. Closed/Billing depend on the status of EVERY item in the order
  // (not just the one that changed), so any status change while viewing
  // either of those always needs a refresh, not just a non-matching one.
  if (currentFilter === "closed" || currentFilter === "billing" || (currentFilter && currentFilter !== status)) {
    loadOrders();
  }
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

function fmtMs(ms) {
  if (ms == null) return "--";
  return ms >= 1000 ? (ms / 1000).toFixed(1) + "s" : Math.round(ms) + "ms";
}

async function loadSyncHealth() {
  const card = document.getElementById("syncHealthCard");
  if (!card || !(currentRole === "owner" || currentRole === "staff")) return;
  try {
    const res = await fetch("/api/sync/health");
    if (!res.ok) return;
    const d = await res.json();
    card.hidden = false;
    document.getElementById("syncHealthLabel").textContent = "SYNC INTEGRITY";
    document.getElementById("syncBar").hidden = true;
    const pct = document.getElementById("syncHealthPct");
    const sub = document.getElementById("syncHealthSub");
    const dot = document.getElementById("syncHealthDot");
    const status = document.getElementById("syncHealthStatus");
    const avg = document.getElementById("syncHealthAvg");
    if (!d.samples) {
      pct.textContent = "--";
      sub.textContent = "Run a sync to measure latency";
      sub.className = "sync-health-sub";
      status.textContent = "No data";
      avg.textContent = "";
      dot.className = "sync-dot";
      return;
    }
    pct.textContent = d.integrity_pct + "%";
    sub.textContent = "Webhook latency: " + fmtMs(d.last_ms);
    sub.className = "sync-health-sub" + (!d.last_ok || d.last_ms > 3000 ? " bad" : d.last_ms > 1000 ? " warn" : "");
    status.textContent = d.status;
    avg.textContent = d.avg_ms != null ? "avg " + fmtMs(d.avg_ms) + " · " + d.samples + " syncs" : d.samples + " syncs";
    dot.className = "sync-dot " + (d.last_ok ? "ok" : "bad");
  } catch (e) { /* card is non-critical */ }
}

function renderSyncProgress(p) {
  const card = document.getElementById("syncHealthCard");
  card.hidden = false;
  document.getElementById("syncHealthLabel").textContent = "SYNCING";
  document.getElementById("syncHealthPct").textContent = (p.percent || 0) + "%";
  const sub = document.getElementById("syncHealthSub");
  sub.className = "sync-health-sub";
  sub.textContent = p.phase === "saving"
    ? `Saving orders ${p.done || 0}/${p.total || 0}`
    : "Fetching from Shopify…";
  document.getElementById("syncHealthStatus").textContent = "Syncing";
  document.getElementById("syncHealthAvg").textContent = "";
  document.getElementById("syncHealthDot").className = "sync-dot ok";
  document.getElementById("syncBar").hidden = false;
  document.getElementById("syncBarFill").style.width = (p.percent || 0) + "%";
}

async function pollSyncProgress(runId) {
  const deadline = Date.now() + 15 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 600));
    const res = await fetch("/api/sync/progress");
    if (!res.ok) continue;
    const p = await res.json();
    if (p.id !== runId) continue;
    renderSyncProgress(p);
    if (p.state !== "running") return p;
  }
  return { state: "error", message: "Sync is taking too long — check n8n." };
}

syncBtn.addEventListener("click", async () => {
  syncBtn.disabled = true;
  syncBtn.textContent = "Syncing…";
  try {
    const res = await fetch("/api/sync", { method: "POST" });
    const data = await res.json();
    if (!res.ok) {
      showMessage(data.error || "Sync failed.", true);
      return;
    }
    renderSyncProgress({ percent: 0, phase: "fetching" });
    const final = await pollSyncProgress(data.id);
    if (final.state === "done") {
      renderSyncProgress({ ...final, percent: 100 });
      showMessage(`Synced ${final.orders_synced} orders, ${final.items_synced} items.`);
      loadOrders();
      await new Promise((r) => setTimeout(r, 900));
    } else {
      showMessage(final.message || "Sync failed.", true);
    }
  } catch (err) {
    showMessage("Sync failed: " + err.message, true);
  } finally {
    loadSyncHealth();
    syncBtn.disabled = false;
    syncBtn.textContent = "Sync from Shopify";
  }
});

// Same state list as invoice.py's GST_STATE_CODES, so every manually
// entered order always resolves to a valid GST state code on the invoice.
const INDIA_STATES = [
  "Andaman and Nicobar Islands", "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar",
  "Chandigarh", "Chhattisgarh", "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Goa",
  "Gujarat", "Haryana", "Himachal Pradesh", "Jammu and Kashmir", "Jharkhand", "Karnataka",
  "Kerala", "Ladakh", "Lakshadweep", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya",
  "Mizoram", "Nagaland", "Odisha", "Puducherry", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu",
  "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
];
moState.innerHTML = `<option value="">Select…</option>` +
  INDIA_STATES.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join("");

let moItemRowCount = 0;

function moAddItemRow(prefill) {
  const rowId = `mo-item-${moItemRowCount++}`;
  const row = document.createElement("div");
  row.className = "mo-item-row";
  row.dataset.rowId = rowId;
  row.innerHTML = `
    <input type="text" class="mo-item-vendor" placeholder="Vendor / Model (optional)" value="${escapeHtml(prefill?.vendor || "")}">
    <input type="text" class="mo-item-title" placeholder="Item name" value="${escapeHtml(prefill?.title || "")}">
    <input type="number" class="mo-item-qty" min="1" step="1" value="${prefill?.quantity || 1}" placeholder="Qty">
    <input type="number" class="mo-item-price" min="0" step="0.01" value="${prefill?.price ?? ""}" placeholder="Price ₹">
    <button type="button" class="mo-item-remove" title="Remove item">✕</button>
  `;
  moItemsList.appendChild(row);
  row.querySelectorAll(".mo-item-qty, .mo-item-price").forEach((el) => {
    el.addEventListener("input", moUpdateTotal);
  });
  row.querySelector(".mo-item-remove").addEventListener("click", () => {
    row.remove();
    moUpdateTotal();
  });
  moUpdateTotal();
}

function moUpdateTotal() {
  let total = 0;
  moItemsList.querySelectorAll(".mo-item-row").forEach((row) => {
    const qty = parseFloat(row.querySelector(".mo-item-qty").value) || 0;
    const price = parseFloat(row.querySelector(".mo-item-price").value) || 0;
    total += qty * price;
  });
  total += parseFloat(moShippingAmount.value) || 0;
  moTotalDisplay.textContent = `₹${total.toFixed(2)}`;
}
moShippingAmount.addEventListener("input", moUpdateTotal);

function moResetForm() {
  moOrderId.value = "";
  moCustomerName.value = "";
  moPhone.value = "";
  moPaymentType.value = "";
  moAddress1.value = "";
  moAddress2.value = "";
  moCity.value = "";
  moState.value = "";
  moPincode.value = "";
  moShippingAmount.value = "0";
  moBalanceDue.value = "";
  moItemsList.innerHTML = "";
  moItemRowCount = 0;
  moAddItemRow();
  moUpdateTotal();
}

moAddItemBtn.addEventListener("click", () => moAddItemRow());

pasteOrderBtn.addEventListener("click", () => {
  pasteOrderError.hidden = true;
  moResetForm();
  pasteOrderModal.hidden = false;
});

closePasteOrderBtn.addEventListener("click", () => { pasteOrderModal.hidden = true; });

submitPasteOrderBtn.addEventListener("click", async () => {
  pasteOrderError.hidden = true;

  const items = [];
  moItemsList.querySelectorAll(".mo-item-row").forEach((row) => {
    const title = row.querySelector(".mo-item-title").value.trim();
    if (!title) return;
    items.push({
      vendor: row.querySelector(".mo-item-vendor").value.trim(),
      title,
      quantity: row.querySelector(".mo-item-qty").value || 1,
      price: row.querySelector(".mo-item-price").value || 0,
    });
  });

  const payload = {
    order_id: moOrderId.value.trim(),
    customer_name: moCustomerName.value.trim(),
    phone: moPhone.value.trim(),
    payment_type: moPaymentType.value,
    address1: moAddress1.value.trim(),
    address2: moAddress2.value.trim(),
    city: moCity.value.trim(),
    state: moState.value,
    pincode: moPincode.value.trim(),
    shipping_amount: moShippingAmount.value || 0,
    balance_due: moBalanceDue.value === "" ? null : moBalanceDue.value,
    items,
  };

  submitPasteOrderBtn.disabled = true;
  submitPasteOrderBtn.textContent = "Adding…";
  try {
    const res = await fetch("/api/orders/manual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      pasteOrderError.textContent = data.error || "Could not add order.";
      pasteOrderError.hidden = false;
      return;
    }
    showMessage(`Added order ${moOrderId.value.trim()} with ${data.items_added} item(s).`);
    moResetForm();
    loadOrders();
  } catch (err) {
    pasteOrderError.textContent = "Could not add order: " + err.message;
    pasteOrderError.hidden = false;
  } finally {
    submitPasteOrderBtn.disabled = false;
    submitPasteOrderBtn.textContent = "Add Order";
  }
});


statusFilterSelect.addEventListener("change", () => {
  currentFilter = statusFilterSelect.value;
  invoiceFilterRow.hidden = currentFilter !== "billing";
  if (currentFilter !== "billing") {
    currentInvoiceFilter = "";
    currentTallyFilter = "";
    invoiceFilterSelect.value = "";
    tallyFilterSelect.value = "";
    tallyOnlyNew.disabled = false;
  }
  exportPendingBtn.hidden = currentFilter !== "pending";
  updateTallyExportVisibility();
  loadOrders();
});

paymentFilterButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    paymentFilterButtons.forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    currentPaymentFilter = btn.dataset.payment;
    loadOrders();
  });
});

invoiceFilterSelect.addEventListener("change", () => {
  currentInvoiceFilter = invoiceFilterSelect.value;
  renderOrders(filterByTrackQuery(lastLoadedOrders));
});

tallyFilterSelect.addEventListener("change", () => {
  currentTallyFilter = tallyFilterSelect.value;
  // "Not exported" IS the "give me everything not yet sent" view — tie the
  // checkbox to it so it's visibly on and can't be accidentally unticked
  // while that's what's selected (downloadTally forces only_new either way,
  // this is just so the UI doesn't look like it's about to export duplicates).
  if (currentTallyFilter === "not_exported") {
    tallyOnlyNew.checked = true;
    tallyOnlyNew.disabled = true;
  } else {
    tallyOnlyNew.disabled = false;
  }
  renderOrders(filterByTrackQuery(lastLoadedOrders));
});

trashBtn.addEventListener("click", () => {
  currentFilter = "trash";
  updateTallyExportVisibility();
  currentInvoiceFilter = "";
  currentTallyFilter = "";
  invoiceFilterSelect.value = "";
  tallyFilterSelect.value = "";
  tallyOnlyNew.disabled = false;
  statusFilterSelect.value = "";
  invoiceFilterRow.hidden = true;
  loadOrders();
});

cancelledBtn.addEventListener("click", () => {
  currentFilter = "cancelled";
  updateTallyExportVisibility();
  currentInvoiceFilter = "";
  currentTallyFilter = "";
  invoiceFilterSelect.value = "";
  tallyFilterSelect.value = "";
  tallyOnlyNew.disabled = false;
  statusFilterSelect.value = "";
  invoiceFilterRow.hidden = true;
  loadOrders();
});

// ---------------------------------------------------------------------------
// Repurchase (owner + telecaller) — separate from orders/invoices/Tally
// ---------------------------------------------------------------------------
const repurchaseModal = document.getElementById("repurchaseModal");
const rpItems = document.getElementById("rpItems");
const rpReason = document.getElementById("rpReason");
const rpTitle = document.getElementById("rpTitle");
const repurchaseListModal = document.getElementById("repurchaseListModal");
const rpList = document.getElementById("rpList");
const rpStatusFilter = document.getElementById("rpStatusFilter");
let rpFilter = null; // set per role when the list opens
const RP_LABELS = { pending: "Pending", purchased: "Purchased", resent: "Sent" };
let rpOrderId = null;

async function openRepurchasePicker(orderId) {
  rpOrderId = orderId;
  rpReason.value = "";
  rpItems.innerHTML = `<p class="empty-log">Loading…</p>`;
  repurchaseModal.hidden = false;
  const res = await fetch(`/api/orders/${encodeURIComponent(orderId)}/repurchase-items`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    rpItems.innerHTML = `<p class="empty-log">${escapeHtml(err.error || "Could not load items.")}</p>`;
    return;
  }
  const data = await res.json();
  rpTitle.textContent = `Repurchase — ${data.order_name || orderId}`;
  if (!data.items.length) {
    rpItems.innerHTML = `<p class="empty-log">This order has no items.</p>`;
    return;
  }
  rpItems.innerHTML = data.items.map((it) => {
    const room = Math.max(0, it.quantity - (it.already_repurchased || 0));
    return `
    <div class="rp-item" data-item-id="${escapeHtml(it.id)}">
      <label class="rp-item-main">
        <input type="checkbox" class="rp-check" ${room ? "" : "disabled"} />
        <span>${escapeHtml(it.title || "Item")}${it.variant_title ? ` <small>(${escapeHtml(it.variant_title)})</small>` : ""}
          ${it.already_repurchased ? `<small class="rp-already">already in Repurchase ×${it.already_repurchased}</small>` : ""}</span>
      </label>
      <span class="rp-qty">Qty <input type="number" class="rp-qty-input" min="1" max="${room || 1}" value="${room || 1}" ${room ? "" : "disabled"} /> of ${it.quantity}</span>
    </div>`;
  }).join("");
}

document.getElementById("rpCancelBtn").addEventListener("click", () => { repurchaseModal.hidden = true; });
document.getElementById("rpSaveBtn").addEventListener("click", async () => {
  const picked = [...rpItems.querySelectorAll(".rp-item")]
    .filter((row) => row.querySelector(".rp-check").checked)
    .map((row) => ({ id: row.dataset.itemId, quantity: parseInt(row.querySelector(".rp-qty-input").value, 10) || 1 }));
  if (!picked.length) {
    showMessage("Select at least one item.", true);
    return;
  }
  const res = await fetch(`/api/orders/${encodeURIComponent(rpOrderId)}/repurchase`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: picked, reason: rpReason.value.trim() }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    showMessage(err.error || "Could not add to Repurchase.", true);
    return;
  }
  repurchaseModal.hidden = true;
  showMessage(`Added ${picked.length} item${picked.length > 1 ? "s" : ""} to Repurchase`);
});

function rpAllowedStatuses() {
  // Which pills this role may click (server enforces the same rules).
  if (currentRole === "owner" || currentRole === "telecaller") return ["pending", "purchased", "resent"];
  if (currentRole === "staff") return ["pending", "purchased"];
  if (currentRole === "packer") return ["purchased", "resent"];
  return [];
}

function renderRpFilter() {
  const tabs = currentRole === "packer" ? ["purchased", "resent"] : ["", "pending", "purchased", "resent"];
  rpStatusFilter.innerHTML = "";
  tabs.forEach((s) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "pill" + (rpFilter === s ? " active" : "");
    b.dataset.status = s || "all";
    b.textContent = s ? RP_LABELS[s] : "All";
    b.addEventListener("click", () => { rpFilter = s; renderRpFilter(); loadRepurchases(); });
    rpStatusFilter.appendChild(b);
  });
}

async function printRepurchaseInvoice(batchKey, btn) {
  const tab = window.open("", "_blank");
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Printing…";
  try {
    const res = await fetch(`/api/repurchases/batch/${encodeURIComponent(batchKey)}/invoice.pdf`);
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      if (tab) tab.close();
      showMessage(data.error || "Could not generate the invoice.", true);
      return;
    }
    const blobUrl = URL.createObjectURL(await res.blob());
    if (tab) tab.location = blobUrl;
    loadRepurchases();
  } catch (err) {
    if (tab) tab.close();
    showMessage("Could not reach the server to print the invoice.", true);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

async function loadRepurchases() {
  rpList.innerHTML = `<p class="empty-log">Loading…</p>`;
  const res = await fetch(rpFilter ? `/api/repurchases?status=${encodeURIComponent(rpFilter)}` : "/api/repurchases");
  if (!res.ok) {
    rpList.innerHTML = `<p class="empty-log">Could not load Repurchase list.</p>`;
    return;
  }
  const { rows } = await res.json();
  if (!rows.length) {
    rpList.innerHTML = `<p class="empty-log">Nothing here yet.</p>`;
    return;
  }
  const canRemove = currentRole === "owner" || currentRole === "telecaller";
  const canPrint = ["owner", "telecaller", "accounts", "staff"].includes(currentRole);
  const allowed = rpAllowedStatuses();

  // One block (and one RPR bill) per "Add to Repurchase" action.
  const groups = [];
  const byKey = {};
  rows.forEach((r) => {
    if (!byKey[r.batch_key]) { byKey[r.batch_key] = { key: r.batch_key, rows: [] }; groups.push(byKey[r.batch_key]); }
    byKey[r.batch_key].rows.push(r);
  });

  rpList.innerHTML = groups.map((g) => {
    const first = g.rows[0];
    const invNo = (g.rows.find((r) => r.invoice_number) || {}).invoice_number;
    const ready = g.rows.every((r) => r.status !== "pending");
    const printBtn = canPrint
      ? `<button type="button" class="btn btn-ghost btn-small rp-print" data-key="${escapeHtml(g.key)}" ${ready ? "" : "disabled title=\"Mark all items Purchased first\""}>${invNo ? "Reprint " + escapeHtml(invNo) : "Print invoice"}</button>`
      : (invNo ? `<small class="rp-invno">${escapeHtml(invNo)}</small>` : "");
    return `
    <div class="rp-group" data-key="${escapeHtml(g.key)}">
      <div class="rp-group-head">
        <div><strong>${escapeHtml(first.order_name || "")}</strong> · ${escapeHtml(first.customer_name || "")}${first.customer_phone ? " · " + escapeHtml(first.customer_phone) : ""}
          <div class="rp-row-sub">Added by ${escapeHtml(first.created_by || "")}${first.created_at ? " · " + escapeHtml(new Date(first.created_at).toLocaleString()) : ""}${first.reason ? " · Reason: " + escapeHtml(first.reason) : ""}</div></div>
        ${printBtn}
      </div>
      ${g.rows.map((r) => `
      <div class="rp-row" data-id="${r.id}" data-status="${escapeHtml(r.status)}">
        <div class="rp-row-main">
          <strong>${escapeHtml(r.title || "Item")}</strong>${r.variant_title ? ` <small>(${escapeHtml(r.variant_title)})</small>` : ""} × ${r.quantity}
        </div>
        <div class="rp-row-actions">
          <div class="status-pills rp-pills">
            ${["pending", "purchased", "resent"].map((s) => `<button type="button" class="pill rp-pill ${r.status === s ? "active" : ""} ${allowed.includes(s) ? "" : "pill-readonly"}" data-status="${s}" ${allowed.includes(s) ? "" : "disabled"}>${RP_LABELS[s]}</button>`).join("")}
          </div>
          ${canRemove ? `<button type="button" class="btn btn-ghost btn-small btn-danger rp-delete">Remove</button>` : ""}
        </div>
      </div>`).join("")}
    </div>`;
  }).join("");

  rpList.querySelectorAll(".rp-row").forEach((row) => {
    const id = row.dataset.id;
    row.querySelectorAll(".rp-pill:not([disabled])").forEach((pill) => {
      pill.addEventListener("click", async () => {
        if (pill.dataset.status === row.dataset.status) return;
        const res = await fetch(`/api/repurchases/${id}/status`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: pill.dataset.status }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          showMessage(err.error || "Could not update status.", true);
        }
        loadRepurchases();
      });
    });
    const del = row.querySelector(".rp-delete");
    if (del) del.addEventListener("click", async () => {
      if (!confirm("Remove this item from Repurchase?")) return;
      const res = await fetch(`/api/repurchases/${id}`, { method: "DELETE" });
      if (!res.ok) { showMessage("Could not remove.", true); return; }
      loadRepurchases();
    });
  });
  rpList.querySelectorAll(".rp-print").forEach((btn) => {
    btn.addEventListener("click", () => printRepurchaseInvoice(btn.dataset.key, btn));
  });
}

repurchaseBtn.addEventListener("click", () => {
  repurchaseListModal.hidden = false;
  rpFilter = currentRole === "packer" ? "purchased" : "pending";
  renderRpFilter();
  loadRepurchases();
});
document.getElementById("rpListCloseBtn").addEventListener("click", () => { repurchaseListModal.hidden = true; });

// ---------------------------------------------------------------------------
// Date range filter
// ---------------------------------------------------------------------------

function updateClearDateFilterVisibility() {
  clearDateFilterBtn.hidden = !currentDateFrom && !currentDateTo;
}

dateFromInput.addEventListener("change", () => {
  currentDateFrom = dateFromInput.value;
  updateClearDateFilterVisibility();
  loadOrders();
});

dateToInput.addEventListener("change", () => {
  currentDateTo = dateToInput.value;
  updateClearDateFilterVisibility();
  loadOrders();
});

clearDateFilterBtn.addEventListener("click", () => {
  currentDateFrom = "";
  currentDateTo = "";
  dateFromInput.value = "";
  dateToInput.value = "";
  updateClearDateFilterVisibility();
  loadOrders();
});

// ---------------------------------------------------------------------------
// Tally Prime export (owner / accounts, Billing view). Uses the From/To dates
// above, filtered by INVOICE date. Downloads via fetch so errors (e.g. "no
// invoices in that range") show as a message instead of a blank JSON page.
// ---------------------------------------------------------------------------

function updateTallyExportVisibility() {
  tallyExportGroup.hidden = !(
    currentFilter === "billing" && (currentRole === "owner" || currentRole === "accounts")
  );
}

// ---------------------------------------------------------------------------
// Billing: tick orders (or Select all) -> PDF downloads all their invoices in
// one file. Each invoice goes through the same server steps as Print Invoice
// (never-printed orders get their invoice number now). Max 200 per PDF.
// ---------------------------------------------------------------------------
const BULK_PDF_LIMIT = 200;
// Find the Select all / PDF controls — or add them next to Tally XML if this page
// was served from an older index.html, so a missing element can never stop the
// rest of the app (roles, buttons, sync) from loading.
function ensureBulkControls() {
  let all = document.getElementById("bulkSelectAll");
  let btn = document.getElementById("bulkPdfBtn");
  if (all && btn) return [all, btn];
  if (!tallyExportGroup) return [all, btn];
  if (!all) {
    const div = document.createElement("span");
    div.className = "bulk-divider";
    div.setAttribute("aria-hidden", "true");
    const lab = document.createElement("label");
    lab.className = "check-label bulk-select-all";
    lab.title = "Select every order shown below (max 200 per PDF)";
    all = document.createElement("input");
    all.type = "checkbox";
    all.id = "bulkSelectAll";
    lab.append(all, " Select all");
    tallyExportGroup.append(div, lab);
  }
  if (!btn) {
    btn = document.createElement("button");
    btn.id = "bulkPdfBtn";
    btn.className = "btn btn-primary";
    btn.type = "button";
    btn.disabled = true;
    btn.title = "Download the selected orders' invoices as one PDF";
    btn.textContent = "PDF";
    tallyExportGroup.append(btn);
  }
  return [all, btn];
}
const [bulkSelectAll, bulkPdfBtn] = ensureBulkControls();

function bulkPickableOrders() {
  return lastRenderedOrders.filter((o) => !o.bill_cancelled);
}

function updateBulkUI() {
  const bulkPdfBtn = document.getElementById("bulkPdfBtn");
  const bulkSelectAll = document.getElementById("bulkSelectAll");
  if (!bulkPdfBtn || !bulkSelectAll) return;
  const n = bulkSelected.size;
  bulkPdfBtn.disabled = n === 0;
  bulkPdfBtn.textContent = n ? `PDF (${n})` : "PDF";
  bulkPdfBtn.title = n > BULK_PDF_LIMIT
    ? `Max ${BULK_PDF_LIMIT} invoices per PDF — ${n} selected`
    : "Download the selected orders' invoices as one PDF";
  const pickable = bulkPickableOrders();
  const pickedHere = pickable.filter((o) => bulkSelected.has(o.order_id)).length;
  bulkSelectAll.checked = bulkAllMode || (pickable.length > 0 && pickedHere === pickable.length);
  bulkSelectAll.indeterminate = pickedHere > 0 && pickedHere < pickable.length;
}

// One listener for every order's tick box
ordersContainer.addEventListener("change", (e) => {
  const box = e.target.closest(".bulk-pick-input");
  if (!box) return;
  const id = box.dataset.orderId;
  if (box.checked) bulkSelected.add(id); else { bulkSelected.delete(id); bulkAllMode = false; }
  const card = box.closest(".order-card");
  if (card) card.classList.toggle("is-picked", box.checked);
  updateBulkUI();
});

bulkSelectAll?.addEventListener("change", () => {
  bulkAllMode = bulkSelectAll.checked;
  const pickable = bulkPickableOrders();
  for (const o of pickable) {
    if (bulkSelectAll.checked) bulkSelected.add(o.order_id); else bulkSelected.delete(o.order_id);
  }
  ordersContainer.querySelectorAll(".bulk-pick-input:not(:disabled)").forEach((box) => {
    box.checked = bulkSelectAll.checked;
    const card = box.closest(".order-card");
    if (card) card.classList.toggle("is-picked", box.checked);
  });
  updateBulkUI();
  if (bulkSelectAll.checked && bulkSelected.size > BULK_PDF_LIMIT) {
    showMessage(`${bulkSelected.size} orders selected — max ${BULK_PDF_LIMIT} per PDF. Narrow it with the date or Invoice filter.`, true);
  }
});

bulkPdfBtn?.addEventListener("click", async () => {
  const ids = [...bulkSelected];
  if (!ids.length) return;
  if (ids.length > BULK_PDF_LIMIT) {
    showMessage(`Max ${BULK_PDF_LIMIT} invoices per PDF — ${ids.length} selected. Narrow it with the date or Invoice filter.`, true);
    return;
  }
  const notPrinted = lastLoadedOrders.filter((o) => bulkSelected.has(o.order_id) && !o.invoice_number).length;
  if (notPrinted && !confirm(`${notPrinted} of the ${ids.length} selected order(s) have never been printed — they will get their invoice numbers now. Continue?`)) {
    return;
  }
  bulkPdfBtn.disabled = true;
  bulkPdfBtn.textContent = `Preparing ${ids.length}…`;
  try {
    const res = await fetch("/api/invoices/bulk.pdf", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ order_ids: ids }),
    });
    if (!res.ok) {
      if (res.status === 404) {
        // The page is new but the server is still the old app.py (no PDF route yet)
        showMessage("PDF isn't available on the server yet — the app is still running the old app.py. Close the app window, start it again (python app.py), then try again.", true);
        return;
      }
      const data = await res.json().catch(() => ({}));
      showMessage(data.error || "Could not make the invoices PDF.", true);
      return;
    }
    const count = Number(res.headers.get("X-Invoice-Count") || 0);
    const fresh = Number(res.headers.get("X-Invoice-New") || 0);
    const skipped = Number(res.headers.get("X-Invoice-Skipped") || 0);
    const skippedInfo = decodeURIComponent(res.headers.get("X-Invoice-Skipped-Info") || "");
    const cd = res.headers.get("Content-Disposition") || "";
    const nameMatch = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
    const filename = nameMatch ? decodeURIComponent(nameMatch[1]) : "invoices.pdf";
    const blobUrl = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = blobUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(blobUrl), 60000);

    // Same as single Print Invoice: flip each order's badge to "Invoice Printed · number
    // · by you" and its button to "Reprint Invoice" right now, from this response.
    try {
      const results = JSON.parse(decodeURIComponent(res.headers.get("X-Invoice-Results") || "[]"));
      const printedBy = decodeURIComponent(res.headers.get("X-Invoice-Printed-By") || "") || currentName;
      const byId = new Map(lastLoadedOrders.map((o) => [o.order_id, o]));
      for (const [orderId, invoiceNumber] of results) {
        const order = byId.get(orderId);
        if (order) { order.invoice_number = invoiceNumber; order.invoice_printed_by = printedBy; }
      }
    } catch (e) { /* the list reload below still brings everything up to date */ }

    let msg = `Downloaded ${count} invoice${count === 1 ? "" : "s"}` + (fresh ? ` (${fresh} newly numbered)` : "") + ".";
    if (skipped) msg += ` Skipped ${skipped}: ${skippedInfo}`;
    showMessage(msg, skipped > 0);
    bulkSelected.clear();
    bulkAllMode = false;
    renderOrders(filterByTrackQuery(lastLoadedOrders)); // instant: Printed badges + Reprint buttons
    loadOrders(); // then refresh everything from the server (dates, totals, Tally status)
  } catch (err) {
    showMessage("Could not reach the server to make the invoices PDF.", true);
  } finally {
    bulkPdfBtn.disabled = bulkSelected.size === 0;
    updateBulkUI();
  }
});

async function downloadTally(kind, buttonEl) {
  const params = new URLSearchParams();
  if (currentDateFrom) params.set("date_from", currentDateFrom);
  if (currentDateTo) params.set("date_to", currentDateTo);
  // Viewing the "Not exported" filter means the whole point of this export
  // is "everything I haven't sent yet" — force only_new regardless of the
  // checkbox, so a monthly export from that filter can never duplicate an
  // invoice even if the checkbox was left unticked.
  if (tallyOnlyNew.checked || currentTallyFilter === "not_exported") params.set("only_new", "1");

  const original = buttonEl.textContent;
  buttonEl.disabled = true;
  buttonEl.textContent = "Preparing…";
  try {
    const res = await fetch(`/api/invoices/export/tally.${kind}?${params.toString()}`);
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      showMessage(data.error || "Could not create the Tally export.", true);
      return;
    }
    const count = res.headers.get("X-Invoice-Count");
    const blob = await res.blob();
    const disposition = res.headers.get("Content-Disposition") || "";
    const match = /filename="?([^";]+)"?/.exec(disposition);
    const filename = match ? match[1] : `tally_invoices.${kind}`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    showMessage(`Exported ${count || ""} invoice(s) to ${kind.toUpperCase()}.`, false);
  } catch (err) {
    showMessage("Could not reach the server for the Tally export.", true);
  } finally {
    buttonEl.disabled = false;
    buttonEl.textContent = original;
  }
}

exportTallyXmlBtn.addEventListener("click", (e) => downloadTally("xml", e.currentTarget));

exportPendingBtn.addEventListener("click", () => {
  const params = new URLSearchParams();
  if (currentDateFrom) params.set("date_from", currentDateFrom);
  if (currentDateTo) params.set("date_to", currentDateTo);
  window.location.href = `/api/orders/export/pending.xlsx?${params.toString()}`;
});

// ---------------------------------------------------------------------------
// Settings modal (owner only)
// ---------------------------------------------------------------------------

settingsBtn.addEventListener("click", async () => {
  const res = await fetch("/api/settings");
  const data = await res.json();
  webhookInput.value = data.n8n_webhook_url || "";
  settingsModal.hidden = false;
  scheduleError.hidden = true;
  await loadScheduleSettings();
});

closeSettingsBtn.addEventListener("click", () => { settingsModal.hidden = true; });

saveSettingsBtn.addEventListener("click", async () => {
  await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ n8n_webhook_url: webhookInput.value.trim() }),
  });
  settingsModal.hidden = true;
  showMessage("Webhook URL saved.");
});

// ---------------------------------------------------------------------------
// Schedule settings (owner only — COD/Prepaid staff assignment)
// ---------------------------------------------------------------------------

async function loadScheduleSettings() {
  const res = await fetch("/api/settings/schedule");
  if (!res.ok) return;
  const data = await res.json();
  codThresholdInput.value = data.cod_shipping_threshold || "140";

  const fillStaffCheckboxes = (container, selectedIds, groupName) => {
    container.innerHTML = "";
    if (!data.staff.length) {
      container.innerHTML = `<span class="staff-checkbox-empty">No staff accounts yet — add one in Manage Users.</span>`;
      return;
    }
    for (const s of data.staff) {
      const id = `${groupName}-${s.id}`;
      const label = document.createElement("label");
      label.innerHTML = `<input type="checkbox" id="${id}" value="${s.id}"> ${escapeHtml(s.name)}`;
      label.querySelector("input").checked = selectedIds.map(String).includes(String(s.id));
      container.appendChild(label);
    }
  };
  fillStaffCheckboxes(codStaffList, data.cod_staff_ids || [], "cod");
  fillStaffCheckboxes(prepaidStaffList, data.prepaid_staff_ids || [], "prepaid");
}

function checkedStaffIds(container) {
  return Array.from(container.querySelectorAll("input[type=checkbox]:checked")).map((cb) => cb.value);
}

saveScheduleBtn.addEventListener("click", async () => {
  scheduleError.hidden = true;
  const res = await fetch("/api/settings/schedule", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      cod_shipping_threshold: codThresholdInput.value,
      cod_staff_ids: checkedStaffIds(codStaffList),
      prepaid_staff_ids: checkedStaffIds(prepaidStaffList),
    }),
  });
  const data = await res.json();
  if (!res.ok) {
    scheduleError.textContent = data.error || "Could not save schedule.";
    scheduleError.hidden = false;
    return;
  }
  showMessage("Schedule saved.");
  loadOrders();
});

// ---------------------------------------------------------------------------
// Account modal (everyone — change own password)
// ---------------------------------------------------------------------------

accountBtn.addEventListener("click", () => {
  currentPasswordInput.value = "";
  newPasswordInput.value = "";
  accountError.hidden = true;
  accountModal.hidden = false;
});

closeAccountBtn.addEventListener("click", () => { accountModal.hidden = true; });

savePasswordBtn.addEventListener("click", async () => {
  accountError.hidden = true;
  const res = await fetch("/api/me/password", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      current_password: currentPasswordInput.value,
      new_password: newPasswordInput.value,
    }),
  });
  const data = await res.json();
  if (!res.ok) {
    accountError.textContent = data.error || "Could not change password.";
    accountError.hidden = false;
    return;
  }
  accountModal.hidden = true;
  showMessage("Password changed.");
});

// ---------------------------------------------------------------------------
// Manage Users modal (owner only)
// ---------------------------------------------------------------------------

usersBtn.addEventListener("click", async () => {
  usersError.hidden = true;
  newUserName.value = "";
  newUserPassword.value = "";
  newUserRole.value = "staff";
  await loadUsers();
  usersModal.hidden = false;
});

closeUsersBtn.addEventListener("click", () => { usersModal.hidden = true; });

async function loadUsers() {
  const res = await fetch("/api/users");
  if (!res.ok) return;
  const users = await res.json();
  usersTableBody.innerHTML = "";
  for (const u of users) {
    const tr = document.createElement("tr");
    const isSelf = u.name === currentName;
    tr.innerHTML = `
      <td>${escapeHtml(u.name)}${isSelf ? " (you)" : ""}</td>
      <td>${ROLE_LABELS[u.role] || u.role}</td>
      <td class="users-table-actions"></td>
    `;
    const actionsCell = tr.querySelector(".users-table-actions");

    if (!isSelf) {
      const resetBtn = document.createElement("button");
      resetBtn.className = "btn btn-ghost btn-small";
      resetBtn.textContent = "Reset password";
      resetBtn.addEventListener("click", () => resetUserPassword(u.id, u.name));
      actionsCell.appendChild(resetBtn);

      const deleteBtn = document.createElement("button");
      deleteBtn.className = "btn btn-ghost btn-small";
      deleteBtn.textContent = "Remove";
      deleteBtn.addEventListener("click", () => deleteUser(u.id, u.name));
      actionsCell.appendChild(deleteBtn);
    }

    usersTableBody.appendChild(tr);
  }
}

async function resetUserPassword(userId, name) {
  const newPassword = prompt(`New password for ${name} (4+ characters):`);
  if (newPassword === null) return;
  const res = await fetch(`/api/users/${userId}/reset-password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ new_password: newPassword }),
  });
  const data = await res.json();
  if (!res.ok) {
    alert(data.error || "Could not reset password.");
    return;
  }
  showMessage(`Password reset for ${name}.`);
}

async function deleteUser(userId, name) {
  if (!confirm(`Remove ${name}'s account? This can't be undone.`)) return;
  const res = await fetch(`/api/users/${userId}`, { method: "DELETE" });
  const data = await res.json();
  if (!res.ok) {
    alert(data.error || "Could not remove user.");
    return;
  }
  loadUsers();
}

addUserBtn.addEventListener("click", async () => {
  usersError.hidden = true;
  const res = await fetch("/api/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: newUserName.value.trim(),
      password: newUserPassword.value,
      role: newUserRole.value,
    }),
  });
  const data = await res.json();
  if (!res.ok) {
    usersError.textContent = data.error || "Could not add user.";
    usersError.hidden = false;
    return;
  }
  newUserName.value = "";
  newUserPassword.value = "";
  newUserRole.value = "staff";
  loadUsers();
});

// ---------------------------------------------------------------------------
// Activity log (owner only)
// ---------------------------------------------------------------------------

const ACTIVITY_ACTION_LABELS = {
  status_update: "Status",
  purchase_amount_update: "Amount",
  packed_update: "Packed",
  cancelled_update: "Cancelled",
  repurchase_add: "Repurchase",
  repurchase_status: "Repurchase status",
  repurchase_delete: "Repurchase removed",
  repurchase_invoice: "Repurchase invoice",
  sync: "Sync",
  manual_add: "Added (paste)",
  order_edit: "Order edited",
  invoice_print: "Invoice printed",
  invoice_reprint: "Invoice reprinted",
  create_user: "New user",
  delete_user: "Removed user",
  reset_password: "Password reset",
  settings: "Settings",
  schedule: "Schedule",
};

activityLogBtn.addEventListener("click", async () => {
  activityLogSearch.value = "";
  activityLogOrderFilter = null;
  activityLogModal.hidden = false;
  await loadActivityLog();
});

function openActivityLogForOrder(orderId) {
  activityLogSearch.value = "";
  activityLogOrderFilter = orderId;
  activityLogModal.hidden = false;
  loadActivityLog();
}

closeActivityLogBtn.addEventListener("click", () => { activityLogModal.hidden = true; });

let activityLogDebounce;
let activityLogOrderFilter = null;
activityLogSearch.addEventListener("input", () => {
  // Typing a new search clears any "History"-link order filter, so it
  // doesn't silently restrict results the person no longer intends.
  activityLogOrderFilter = null;
  clearTimeout(activityLogDebounce);
  activityLogDebounce = setTimeout(loadActivityLog, 250);
});

async function loadActivityLog() {
  const search = activityLogSearch.value.trim();
  activityLogList.innerHTML = `<p class="empty-log">Loading…</p>`;
  const params = new URLSearchParams();
  if (search) params.set("search", search);
  if (activityLogOrderFilter) params.set("order_id", activityLogOrderFilter);
  const url = params.toString() ? `/api/activity-log?${params.toString()}` : "/api/activity-log";
  const res = await fetch(url);
  if (!res.ok) {
    activityLogList.innerHTML = `<p class="empty-log">Could not load activity log.</p>`;
    return;
  }
  const rows = await res.json();
  if (!rows.length) {
    activityLogList.innerHTML = search
      ? `<p class="empty-log">No activity matches "${escapeHtml(search)}".</p>`
      : `<p class="empty-log">No activity yet.</p>`;
    return;
  }
  activityLogList.innerHTML = rows
    .map(
      (r) => `
      <div class="log-row">
        <span class="log-action">${escapeHtml(ACTIVITY_ACTION_LABELS[r.action] || r.action)}</span>
        <div class="log-info">
          ${r.order_name ? `<span class="log-order">${escapeHtml(r.order_name)}</span> · ` : ""}${r.item_name ? `<strong>${escapeHtml(r.item_name)}</strong> — ` : ""}${escapeHtml(r.details || "")}
        </div>
        <div class="log-when">${escapeHtml(r.created_at)}</div>
      </div>
    `
    )
    .join("");
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

// Live loading progress (0-100%) for the first page load.
//  0-60%  waiting for the server/database (estimated from the last load time)
// 60-95%  downloading the orders (real bytes received)
// 95-100% rendering
function showOrdersLoading() {
  ordersContainer.innerHTML =
    '<div class="empty-state"><div class="glyph">Loading orders… <span id="loadPct">0%</span></div>' +
    '<div class="load-bar"><div class="load-bar-fill" id="loadBarFill"></div></div></div>';
}

function setOrdersProgress(pct) {
  pct = Math.max(0, Math.min(100, Math.round(pct)));
  const t = document.getElementById("loadPct");
  const f = document.getElementById("loadBarFill");
  if (t) t.textContent = pct + "%";
  if (f) f.style.width = pct + "%";
}

async function fetchOrdersWithProgress() {
  let expected = 3000;
  try { expected = Number(localStorage.getItem("ordersLoadMs")) || 3000; } catch (e) {}
  const t0 = performance.now();
  let gotHeaders = false;
  const timer = setInterval(() => {
    if (gotHeaders) return;
    const elapsed = performance.now() - t0;
    setOrdersProgress(Math.min(58, 60 * (1 - Math.exp(-1.6 * elapsed / expected))));
  }, 100);
  try {
    const res = await fetch("/api/orders");
    gotHeaders = true;
    const total = Number(res.headers.get("X-Raw-Length")) || 0;
    let data;
    if (res.body && total) {
      const reader = res.body.getReader();
      const chunks = [];
      let received = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        received += value.length;
        setOrdersProgress(60 + 35 * Math.min(1, received / total));
      }
      const buf = new Uint8Array(received);
      let off = 0;
      for (const c of chunks) { buf.set(c, off); off += c.length; }
      data = JSON.parse(new TextDecoder().decode(buf));
    } else {
      setOrdersProgress(80);
      data = await res.json();
    }
    setOrdersProgress(96);
    try { localStorage.setItem("ordersLoadMs", String(Math.round(performance.now() - t0))); } catch (e) {}
    return data;
  } finally {
    clearInterval(timer);
  }
}

(async function init() {
  // Show a loading state immediately; fetch user + the newest orders in parallel.
  showOrdersLoading();
  const seq = ++loadOrdersSeq;
  const _t0 = performance.now();
  const _timer = setInterval(() => {
    try { setOrdersProgress(Math.min(90, 90 * (performance.now() - _t0) / 1500)); } catch (e) {}
  }, 100);
  const firstFetch = fetchOrders({ limit: FIRST_PAGE })
    .catch(() => null)
    .finally(() => {
      clearInterval(_timer);
      try { setOrdersProgress(96); } catch (e) {}
    });
  await loadMe();
  const first = await firstFetch;
  if (!first || !Array.isArray(first.data)) {
    loadOrders();
    return;
  }
  if (seq !== loadOrdersSeq) return;
  lastLoadedOrders = first.data;
  renderOrders(filterByTrackQuery(first.data));
  if (!first.truncated) return;
  fetchOrders().then((all) => {
    if (seq !== loadOrdersSeq) return;
    lastLoadedOrders = all.data;
    renderOrders(filterByTrackQuery(all.data));
  });
})();
