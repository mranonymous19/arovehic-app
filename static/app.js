const STATUS_LABELS = {
  pending: "Pending",
  purchased: "Purchased",
  stock: "In Stock",
  na: "N/A",
};

const ROLE_LABELS = {
  owner: "Owner",
  staff: "Staff",
  telecaller: "Telecaller",
  packer: "Packer",
};

let currentFilter = "";
let currentPaymentFilter = "";
let currentDateFrom = "";
let currentDateTo = "";
let currentRole = null;
let currentName = "";
let lastLoadedOrders = [];
let orderTrackQuery = "";

const ordersContainer = document.getElementById("ordersContainer");
const orderTrackInput = document.getElementById("orderTrackInput");
const syncBtn = document.getElementById("syncBtn");
const syncMsg = document.getElementById("syncMsg");
const settingsBtn = document.getElementById("settingsBtn");
const settingsModal = document.getElementById("settingsModal");
const webhookInput = document.getElementById("webhookInput");
const saveSettingsBtn = document.getElementById("saveSettingsBtn");
const closeSettingsBtn = document.getElementById("closeSettingsBtn");
const filterButtons = document.querySelectorAll(".filter-btn[data-status]");
const paymentFilterButtons = document.querySelectorAll(".filter-btn[data-payment]");
const dateFromInput = document.getElementById("dateFromInput");
const dateToInput = document.getElementById("dateToInput");
const clearDateFilterBtn = document.getElementById("clearDateFilterBtn");

const codThresholdInput = document.getElementById("codThresholdInput");
const codStaffSelect = document.getElementById("codStaffSelect");
const prepaidStaffSelect = document.getElementById("prepaidStaffSelect");
const scheduleError = document.getElementById("scheduleError");
const saveScheduleBtn = document.getElementById("saveScheduleBtn");

const pasteOrderBtn = document.getElementById("pasteOrderBtn");
const pasteOrderModal = document.getElementById("pasteOrderModal");
const pasteOrderText = document.getElementById("pasteOrderText");
const pasteOrderError = document.getElementById("pasteOrderError");
const submitPasteOrderBtn = document.getElementById("submitPasteOrderBtn");
const closePasteOrderBtn = document.getElementById("closePasteOrderBtn");

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
  syncBtn.hidden = !isOwner;
  pasteOrderBtn.hidden = !isOwner;
  settingsBtn.hidden = !isOwner;
  usersBtn.hidden = !isOwner;
  activityLogBtn.hidden = !isOwner;
}

function canEditStatus() {
  return currentRole === "owner" || currentRole === "staff";
}

function canTogglePacked() {
  return currentRole === "owner" || currentRole === "packer";
}

// ---------------------------------------------------------------------------
// Orders
// ---------------------------------------------------------------------------

async function loadOrders() {
  const params = new URLSearchParams();
  if (currentFilter) params.set("status", currentFilter);
  if (currentPaymentFilter) params.set("payment", currentPaymentFilter);
  if (currentDateFrom) params.set("date_from", currentDateFrom);
  if (currentDateTo) params.set("date_to", currentDateTo);
  const url = params.toString() ? `/api/orders?${params.toString()}` : "/api/orders";
  const res = await fetch(url);
  const orders = await res.json();
  lastLoadedOrders = orders;
  renderOrders(filterByTrackQuery(orders));
}

function filterByTrackQuery(orders) {
  const q = orderTrackQuery.trim().toLowerCase();
  if (!q) return orders;
  return orders.filter((order) => {
    return (
      (order.order_name || "").toLowerCase().includes(q) ||
      (order.customer_name || "").toLowerCase().includes(q) ||
      (order.order_id || "").toLowerCase().includes(q)
    );
  });
}

let orderTrackDebounce;
orderTrackInput.addEventListener("input", () => {
  clearTimeout(orderTrackDebounce);
  orderTrackDebounce = setTimeout(() => {
    orderTrackQuery = orderTrackInput.value;
    renderOrders(filterByTrackQuery(lastLoadedOrders));
  }, 200);
});

