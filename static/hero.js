// Layout helpers for the restyled UI. Only drives the look (titles, nav state,
// menu, toasts); every order action still goes through app.js unchanged.
(function () {
  var titles = { orders: 'Order desk', billing: 'Billing', trash: 'Trash', cancelled: 'Cancelled orders' };
  var page = document.querySelector('.page');
  var title = document.getElementById('heroTitle');
  var status = document.getElementById('statusFilterSelect');
  var row = document.getElementById('invoiceFilterRow');
  var navOrders = document.getElementById('navOrders');
  var navBilling = document.getElementById('navBilling');
  var navMap = { orders: navOrders, billing: navBilling,
    trash: document.getElementById('trashBtn'), cancelled: document.getElementById('cancelledBtn') };

  // ---- Sync integrity pill lives in the middle of the top bar ----
  // (also fixes pages served from an older index.html where it sat above the filters)
  var tbInner = document.querySelector('.topbar-inner');
  var syncCard = document.getElementById('syncHealthCard');
  if (tbInner && syncCard && !tbInner.contains(syncCard)) {
    tbInner.insertBefore(syncCard, tbInner.querySelector('.topbar-right'));
  }

  // ---- Logo: swap the old icon for the Arovehic logo if an older index.html is served ----
  var mark = document.querySelector('.brand-mark');
  if (mark && !mark.querySelector('img')) {
    mark.innerHTML = '<img src="/static/img/logo-144.png" alt="" width="36" height="36">';
  }

  // ---- Dark mode: switch inside the Menu, remembered in this browser ----
  var THEME_KEY = 'arovehic-theme';
  function applyTheme(dark) {
    var root = document.documentElement;
    // Switch colours in one go: no colour animations on thousands of elements.
    root.classList.add('theme-switching');
    if (dark) root.setAttribute('data-theme', 'dark');
    else root.removeAttribute('data-theme');
    requestAnimationFrame(function () { requestAnimationFrame(function () { root.classList.remove('theme-switching'); }); });
    var t = document.getElementById('themeToggle');
    if (t) t.setAttribute('aria-checked', dark ? 'true' : 'false');
  }
  var savedDark = false;
  try { savedDark = localStorage.getItem(THEME_KEY) === 'dark'; } catch (e) {}
  var menuActions = document.querySelector('.menu-panel .topbar-actions');
  if (menuActions && !document.getElementById('themeToggle')) {
    var tt = document.createElement('button');
    tt.type = 'button'; tt.id = 'themeToggle'; tt.className = 'menu-item theme-toggle';
    tt.setAttribute('role', 'menuitemcheckbox');
    tt.setAttribute('data-hint', 'Easier on the eyes at night');
    tt.appendChild(document.createTextNode('Dark mode'));
    var sw = document.createElement('span'); sw.className = 'switch'; sw.setAttribute('aria-hidden', 'true');
    tt.appendChild(sw);
    var divider = menuActions.querySelector('.menu-divider');
    menuActions.insertBefore(tt, divider || null);
    tt.addEventListener('click', function () {
      var dark = document.documentElement.getAttribute('data-theme') !== 'dark';
      applyTheme(dark);
      try { localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light'); } catch (e) {}
    });
  }
  applyTheme(savedDark);

  // ---- Today's date pill (en-IN, e.g. "Mon, 26 Oct, 2026") ----
  var datePill = document.querySelector('[data-today]');
  if (datePill) {
    datePill.textContent = new Date().toLocaleDateString('en-IN',
      { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
  }

  // ---- Current view -> title + active nav item ----
  var currentKey = null;
  function viewKey() {
    var mode = (typeof currentFilter !== 'undefined') ? currentFilter : '';
    if (mode === 'trash' || mode === 'cancelled') return mode;
    var billing = (status && status.value === 'billing') || (row && !row.hidden);
    return billing ? 'billing' : 'orders';
  }
  function navVisibility() {
    if (!status) return;
    // Accounts / Packer are locked to Billing; Staff can't open Billing.
    var lockedBilling = status.disabled && status.value === 'billing';
    if (navOrders) navOrders.hidden = lockedBilling;
    if (navBilling) navBilling.hidden = status.disabled && !lockedBilling;
  }
  function sync() {
    navVisibility();
    var key = viewKey();
    Object.keys(navMap).forEach(function (k) {
      if (navMap[k]) navMap[k].classList.toggle('is-active', k === key);
    });
    if (key === currentKey) return;
    currentKey = key;
    if (title) title.textContent = titles[key];
    if (page) { page.classList.remove('view-in'); void page.offsetWidth; page.classList.add('view-in'); }
  }

  if (status) {
    status.addEventListener('change', function () { setTimeout(sync, 0); });
    new MutationObserver(function () { setTimeout(sync, 0); })
      .observe(status, { attributes: true, attributeFilter: ['disabled', 'hidden'] });
  }
  if (row) new MutationObserver(sync).observe(row, { attributes: true, attributeFilter: ['hidden'] });
  ['trashBtn', 'cancelledBtn'].forEach(function (id) {
    var b = document.getElementById(id);
    if (b) b.addEventListener('click', function () { setTimeout(sync, 0); });
  });

  // Orders / Billing nav items drive the existing Status dropdown, exactly like
  // a user picking the option there, so app.js handles the switch itself.
  function setStatus(value) {
    if (!status || status.disabled) return;
    status.value = value;
    status.dispatchEvent(new Event('change'));
  }
  if (navOrders) navOrders.addEventListener('click', function () {
    if (!status || viewKey() === 'orders') return;
    // From Trash / Cancelled / Billing back to the normal order list.
    setStatus(status.value === 'billing' ? '' : status.value);
  });
  if (navBilling) navBilling.addEventListener('click', function () {
    if (viewKey() !== 'billing') setStatus('billing');
  });
  sync();

  // ---- Top-bar menu (custom dropdown) ----
  var menuBtn = document.getElementById('menuToggle');
  var panel = document.querySelector('.menu-panel');
  function items() {
    return Array.prototype.filter.call(panel.querySelectorAll('.menu-item'), function (el) {
      return !el.hidden && !el.disabled;
    });
  }
  function setMenu(open, focusFirst) {
    if (!menuBtn || !panel) return;
    panel.classList.toggle('hidden-view', !open);
    menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open && focusFirst) { var f = items()[0]; if (f) f.focus(); }
  }
  function menuOpen() { return panel && !panel.classList.contains('hidden-view'); }
  if (menuBtn && panel) {
    menuBtn.addEventListener('click', function () { setMenu(!menuOpen()); });
    menuBtn.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); setMenu(true, true); }
    });
    panel.addEventListener('click', function (e) {
      // The dark mode switch keeps the menu open so you can see it flip.
      if (e.target.closest('.menu-item') && !e.target.closest('.theme-toggle')) setMenu(false);
    });
    panel.addEventListener('keydown', function (e) {
      var list = items(), i = list.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') { e.preventDefault(); (list[i + 1] || list[0]).focus(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); (list[i - 1] || list[list.length - 1]).focus(); }
      else if (e.key === 'Tab') { setMenu(false); }
    });
    document.addEventListener('mousedown', function (e) {
      if (menuOpen() && !e.target.closest('.menu-wrap')) setMenu(false);
    });
  }

  // ---- Popups: header close (X), click outside, Esc ----
  var xIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  document.querySelectorAll('.modal').forEach(function (m) {
    var box = m.querySelector('.modal-box');
    if (!box) return;
    var x = document.createElement('button');
    x.type = 'button'; x.className = 'modal-close'; x.setAttribute('aria-label', 'Close'); x.innerHTML = xIcon;
    x.addEventListener('click', function () { m.hidden = true; });
    var head = box.querySelector('.modal-head');
    if (head) head.appendChild(x); else box.insertBefore(x, box.firstChild);
    m.addEventListener('mousedown', function (e) { if (e.target === m) m.hidden = true; });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (menuOpen()) { setMenu(false); menuBtn.focus(); return; }
    document.querySelectorAll('.modal:not([hidden])').forEach(function (m) { m.hidden = true; });
  });

  // ---- Order card actions: secondary buttons go into a "More" menu ----
  // The original buttons (with their app.js click handlers) are moved, not copied.
  var ROW_ITEMS = [
    { sel: '.order-repurchase-link', hint: 'Re-send damaged items', ic: 'ic-repurchase' },
    { sel: '.order-history-link', hint: 'Activity for this order', ic: 'ic-history' },
    { sel: '.order-cancelled-checkbox', hint: 'Tick to mark order cancelled', ic: 'ic-cancel', danger: true },
    { sel: '.order-bill-cancel-link', hint: 'Skipped in Tally export', ic: 'ic-bill', danger: true },
    { sel: '.order-delete-link', hint: 'Move order to Trash', ic: 'ic-trash', danger: true }
  ];
  var dotsIcon = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>';
  var openRow = null;
  function closeRow(focusBtn) {
    if (!openRow) return;
    openRow.panel.classList.add('hidden-view');
    openRow.btn.setAttribute('aria-expanded', 'false');
    var card = openRow.btn.closest('.order-card');
    if (card) card.classList.remove('menu-open');
    if (focusBtn) openRow.btn.focus();
    openRow = null;
  }
  function rowItems(panel) {
    return Array.prototype.filter.call(panel.querySelectorAll('.row-menu-item'), function (el) { return !el.hidden; });
  }
  // One pre-built "More" button + panel, cloned per card (much cheaper than building each).
  var rowTpl = document.createElement('div');
  rowTpl.className = 'row-menu';
  rowTpl.innerHTML = '<button type="button" class="row-menu-btn" aria-haspopup="true" aria-expanded="false" aria-label="More actions" title="More actions">' +
    dotsIcon + '<span>More</span></button><div class="row-menu-panel hidden-view" role="menu"></div>';
  var dividerTpl = document.createElement('div'); dividerTpl.className = 'menu-divider';

  function enhanceHead(head) {
    if (head.hasAttribute('data-enhanced')) return;
    head.setAttribute('data-enhanced', '1');
    var actions = head.querySelector('.order-head-actions');
    if (!actions) return;
    var found = [];
    for (var i = 0; i < ROW_ITEMS.length; i++) {
      var el = actions.querySelector(ROW_ITEMS[i].sel);
      if (el) found.push({ d: ROW_ITEMS[i], el: el });
    }
    if (found.length < 2) return; // one extra button fits fine on its own
    var wrap = rowTpl.cloneNode(true);
    var panel = wrap.lastChild;
    var dividerAdded = false;
    for (var j = 0; j < found.length; j++) {
      var d = found[j].d, el2 = found[j].el;
      if (d.danger && !dividerAdded && panel.firstChild) panel.appendChild(dividerTpl.cloneNode(false));
      if (d.danger) dividerAdded = true;
      el2.classList.remove('btn', 'btn-ghost', 'btn-small', 'btn-danger');
      el2.classList.add('row-menu-item', d.ic);
      if (d.danger) el2.classList.add('danger');
      el2.setAttribute('data-hint', d.hint);
      el2.setAttribute('role', 'menuitem');
      if (el2.tagName === 'LABEL') el2.tabIndex = -1;
      panel.appendChild(el2);
    }
    actions.appendChild(wrap);
  }

  // One shared set of listeners for every card's "More" menu (instead of 4 per card).
  function openRowMenu(btn) {
    var panel = btn.nextElementSibling;
    var isOpen = openRow && openRow.panel === panel;
    closeRow();
    if (isOpen) return;
    var card = btn.closest('.order-card');
    if (card) card.classList.add('menu-open'); // let the dropdown overflow the card
    panel.classList.remove('hidden-view', 'up');
    // Open upwards when there isn't room above the bottom navigation.
    var r = btn.getBoundingClientRect();
    if (r.bottom + panel.offsetHeight + 96 > window.innerHeight && r.top > panel.offsetHeight + 70) panel.classList.add('up');
    btn.setAttribute('aria-expanded', 'true');
    openRow = { panel: panel, btn: btn };
  }
  var ordersRoot = document.getElementById('ordersContainer');
  if (ordersRoot) {
    ordersRoot.addEventListener('click', function (e) {
      var btn = e.target.closest('.row-menu-btn');
      if (btn) { openRowMenu(btn); return; }
      // Close after picking an item (the checkbox fires its own change first).
      if (e.target.closest('.row-menu-panel .row-menu-item') && e.target.tagName !== 'LABEL') setTimeout(function () { closeRow(); }, 0);
    });
    ordersRoot.addEventListener('keydown', function (e) {
      var btn = e.target.closest && e.target.closest('.row-menu-btn');
      if (btn && e.key === 'ArrowDown') {
        e.preventDefault();
        if (!openRow || openRow.btn !== btn) openRowMenu(btn);
        var f = rowItems(btn.nextElementSibling)[0]; if (f) (f.querySelector('input') || f).focus();
        return;
      }
      var panel = e.target.closest && e.target.closest('.row-menu-panel');
      if (!panel) return;
      var list = rowItems(panel);
      var i = list.indexOf(document.activeElement.closest('.row-menu-item')), next = null;
      if (e.key === 'ArrowDown') next = list[i + 1] || list[0];
      else if (e.key === 'ArrowUp') next = list[i - 1] || list[list.length - 1];
      else if (e.key === 'Tab') closeRow();
      if (next) { e.preventDefault(); (next.querySelector('input') || next).focus(); }
    });
  }
  // ---- Speed: watch app.js requests (read-only) for instant feedback ----
  // Requests and responses pass through unchanged; we only react to them.
  var nextStatusSave = null;
  var loadingCount = 0;
  function setLoading(on) {
    loadingCount = Math.max(0, loadingCount + (on ? 1 : -1));
    document.body.classList.toggle('is-loading', loadingCount > 0);
  }
  if (window.fetch) {
    var origFetch = window.fetch;
    window.fetch = function (input, init) {
      var req = origFetch.apply(this, arguments);
      try {
        var url = typeof input === 'string' ? input : (input && input.url) || '';
        var method = ((init && init.method) || 'GET').toUpperCase();
        if (method === 'POST' && /\/api\/items\/[^/]+\/status$/.test(url) && nextStatusSave) {
          var cb = nextStatusSave; nextStatusSave = null;
          req.then(function (r) { cb(r.ok); }, function () { cb(false); });
        } else if (method === 'GET' && /^\/api\/orders\?(.*&)?limit=/.test(url)) {
          // A list reload started (filter, nav, date...): dim the list until it re-renders.
          setLoading(true);
          var done = false;
          var finish = function () { if (!done) { done = true; setLoading(false); } };
          req.then(function () { setTimeout(finish, 4000); }, finish);
          if (ordersBoxForLoading) {
            var mo = new MutationObserver(function () { mo.disconnect(); finish(); });
            mo.observe(ordersBoxForLoading, { childList: true });
            setTimeout(function () { mo.disconnect(); }, 15000);
          }
        }
      } catch (e) {}
      return req;
    };
  }
  var ordersBoxForLoading = document.getElementById('ordersContainer');

  // ---- Item status: show only the selected status; click it to expand ----
  // app.js still saves the status; this only shows/hides the other pills.
  var openPills = null;
  function closePills(box) {
    box = box || openPills;
    if (!box) return;
    box.classList.remove('open');
    var a = box.querySelector('.pill.active');
    if (a) a.setAttribute('aria-expanded', 'false');
    if (openPills === box) openPills = null;
  }
  function openPillsBox(box) {
    if (openPills && openPills !== box) closePills(openPills);
    box.classList.add('open');
    var a = box.querySelector('.pill.active');
    if (a) a.setAttribute('aria-expanded', 'true');
    openPills = box;
  }
  // One listener for every status chip in the list (no per-item setup needed).
  // Changes show instantly. If a save for the same item is still on its way, the new
  // choice is NOT blocked: it shows at once and is sent right after the first save
  // returns, so the last status you tapped is always the one that gets saved.
  function startSave(box, pill, prev) {
    box._saving = true; box._want = null;
    box.classList.add('saving');
    // Safety net: no reply at all (weak connection) -> stop waiting after 8 s
    var unlock = setTimeout(function () { box._saving = false; box._want = null; box.classList.remove('saving'); }, 8000);
    nextStatusSave = function (ok) {
      clearTimeout(unlock);
      var want = box._want;
      box._saving = false; box._want = null;
      box.classList.remove('saving');
      if (!ok) {
        // Server said no: show the status from before (app.js shows the error)
        var cur = box.querySelector('.pill.active'); if (cur) cur.classList.remove('active');
        if (prev) prev.classList.add('active');
        return;
      }
      if (want && want !== pill && want.isConnected) {
        // Runs right after app.js has marked `pill` as saved: send the newer choice
        Promise.resolve().then(function () { box._replay = want; want.click(); });
      }
    };
  }
  if (ordersRoot) {
    ordersRoot.addEventListener('click', function (e) {
      var pill = e.target.closest('.item-row .status-pills > .pill');
      if (!pill || pill.disabled) return;
      var box = pill.parentNode;
      if (box._replay === pill) {
        // Our own follow-up save of the newer choice: let app.js send it
        box._replay = null;
        var saved = box.querySelector('.pill.active');
        if (saved && saved !== pill) saved.classList.remove('active');
        pill.classList.add('active');
        startSave(box, pill, saved);
        return;
      }
      var prev = box.querySelector('.pill.active');
      if (!prev) return; // nothing selected yet: all options show, normal behaviour
      if (pill === prev) {
        // Selected pill = open/close toggle; never re-saves the same status.
        e.stopPropagation();
        if (box.classList.contains('open')) closePills(box); else openPillsBox(box);
        return;
      }
      if (!box.classList.contains('open')) { e.stopPropagation(); return; }
      prev.classList.remove('active');
      pill.classList.add('active');
      closePills(box);
      if (box._saving) {
        // A save for this item is still on its way: show the new choice now, send it after
        e.stopPropagation();
        box._want = pill;
        return;
      }
      startSave(box, pill, prev); // app.js sends this save right after this handler
    }, true);
  }
  document.addEventListener('mousedown', function (e) {
    if (openPills && !openPills.contains(e.target)) closePills();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && openPills) { e.stopImmediatePropagation(); closePills(); }
  }, true);

  var ordersBox = document.getElementById('ordersContainer');
  if (ordersBox) {
    // Every batch of cards (100 at a time) gets its "More" menu in the same moment it is
    // drawn — before the screen updates — and nothing changes in the background after
    // that. (A background queue that kept hiding/showing buttons made iPhone Safari
    // treat taps as "hover" and cancel them, so the page looked frozen.)
    new MutationObserver(function () {
      var heads = ordersBox.getElementsByClassName('order-head');
      for (var i = 0; i < heads.length; i++) if (!heads[i].hasAttribute('data-enhanced')) enhanceHead(heads[i]);
    }).observe(ordersBox, { childList: true });
  }
  document.addEventListener('mousedown', function (e) {
    if (openRow && !e.target.closest('.row-menu')) closeRow();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && openRow) { e.stopImmediatePropagation(); closeRow(true); }
  }, true);

  // ---- Custom dropdowns (replace the browser's native option list) ----
  // The real <select> stays in the page with the same ID (hidden): picking an item sets
  // its value and fires "change", so app.js works exactly as before. Values app.js sets
  // in code (select.value = ...), disabled/hidden and new options are mirrored here.
  var chevSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>';
  var checkSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
  var ddPanel = document.createElement('div');
  ddPanel.className = 'dd-panel hidden-view'; ddPanel.setAttribute('role', 'listbox');
  document.body.appendChild(ddPanel);
  var ddOpen = null; // { sel, btn }
  var valueDesc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
  var indexDesc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'selectedIndex');

  function ddLabel(sel) {
    var o = sel.options[sel.selectedIndex];
    return o ? o.textContent.trim() : '';
  }
  function ddRefresh(sel) {
    var btn = sel._ddBtn; if (!btn) return;
    btn.querySelector('.dd-value').textContent = ddLabel(sel) || ' ';
    btn.setAttribute('data-value', sel.value);
    btn.disabled = sel.disabled;
    btn.hidden = sel.hidden;
    if (ddOpen && ddOpen.sel === sel) ddBuild(sel);
  }
  function ddClose(focusBtn) {
    if (!ddOpen) return;
    ddPanel.classList.add('hidden-view');
    ddOpen.btn.setAttribute('aria-expanded', 'false');
    if (focusBtn) ddOpen.btn.focus();
    ddOpen = null;
  }
  function ddBuild(sel) {
    var html = '';
    for (var i = 0; i < sel.options.length; i++) {
      var o = sel.options[i];
      if (o.hidden || o.disabled) continue;
      var on = i === sel.selectedIndex;
      html += '<div class="dd-item' + (on ? ' selected' : '') + '" role="option" aria-selected="' + on + '" data-index="' + i +
        '" data-value="' + o.value.replace(/"/g, '&quot;') + '"><span class="dd-text"></span>' + (on ? checkSvg : '') + '</div>';
    }
    ddPanel.innerHTML = html;
    if (sel._ddFooter) ddPanel.appendChild(sel._ddFooter); // e.g. Risk "Custom range" box
    var spans = ddPanel.querySelectorAll('.dd-text'), k = 0;
    for (var j = 0; j < sel.options.length; j++) {
      if (sel.options[j].hidden || sel.options[j].disabled) continue;
      spans[k++].textContent = sel.options[j].textContent.trim();
    }
    ddPanel.setAttribute('data-for', sel.id || '');
  }
  function ddPlace(btn) {
    var r = btn.getBoundingClientRect();
    ddPanel.style.minWidth = r.width + 'px';
    ddPanel.style.left = Math.max(8, Math.min(r.left, window.innerWidth - Math.max(r.width, 200) - 8)) + 'px';
    var below = window.innerHeight - r.bottom - 12, above = r.top - 12;
    var h = Math.min(ddPanel.scrollHeight, 380);
    if (below < h && above > below) { ddPanel.style.top = ''; ddPanel.style.bottom = (window.innerHeight - r.top + 6) + 'px'; ddPanel.style.maxHeight = Math.min(380, above) + 'px'; }
    else { ddPanel.style.bottom = ''; ddPanel.style.top = (r.bottom + 6) + 'px'; ddPanel.style.maxHeight = Math.min(380, Math.max(below, 160)) + 'px'; }
  }
  function ddOpenFor(sel) {
    var btn = sel._ddBtn;
    if (btn.disabled) return;
    if (ddOpen && ddOpen.sel === sel) { ddClose(); return; }
    ddClose();
    ddBuild(sel);
    ddPanel.classList.remove('hidden-view');
    ddPlace(btn);
    btn.setAttribute('aria-expanded', 'true');
    ddOpen = { sel: sel, btn: btn };
    var cur = ddPanel.querySelector('.dd-item.selected') || ddPanel.querySelector('.dd-item');
    if (cur) { cur.classList.add('focus'); cur.scrollIntoView({ block: 'nearest' }); }
  }
  function ddPick(item) {
    var sel = ddOpen.sel, idx = +item.getAttribute('data-index');
    ddClose(true);
    if (idx === sel.selectedIndex) return;
    sel.selectedIndex = idx;
    sel.dispatchEvent(new Event('input', { bubbles: true }));
    sel.dispatchEvent(new Event('change', { bubbles: true }));
  }
  function ddMove(step) {
    var items = ddPanel.querySelectorAll('.dd-item'); if (!items.length) return;
    var cur = ddPanel.querySelector('.dd-item.focus'), i = Array.prototype.indexOf.call(items, cur);
    var n = items[Math.max(0, Math.min(items.length - 1, i + step))];
    if (cur) cur.classList.remove('focus');
    n.classList.add('focus'); n.scrollIntoView({ block: 'nearest' });
  }
  function inFooter(el) { return !!(el && el.closest && el.closest('.dd-footer')); }
  ddPanel.addEventListener('mousedown', function (e) { if (!inFooter(e.target)) e.preventDefault(); }); // keep focus on the button
  ddPanel.addEventListener('click', function (e) {
    var item = e.target.closest('.dd-item'); if (item && ddOpen) ddPick(item);
  });
  ddPanel.addEventListener('mousemove', function (e) {
    var item = e.target.closest('.dd-item'); if (!item || item.classList.contains('focus')) return;
    var f = ddPanel.querySelector('.dd-item.focus'); if (f) f.classList.remove('focus');
    item.classList.add('focus');
  });
  document.addEventListener('mousedown', function (e) {
    if (ddOpen && !ddPanel.contains(e.target) && !ddOpen.btn.contains(e.target)) ddClose();
  });
  document.addEventListener('keydown', function (e) {
    if (ddOpen && e.key === 'Escape') { e.stopImmediatePropagation(); ddClose(true); }
  }, true);
  // (Typing in a footer box opens the phone keyboard, which resizes/scrolls the page — keep it open then.)
  window.addEventListener('resize', function () { if (!inFooter(document.activeElement)) ddClose(); });
  document.addEventListener('scroll', function (e) {
    if (ddOpen && e.target !== ddPanel && !inFooter(document.activeElement)) ddClose();
  }, true);

  window.heroDdClose = function () { ddClose(); };
  function ddEnhance(sel) {
    if (sel._ddBtn || sel.multiple) return;
    var btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'dd-btn';
    btn.setAttribute('aria-haspopup', 'listbox'); btn.setAttribute('aria-expanded', 'false');
    btn.innerHTML = '<span class="dd-value"></span>' + chevSvg;
    if (sel.id) btn.setAttribute('data-for', sel.id);
    sel._ddBtn = btn;
    sel.classList.add('dd-native'); sel.tabIndex = -1; sel.setAttribute('aria-hidden', 'true');
    sel.parentNode.insertBefore(btn, sel.nextSibling);
    // Label "for" the select should open/focus the new button
    if (sel.id) {
      var lab = document.querySelector('label[for="' + sel.id + '"]');
      if (lab) { lab.addEventListener('click', function (e) { e.preventDefault(); btn.focus(); }); var lid = lab.id || ('lbl-' + sel.id); lab.id = lid; btn.setAttribute('aria-labelledby', lid); }
    }
    btn.addEventListener('click', function () { ddOpenFor(sel); });
    btn.addEventListener('keydown', function (e) {
      var k = e.key;
      if (!ddOpen || ddOpen.sel !== sel) {
        if (k === 'ArrowDown' || k === 'ArrowUp' || k === 'Enter' || k === ' ') { e.preventDefault(); ddOpenFor(sel); }
        return;
      }
      if (k === 'ArrowDown') { e.preventDefault(); ddMove(1); }
      else if (k === 'ArrowUp') { e.preventDefault(); ddMove(-1); }
      else if (k === 'Enter' || k === ' ') { e.preventDefault(); var f = ddPanel.querySelector('.dd-item.focus'); if (f) ddPick(f); }
      else if (k === 'Tab') ddClose();
      else if (k.length === 1) { // type a letter to jump (e.g. states)
        var items = ddPanel.querySelectorAll('.dd-item'), cur = ddPanel.querySelector('.dd-item.focus');
        var start = Array.prototype.indexOf.call(items, cur), low = k.toLowerCase();
        for (var s = 1; s <= items.length; s++) {
          var it = items[(start + s) % items.length];
          if (it.textContent.trim().toLowerCase().indexOf(low) === 0) { if (cur) cur.classList.remove('focus'); it.classList.add('focus'); it.scrollIntoView({ block: 'nearest' }); break; }
        }
      }
    });
    // Mirror values set in code, and option / disabled / hidden changes
    Object.defineProperty(sel, 'value', { configurable: true,
      get: function () { return valueDesc.get.call(this); },
      set: function (v) { valueDesc.set.call(this, v); ddRefresh(this); } });
    Object.defineProperty(sel, 'selectedIndex', { configurable: true,
      get: function () { return indexDesc.get.call(this); },
      set: function (v) { indexDesc.set.call(this, v); ddRefresh(this); } });
    new MutationObserver(function () { ddRefresh(sel); })
      .observe(sel, { childList: true, subtree: true, attributes: true, attributeFilter: ['disabled', 'hidden', 'selected'] });
    sel.addEventListener('change', function () { ddRefresh(sel); });
    ddRefresh(sel);
  }
  // All selects outside the order list (filters + popups), plus ones popups add later
  document.querySelectorAll('select').forEach(function (s) { if (!s.closest('#ordersContainer')) ddEnhance(s); });
  ['eoItemsList', 'moItemsList'].forEach(function (id) {
    var host = document.getElementById(id); if (!host) return;
    new MutationObserver(function () { host.querySelectorAll('select').forEach(ddEnhance); })
      .observe(host, { childList: true, subtree: true });
  });

  // ---- Toasts: mirror every message app.js shows in #syncMsg ----
  var stack = document.querySelector('.toast-stack');
  var msg = document.getElementById('syncMsg');
  var okIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
  var errIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  function toast(text, isError) {
    if (!stack || !text) return;
    var t = document.createElement('div');
    t.className = 'toast' + (isError ? ' error' : '');
    t.setAttribute('role', isError ? 'alert' : 'status');
    var dot = document.createElement('span');
    dot.className = 'toast-dot'; dot.innerHTML = isError ? errIcon : okIcon;
    var span = document.createElement('span');
    span.className = 'toast-text'; span.textContent = text;
    t.appendChild(dot); t.appendChild(span);
    stack.appendChild(t);
    while (stack.children.length > 4) stack.removeChild(stack.firstChild);
    setTimeout(function () {
      t.classList.add('out');
      setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 300);
    }, 2600);
  }
  if (msg) {
    new MutationObserver(function (records) {
      var changed = records.some(function (r) { return r.type === 'childList'; });
      if (changed && !msg.hidden) toast(msg.textContent.trim(), msg.classList.contains('error'));
    }).observe(msg, { childList: true, attributes: true, attributeFilter: ['hidden'] });
  }
})();
