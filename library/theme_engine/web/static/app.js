// Theme manager dashboard — vanilla JS, no build step.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const els = {
  statusRunning: $('#status-running'),
  statusTheme: $('#status-theme'),
  statusMeta: $('#status-meta'),
  stopBtn: $('#stop-btn'),
  themeGrid: $('#theme-grid'),
  filterBar: $('#filter-bar'),
  statusMemory: $('#status-memory'),
  rotate180: $('#param-rotate-180'),
  rotateVideo: $('#param-rotate-video'),
  fontScale: $('#param-font-scale'),
  fontScaleVal: $('#param-font-scale-val'),
  widgetPeriod: $('#param-widget-period'),
  widgetPeriodVal: $('#param-widget-period-val'),
};

let themesCache = [];
let lastStatus = null;
let currentFilter = 'all';

// Per-theme preview rotation, persisted in localStorage so the user's
// preferred orientation for each card survives reloads. Map: dir_name → deg.
const PREVIEW_ROT_KEY = 'preview_rotations_v1';
function loadPreviewRotations() {
  try { return JSON.parse(localStorage.getItem(PREVIEW_ROT_KEY)) || {}; }
  catch { return {}; }
}
function savePreviewRotations(map) {
  try { localStorage.setItem(PREVIEW_ROT_KEY, JSON.stringify(map)); } catch {}
}
let previewRotations = loadPreviewRotations();

function nextRotation(deg) { return (deg + 90) % 360; }

function readParams() {
  return {
    rotate_180: els.rotate180.checked,
    rotate_video: parseInt(els.rotateVideo.value, 10),
    font_scale: parseFloat(els.fontScale.value),
    widget_period: parseFloat(els.widgetPeriod.value),
  };
}

function writeParams(p) {
  if (!p) return;
  els.rotate180.checked = !!p.rotate_180;
  els.rotateVideo.value = String(p.rotate_video ?? 0);
  els.fontScale.value = String(p.font_scale ?? 1.3);
  els.fontScaleVal.textContent = Number(p.font_scale ?? 1.3).toFixed(2);
  els.widgetPeriod.value = String(p.widget_period ?? 1.0);
  els.widgetPeriodVal.textContent = Number(p.widget_period ?? 1.0).toFixed(1);
}

els.fontScale.addEventListener('input', () => {
  els.fontScaleVal.textContent = Number(els.fontScale.value).toFixed(2);
});
els.widgetPeriod.addEventListener('input', () => {
  els.widgetPeriodVal.textContent = Number(els.widgetPeriod.value).toFixed(1);
});

async function fetchJSON(url, opts) {
  const res = await fetch(url, opts);
  const ct = res.headers.get('content-type') || '';
  const body = ct.includes('application/json') ? await res.json() : await res.text();
  if (!res.ok) throw new Error(typeof body === 'string' ? body : (body.error || res.statusText));
  return body;
}

async function refreshThemes() {
  themesCache = await fetchJSON('/api/themes');
  renderThemes();
}

function passesFilter(t) {
  if (currentFilter === 'all') return true;
  if (currentFilter === 'native') return t.runnable;
  return t.background_type === currentFilter;
}

function bgTypeIcon(type) {
  return ({video: '🎥', gif: '🌀', image: '🖼', none: '·'}[type] || '·');
}