function renderOrders(orders) {
  ordersContainer.innerHTML = "";

  if (!orders.length) {
    if (orderTrackQuery.trim()) {
      ordersContainer.innerHTML = `
        <div class="empty-state">
          <div class="glyph">— no match —</div>
          <p>No order matches "${escapeHtml(orderTrackQuery.trim())}".</p>
        </div>`;
      return;
    }
    ordersContainer.innerHTML = `
      <div class="empty-state">
        <div class="glyph">— empty manifest —</div>
        <p>No orders yet.${currentRole === "owner" ? " Set your n8n webhook in Settings, then hit Sync from Shopify." : " Ask an owner to sync from Shopify."}</p>
      </div>`;
    return;
  }

  for (const order of orders) {
    const card = document.createElement("div");
    card.className = "order-card";

    const head = document.createElement("div");
    head.className = "order-head";
    const paymentBadge = order.payment_type
      ? `<span class="payment-badge payment-badge-${order.payment_type}">${order.payment_type === "cod" ? "COD" : "Prepaid"}</span>`
      : "";
    const assignedTo = order.assigned_to
      ? `<span class="assigned-to">Assigned: ${escapeHtml(order.assigned_to)}</span>`
      : "";
    const showInvoiceBtn = currentFilter === "billing";
    head.innerHTML = `
      <span class="order-head-left">
        <span class="order-name">${escapeHtml(order.order_name)}</span>
        <span class="customer">${escapeHtml(order.customer_name || "")}</span>
        ${order.closed ? `<span class="order-closed-badge">Closed</span>` : ""}
        ${paymentBadge}
        ${assignedTo}
      </span>
      <span class="order-head-actions">
        ${showInvoiceBtn ? `<button type="button" class="btn btn-ghost btn-small order-invoice-link" data-order-id="${escapeHtml(order.order_id)}">${order.invoice_number ? "Reprint Invoice" : "Print Invoice"}</button>` : ""}
        ${currentRole === "owner" ? `<button type="button" class="order-history-link" data-order-id="${escapeHtml(order.order_id)}">History</button>` : ""}
      </span>
    `;
    if (showInvoiceBtn) {
      head.querySelector(".order-invoice-link").addEventListener("click", () => {
        window.open(`/api/orders/${encodeURIComponent(order.order_id)}/invoice.pdf`, "_blank");
      });
    }
    if (currentRole === "owner") {
      head.querySelector(".order-history-link").addEventListener("click", () => openActivityLogForOrder(order.order_id));
    }
    card.appendChild(head);

    for (const item of order.items) {
      card.appendChild(renderItemRow(item));
    }

    ordersContainer.appendChild(card);
  }
}

function renderItemRow(item) {
  const row = document.createElement("div");
  row.className = "item-row";

  const titleBlock = document.createElement("div");
  titleBlock.innerHTML = `
    <div class="item-title">${escapeHtml(item.title)}${item.variant_title ? ` — ${escapeHtml(item.variant_title)}` : ""}</div>
    <div class="item-meta">qty ${item.quantity} · ${escapeHtml(item.price || "")}${item.vendor ? " · " + escapeHtml(item.vendor) : ""}</div>
  `;

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

  const packedBlock = document.createElement("div");
  packedBlock.className = "packed-block";
  const packable = item.status === "purchased" || item.status === "stock";
  if (!packable) {
    packedBlock.innerHTML = `<div class="packed-readonly">—</div>`;
  } else if (canTogglePacked()) {
    const label = document.createElement("label");
    label.className = "packed-checkbox";
    label.innerHTML = `<input type="checkbox" ${item.packed ? "checked" : ""} /> Packed`;
    const checkbox = label.querySelector("input");
    checkbox.addEventListener("change", () => updatePacked(item.id, checkbox.checked, packedBlock));
    packedBlock.appendChild(label);
  } else {
    packedBlock.innerHTML = `<div class="packed-readonly ${item.packed ? "packed-yes" : ""}">${item.packed ? "Packed" : "Not packed"}</div>`;
  }

  row.appendChild(titleBlock);
  row.appendChild(purchaseBlock);
  row.appendChild(pills);
  row.appendChild(packedBlock);
  return row;
}

