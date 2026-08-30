// ── State ──
let currentCountry = null;
let currentPage = 1;
let countries = [];
let allCountries = [];
let debounceTimer = null;
let abortController = null;

// ── Init ──
(async function init() {
  document.getElementById('title').addEventListener('click', goBack);
  await loadStats();
  await loadCountries();
})();

// ── Stats bar ──
async function loadStats() {
  try {
    const r = await fetch('/api/stats');
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    const bar = document.getElementById('stats-bar');
    bar.textContent = `共 ${d.total.toLocaleString()} 节点 · ${Object.keys(d.regions||{}).length} 个国家 · 仅展示 <500ms`;
  } catch(e) {}
}

// ── Countries ──
async function loadCountries() {
  try {
    const r = await fetch('/api/countries');
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    allCountries = d.countries || [];
    countries = [...allCountries];
    renderCountries();
  } catch(e) {
    document.getElementById('country-grid').innerHTML = '<p style="text-align:center;color:#888">加载失败</p>';
  }
}

function filterCountries() {
  const q = (document.getElementById('country-search').value || '').toLowerCase();
  if (!q) { countries = [...allCountries]; }
  else { countries = allCountries.filter(c => c.name.toLowerCase().includes(q) || c.code.toLowerCase().includes(q)); }
  renderCountries();
}

function flagEmoji(code) {
  if (!code || code === '?' || code.length !== 2) return '🏳️';
  const a = code.toUpperCase();
  return String.fromCodePoint(0x1F1E6 + a.charCodeAt(0) - 65, 0x1F1E6 + a.charCodeAt(1) - 65);
}

function renderCountries() {
  const grid = document.getElementById('country-grid');
  if (!countries.length) {
    grid.innerHTML = '<p style="text-align:center;color:#888">无匹配国家</p>';
    return;
  }
  grid.innerHTML = '';
  countries.forEach(c => {
    const flag = flagEmoji(c.code);
    const card = document.createElement('div');
    card.className = 'country-card';
    card.dataset.code = c.code;
    card.dataset.name = c.name;
    card.addEventListener('click', function() {
      drillInto(this.dataset.code, this.dataset.name);
    });
    const flagSpan = document.createElement('span');
    flagSpan.className = 'country-flag';
    flagSpan.textContent = flag;
    const nameSpan = document.createElement('span');
    nameSpan.className = 'country-name';
    nameSpan.textContent = c.name;
    const countSpan = document.createElement('span');
    countSpan.className = 'country-count';
    countSpan.textContent = c.count.toLocaleString();
    card.appendChild(flagSpan);
    card.appendChild(nameSpan);
    card.appendChild(countSpan);
    grid.appendChild(card);
  });
}

// ── Drill ──
function drillInto(code, name) {
  clearTimeout(debounceTimer);
  currentCountry = code;
  currentPage = 1;
  document.getElementById('view-country-list').style.display = 'none';
  document.getElementById('view-country-detail').style.display = 'block';
  document.getElementById('detail-title').textContent = `${flagEmoji(code)} ${name || code}`;
  // Reset filter inputs
  document.getElementById('f-proto').value = '';
  document.getElementById('f-location').value = '';
  document.getElementById('f-delay').value = '';
  document.getElementById('f-search').value = '';
  document.getElementById('f-sort').value = '';
  // Populate protocol filter
  const protoSel = document.getElementById('f-proto');
  protoSel.innerHTML = '<option value="">全部协议</option>' +
    ['http','https','socks4','socks5'].map(p => `<option value="${p}">${p.toUpperCase()}</option>`).join('');
  loadDetail(1);
}

function goBack() {
  clearTimeout(debounceTimer);
  currentCountry = null;
  document.getElementById('view-country-list').style.display = 'block';
  document.getElementById('view-country-detail').style.display = 'none';
}

function debounceLoad() {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => loadDetail(1), 400);
}

