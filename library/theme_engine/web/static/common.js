/* Shared UI helpers used by both the dashboard and the editor.
 *
 * Loaded BEFORE app.js / editor.js in each template. Defines:
 *   toast(message, type, duration)     non-blocking bottom-right notification
 *   confirmDialog(message, ok)         promise-based replacement for window.confirm
 *   promptDialog(message, default_)    promise-based replacement for window.prompt
 *   parseErrorResponse(res)            extracts a useful error from a Response
 *   onEscape(fn)                       attach an ESC handler that auto-removes
 *
 * Toast and dialogs use shared CSS in common.css.
 */
(function () {
  // ---------- Toast ----------
  function toast(message, type = 'info', duration = 3500) {
    let host = document.getElementById('toast-host');
    if (!host) {
      host = document.createElement('div');
      host.id = 'toast-host';
      document.body.appendChild(host);
    }
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.textContent = message;
    host.appendChild(el);
    requestAnimationFrame(() => el.classList.add('show'));
    const hide = () => {
      el.classList.remove('show');
      setTimeout(() => el.remove(), 250);
    };
    el.addEventListener('click', hide);
    setTimeout(hide, duration);
    return el;
  }

  // ---------- Modal helper ----------
  // Builds a minimal modal at runtime; resolves the promise on OK / cancel / esc.
  function _makeModal({title, bodyHTML, okLabel = 'OK', cancelLabel = 'Отмена', danger = false}) {
    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.className = 'modal js-dialog-modal';
      overlay.innerHTML = `
        <div class="modal-inner dialog-inner">
          <div class="modal-bar"><span>${escapeHTML(title)}</span></div>
          <div class="modal-body">${bodyHTML}</div>
          <div class="modal-actions dialog-actions">
            <button class="btn" data-role="cancel">${escapeHTML(cancelLabel)}</button>
            <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-role="ok">${escapeHTML(okLabel)}</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);

      const close = (result) => {
        document.removeEventListener('keydown', onKey);
        overlay.remove();
        resolve(result);
      };
      function onKey(ev) {
        if (ev.key === 'Escape') { ev.preventDefault(); close(null); }
        if (ev.key === 'Enter') {
          // Don't close on Enter inside a textarea
          if (ev.target.tagName === 'TEXTAREA') return;
          ev.preventDefault();
          const input = overlay.querySelector('[data-role="ok"]');
          input.click();
        }
      }
      document.addEventListener('keydown', onKey);
      overlay.addEventListener('click', (ev) => {
        if (ev.target === overlay) close(null);
      });
      overlay.querySelector('[data-role="cancel"]').addEventListener('click', () => close(null));
      overlay.querySelector('[data-role="ok"]').addEventListener('click', () => {
        const input = overlay.querySelector('[data-dialog-input]');
        close(input ? input.value : true);
      });
      // Autofocus the input if present, otherwise the OK button
      setTimeout(() => {
        const input = overlay.querySelector('[data-dialog-input]');
        if (input) { input.focus(); input.select?.(); }
        else overlay.querySelector('[data-role="ok"]').focus();
      }, 50);
    });
  }

  function confirmDialog(message, {okLabel = 'OK', cancelLabel = 'Отмена', title = 'Подтверждение', danger = false} = {}) {
    return _makeModal({
      title, okLabel, cancelLabel, danger,
      bodyHTML: `<p class="dialog-message">${escapeHTML(message)}</p>`,
    });
  }

  function promptDialog(message, defaultValue = '', {okLabel = 'OK', title = 'Ввод'} = {}) {
    const id = 'dlg-input-' + Math.random().toString(36).slice(2, 9);
    return _makeModal({
      title, okLabel,
      bodyHTML: `
        <label class="form-row" for="${id}">
          <span>${escapeHTML(message)}</span>
          <input id="${id}" type="text" data-dialog-input value="${escapeAttr(defaultValue)}">
        </label>`,
    }).then((v) => (v == null ? null : String(v).trim() || null));
  }

  // ---------- Response parsing ----------
  async function parseErrorResponse(res) {
    try {
      const body = await res.json();
      if (body.error) return body.error;
      if (body.reason) return body.reason;
    } catch { /* not JSON */ }
    return `HTTP ${res.status}`;
  }

  // ---------- Misc ----------
  function onEscape(fn) {
    const handler = (ev) => { if (ev.key === 'Escape') fn(ev); };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }

  function escapeHTML(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  function escapeAttr(s) { return escapeHTML(s); }

  // Expose
  window.UI = {
    toast,
    confirmDialog,
    promptDialog,
    parseErrorResponse,
    onEscape,
    escapeHTML,
    escapeAttr,
  };
})();
