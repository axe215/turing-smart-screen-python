/* Editor (Phase 6a + 6b) — load axe215_v1 theme, render canvas, drag
 * widgets, edit properties, add/delete/duplicate widgets, save to YAML.
 *
 * Data flow:
 *   themeData              live source of truth, mutated in place by
 *                          the property panel and drag handlers.
 *   widgetsById            id → DOM box on the canvas.
 *   selectedId             current selection (or null).
 *   sourcesList            populated from GET /api/themes/<dir>.
 *   fontsList              fetched once from GET /api/fonts.
 *
 * Widget schema (WIDGET_SCHEMAS) drives the form: each type lists its
 * editable fields with kind (string/int/float/bool/color/font/source/
 * choice) and an optional default. _Common_ fields (id, x, y, hide,
 * enabled, font) live on every widget that uses them.
 */

const dirName = window.EDITOR_DIR;
let themeData = null;
let scale = 1.0;
let selectedId = null;
let sourcesList = [];
let fontsList = [];
const widgetsById = new Map();
let dragState = null;

const $ = (id) => document.getElementById(id);
const $$ = (sel) => document.querySelectorAll(sel);
const designCanvas = $('design-canvas');
const widgetList = $('widget-list');
const propertiesPanel = $('properties-panel');
const editorMeta = $('editor-meta');
const editorTitle = $('editor-title');
const canvasHint = $('canvas-hint');

// ---------- Widget schema -------------------------------------------------

const COMMON_FIELDS = [
  {key: 'id',      label: 'ID',      kind: 'id'},
  {key: 'x',       label: 'X',       kind: 'int', step: 1},
  {key: 'y',       label: 'Y',       kind: 'int', step: 1},
  {key: 'hide',    label: 'Hidden',  kind: 'bool'},
  {key: 'enabled', label: 'Enabled', kind: 'bool'},
];

const FONT_FIELDS = [
  {key: 'font.family', label: 'Font',       kind: 'font'},
  {key: 'font.size',   label: 'Size',       kind: 'int', step: 1, min: 6, max: 200},
  {key: 'font.bold',   label: 'Bold',       kind: 'bool'},
  {key: 'font.color',  label: 'Color',      kind: 'color_rgba'},
];

