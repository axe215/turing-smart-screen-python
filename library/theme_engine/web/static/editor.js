/* Editor (Phase 6a) — load axe215_v1 theme, render canvas, drag widgets,
 * save to YAML. Properties panel + widget CRUD comes in 6b; for now the
 * sidebar shows id/type/x/y of the selected widget read-only.
 *
 * State model:
 *   themeData        — the YAML dict, mutated as widgets are dragged.
 *                      Server is the source of truth on initial load and
 *                      after every Save (it normalizes / stamps fields).
 *   widgetsById      — map id → DOM box element for fast highlighting.
 *   scale            — CSS pixels per design pixel for the current
 *                      canvas size. Recomputed on window resize.
 *   selectedId       — currently selected widget id (or null).
 */

const dirName = window.EDITOR_DIR;
let themeData = null;
let scale = 1.0;
let selectedId = null;
const widgetsById = new Map();
let dragState = null;

const $ = (id) => document.getElementById(id);
const designCanvas = $('design-canvas');
const widgetList = $('widget-list');
const propertiesPanel = $('properties-panel');
const editorMeta = $('editor-meta');
const editorTitle = $('editor-title');
const canvasHint = $('canvas-hint');

// ---------- Boot ----------------------------------------------------------

async function boot() {
  try {
    const res = await fetch(`/api/themes/${encodeURIComponent(dirName)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = await res.json();
    themeData = payload.data || {};
    editorTitle.textContent = themeData.name || dirName;
    const canvas = themeData.canvas || {width: 1920, height: 480};
    editorMeta.textContent = `${canvas.width}×${canvas.height} · widgets ${(themeData.widgets || []).length} · schema ${payload.schema}`;
    layoutCanvas();
    renderWidgets();
  } catch (err) {
    editorMeta.textContent = `error: ${err.message}`;
    console.error(err);
  }
}

window.addEventListener('resize', () => {
  if (themeData) layoutCanvas();
});

// ---------- Canvas layout -------------------------------------------------

function layoutCanvas() {
  const canvas = themeData.canvas || {width: 1920, height: 480};
  const stage = $('canvas-stage');
  // Fit the design canvas inside the stage at the largest scale that
  // preserves the aspect ratio AND keeps things on screen.
  const stageW = stage.clientWidth - 24;
  const stageH = stage.clientHeight - 48;
  const sw = stageW / canvas.width;
  const sh = stageH / canvas.height;
  scale = Math.min(sw, sh);
  if (!isFinite(scale) || scale <= 0) scale = 0.3;
  designCanvas.style.width = (canvas.width * scale) + 'px';
  designCanvas.style.height = (canvas.height * scale) + 'px';

  // Background — image-mode themes ship a static png; video themes get
  // a checkerboard placeholder so widget rectangles are still visible.
  const imageBlock = themeData.image;
  const videoBlock = themeData.video;
  if (imageBlock && imageBlock.path) {
    const url = `/api/themes/${encodeURIComponent(dirName)}/asset/${encodeURI(imageBlock.path)}`;
    designCanvas.style.background = `url("${url}") center / 100% 100% no-repeat`;
  } else if (videoBlock && videoBlock.path) {
    designCanvas.style.background = 'repeating-conic-gradient(#2a2a2a 0deg 90deg, #353535 90deg 180deg) 0 0 / 24px 24px';
    canvasHint.textContent = `Видео-тема: фон — заглушка. Render preview покажет первый кадр.`;
  } else {
    designCanvas.style.background = '#222';
  }

  // Re-position all widget boxes to match the new scale.
  for (const box of widgetsById.values()) {
    positionBox(box);
  }
}

// ---------- Widget rendering ----------------------------------------------

function renderWidgets() {
  // Clear list
  widgetList.innerHTML = '';
  widgetsById.clear();
  // Clear canvas (preserve element, just remove children we added)
  designCanvas.innerHTML = '';

  const widgets = themeData.widgets || [];
  for (const w of widgets) {
    addWidgetBox(w);
    addListItem(w);
  }
  if (selectedId && !widgetsById.has(selectedId)) selectedId = null;
  refreshProperties();
}

function addWidgetBox(w) {
  const box = document.createElement('div');
  box.className = 'widget-box type-' + (w.type || 'unknown');
  box.dataset.id = w.id;
  box.innerHTML = `<span class="widget-label">${escapeHTML(w.id)}<small>${escapeHTML(w.type || '')}</small></span>`;
  positionBox(box, w);
  box.addEventListener('mousedown', onWidgetMouseDown);
  designCanvas.appendChild(box);
  widgetsById.set(w.id, box);
}

function positionBox(box, w) {
  if (!w) w = themeData.widgets.find((x) => x.id === box.dataset.id);
  if (!w) return;
  const x = (w.x || 0) * scale;
  const y = (w.y || 0) * scale;
  // Type-specific box dimensions. data/text widgets have no width in YAML,
  // so we render a minimal label-sized box; chart/image/progress_bar/radial
  // have explicit dimensions.
  let bw, bh;
  if (w.type === 'chart' || w.type === 'progress_bar' || w.type === 'line_graph') {
    bw = Math.max(20, (w.width || 100) * scale);
    bh = Math.max(20, (w.height || 30) * scale);
  } else if (w.type === 'radial') {
    const r = (w.raw && w.raw.radius) || w.radius || 30;
    bw = bh = r * 2 * scale;
  } else if (w.type === 'image') {
    bw = bh = 30 * scale;  // placeholder until we know image size
  } else {
    // text / data — size from font.size (approx) and the text length
    const fontSize = (w.font && w.font.size) || 12;
    const approxLen = Math.max(2, ((w.text || w.source || w.id).length) * 0.6);
    bw = fontSize * approxLen * scale;
    bh = fontSize * 1.2 * scale;
  }
  box.style.left = x + 'px';
  box.style.top = y + 'px';
  box.style.width = bw + 'px';
  box.style.height = bh + 'px';
}

function addListItem(w) {
  const li = document.createElement('li');
  li.dataset.id = w.id;
  li.className = 'widget-list-item';
  li.innerHTML = `<span class="dot type-${escapeHTML(w.type || 'unknown')}"></span>
                  <span class="li-id">${escapeHTML(w.id)}</span>
                  <span class="li-type">${escapeHTML(w.type || '')}</span>`;
  li.addEventListener('click', () => selectWidget(w.id));
  widgetList.appendChild(li);
}

// ---------- Selection -----------------------------------------------------

function selectWidget(id) {
  selectedId = id;
  for (const [wid, box] of widgetsById) {
    box.classList.toggle('selected', wid === id);
  }
  for (const li of widgetList.children) {
    li.classList.toggle('selected', li.dataset.id === id);
  }
  refreshProperties();
}

function refreshProperties() {
  if (!selectedId) {
    propertiesPanel.innerHTML = '<p class="muted">Выбери виджет в списке слева или на холсте.</p>';
    return;
  }
  const w = themeData.widgets.find((x) => x.id === selectedId);
  if (!w) {
    propertiesPanel.innerHTML = '<p class="muted">Виджет не найден.</p>';
    return;
  }
  // Phase 6a: read-only summary. Phase 6b will swap this for live inputs.
  const fontFamily = (w.font && w.font.family) || '—';
  const fontSize = (w.font && w.font.size) || '—';
  const colorArr = (w.font && w.font.color) || null;
  const colorPreview = colorArr
    ? `<span class="color-swatch" style="background: rgba(${colorArr.slice(0,3).join(',')}, ${(colorArr[3] || 255) / 255})"></span>`
    : '';
  propertiesPanel.innerHTML = `
    <dl>
      <dt>id</dt><dd>${escapeHTML(w.id)}</dd>
      <dt>type</dt><dd>${escapeHTML(w.type || '')}</dd>
      <dt>x / y</dt><dd>
        <input type="number" id="prop-x" value="${w.x || 0}" step="1"> /
        <input type="number" id="prop-y" value="${w.y || 0}" step="1">
      </dd>
      ${w.source ? `<dt>source</dt><dd>${escapeHTML(w.source)}</dd>` : ''}
      ${w.text ? `<dt>text</dt><dd>${escapeHTML(w.text)}</dd>` : ''}
      <dt>font</dt><dd>${escapeHTML(String(fontFamily))} · ${escapeHTML(String(fontSize))}px ${colorPreview}</dd>
    </dl>
    <p class="hint">Phase 6a: можно править X/Y. Полная панель свойств — Phase 6b.</p>
  `;
  $('prop-x').addEventListener('change', (e) => updateXY(w.id, parseInt(e.target.value, 10), null));
  $('prop-y').addEventListener('change', (e) => updateXY(w.id, null, parseInt(e.target.value, 10)));
}

function updateXY(id, x, y) {
  const w = themeData.widgets.find((x) => x.id === id);
  if (!w) return;
  if (x !== null && !isNaN(x)) w.x = x;
  if (y !== null && !isNaN(y)) w.y = y;
  const box = widgetsById.get(id);
  if (box) positionBox(box, w);
}

// ---------- Drag-and-drop -------------------------------------------------

function onWidgetMouseDown(ev) {
  if (ev.button !== 0) return;
  ev.preventDefault();
  const id = ev.currentTarget.dataset.id;
  selectWidget(id);
  const w = themeData.widgets.find((x) => x.id === id);
  if (!w) return;
  dragState = {
    id,
    startMouseX: ev.clientX,
    startMouseY: ev.clientY,
    startWidgetX: w.x || 0,
    startWidgetY: w.y || 0,
  };
  window.addEventListener('mousemove', onDragMove);
  window.addEventListener('mouseup', onDragEnd);
}

function onDragMove(ev) {
  if (!dragState) return;
  const dx = (ev.clientX - dragState.startMouseX) / scale;
  const dy = (ev.clientY - dragState.startMouseY) / scale;
  const w = themeData.widgets.find((x) => x.id === dragState.id);
  if (!w) return;
  w.x = Math.round(dragState.startWidgetX + dx);
  w.y = Math.round(dragState.startWidgetY + dy);
  const box = widgetsById.get(dragState.id);
  if (box) positionBox(box, w);
  if (selectedId === dragState.id) {
    const xi = $('prop-x'); const yi = $('prop-y');
    if (xi) xi.value = w.x;
    if (yi) yi.value = w.y;
  }
}

function onDragEnd() {
  dragState = null;
  window.removeEventListener('mousemove', onDragMove);
  window.removeEventListener('mouseup', onDragEnd);
}

// ---------- Save / preview ------------------------------------------------

async function save() {
  const btn = $('btn-save');
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = 'Saving…';
  try {
    const res = await fetch(`/api/themes/${encodeURIComponent(dirName)}/save`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({data: themeData}),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${res.status}`);
    }
    btn.textContent = 'Saved ✓';
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1200);
  } catch (err) {
    alert('Save failed: ' + err.message);
    btn.textContent = orig;
    btn.disabled = false;
  }
}

async function previewRender() {
  const btn = $('btn-preview');
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = 'Rendering…';
  try {
    const res = await fetch(`/api/themes/${encodeURIComponent(dirName)}/preview`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({data: themeData}),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${res.status}`);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const img = $('preview-image');
    img.onload = () => URL.revokeObjectURL(url);
    img.src = url;
    $('preview-modal').classList.remove('hidden');
  } catch (err) {
    alert('Preview render failed: ' + err.message);
  } finally {
    btn.textContent = orig;
    btn.disabled = false;
  }
}

$('btn-save').addEventListener('click', save);
$('btn-preview').addEventListener('click', previewRender);
$('preview-close').addEventListener('click', () => $('preview-modal').classList.add('hidden'));
$('preview-modal').addEventListener('click', (ev) => {
  if (ev.target.id === 'preview-modal') $('preview-modal').classList.add('hidden');
});

// ---------- utils ---------------------------------------------------------

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

boot();