async function loadDetail(page) {
  if (!currentCountry) return;
  currentPage = page;
  const proto = document.getElementById('f-proto').value;
  const loc = document.getElementById('f-location').value.trim();
  const delay = document.getElementById('f-delay').value.trim();
  const sort = document.getElementById('f-sort').value;
  const search = document.getElementById('f-search').value.trim();

  const params = new URLSearchParams();
  params.set('page', page);
  params.set('limit', 50);
  if (proto) params.set('protocol', proto);
  if (loc) params.set('location', loc);
  if (delay) params.set('delay', delay);
  if (search) params.set('search', search);
  if (sort) { const [by, dir] = sort.split('-'); params.set('sort', by); params.set('asc', dir === 'asc' ? '1' : '0'); }

  // Abort previous request
  if (abortController) { abortController.abort(); }
  abortController = new AbortController();

  try {
    const r = await fetch(`/api/country/${currentCountry}?${params}`, { signal: abortController.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    renderDetailTable(d);
  } catch(e) {
    if (e.name !== 'AbortError') {
      document.getElementById('detail-tbody').innerHTML = '<tr><td colspan="5" style="color:#888">加载失败</td></tr>';
    }
  }
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function renderDetailTable(d) {
  document.getElementById('detail-info').textContent =
    `共 ${d.total_matched.toLocaleString()} 条 · 第 ${d.page}/${d.pages} 页`;

  const proxies = d.proxies || [];
  const tbody = document.getElementById('detail-tbody');
  tbody.innerHTML = '';

  if (!proxies.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 5;
    td.style.textAlign = 'center';
    td.style.color = '#888';
    td.textContent = '无代理';
    tr.appendChild(td);
    tbody.appendChild(tr);
  } else {
    proxies.forEach(p => {
      const addr = `${p.ip}:${p.port}`;
      const lat = p.delay > 0 ? p.delay + 'ms' : '-';
      const cls = p.delay < 200 ? 'fast' : p.delay < 350 ? 'mid' : '';

      const tr = document.createElement('tr');

      const tdAddr = document.createElement('td');
      tdAddr.className = 'proxy-addr';
      tdAddr.textContent = addr;
      tdAddr.title = '点击复制';
      tdAddr.addEventListener('click', function(e) { copyAddr(addr, e); });

      const tdProto = document.createElement('td');
      const protoBadge = document.createElement('span');
      protoBadge.className = 'proto-badge proto-' + (p.protocol || 'http');
      protoBadge.textContent = (p.protocol || 'HTTP').toUpperCase();
      tdProto.appendChild(protoBadge);

      const tdLat = document.createElement('td');
      if (cls) tdLat.className = 'lat-' + cls;
      tdLat.textContent = lat;

      const tdLoc = document.createElement('td');
      tdLoc.textContent = p.location || '-';

      const tdTime = document.createElement('td');
      tdTime.className = 'time-col';
      tdTime.textContent = p.last_check || '-';

      tr.appendChild(tdAddr);
      tr.appendChild(tdProto);
      tr.appendChild(tdLat);
      tr.appendChild(tdLoc);
      tr.appendChild(tdTime);
      tbody.appendChild(tr);
    });
  }

  // Pager
  const pager = document.getElementById('detail-pager');
  if (d.pages <= 1) { pager.innerHTML = ''; return; }
  pager.innerHTML = '';
  {
    const prevBtn = document.createElement('button');
    prevBtn.textContent = '‹';
    if (d.page <= 1) prevBtn.disabled = true;
    else prevBtn.addEventListener('click', () => loadDetail(d.page - 1));
    pager.appendChild(prevBtn);
  }
  for (let i = Math.max(1, d.page - 3); i <= Math.min(d.pages, d.page + 3); i++) {
    const btn = document.createElement('button');
    btn.textContent = i;
    if (i === d.page) btn.className = 'active';
    btn.addEventListener('click', function() { loadDetail(parseInt(this.textContent)); });
    pager.appendChild(btn);
  }
  {
    const nextBtn = document.createElement('button');
    nextBtn.textContent = '›';
    if (d.page >= d.pages) nextBtn.disabled = true;
    else nextBtn.addEventListener('click', () => loadDetail(d.page + 1));
    pager.appendChild(nextBtn);
  }
}

function copyAddr(addr, evt) {
  navigator.clipboard.writeText(addr).then(() => {
    // Brief visual feedback
    const el = document.querySelector('.proxy-addr.copied');
    if (el) el.classList.remove('copied');
    const clicked = evt && evt.target ? evt.target.closest('.proxy-addr') : null;
    if (clicked) {
      clicked.classList.add('copied');
      setTimeout(() => clicked.classList.remove('copied'), 800);
    }
  }).catch(() => {});
}