const WIDGET_SCHEMAS = {
  text: {
    label: 'Text',
    fields: [
      {key: 'text', label: 'Text', kind: 'string'},
      ...FONT_FIELDS,
    ],
    defaults: () => ({type: 'text', text: 'Hello', font: {family: '', size: 24, bold: false, color: [255, 255, 255, 255]}}),
  },
  data: {
    label: 'Data',
    fields: [
      {key: 'source',    label: 'Source',    kind: 'source'},
      {key: 'show_unit', label: 'Show unit', kind: 'bool'},
      {key: 'min_size',  label: 'Min size',  kind: 'int_opt', help: 'Right-pad digits with spaces'},
      ...FONT_FIELDS,
    ],
    defaults: () => ({type: 'data', source: 'cpu_percentage', show_unit: true, font: {family: '', size: 24, bold: false, color: [255, 255, 255, 255]}}),
  },
  chart: {
    label: 'Chart (columns)',
    fields: [
      {key: 'source',          label: 'Source',       kind: 'source'},
      {key: 'width',           label: 'Width',        kind: 'int', step: 1},
      {key: 'height',          label: 'Height',       kind: 'int', step: 1},
      {key: 'max_value',       label: 'Max value',    kind: 'float'},
      {key: 'column_width',    label: 'Column width', kind: 'int', step: 1, min: 1},
      {key: 'line_color',      label: 'Line color',   kind: 'color_rgba'},
      {key: 'fill_color',      label: 'Fill color',   kind: 'color_rgba'},
      {key: 'border_color',    label: 'Border color', kind: 'color_rgba'},
      {key: 'bar_color',       label: 'Bar color',    kind: 'color_rgba'},
      {key: 'bar_stroke_color',label: 'Bar stroke',   kind: 'color_rgba'},
      {key: 'border_width',    label: 'Border width', kind: 'int', step: 1, min: 0},
    ],
    defaults: () => ({type: 'chart', source: 'cpu_percentage', width: 150, height: 50, max_value: 100, column_width: 5}),
  },
  image: {
    label: 'Image',
    fields: [
      {key: 'image', label: 'Image path', kind: 'string', help: 'Relative to theme dir'},
      {key: 'scale', label: 'Scale',      kind: 'float', step: 0.05, min: 0.05, max: 10},
    ],
    defaults: () => ({type: 'image', image: '', scale: 1.0}),
  },
  progress_bar: {
    label: 'Progress bar',
    fields: [
      {key: 'source',            label: 'Source',     kind: 'source'},
      {key: 'width',             label: 'Width',      kind: 'int', step: 1},
      {key: 'height',            label: 'Height',     kind: 'int', step: 1},
      {key: 'min_value',         label: 'Min value',  kind: 'float'},
      {key: 'max_value',         label: 'Max value',  kind: 'float'},
      {key: 'bar_color',         label: 'Bar color',  kind: 'color_rgba'},
      {key: 'bar_outline',       label: 'Outline',    kind: 'bool'},
      {key: 'reverse_direction', label: 'Reverse',    kind: 'bool'},
      {key: 'background_color',  label: 'Background', kind: 'color_rgba_opt'},
    ],
    defaults: () => ({type: 'progress_bar', source: 'cpu_percentage', width: 200, height: 15, min_value: 0, max_value: 100, bar_color: [0, 255, 0, 255]}),
  },
  radial: {
    label: 'Radial',
    fields: [
      {key: 'source',       label: 'Source',     kind: 'source'},
      {key: 'radius',       label: 'Radius',     kind: 'int', step: 1, min: 5},
      {key: 'width',        label: 'Thickness',  kind: 'int', step: 1, min: 1, help: 'Stroke width of the arc'},
      {key: 'min_value',    label: 'Min value',  kind: 'float'},
      {key: 'max_value',    label: 'Max value',  kind: 'float'},
      {key: 'angle_start',  label: 'Angle start',kind: 'float'},
      {key: 'angle_end',    label: 'Angle end',  kind: 'float'},
      {key: 'clockwise',    label: 'Clockwise',  kind: 'bool'},
      {key: 'bar_color',    label: 'Bar color',  kind: 'color_rgba'},
      {key: 'show_text',    label: 'Show value', kind: 'bool'},
      {key: 'show_unit',    label: 'Show unit',  kind: 'bool'},
      ...FONT_FIELDS,
    ],
    defaults: () => ({type: 'radial', source: 'cpu_percentage', radius: 40, width: 10, min_value: 0, max_value: 100, angle_start: 0, angle_end: 360, clockwise: true, bar_color: [0, 255, 0, 255], show_text: false, show_unit: false, font: {family: '', size: 16, bold: false, color: [255, 255, 255, 255]}}),
  },
  line_graph: {
    label: 'Line graph',
    fields: [
      {key: 'source',           label: 'Source',       kind: 'source'},
      {key: 'width',            label: 'Width',        kind: 'int', step: 1},
      {key: 'height',           label: 'Height',       kind: 'int', step: 1},
      {key: 'min_value',        label: 'Min value',    kind: 'float'},
      {key: 'max_value',        label: 'Max value',    kind: 'float'},
      {key: 'history_size',     label: 'History size', kind: 'int', step: 1, min: 2},
      {key: 'autoscale',        label: 'Autoscale',    kind: 'bool'},
      {key: 'line_color',       label: 'Line color',   kind: 'color_rgba'},
      {key: 'line_width',       label: 'Line width',   kind: 'int', step: 1, min: 1},
      {key: 'axis',             label: 'Axis',         kind: 'bool'},
      {key: 'axis_color',       label: 'Axis color',   kind: 'color_rgba'},
      {key: 'background_color', label: 'Background',   kind: 'color_rgba_opt'},
    ],
    defaults: () => ({type: 'line_graph', source: 'cpu_percentage', width: 150, height: 50, min_value: 0, max_value: 100, history_size: 60, line_color: [255, 255, 255, 255], line_width: 2}),
  },
};

const WIDGET_TYPES = Object.keys(WIDGET_SCHEMAS);

// ---------- Path helpers --------------------------------------------------

function getByPath(obj, path) {
  const parts = path.split('.');
  let cur = obj;
  for (const p of parts) {
    if (cur == null) return undefined;
    cur = cur[p];
  }
  return cur;
}
function setByPath(obj, path, value) {
  const parts = path.split('.');
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    if (cur[parts[i]] == null || typeof cur[parts[i]] !== 'object') cur[parts[i]] = {};
    cur = cur[parts[i]];
  }
  cur[parts[parts.length - 1]] = value;
}
function deleteByPath(obj, path) {
  const parts = path.split('.');
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    if (cur[parts[i]] == null) return;
    cur = cur[parts[i]];
  }
  delete cur[parts[parts.length - 1]];
}

// ---------- Color helpers -------------------------------------------------

function arrToHex(a) {
  if (!a || a.length < 3) return '#ffffff';
  return '#' + a.slice(0, 3).map((v) => Math.max(0, Math.min(255, v|0)).toString(16).padStart(2, '0')).join('');
}
function hexToArr(hex, alpha) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
  if (!m) return [255, 255, 255, alpha == null ? 255 : alpha];
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff, alpha == null ? 255 : alpha];
}

// ---------- Boot ----------------------------------------------------------

async function boot() {
  try {
    const [themeRes, fontsRes] = await Promise.all([
      fetch(`/api/themes/${encodeURIComponent(dirName)}`),
      fetch('/api/fonts'),
    ]);
    if (!themeRes.ok) throw new Error(`theme load HTTP ${themeRes.status}`);
    const payload = await themeRes.json();
    themeData = payload.data || {};
    sourcesList = payload.sources || [];
    if (fontsRes.ok) {
      const fp = await fontsRes.json();
      fontsList = fp.fonts || [];
    }
    editorTitle.textContent = themeData.name || dirName;
    refreshMeta();
    layoutCanvas();
    renderWidgets();
  } catch (err) {
    editorMeta.textContent = `error: ${err.message}`;
    console.error(err);
  }
}

