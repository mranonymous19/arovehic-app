(function () {
  var U = 'https://images.unsplash.com/photo-';
  var scenes = {
    orders: { img: U + '1570129476815-ba368ac77013', title: 'Order desk',
      sub: 'Track purchases, status and dispatch for every order.',
      page: '1698856712618-448b660a8d1f', who: 'todd kent', url: 'https://unsplash.com/photos/onnJOfF-okU' },
    trash: { img: U + '1570129476815-ba368ac77013', page: '1698856712618-448b660a8d1f', title: 'Trash',
      sub: 'Removed orders. Restore brings an order back to the order desk.', who: 'todd kent', url: 'https://unsplash.com/photos/onnJOfF-okU' },
    cancelled: { img: U + '1570129476815-ba368ac77013', page: '1698856712618-448b660a8d1f', title: 'Cancelled orders',
      sub: 'Orders that were cancelled.', who: 'todd kent', url: 'https://unsplash.com/photos/onnJOfF-okU' },
    billing: { img: U + '1768661608008-74f74ad6923a', title: 'Billing',
      sub: 'Print invoices, export to Tally and check what is still unbilled.',
      page: '1768661608008-74f74ad6923a', who: 'heino eisner', url: 'https://unsplash.com/photos/e4Tr3gRDnd0' }
  };
  var hero = document.getElementById('heroBanner');
  if (!hero) return;
  var title = document.getElementById('heroTitle'),
      sub = document.getElementById('heroSub'),
      credit = document.getElementById('heroCredit'),
      status = document.getElementById('statusFilterSelect'),
      row = document.getElementById('invoiceFilterRow');
  function set(key) {
    var s = scenes[key];
    if (hero.dataset.scene === key) return;
    hero.dataset.scene = key;
    hero.style.backgroundImage = 'linear-gradient(90deg,rgba(14,18,23,.88),rgba(14,18,23,.35)),url("' + s.img + '?fm=jpg&q=75&w=2000&fit=max")';
    document.body.style.setProperty('--page-img', 'url("' + U + s.page + '?fm=jpg&q=70&w=2400&fit=max")');
    title.textContent = s.title; sub.textContent = s.sub;
    credit.textContent = 'Photo by ' + s.who + ' on Unsplash'; credit.href = s.url;
  }
  function sync() {
    var mode = (typeof currentFilter !== 'undefined') ? currentFilter : '';
    var billing = (status && status.value === 'billing') || (row && !row.hidden);
    var key = (mode === 'trash' || mode === 'cancelled') ? mode : (billing ? 'billing' : 'orders');
    set(key);
    var back = document.getElementById('heroBack');
    if (back) back.hidden = !(key === 'trash' || key === 'cancelled');
    ['trashBtn', 'cancelledBtn'].forEach(function (id) {
      var b = document.getElementById(id);
      if (b) b.classList.toggle('is-active', id === key + 'Btn');
    });
  }
  if (status) status.addEventListener('change', function () { setTimeout(sync, 0); });
  if (row) new MutationObserver(sync).observe(row, { attributes: true, attributeFilter: ['hidden'] });
  ['trashBtn', 'cancelledBtn'].forEach(function (id) {
    var b = document.getElementById(id);
    if (b) b.addEventListener('click', function () { setTimeout(sync, 0); });
  });
  var backBtn = document.getElementById('heroBack');
  if (backBtn && status) backBtn.addEventListener('click', function () {
    status.value = '';
    status.dispatchEvent(new Event('change'));
    setTimeout(sync, 0);
  });
  sync();

  // Modals: close button, Esc and click-outside so every popup can be dismissed and scrolled
  document.querySelectorAll('.modal').forEach(function (m) {
    var box = m.querySelector('.modal-box');
    if (!box) return;
    var x = document.createElement('button');
    x.type = 'button'; x.className = 'modal-close'; x.setAttribute('aria-label', 'Close'); x.textContent = '\u00d7';
    x.addEventListener('click', function () { m.hidden = true; });
    box.insertBefore(x, box.firstChild);
    m.addEventListener('mousedown', function (e) { if (e.target === m) m.hidden = true; });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    document.querySelectorAll('.modal:not([hidden])').forEach(function (m) { m.hidden = true; });
  });

  // Mobile menu
  var tb = document.querySelector('.topbar'), mt = document.getElementById('menuToggle');
  function setMenu(open) {
    if (!tb || !mt) return;
    tb.classList.toggle('open', open);
    mt.setAttribute('aria-expanded', open ? 'true' : 'false');
    mt.innerHTML = open ? '&times;' : '&#9776;';
  }
  if (tb && mt) {
    mt.addEventListener('click', function () { setMenu(!tb.classList.contains('open')); });
    tb.querySelectorAll('.topbar-actions button').forEach(function (b) {
      b.addEventListener('click', function () { setMenu(false); });
    });
  }

  // Mobile: move search + filters into the menu
  var actions = document.querySelector('.topbar-actions'), orders = document.getElementById('ordersContainer');
  var bars = Array.prototype.slice.call(document.querySelectorAll('.filters')).filter(function (b) { return !b.closest('.modal'); });
  if (actions && orders && bars.length) {
    var box = document.createElement('div');
    box.id = 'menuFilters';
    var head = document.createElement('div');
    head.className = 'menu-filters-title'; head.textContent = 'Search and filters';
    box.appendChild(head);
    var done = document.createElement('button');
    done.type = 'button'; done.className = 'btn btn-primary menu-filters-done'; done.textContent = 'Show orders';
    actions.insertBefore(box, actions.firstChild);
    var search = document.getElementById('orderTrackInput');
    var searchBar = document.createElement('div');
    searchBar.id = 'mobileSearch';
    var mq = window.matchMedia('(max-width: 900px)');
    function place() {
      if (mq.matches) {
        bars.forEach(function (b) { box.insertBefore(b, null); });
        box.appendChild(done);
        if (search) { searchBar.appendChild(search); orders.parentNode.insertBefore(searchBar, orders); }
      } else {
        bars.forEach(function (b) { orders.parentNode.insertBefore(b, orders); });
        if (done.parentNode) done.parentNode.removeChild(done);
        if (search) bars[0].insertBefore(search, bars[0].firstChild);
        if (searchBar.parentNode) searchBar.parentNode.removeChild(searchBar);
      }
    }
    done.addEventListener('click', function () { setMenu(false); window.scrollTo({ top: 0 }); });
    if (mq.addEventListener) mq.addEventListener('change', place); else mq.addListener(place);
    place();
  }
})();
