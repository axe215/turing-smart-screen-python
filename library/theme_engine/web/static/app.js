// Theme manager dashboard — vanilla JS, no build step.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const els = {
  statusRunning: $('#status-running'),
  statusTheme: $('#status-theme'),
  statusMeta: $('#status-meta'),
  stopBtn: $('#stop-btn'),
  themeGrid: $('#theme-grid'),
  // params
  rotate180: $('#param-rotate-180'),
  rotateVideo: $('#param-rotate-video'),
  fontScale: $('#param-font-scale'),
  fontScaleVal: $('#param-font-scale-val'),
  widgetPeriod: $('#param-widget-period'),
  widgetPeriodVal: $('#param-widget-period-val'),
};

let themesCache = [];
let lastStatus = null;

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

function renderThemes() {
  if (!themesCache.length) {
    els.themeGrid.innerHTML = '<p style="color:var(--muted)">Нет тем в res/themes/. Сгенерируй через phase3_parse_turtheme.py --emit-theme.</p>';
    return;
  }
  const activeDir = lastStatus?.active_theme;
  els.themeGrid.innerHTML = themesCache.map(t => {
    const isActive = t.dir_name === activeDir;
    const preview = t.preview_url
      ? `<img src="${t.preview_url}" alt="${t.name}">`
      : (t.has_video ? '<span>🎥 video</span>' : '<span>no preview</span>');
    const videoTag = t.has_video ? '🎥' : '·';
    const badge = isActive ? '<div class="active-badge">ACTIVE</div>' : '';
    return `
      <div class="theme-card${isActive ? ' active' : ''}" data-dir="${t.dir_name}">
        ${badge}
        <div class="preview">${preview}</div>
        <div class="name">${escapeHTML(t.name)}</div>
        <div class="info">
          ${t.canvas_width}×${t.canvas_height} ${videoTag} · ${t.widget_count} widgets
        </div>
        <div class="actions">
          <button class="btn btn-primary" data-action="activate">${isActive ? 'Restart' : 'Activate'}</button>
        </div>
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
        // Re-render cards so the active state moves to this card
        renderThemes();
      } catch (err) {
        alert('Start failed: ' + err.message);
      } finally {
        btn.disabled = false;
        btn.textContent = prev;
      }
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
  // Header status
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
  // Sync params display (from server) — first time only
  if (!els.fontScale.dataset.synced) {
    writeParams(lastStatus.params);
    els.fontScale.dataset.synced = '1';
  }
  // If the active theme changed, re-render cards so the badge moves
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
    renderThemes(); // remove active badge
  } catch (err) {
    alert('Stop failed: ' + err.message);
    els.stopBtn.disabled = false; // re-enable so user can try again
  }
});

// initial load + 2s polling for status
(async () => {
  await refreshStatus();
  await refreshThemes();
  setInterval(refreshStatus, 2000);
})();