function renderThemes() {
  if (!themesCache.length) {
    els.themeGrid.innerHTML = '<p style="color:var(--muted)">Нет тем в res/themes/. Сгенерируй через phase3_parse_turtheme.py --emit-theme.</p>';
    return;
  }
  const activeDir = lastStatus?.active_theme;
  const visible = themesCache.filter(passesFilter);
  if (!visible.length) {
    els.themeGrid.innerHTML = '<p style="color:var(--muted)">Под фильтр ничего не подошло.</p>';
    return;
  }
  els.themeGrid.innerHTML = visible.map(t => {
    const isActive = t.dir_name === activeDir;
    const activeBadge = isActive ? '<div class="active-badge">ACTIVE</div>' : '';
    const typeBadge = `<span class="type-badge ${t.background_type}">${bgTypeIcon(t.background_type)} ${t.background_type}</span>`;
    const schemaBadge = t.runnable
      ? ''
      : '<span class="type-badge legacy">legacy</span>';
    const btn = t.runnable
      ? `<button class="btn btn-primary" data-action="activate">${isActive ? 'Restart' : 'Activate'}</button>`
      : `<button class="btn" disabled title="Upstream schema — run via main.py">Read-only</button>`;
    // Per-card preview rotation: applied client-side, persisted to localStorage.
    const rot = previewRotations[t.dir_name] | 0;
    const imgClass = rot ? ` class="rot-${rot}"` : '';
    const imgStyle = rot ? ` style="transform: rotate(${rot}deg);"` : '';
    const previewImg = t.preview_url
      ? `<img src="${t.preview_url}" alt="${escapeHTML(t.name)}"${imgClass}${imgStyle}>`
      : `<span>${bgTypeIcon(t.background_type)}</span>`;
    const rotateBtn = t.preview_url
      ? `<button class="preview-rotate" data-action="rotate-preview" title="Повернуть превью">↻</button>`
      : '';
    const rotLabel = rot ? `<div class="preview-rotate-label">${rot}°</div>` : '<div class="preview-rotate-label"></div>';
    return `
      <div class="theme-card${isActive ? ' active' : ''}${t.runnable ? '' : ' disabled'}" data-dir="${escapeHTML(t.dir_name)}">
        ${activeBadge}
        <div class="preview">
          ${previewImg}
          ${rotLabel}
          ${rotateBtn}
        </div>
        <div class="name">${escapeHTML(t.name)} <span class="meta">/${escapeHTML(t.dir_name)}</span></div>
        <div class="info">
          ${typeBadge}${schemaBadge}
          ${t.canvas_width}×${t.canvas_height} · ${t.widget_count} widgets
        </div>
        <div class="actions">${btn}</div>
      </div>
    `;
  }).join('');

  $$('[data-action="activate"]').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      const card = e.target.closest('.theme-card');
      const dir = card.dataset.dir;
      btn.disabled = true;
      const prev = btn.textContent;
      btn.textContent = 'Starting…';
      try {
        await fetchJSON('/api/start', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({dir_name: dir, params: readParams()}),
        });
        await refreshStatus();
        renderThemes();
      } catch (err) {
        alert('Start failed: ' + err.message);
      } finally {
        btn.disabled = false;
        btn.textContent = prev;
      }
    });
  });

  // Preview rotation cycle button (per-card, persists in localStorage)
  $$('[data-action="rotate-preview"]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const card = e.target.closest('.theme-card');
      const dir = card.dataset.dir;
      const cur = previewRotations[dir] | 0;
      const next = nextRotation(cur);
      if (next === 0) delete previewRotations[dir];
      else previewRotations[dir] = next;
      savePreviewRotations(previewRotations);
      // In-place update — no full re-render needed
      const img = card.querySelector('.preview img');
      const label = card.querySelector('.preview-rotate-label');
      if (img) {
        img.style.transform = next ? `rotate(${next}deg)` : '';
        // Swap rotation-specific size class (90/270 vs 0/180)
        img.classList.remove('rot-90', 'rot-180', 'rot-270');
        if (next) img.classList.add(`rot-${next}`);
      }
      if (label) label.textContent = next ? `${next}°` : '';
    });
  });
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

async function refreshStatus() {
  const prevActive = lastStatus?.active_theme;
  try {
    lastStatus = await fetchJSON('/api/status');
  } catch (err) {
    console.warn('status fetch failed', err);
    return;
  }
  if (lastStatus.running) {
    els.statusRunning.textContent = 'Running';
    els.statusRunning.className = 'badge running';
    els.statusTheme.textContent = lastStatus.active_theme || '—';
    const e = lastStatus.engine || {};
    els.statusMeta.textContent =
      `uptime ${formatUptime(e.uptime_sec || 0)} · ` +
      `widgets ${e.widgets_sent || 0} · ` +
      `chunks ${e.stream_chunks || 0} · ` +
      `avg send ${e.widget_send_ms_avg || 0}ms`;
    els.stopBtn.disabled = false;
  } else {
    els.statusRunning.textContent = 'Stopped';
    els.statusRunning.className = 'badge stopped';
    els.statusTheme.textContent = '—';
    els.statusMeta.textContent = '';
    els.stopBtn.disabled = true;
  }
  if (typeof lastStatus.process_rss_mb === 'number') {
    els.statusMemory.textContent = `${lastStatus.process_rss_mb.toFixed(0)} MB`;
  }
  if (!els.fontScale.dataset.synced) {
    writeParams(lastStatus.params);
    els.fontScale.dataset.synced = '1';
  }
  if (prevActive !== lastStatus.active_theme && themesCache.length) {
    renderThemes();
  }
}

function formatUptime(secs) {
  secs = Math.floor(secs);
  if (secs < 60) return secs + 's';
  if (secs < 3600) return Math.floor(secs / 60) + 'm ' + (secs % 60) + 's';
  return Math.floor(secs / 3600) + 'h ' + Math.floor((secs % 3600) / 60) + 'm';
}

els.stopBtn.addEventListener('click', async () => {
  els.stopBtn.disabled = true;
  try {
    await fetchJSON('/api/stop', {method: 'POST'});
    await refreshStatus();
    renderThemes();
  } catch (err) {
    alert('Stop failed: ' + err.message);
    els.stopBtn.disabled = false;
  }
});

// filter buttons
els.filterBar.addEventListener('click', (e) => {
  const btn = e.target.closest('.filter-btn');
  if (!btn) return;
  $$('.filter-btn').forEach(b => b.classList.toggle('active', b === btn));
  currentFilter = btn.dataset.filter;
  renderThemes();
});

(async () => {
  await refreshStatus();
  await refreshThemes();
  setInterval(refreshStatus, 2000);
})();