function refreshMeta() {
  const c = themeData.canvas || {width: 1920, height: 480};
  editorMeta.textContent = `${c.width}×${c.height} · widgets ${(themeData.widgets || []).length} · ${fontsList.length} fonts indexed`;
}

window.addEventListener('resize', () => {
  if (themeData) layoutCanvas();
});

// ---------- Canvas layout -------------------------------------------------

function layoutCanvas() {
  const canvas = themeData.canvas || {width: 1920, height: 480};
  const stage = $('canvas-stage');
  const stageW = stage.clientWidth - 24;
  const stageH = stage.clientHeight - 48;
  scale = Math.min(stageW / canvas.width, stageH / canvas.height);
  if (!isFinite(scale) || scale <= 0) scale = 0.3;
  designCanvas.style.width = (canvas.width * scale) + 'px';
  designCanvas.style.height = (canvas.height * scale) + 'px';

  const imageBlock = themeData.image;
  const videoBlock = themeData.video;
  if (imageBlock && imageBlock.path) {
    const url = `/api/themes/${encodeURIComponent(dirName)}/asset/${encodeURI(imageBlock.path)}`;
    designCanvas.style.background = `url("${url}") center / 100% 100% no-repeat`;
    canvasHint.textContent = '';
  } else if (videoBlock && videoBlock.path) {
    designCanvas.style.background = 'repeating-conic-gradient(#2a2a2a 0deg 90deg, #353535 90deg 180deg) 0 0 / 24px 24px';
    canvasHint.textContent = 'Видео-тема: фон — заглушка. Render preview покажет первый кадр.';
  } else {
    designCanvas.style.background = '#222';
    canvasHint.textContent = '';
  }

  for (const box of widgetsById.values()) positionBox(box);
}

// ---------- Widget rendering ----------------------------------------------

function renderWidgets() {
  widgetList.innerHTML = '';
  widgetsById.clear();
  designCanvas.innerHTML = '';

  const widgets = themeData.widgets || [];
  for (const w of widgets) {
    addWidgetBox(w);
    addListItem(w);
  }
  if (selectedId && !widgetsById.has(selectedId)) selectedId = null;
  refreshProperties();
  refreshMeta();
}