async function updatePacked(itemId, packed, blockEl) {
  const res = await fetch(`/api/items/${itemId}/packed`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ packed }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    showMessage(err.error || "Could not update packed status.", true);
    const checkbox = blockEl.querySelector("input");
    if (checkbox) checkbox.checked = !packed;
    return;
  }
  showMessage(packed ? "Marked as packed" : "Marked as not packed");
}

async function updatePurchaseAmount(itemId, value, inputEl) {
  const res = await fetch(`/api/items/${itemId}/purchase-amount`, {
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
  const res = await fetch(`/api/items/${itemId}/status`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  if (!res.ok) {
    showMessage("Could not update status.", true);
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

syncBtn.addEventListener("click", async () => {
  syncBtn.disabled = true;
  syncBtn.textContent = "Syncing…";
  try {
    const res = await fetch("/api/sync", { method: "POST" });
    const data = await res.json();
    if (!res.ok) {
      showMessage(data.error || "Sync failed.", true);
    } else {
      showMessage(`Synced ${data.orders_synced} orders, ${data.items_synced} items.`);
      loadOrders();
    }
  } catch (err) {
    showMessage("Sync failed: " + err.message, true);
  } finally {
    syncBtn.disabled = false;
    syncBtn.textContent = "Sync from Shopify";
  }
});

pasteOrderBtn.addEventListener("click", () => {
  pasteOrderText.value = "";
  pasteOrderError.hidden = true;
  pasteOrderModal.hidden = false;
});

closePasteOrderBtn.addEventListener("click", () => { pasteOrderModal.hidden = true; });

submitPasteOrderBtn.addEventListener("click", async () => {
  pasteOrderError.hidden = true;
  const text = pasteOrderText.value.trim();
  if (!text) {
    pasteOrderError.textContent = "Paste some order text first.";
    pasteOrderError.hidden = false;
    return;
  }
  submitPasteOrderBtn.disabled = true;
  submitPasteOrderBtn.textContent = "Adding…";
  try {
    const res = await fetch("/api/orders/paste", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const data = await res.json();
    if (!res.ok) {
      pasteOrderError.textContent = data.error || "Could not add order.";
      pasteOrderError.hidden = false;
      return;
    }
    pasteOrderModal.hidden = true;
    let msg = `Added ${data.items_added} item(s) across ${data.orders_touched} order(s).`;
    if (data.warnings && data.warnings.length) {
      msg += ` Skipped: ${data.warnings.join("; ")}`;
    }
    showMessage(msg, !!(data.warnings && data.warnings.length));
    loadOrders();
  } catch (err) {
    pasteOrderError.textContent = "Could not add order: " + err.message;
    pasteOrderError.hidden = false;
  } finally {
    submitPasteOrderBtn.disabled = false;
    submitPasteOrderBtn.textContent = "Add";
  }
});

filterButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    filterButtons.forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    currentFilter = btn.dataset.status;
    loadOrders();
  });
});

paymentFilterButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    paymentFilterButtons.forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    currentPaymentFilter = btn.dataset.payment;
    loadOrders();
  });
});

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

  const fillStaffSelect = (select, selectedId) => {
    select.innerHTML = `<option value="">— none —</option>`;
    for (const s of data.staff) {
      const opt = document.createElement("option");
      opt.value = s.id;
      opt.textContent = s.name;
      if (String(s.id) === String(selectedId)) opt.selected = true;
      select.appendChild(opt);
    }
  };
  fillStaffSelect(codStaffSelect, data.cod_staff_id);
  fillStaffSelect(prepaidStaffSelect, data.prepaid_staff_id);
}

saveScheduleBtn.addEventListener("click", async () => {
  scheduleError.hidden = true;
  const res = await fetch("/api/settings/schedule", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      cod_shipping_threshold: codThresholdInput.value,
      cod_staff_id: codStaffSelect.value,
      prepaid_staff_id: prepaidStaffSelect.value,
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
  sync: "Sync",
  manual_add: "Added (paste)",
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

(async function init() {
  await loadMe();
  loadOrders();
})();
