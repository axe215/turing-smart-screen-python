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
  forceBlackText: $('#param-force-black-text'),
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

/**
 * Apply a CSS rotation to a preview <img>.
 *
 * For 0° / 180° we let CSS handle sizing (max-width/height + contain).
 * For 90° / 270° we explicitly size the IMG element to the SWAPPED
 * tray dimensions before rotating: the image lays out at tray-height
 * wide × tray-width tall, object-fit:contain keeps its native aspect,
 * then the visual rotation swaps it back into a tray-width × tray-height
 * landscape that actually fills the box.
 */
function applyPreviewRotation(img, deg) {
  if (!img) return;
  const tray = img.closest('.preview');
  if (!tray) return;
  const tw = tray.clientWidth;
  const th = tray.clientHeight;
  if (deg === 90 || deg === 270) {
    img.style.width = th + 'px';
    img.style.height = tw + 'px';
    img.style.maxWidth = 'none';
    img.style.maxHeight = 'none';
    img.style.transform = `rotate(${deg}deg)`;
  } else {
    img.style.width = '';
    img.style.height = '';
    img.style.maxWidth = '';
    img.style.maxHeight = '';
    img.style.transform = deg ? `rotate(${deg}deg)` : '';
  }
}

function readParams() {
  return {
    rotate_180: els.rotate180.checked,
    rotate_video: parseInt(els.rotateVideo.value, 10),
    font_scale: parseFloat(els.fontScale.value),
    widget_period: parseFloat(els.widgetPeriod.value),
    force_black_text: els.forceBlackText.checked,
  };
}

function writeParams(p) {
  if (!p) return;
  els.rotate180.checked = !!p.rotate_180;
  els.forceBlackText.checked = !!p.force_black_text;
  els.rotateVideo.value = String(p.rotate_video ?? 0);
  els.fontScale.value = String(p.font_scale ?? 1.3);
  els.fontScaleVal.textContent = Number(p.font_scale ?? 1.3).toFixed(2);
  els.widgetPeriod.value = String(p.widget_period ?? 1.0);
  els.widgetPeriodVal.textContent = Number(p.widget_period ?? 1.0).toFixed(1);
}

// Per-theme params persistence — different themes want different
// rotate/scale/etc. (eva.rei: 180° + 1.3 + force_black; Cyberpunk:
// 0° + 1.0 + no force_black). Stored in localStorage as
// {dir_name: {…params}}.
const THEME_PARAMS_KEY = 'theme_params_v1';
function loadThemeParams() {
  try { return JSON.parse(localStorage.getItem(THEME_PARAMS_KEY)) || {}; }
  catch { return {}; }
}
function saveThemeParams(map) {
  try { localStorage.setItem(THEME_PARAMS_KEY, JSON.stringify(map)); } catch {}
}
let themeParams = loadThemeParams();

function defaultsForSchema(schema) {
  // Sane starting params per theme schema. axe215_v1 (our themes,
  // typically video) want the user's mount rotation + larger fonts;
  // upstream themes are pixel-perfect by their author and should
  // start with no rotation, no font scaling, original colors.
  if (schema === 'axe215_v1') {
    return {rotate_180: true, rotate_video: 180, font_scale: 1.3,
            widget_period: 1.0, force_black_text: true};
  }
  return {rotate_180: false, rotate_video: 0, font_scale: 1.0,
          widget_period: 1.0, force_black_text: false};
}

function paramsForTheme(theme) {
  return themeParams[theme.dir_name] || defaultsForSchema(theme.schema);
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
  if (currentFilter === 'native') return t.schema === 'axe215_v1';
  if (currentFilter === 'upstream') return t.schema === 'upstream';
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
    // Tag upstream themes (mathoudebine) — they run via our adapter,
    // not as a "native" axe215 theme. Useful for users to spot which
    // are converted on the fly vs purpose-built for our engine.
    const schemaBadge = t.schema === 'axe215_v1'
      ? ''
      : '<span class="type-badge legacy" title="Upstream mathoudebine theme — adapted at runtime">upstream</span>';
    const btn = t.runnable
      ? `<button class="btn btn-primary" data-action="activate">${isActive ? 'Restart' : 'Activate'}</button>`
      : `<button class="btn" disabled title="Unsupported schema">Read-only</button>`;
    // Per-card preview rotation: applied client-side, persisted to localStorage.
    const rot = previewRotations[t.dir_name] | 0;
    // No inline style up front — applyPreviewRotation() handles dimensions
    // after the img loads (we need the tray's clientWidth/Height first).
    const previewImg = t.preview_url
      ? `<img src="${t.preview_url}" alt="${escapeHTML(t.name)}" data-rot="${rot}">`
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
      const theme = themesCache.find(t => t.dir_name === dir);
      const isActive = lastStatus?.active_theme === dir;
      btn.disabled = true;
      const prev = btn.textContent;
      btn.textContent = 'Starting…';

      // Pick params:
      //   - Restarting the ACTIVE theme → use the current UI values
      //     (user might be tweaking on the fly).
      //   - Switching to a different theme → pull saved-for-this-theme
      //     params, falling back to schema defaults. UI sliders are
      //     updated to match so the user sees what will be applied.
      let params;
      if (isActive) {
        params = readParams();
      } else {
        params = paramsForTheme(theme);
        writeParams(params);
      }

      try {
        await fetchJSON('/api/start', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({dir_name: dir, params}),
        });
        themeParams[dir] = params;
        saveThemeParams(themeParams);
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
      const img = card.querySelector('.preview img');
      const label = card.querySelector('.preview-rotate-label');
      applyPreviewRotation(img, next);
      if (img) img.dataset.rot = next;
      if (label) label.textContent = next ? `${next}°` : '';
    });
  });

  // Apply persisted rotations to images as they load (need tray
  // dimensions, which are stable once the card is in the DOM).
  $$('.theme-card .preview img').forEach(img => {
    const deg = parseInt(img.dataset.rot || '0', 10);
    if (img.complete) {
      applyPreviewRotation(img, deg);
    } else {
      img.addEventListener('load', () => applyPreviewRotation(img, deg), {once: true});
    }
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