function addWidgetBox(w) {
  const box = document.createElement('div');
  box.className = 'widget-box type-' + (w.type || 'unknown');
  if (w.hide) box.classList.add('is-hidden');
  if (w.enabled === false) box.classList.add('is-disabled');
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
  let bw, bh;
  if (w.type === 'chart' || w.type === 'progress_bar' || w.type === 'line_graph') {
    bw = Math.max(20, (w.width || 100) * scale);
    bh = Math.max(20, (w.height || 30) * scale);
  } else if (w.type === 'radial') {
    const r = w.radius || 30;
    bw = bh = r * 2 * scale;
  } else if (w.type === 'image') {
    bw = bh = 30 * scale;
  } else {
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
  for (const [wid, box] of widgetsById) box.classList.toggle('selected', wid === id);
  for (const li of widgetList.children) li.classList.toggle('selected', li.dataset.id === id);
  refreshProperties();
}

// ---------- Properties form ----------------------------------------------

function refreshProperties() {
  if (!selectedId) {
    propertiesPanel.innerHTML = `
      <p class="muted">Выбери виджет в списке слева или на холсте.</p>
      <p class="hint">Горячие клавиши: <kbd>←↑→↓</kbd> двигают на 1px (<kbd>Shift</kbd> = 10px), <kbd>Del</kbd>/<kbd>Backspace</kbd> удаляет, <kbd>Ctrl+D</kbd> дублирует, <kbd>Ctrl+S</kbd> сохраняет, <kbd>Esc</kbd> закрывает модалки.</p>`;
    return;
  }
  const w = findWidget(selectedId);
  if (!w) {
    propertiesPanel.innerHTML = '<p class="muted">Виджет не найден.</p>';
    return;
  }
  const schema = WIDGET_SCHEMAS[w.type] || {fields: [], label: w.type};
  const fields = [...COMMON_FIELDS, ...schema.fields];

  const buttons = `
    <div class="prop-buttons">
      <button class="btn" data-action="duplicate">Duplicate</button>
      <button class="btn btn-danger" data-action="delete">Delete</button>
    </div>`;

  const typeRow = `
    <div class="prop-row">
      <label>Type</label>
      <span class="muted">${escapeHTML(schema.label || w.type)} <small>(сменить — удали и добавь заново)</small></span>
    </div>`;

  const rows = fields.map((f) => renderField(w, f)).join('');
  propertiesPanel.innerHTML = typeRow + `<div class="prop-grid">${rows}</div>` + buttons;

  // Wire change handlers
  propertiesPanel.querySelectorAll('[data-bind]').forEach((el) => {
    el.addEventListener('input', onFieldInput);
    el.addEventListener('change', onFieldInput);
  });
  propertiesPanel.querySelector('[data-action="duplicate"]').addEventListener('click', () => duplicateWidget(selectedId));
  propertiesPanel.querySelector('[data-action="delete"]').addEventListener('click', () => deleteWidget(selectedId));
}

function renderField(w, f) {
  const v = getByPath(w, f.key);
  const help = f.help ? `<small class="hint">${escapeHTML(f.help)}</small>` : '';
  let input;
  switch (f.kind) {
    case 'id':
      input = `<input type="text" data-bind="${f.key}" data-kind="id" value="${escapeAttr(v || '')}">`;
      break;
    case 'string':
      input = `<input type="text" data-bind="${f.key}" data-kind="string" value="${escapeAttr(v == null ? '' : String(v))}">`;
      break;
    case 'int':
    case 'float': {
      const step = f.step != null ? f.step : (f.kind === 'int' ? 1 : 0.1);
      const min = f.min != null ? `min="${f.min}"` : '';
      const max = f.max != null ? `max="${f.max}"` : '';
      input = `<input type="number" data-bind="${f.key}" data-kind="${f.kind}" step="${step}" ${min} ${max} value="${v == null ? '' : v}">`;
      break;
    }
    case 'int_opt': {
      input = `<input type="number" data-bind="${f.key}" data-kind="int_opt" step="1" placeholder="auto" value="${v == null ? '' : v}">`;
      break;
    }
    case 'bool':
      input = `<input type="checkbox" data-bind="${f.key}" data-kind="bool" ${v ? 'checked' : ''}>`;
      break;
    case 'color_rgba':
    case 'color_rgba_opt': {
      const hasValue = Array.isArray(v) && v.length >= 3;
      const hex = arrToHex(v);
      const alpha = hasValue ? (v[3] == null ? 255 : v[3]) : 255;
      const optBtn = f.kind === 'color_rgba_opt'
        ? `<button class="micro" data-bind="${f.key}" data-kind="color_clear" type="button" title="Не задано">×</button>`
        : '';
      input = `
        <span class="color-row">
          <input type="color" data-bind="${f.key}" data-kind="color_hex" value="${hex}" ${hasValue ? '' : 'data-empty="1"'}>
          <input type="number" data-bind="${f.key}" data-kind="color_alpha" min="0" max="255" step="1" value="${alpha}" title="Alpha 0-255">
          ${optBtn}
        </span>`;
      break;
    }
    case 'font': {
      const options = ['<option value="">—</option>'].concat(
        fontsList.map((ff) => `<option value="${escapeAttr(ff.family)}" ${v === ff.family ? 'selected' : ''}>${escapeHTML(ff.family)} <small>· ${escapeHTML(ff.origin)}</small></option>`)
      ).join('');
      input = `<select data-bind="${f.key}" data-kind="font">${options}</select>`;
      break;
    }
    case 'source': {
      const options = ['<option value="">—</option>'].concat(
        sourcesList.map((s) => `<option value="${escapeAttr(s)}" ${v === s ? 'selected' : ''}>${escapeHTML(s)}</option>`)
      ).join('');
      input = `<select data-bind="${f.key}" data-kind="source">${options}</select>`;
      break;
    }
    default:
      input = `<input type="text" data-bind="${f.key}" data-kind="string" value="${escapeAttr(v == null ? '' : String(v))}">`;
  }
  return `<div class="prop-row"><label>${escapeHTML(f.label)}</label><span>${input}${help}</span></div>`;
}

function onFieldInput(ev) {
  const el = ev.currentTarget;
  const path = el.dataset.bind;
  const kind = el.dataset.kind;
  const w = findWidget(selectedId);
  if (!w) return;

  switch (kind) {
    case 'id': {
      const newId = (el.value || '').trim();
      if (!newId || (newId !== w.id && findWidget(newId))) {
        el.value = w.id;
        flashError(el);
        return;
      }
      const box = widgetsById.get(w.id);
      widgetsById.delete(w.id);
      widgetsById.set(newId, box);
      box.dataset.id = newId;
      w.id = newId;
      selectedId = newId;
      // Re-render list (ids changed)
      widgetList.innerHTML = '';
      themeData.widgets.forEach(addListItem);
      for (const li of widgetList.children) li.classList.toggle('selected', li.dataset.id === newId);
      const lbl = box.querySelector('.widget-label');
      if (lbl) lbl.firstChild.textContent = newId;
      break;
    }
    case 'string':
      setByPath(w, path, el.value);
      break;
    case 'int':
    case 'int_opt': {
      const raw = el.value.trim();
      if (kind === 'int_opt' && raw === '') { deleteByPath(w, path); break; }
      const n = parseInt(raw, 10);
      if (!isNaN(n)) setByPath(w, path, n);
      break;
    }
    case 'float': {
      const n = parseFloat(el.value);
      if (!isNaN(n)) setByPath(w, path, n);
      break;
    }
    case 'bool':
      setByPath(w, path, !!el.checked);
      break;
    case 'color_hex':
    case 'color_alpha': {
      const cur = getByPath(w, path);
      const base = Array.isArray(cur) ? cur.slice() : [255, 255, 255, 255];
      if (kind === 'color_hex') {
        const a = base[3] == null ? 255 : base[3];
        const arr = hexToArr(el.value, a);
        setByPath(w, path, arr);
      } else {
        const a = Math.max(0, Math.min(255, parseInt(el.value, 10) || 0));
        base[3] = a;
        setByPath(w, path, base);
      }
      break;
    }
    case 'color_clear':
      deleteByPath(w, path);
      refreshProperties();
      return;
    case 'font':
    case 'source':
      if (el.value) setByPath(w, path, el.value);
      else deleteByPath(w, path);
      break;
  }
  // Reflect on canvas
  const box = widgetsById.get(w.id);
  if (box) {
    if (path === 'x' || path === 'y' || path.startsWith('font.') || path === 'width' || path === 'height' || path === 'radius') {
      positionBox(box, w);
    }
    box.classList.toggle('is-hidden', !!w.hide);
    box.classList.toggle('is-disabled', w.enabled === false);
  }
  markDirty();
}

function flashError(el) {
  el.classList.add('flash-err');
  setTimeout(() => el.classList.remove('flash-err'), 400);
}

// ---------- Widget CRUD ---------------------------------------------------

function addWidget(type) {
  const schema = WIDGET_SCHEMAS[type];
  if (!schema) return;
  const id = uniqueId(type);
  const w = Object.assign({id, x: 100, y: 100, enabled: true}, schema.defaults());
  themeData.widgets = themeData.widgets || [];
  themeData.widgets.push(w);
  renderWidgets();
  selectWidget(id);
  markDirty();
}

function duplicateWidget(id) {
  const idx = themeData.widgets.findIndex((w) => w.id === id);
  if (idx < 0) return;
  const src = themeData.widgets[idx];
  const copy = JSON.parse(JSON.stringify(src));
  copy.id = uniqueId(src.type || 'widget');
  copy.x = (src.x || 0) + 20;
  copy.y = (src.y || 0) + 20;
  themeData.widgets.splice(idx + 1, 0, copy);
  renderWidgets();
  selectWidget(copy.id);
  markDirty();
}

async function deleteWidget(id) {
  const ok = await UI.confirmDialog(
    `Удалить виджет «${id}»? Действие необратимо (можно восстановить из последнего .bak).`,
    {title: 'Удалить виджет', okLabel: 'Удалить', danger: true},
  );
  if (!ok) return;
  themeData.widgets = (themeData.widgets || []).filter((w) => w.id !== id);
  if (selectedId === id) selectedId = null;
  renderWidgets();
  markDirty();
  UI.toast(`Удалён: ${id}`, 'info', 2500);
}

function uniqueId(base) {
  let n = 1;
  let candidate = `${base}_${n}`;
  while (findWidget(candidate)) {
    n += 1;
    candidate = `${base}_${n}`;
  }
  return candidate;
}

function findWidget(id) {
  return (themeData.widgets || []).find((w) => w.id === id);
}

// ---------- Drag-and-drop -------------------------------------------------

function onWidgetMouseDown(ev) {
  if (ev.button !== 0) return;
  ev.preventDefault();
  const id = ev.currentTarget.dataset.id;
  selectWidget(id);
  const w = findWidget(id);
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
  const w = findWidget(dragState.id);
  if (!w) return;
  w.x = Math.round(dragState.startWidgetX + dx);
  w.y = Math.round(dragState.startWidgetY + dy);
  const box = widgetsById.get(dragState.id);
  if (box) positionBox(box, w);
  if (selectedId === dragState.id) {
    const xi = propertiesPanel.querySelector('[data-bind="x"]');
    const yi = propertiesPanel.querySelector('[data-bind="y"]');
    if (xi) xi.value = w.x;
    if (yi) yi.value = w.y;
  }
  dragState.moved = true;
}

function onDragEnd() {
  if (dragState && dragState.moved) markDirty();
  dragState = null;
  window.removeEventListener('mousemove', onDragMove);
  window.removeEventListener('mouseup', onDragEnd);
}

// ---------- Save / preview ------------------------------------------------

// Dirty-state tracking — beforeunload warns the user before they lose
// unsaved edits by closing the tab / navigating away.
let dirty = false;
function markDirty() { dirty = true; updateSaveBadge(); }
function markClean() { dirty = false; updateSaveBadge(); }
function updateSaveBadge() {
  const btn = $('btn-save');
  if (!btn) return;
  btn.classList.toggle('btn-dirty', dirty);
  btn.textContent = dirty ? 'Save *' : 'Save';
}
window.addEventListener('beforeunload', (ev) => {
  if (!dirty) return;
  ev.preventDefault();
  ev.returnValue = 'Есть несохранённые изменения. Уйти со страницы?';
  return ev.returnValue;
});

async function save() {
  const btn = $('btn-save');
  btn.disabled = true;
  const wasDirty = dirty;
  btn.textContent = 'Saving…';
  try {
    const res = await fetch(`/api/themes/${encodeURIComponent(dirName)}/save`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({data: themeData}),
    });
    if (!res.ok) throw new Error(await UI.parseErrorResponse(res));
    markClean();
    btn.textContent = 'Saved ✓';
    UI.toast('Сохранено в theme.yaml (бекап в .bak)', 'success');
    setTimeout(() => { btn.disabled = false; updateSaveBadge(); }, 1500);
  } catch (err) {
    UI.toast('Save failed: ' + err.message, 'error', 6000);
    btn.disabled = false;
    updateSaveBadge();
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
    if (!res.ok) throw new Error(await UI.parseErrorResponse(res));
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const img = $('preview-image');
    img.onload = () => URL.revokeObjectURL(url);
    img.src = url;
    $('preview-modal').classList.remove('hidden');
  } catch (err) {
    UI.toast('Preview render failed: ' + err.message, 'error', 6000);
  } finally {
    btn.textContent = orig;
    btn.disabled = false;
  }
}

// ---------- Wiring --------------------------------------------------------

async function pushLive() {
  const btn = $('btn-push-live');
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = 'Pushing…';
  try {
    const res = await fetch(`/api/themes/${encodeURIComponent(dirName)}/push-live`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({data: themeData}),
    });
    if (!res.ok) throw new Error(await UI.parseErrorResponse(res));
    btn.textContent = 'Pushed ✓';
    UI.toast('Изменения на экране (без сохранения)', 'success');
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1500);
  } catch (err) {
    UI.toast('Push live failed: ' + err.message, 'error', 7000);
    btn.textContent = orig;
    btn.disabled = false;
  }
}

$('btn-save').addEventListener('click', save);
$('btn-preview').addEventListener('click', previewRender);
if ($('btn-push-live')) $('btn-push-live').addEventListener('click', pushLive);
$('preview-close').addEventListener('click', () => $('preview-modal').classList.add('hidden'));
$('preview-modal').addEventListener('click', (ev) => {
  if (ev.target.id === 'preview-modal') $('preview-modal').classList.add('hidden');
});

// ---------- Keyboard shortcuts -------------------------------------------
// Editor-wide hotkeys. Skip when focus is inside an input/textarea/select
// so typing in the properties panel doesn't trigger them.
document.addEventListener('keydown', (ev) => {
  // Modal escape — close whichever modal is open
  if (ev.key === 'Escape') {
    if (!$('preview-modal').classList.contains('hidden')) {
      $('preview-modal').classList.add('hidden');
      ev.preventDefault();
      return;
    }
    if (!$('crop-modal').classList.contains('hidden')) {
      $('crop-modal').classList.add('hidden');
      ev.preventDefault();
      return;
    }
  }
  // Don't capture typing-area shortcuts
  const tag = (ev.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select' || ev.target.isContentEditable) {
    // Ctrl/Cmd+S still works inside inputs — common save expectation
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 's') {
      ev.preventDefault(); save();
    }
    return;
  }
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 's') {
    ev.preventDefault(); save();
  } else if (ev.key === 'Delete' || ev.key === 'Backspace') {
    if (selectedId) { ev.preventDefault(); deleteWidget(selectedId); }
  } else if (ev.key === 'd' && (ev.ctrlKey || ev.metaKey)) {
    if (selectedId) { ev.preventDefault(); duplicateWidget(selectedId); }
  } else if (['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(ev.key) && selectedId) {
    // Arrow nudge: 1 px, shift = 10 px
    ev.preventDefault();
    const step = ev.shiftKey ? 10 : 1;
    const w = findWidget(selectedId);
    if (!w) return;
    if (ev.key === 'ArrowLeft')  w.x = (w.x || 0) - step;
    if (ev.key === 'ArrowRight') w.x = (w.x || 0) + step;
    if (ev.key === 'ArrowUp')    w.y = (w.y || 0) - step;
    if (ev.key === 'ArrowDown')  w.y = (w.y || 0) + step;
    const box = widgetsById.get(selectedId);
    if (box) positionBox(box, w);
    const xi = propertiesPanel.querySelector('[data-bind="x"]');
    const yi = propertiesPanel.querySelector('[data-bind="y"]');
    if (xi) xi.value = w.x;
    if (yi) yi.value = w.y;
    markDirty();
  }
});

// Populate "Add widget" type picker
const addPicker = $('add-widget-type');
if (addPicker) {
  addPicker.innerHTML = WIDGET_TYPES.map((t) => `<option value="${t}">${WIDGET_SCHEMAS[t].label}</option>`).join('');
  $('add-widget-btn').addEventListener('click', () => addWidget(addPicker.value));
}

// ---------- Canvas resize -------------------------------------------------

function syncCanvasInputs() {
  const c = themeData.canvas || {width: 1920, height: 480};
  const wi = $('canvas-width'); const hi = $('canvas-height');
  if (wi) wi.value = c.width;
  if (hi) hi.value = c.height;
}

if ($('canvas-apply')) {
  $('canvas-apply').addEventListener('click', () => {
    const w = parseInt($('canvas-width').value, 10);
    const h = parseInt($('canvas-height').value, 10);
    if (!w || !h || w < 32 || h < 32 || w > 8000 || h > 8000) {
      UI.toast('Width/Height нужны в диапазоне 32..8000', 'error');
      return;
    }
    themeData.canvas = {width: w, height: h};
    layoutCanvas();
    refreshMeta();
    markDirty();
  });
}

// ---------- Background upload + crop -------------------------------------

let cropState = null;  // { filename, srcW, srcH, dispW, dispH, scale, boxX, boxY, dragOffX, dragOffY }

function refreshBgSummary() {
  const el = $('bg-summary');
  if (!el) return;
  const img = themeData.image;
  const vid = themeData.video;
  if (img && img.path) {
    el.innerHTML = `<span class="muted">image:</span> ${escapeHTML(img.path)}`;
  } else if (vid && vid.path) {
    el.innerHTML = `<span class="muted">video:</span> ${escapeHTML(vid.path)} <small class="hint">(не кропается)</small>`;
  } else {
    el.innerHTML = `<span class="muted">нет фона — загрузи файл</span>`;
  }
}

if ($('bg-upload')) {
  $('bg-upload').addEventListener('change', async (ev) => {
    const file = ev.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    try {
      const res = await fetch(`/api/themes/${encodeURIComponent(dirName)}/upload`, {
        method: 'POST',
        body: fd,
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `HTTP ${res.status}`);
      }
      const meta = await res.json();
      ev.target.value = '';
      if (meta.width && meta.height) {
        openCropModal(meta);
      } else {
        // No dimensions → assume video; ask to use it as-is.
        if (confirm(`Использовать ${meta.filename} как видео-фон?`)) {
          await setVideoBg(meta.filename);
        }
      }
    } catch (err) {
      alert('Upload failed: ' + err.message);
    }
  });
}

async function setVideoBg(filename) {
  try {
    const res = await fetch(`/api/themes/${encodeURIComponent(dirName)}/set-video`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({filename}),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${res.status}`);
    }
    const data = await res.json();
    delete themeData.image;
    themeData.video = {path: data.video_path};
    layoutCanvas();
    refreshBgSummary();
  } catch (err) {
    alert('Set video failed: ' + err.message);
  }
}

function openCropModal(meta) {
  const canvas = themeData.canvas || {width: 1920, height: 480};
  const stage = $('crop-image-wrap');
  const modal = $('crop-modal');
  modal.classList.remove('hidden');

  const img = $('crop-image');
  img.onload = () => {
    // Scale image to fit the modal stage. Stage size known after layout.
    const maxW = window.innerWidth * 0.85;
    const maxH = window.innerHeight * 0.7;
    const scale = Math.min(maxW / meta.width, maxH / meta.height, 1);
    const dispW = meta.width * scale;
    const dispH = meta.height * scale;
    stage.style.width = dispW + 'px';
    stage.style.height = dispH + 'px';
    img.style.width = dispW + 'px';
    img.style.height = dispH + 'px';

    // Crop box: same pixel size as canvas, scaled to display. If canvas
    // bigger than source — clamp box to source dims (user gets a
    // resize-to-canvas warning).
    let boxSrcW = Math.min(canvas.width, meta.width);
    let boxSrcH = Math.min(canvas.height, meta.height);
    const boxDispW = boxSrcW * scale;
    const boxDispH = boxSrcH * scale;
    const box = $('crop-box');
    box.style.width = boxDispW + 'px';
    box.style.height = boxDispH + 'px';
    box.style.left = ((dispW - boxDispW) / 2) + 'px';
    box.style.top = ((dispH - boxDispH) / 2) + 'px';

    cropState = {
      filename: meta.filename,
      srcW: meta.width, srcH: meta.height,
      canvasW: canvas.width, canvasH: canvas.height,
      dispW, dispH,
      scale,
      boxSrcW, boxSrcH,
      boxDispW, boxDispH,
    };
    updateCropInfo();
  };
  img.src = `/api/themes/${encodeURIComponent(dirName)}/asset/${encodeURI(meta.filename)}?t=${Date.now()}`;
}

function updateCropInfo() {
  if (!cropState) return;
  const box = $('crop-box');
  const left = parseFloat(box.style.left) || 0;
  const top = parseFloat(box.style.top) || 0;
  const srcX = Math.round(left / cropState.scale);
  const srcY = Math.round(top / cropState.scale);
  let msg = `Source ${cropState.srcW}×${cropState.srcH} · Crop @ ${srcX},${srcY} size ${cropState.boxSrcW}×${cropState.boxSrcH}`;
  if (cropState.boxSrcW !== cropState.canvasW || cropState.boxSrcH !== cropState.canvasH) {
    msg += ` · scaled to canvas ${cropState.canvasW}×${cropState.canvasH}`;
  }
  $('crop-info').textContent = msg;
}

// Drag the crop box
let cropDrag = null;
const cropBoxEl = $('crop-box');
if (cropBoxEl) {
  cropBoxEl.addEventListener('mousedown', (ev) => {
    if (!cropState) return;
    ev.preventDefault();
    const left = parseFloat(cropBoxEl.style.left) || 0;
    const top = parseFloat(cropBoxEl.style.top) || 0;
    cropDrag = {sx: ev.clientX, sy: ev.clientY, ox: left, oy: top};
    window.addEventListener('mousemove', cropMove);
    window.addEventListener('mouseup', cropEnd);
  });
}
function cropMove(ev) {
  if (!cropDrag || !cropState) return;
  let nx = cropDrag.ox + (ev.clientX - cropDrag.sx);
  let ny = cropDrag.oy + (ev.clientY - cropDrag.sy);
  nx = Math.max(0, Math.min(cropState.dispW - cropState.boxDispW, nx));
  ny = Math.max(0, Math.min(cropState.dispH - cropState.boxDispH, ny));
  cropBoxEl.style.left = nx + 'px';
  cropBoxEl.style.top = ny + 'px';
  updateCropInfo();
}
function cropEnd() {
  cropDrag = null;
  window.removeEventListener('mousemove', cropMove);
  window.removeEventListener('mouseup', cropEnd);
}

if ($('crop-apply')) {
  $('crop-apply').addEventListener('click', async () => {
    if (!cropState) return;
    const left = parseFloat(cropBoxEl.style.left) || 0;
    const top = parseFloat(cropBoxEl.style.top) || 0;
    const srcX = Math.round(left / cropState.scale);
    const srcY = Math.round(top / cropState.scale);
    try {
      const res = await fetch(`/api/themes/${encodeURIComponent(dirName)}/crop-bg`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          filename: cropState.filename,
          crop: {x: srcX, y: srcY, w: cropState.boxSrcW, h: cropState.boxSrcH},
          fit_canvas: true,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `HTTP ${res.status}`);
      }
      const data = await res.json();
      // Update local themeData and re-layout the canvas
      delete themeData.video;
      themeData.image = {path: data.background_path};
      layoutCanvas();
      refreshBgSummary();
      $('crop-modal').classList.add('hidden');
    } catch (err) {
      alert('Crop failed: ' + err.message);
    }
  });
}
if ($('crop-close')) {
  $('crop-close').addEventListener('click', () => $('crop-modal').classList.add('hidden'));
}

// "Crop image…" button — re-open cropper for the currently-set background
if ($('bg-crop')) {
  $('bg-crop').addEventListener('click', async () => {
    const img = themeData.image;
    if (!img || !img.path) { alert('Сначала загрузи изображение.'); return; }
    // Probe its size by fetching as a blob (lightweight)
    try {
      const url = `/api/themes/${encodeURIComponent(dirName)}/asset/${encodeURI(img.path)}?t=${Date.now()}`;
      const probe = await new Promise((res, rej) => {
        const im = new Image();
        im.onload = () => res({width: im.naturalWidth, height: im.naturalHeight, filename: img.path});
        im.onerror = rej;
        im.src = url;
      });
      openCropModal(probe);
    } catch (err) {
      alert('Не удалось прочитать фон для перекропа: ' + err);
    }
  });
}

// ---------- Required fonts panel -----------------------------------------

function refreshRequiredFonts() {
  const el = $('required-fonts');
  if (!el) return;
  // Recompute from current widgets — mirrors backend's _collect_required_fonts
  const seen = new Set();
  for (const w of (themeData.widgets || [])) {
    for (const key of ['font', 'axis_font']) {
      const f = w[key];
      if (f && typeof f === 'object' && f.family) {
        const fam = String(f.family);
        if (!fam.includes('/') && !fam.includes('\\')) seen.add(fam);
      }
    }
  }
  const fonts = [...seen].sort();
  if (!fonts.length) {
    el.innerHTML = '<span class="muted">нет шрифтов в виджетах</span>';
    return;
  }
  const have = new Set(fontsList.map((f) => f.family));
  el.innerHTML = fonts.map((fam) => {
    const ok = have.has(fam);
    return `<div class="rf-row ${ok ? 'ok' : 'bad'}">
              <span class="rf-status">${ok ? '✓' : '✗'}</span>
              <span class="rf-name" title="${escapeAttr(fam)}">${escapeHTML(fam)}</span>
            </div>`;
  }).join('');
}

// Update bg button enabled state on data load
const _origRenderWidgets = renderWidgets;
renderWidgets = function() {
  _origRenderWidgets();
  syncCanvasInputs();
  refreshBgSummary();
  refreshRequiredFonts();
  if ($('bg-crop')) {
    $('bg-crop').disabled = !(themeData.image && themeData.image.path);
  }
};

// ---------- utils ---------------------------------------------------------

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}
function escapeAttr(s) { return escapeHTML(s); }

boot();
