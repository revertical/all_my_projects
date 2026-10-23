/* ==========================================================================
   File Shredder 1.3 - client
   ========================================================================== */
'use strict';

const TOKEN = window.SHREDDER_TOKEN || '';
const API = {
  state: '/api/state',
  modes: '/api/modes',
  home: '/api/home_dir_contents',
  listDir: '/api/list_dir',
  add: '/api/add',
  remove: '/api/remove',
  clear: '/api/clear',
  load: '/api/load',
  save: '/api/save',
  open: '/api/open',
  shred: '/api/shred',
  cancel: '/api/cancel',
  options: '/api/options',
};

const state = {
  queue: [],
  count: 0,
  totalBytes: 0,
  mode: 'expert',
  modes: {},
  trim: true,
  busy: false,
  job: null,
  selection: new Set(),
  home: '~',
  pollTimer: null,
};

/* ------------------------------------------------------------------ dom -- */
const $ = (id) => document.getElementById(id);

const el = {
  connChip: $('conn-chip'), connLabel: $('conn-label'), winSsd: $('win-ssd'),
  queueList: $('queue-list'), queueEmpty: $('queue-empty'), queueMeta: $('queue-meta'),
  dropTarget: $('drop-target'), dropVeil: $('drop-veil'),
  selbar: $('selbar'), selbarLabel: $('selbar-label'),
  btnSelectAll: $('btn-select-all'), btnSelectNone: $('btn-select-none'),
  btnOpen: $('btn-open'), btnRemove: $('btn-remove'),
  btnAddFiles: $('btn-add-files'), btnAddFolders: $('btn-add-folders'),
  btnLoad: $('btn-load'), btnSave: $('btn-save'), btnClear: $('btn-clear'),
  modeTrigger: $('mode-trigger'), modeMenu: $('mode-menu'),
  modeValue: $('mode-value'), modeHint: $('mode-hint'),
  trimToggle: $('trim-toggle'),
  btnShred: $('btn-shred'), shredLabel: $('shred-label'),
  statusPip: $('status-pip'), statusText: $('status-text'), statusDetail: $('status-detail'),
  dlgBrowser: $('dlg-browser'), browserTitle: $('browser-title'),
  browserCrumbs: $('browser-crumbs'), browserList: $('browser-list'),
  browserPath: $('browser-path'), browserNote: $('browser-note'),
  browserConfirm: $('browser-confirm'), browserCancel: $('browser-cancel'),
  browserClose: $('browser-close'),
  dlgConfirm: $('dlg-confirm'), confirmTitle: $('confirm-title'),
  confirmBody: $('confirm-body'), confirmOk: $('confirm-ok'), confirmCancel: $('confirm-cancel'),
  dlgProgress: $('dlg-progress'), progItem: $('prog-item'),
  progOverallVal: $('prog-overall-val'), progOverallBar: $('prog-overall-bar'),
  progItemVal: $('prog-item-val'), progItemBar: $('prog-item-bar'),
  progPct: $('prog-pct'), progCancel: $('prog-cancel'),
  snackbar: $('snackbar'), snackbarText: $('snackbar-text'), snackbarClose: $('snackbar-close'),
};

/* --------------------------------------------------------------- icons -- */
const ICON = {
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  folder: '<path d="M10 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-8z"/>',
  gone: '<path d="M12 4l9 16H3z"/><path d="M12 10v4"/><path d="M12 17h.01"/>',
  grip: '<path d="M9 6h.01"/><path d="M9 12h.01"/><path d="M9 18h.01"/>' +
        '<path d="M15 6h.01"/><path d="M15 12h.01"/><path d="M15 18h.01"/>',
  check: '<path d="M5 13l4 4L19 7"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
};

function svg(inner, extra) {
  const extraAttrs = extra ? ' ' + extra : '';
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"` +
         ` stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"${extraAttrs}>` +
         inner + '</svg>';
}

/* ----------------------------------------------------------------- net -- */
async function api(path, body) {
  const options = { method: body === undefined ? 'GET' : 'POST',
                    headers: { 'X-Shredder-Token': TOKEN } };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let payload;
  try {
    payload = await response.json();
  } catch (_err) {
    throw new Error('server returned a non-JSON response (' + response.status + ')');
  }
  if (!response.ok || payload.ok === false) {
    const error = new Error(payload.error || ('request failed (' + response.status + ')'));
    error.detail = payload.detail;
    error.status = response.status;
    throw error;
  }
  return payload;
}

function setConnected(ok, label) {
  el.connChip.classList.toggle('ok', ok);
  el.connChip.classList.toggle('bad', !ok);
  el.connLabel.textContent = label;
}

/* -------------------------------------------------------------- format -- */
function bytes(n) {
  if (n === null || n === undefined) return '--';
  if (n === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const index = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  const value = n / Math.pow(1024, index);
  return (value >= 100 || index === 0 ? value.toFixed(0) : value.toFixed(1)) + ' ' + units[index];
}

function passes(n) { return n === 1 ? '1 pass' : n + ' passes'; }

/* Every array coming back from the API is treated as possibly-absent so a
   payload change can never throw mid-render. */
function list(value) { return Array.isArray(value) ? value : []; }

function summariseAdd(data) {
  const parts = [];
  const added = list(data.added).length;
  const skipped = list(data.skipped).length;
  const blocked = list(data.queue).filter((e) => e.shreddable === false).length;
  if (added) parts.push(added + ' added');
  if (skipped) parts.push(skipped + ' skipped');
  if (blocked) parts.push(blocked + ' view-only');
  return parts.join(', ');
}

function shorten(path) {
  const home = state.home;
  if (home && path.toLowerCase().startsWith(home.toLowerCase())) {
    return '~' + path.slice(home.length).replace(/\\/g, '/');
  }
  return path;
}

/* ------------------------------------------------------------ snackbar -- */
let snackTimer = null;
function toast(message) {
  el.snackbarText.textContent = message;
  el.snackbar.hidden = false;
  el.snackbar.style.animation = 'none';
  void el.snackbar.offsetWidth;
  el.snackbar.style.animation = '';
  clearTimeout(snackTimer);
  snackTimer = setTimeout(() => { el.snackbar.hidden = true; }, 6000);
}
el.snackbarClose.addEventListener('click', () => { el.snackbar.hidden = true; });

/* -------------------------------------------------------------- status -- */
function setStatus(text, detail, tone) {
  el.statusText.textContent = text;
  el.statusDetail.textContent = detail || '';
  el.statusPip.className = 'pip' + (tone ? ' ' + tone : '');
}

/* --------------------------------------------------------------- render -- */
function render() {
  const queue = state.queue;

  el.queueList.replaceChildren(...queue.map((entry) => {
    const li = document.createElement('li');
    li.className = 'row-item' + (state.selection.has(entry.index) ? ' selected' : '');
    li.setAttribute('role', 'option');
    li.setAttribute('aria-selected', state.selection.has(entry.index) ? 'true' : 'false');
    li.dataset.index = entry.index;

    const leadClass = entry.exists === false ? 'lead gone'
                    : entry.is_dir ? 'lead folder' : 'lead';
    const icon = entry.exists === false ? ICON.gone : entry.is_dir ? ICON.folder : ICON.file;

    const lead = document.createElement('span');
    lead.className = leadClass;
    lead.innerHTML = svg(icon, entry.is_dir && entry.exists !== false ? 'fill="currentColor"' : '');

    const text = document.createElement('span');
    text.className = 'row-text';
    const name = document.createElement('span');
    name.className = 'row-name';
    name.textContent = entry.name;
    const sub = document.createElement('span');
    sub.className = 'row-sub';
    sub.textContent = entry.exists === false
      ? 'missing on disk'
      : (entry.block_reason ? entry.block_reason + ' \u2014 view only' : shorten(entry.path));
    text.append(name, sub);

    const tail = document.createElement('span');
    tail.className = 'row-tail';
    const size = document.createElement('span');
    size.className = 'row-size';
    size.textContent = entry.is_dir ? 'folder' : bytes(entry.size);
    const pass = document.createElement('span');
    pass.className = 'row-pass';
    pass.textContent = passes(entry.passes);
    tail.append(size, pass);

    const caret = document.createElement('span');
    caret.className = 'caret';
    caret.innerHTML = svg(ICON.grip);

    li.append(lead, text, tail, caret);
    return li;
  }));

  el.queueEmpty.classList.toggle('show', queue.length === 0);
  el.queueMeta.textContent = queue.length === 0
    ? 'Empty'
    : queue.length + ' item' + (queue.length === 1 ? '' : 's') + ' \u00b7 ' +
      bytes(state.totalBytes) + ' to write';

  const selected = state.selection.size;
  el.selbar.hidden = selected === 0;
  el.selbarLabel.textContent = selected + ' selected';

  const hasQueue = queue.length > 0;
  el.btnClear.disabled = !hasQueue || state.busy;
  el.btnSave.disabled = !hasQueue || state.busy;
  el.btnLoad.disabled = state.busy;
  el.btnAddFiles.disabled = state.busy;
  el.btnAddFolders.disabled = state.busy;
  el.btnOpen.disabled = selected === 0 || state.busy;
  el.btnRemove.disabled = selected === 0 || state.busy;
  el.modeTrigger.disabled = state.busy;
  el.trimToggle.disabled = state.busy;

  el.btnShred.disabled = selected === 0 || state.busy;
  el.shredLabel.textContent = selected === 0
    ? (hasQueue ? 'Select items to shred' : 'Shred Selected Files/Folders')
    : 'Shred ' + selected + ' Item' + (selected === 1 ? '' : 's');

  if (!state.busy) {
    const job = state.job;
    let text = 'Ready';
    let tone = '';
    if (job && !job.running) {
      const failed = list(job.failed);
      if (job.cancelled) { text = 'Stopped'; tone = 'bad'; }
      else if (failed.length) {
        text = 'Finished with ' + failed.length + ' failure' +
               (failed.length === 1 ? '' : 's');
        tone = 'bad';
      } else {
        text = 'Destroyed ' + list(job.succeeded).length + ' item(s)';
      }
    }
    setStatus(text,
      hasQueue ? queue.length + ' queued \u00b7 ' + bytes(state.totalBytes) : '', tone);
  }
}

/* ----------------------------------------------------------- selection -- */
el.queueList.addEventListener('click', (event) => {
  const row = event.target.closest('.row-item');
  if (!row) return;
  const index = Number(row.dataset.index);
  if (event.ctrlKey || event.metaKey) {
    state.selection.has(index) ? state.selection.delete(index)
                               : state.selection.add(index);
  } else if (event.shiftKey && state.selection.size) {
    const anchor = Math.min(...state.selection);
    const [from, to] = index < anchor ? [index, anchor] : [anchor, index];
    for (let i = from; i <= to; i += 1) state.selection.add(i);
  } else {
    state.selection.clear();
    state.selection.add(index);
  }
  render();
});

el.queueList.addEventListener('keydown', (event) => {
  if (!state.queue.length) return;
  const indices = state.queue.map((e) => e.index);
  let cursor = state.selection.size ? Math.min(...state.selection) : 0;
  if (event.key === 'ArrowDown') cursor = Math.min(cursor + 1, indices.length - 1);
  else if (event.key === 'ArrowUp') cursor = Math.max(cursor - 1, 0);
  else if (event.key === 'Home') cursor = 0;
  else if (event.key === 'End') cursor = indices.length - 1;
  else if (event.key === 'a' && (event.ctrlKey || event.metaKey)) {
    indices.forEach((i) => state.selection.add(i)); render(); return;
  } else if (event.key === 'Escape') {
    state.selection.clear(); render(); return;
  } else return;
  event.preventDefault();
  if (event.shiftKey) { state.selection.add(indices[cursor]); }
  else { state.selection.clear(); state.selection.add(indices[cursor]); }
  render();
});

el.btnSelectAll.addEventListener('click', () => {
  state.queue.forEach((e) => state.selection.add(e.index));
  render();
});
el.btnSelectNone.addEventListener('click', () => {
  state.selection.clear();
  render();
});

/* ---------------------------------------------------------------- modes -- */
function buildModeMenu() {
  const entries = Object.entries(state.modes);
  if (!entries.length) {
    // Never present an empty dropdown: the mode could not be loaded.
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'menu-item';
    retry.textContent = 'Reload shredding modes';
    retry.addEventListener('click', async () => {
      closeMenu();
      try {
        await loadModes();
        toast('Shredding modes reloaded.');
      } catch (err) { toast(err.message); }
    });
    const note = document.createElement('div');
    note.className = 'menu-note';
    note.textContent = 'Mode list unavailable.';
    el.modeMenu.replaceChildren(note, retry);
    return;
  }

  el.modeMenu.replaceChildren(...entries.map(([key, mode]) => {
    const button = document.createElement('button');
    button.className = 'menu-item';
    button.type = 'button';
    button.setAttribute('role', 'option');
    button.dataset.mode = key;
    button.setAttribute('aria-selected', key === state.mode ? 'true' : 'false');

    const check = document.createElement('span');
    check.className = 'mi-check';
    check.innerHTML = svg(ICON.check);

    const label = document.createElement('span');
    label.textContent = mode.label;
    const hint = document.createElement('span');
    hint.className = 'supporting';
    hint.style.marginLeft = 'auto';
    hint.style.paddingLeft = '12px';
    hint.textContent = passes(mode.passes);

    button.append(check, label, hint);
    button.addEventListener('click', async () => {
      closeMenu();
      const seq = ++modeSeq;
      try {
        const data = await api(API.options, { mode: key });
        // A newer click may have landed while this was in flight; the last
        // selection wins, otherwise the label could show a stale mode.
        if (seq !== modeSeq) return;
        apply(data);
        el.modeMenu.setAttribute('aria-activedescendant', key);
      } catch (err) {
        if (seq === modeSeq) toast(err.message);
      }
    });
    return button;
  }));
}

/* Monotonic guard so out-of-order /api/options replies cannot win. */
let modeSeq = 0;

async function loadModes() {
  const data = await api(API.modes);
  state.modes = data.modes || {};
  const active = state.modes[data.active];
  if (active) {
    el.modeValue.textContent = active.label;
    el.modeHint.textContent = active.hint;
  }
  el.modeTrigger.disabled = false;
  return state.modes;
}

function openMenu() {
  buildModeMenu();
  el.modeMenu.hidden = false;
  el.modeTrigger.setAttribute('aria-expanded', 'true');
}
function closeMenu() {
  el.modeMenu.hidden = true;
  el.modeTrigger.setAttribute('aria-expanded', 'false');
}
el.modeTrigger.addEventListener('click', (event) => {
  event.stopPropagation();
  el.modeMenu.hidden ? openMenu() : closeMenu();
});
document.addEventListener('click', (event) => {
  if (!el.modeMenu.hidden && !el.modeMenu.contains(event.target) &&
      event.target !== el.modeTrigger) closeMenu();
});

el.trimToggle.addEventListener('click', async () => {
  const next = el.trimToggle.getAttribute('aria-checked') !== 'true';
  try {
    const data = await api(API.options, { trim: next });
    apply(data);
  } catch (err) { toast(err.message); }
});

/* ------------------------------------------------------- confirm dialog -- */
let confirmResolve = null;
function askConfirm({ title, body, ok, danger }) {
  el.confirmTitle.textContent = title;
  el.confirmBody.textContent = body;
  el.confirmOk.textContent = ok;
  el.confirmOk.className = danger === false ? 'btn filled' : 'btn error';
  el.dlgConfirm.hidden = false;
  el.confirmOk.focus();
  return new Promise((resolve) => { confirmResolve = resolve; });
}
function closeConfirm(result) {
  el.dlgConfirm.hidden = true;
  if (confirmResolve) { confirmResolve(result); confirmResolve = null; }
}
el.confirmOk.addEventListener('click', () => closeConfirm(true));
el.confirmCancel.addEventListener('click', () => closeConfirm(false));
el.dlgConfirm.addEventListener('click', (event) => {
  if (event.target === el.dlgConfirm) closeConfirm(false);
});

/* -------------------------------------------------------- file browser -- */
const browser = { mode: 'files', path: null, parent: null, picked: new Set(), cursor: null };

const BROWSER_MODES = {
  files:   { title: 'Add Files',   note: 'Click a file to select. Click a folder or press the chevron to open it.',
             dirs: false, confirm: 'Add Selected', pickable: 'file' },
  folders: { title: 'Add Folders', note: 'Click a folder to select it. Double-click, click the chevron, or press Enter to open it.',
             dirs: true, confirm: 'Add Selected', pickable: 'dir' },
  load:    { title: 'Load List',   note: 'Select a .txt file containing one path per line. Ctrl+click adds more.',
             dirs: false, confirm: 'Load List', pickable: 'file' },
  save:    { title: 'Save List',   note: 'Pick a destination for the queue, or type a path below.',
             dirs: false, confirm: 'Save List', pickable: null },
};

async function openBrowser(mode, startPath) {
  browser.mode = mode;
  browser.picked.clear();
  const config = BROWSER_MODES[mode];
  el.browserTitle.textContent = config.title;
  el.browserNote.textContent = config.note;
  el.browserConfirm.textContent = config.confirm;
  el.browserPath.value = startPath || state.home;
  el.dlgBrowser.hidden = false;
  await navigate(startPath || state.home);
}

function closeBrowser() { el.dlgBrowser.hidden = true; }
el.browserCancel.addEventListener('click', closeBrowser);
el.browserClose.addEventListener('click', closeBrowser);
el.dlgBrowser.addEventListener('click', (event) => {
  if (event.target === el.dlgBrowser) closeBrowser();
});

async function navigate(path) {
  try {
    const data = await api(API.listDir + '?path=' + encodeURIComponent(path));
    browser.path = data.path;
    browser.parent = data.parent;
    browser.cursor = null;
    // A selection made in the previous folder would otherwise be confirmed
    // against paths the user can no longer see.
    browser.picked.clear();
    el.browserPath.value = data.path;
    renderCrumbs(data.path, data.parent);
    renderBrowser(data.entries);
  } catch (err) {
    renderCrumbs(path, null);
    el.browserList.replaceChildren();
    const empty = document.createElement('div');
    empty.className = 'browser-empty';
    empty.textContent = err.message;
    el.browserList.append(empty);
  }
}

function renderCrumbs(path, parent) {
  const parts = path.split(/[\\/]/).filter(Boolean);
  const crumbs = document.createElement('span');
  const crumbsLabel = document.createElement('span');

  if (parent) {
    const up = document.createElement('button');
    up.className = 'crumb';
    up.type = 'button';
    up.textContent = '\u2191 Up';
    up.addEventListener('click', () => navigate(parent));
    crumbs.append(up, sep());
  }

  const homeLabel = state.home;
  let acc = '';
  const segments = path.startsWith(homeLabel) && /^[A-Za-z]:[\\/]?$/.test(path)
    ? [homeLabel].concat(path.slice(homeLabel.length).split(/[\\/]/).filter(Boolean))
    : parts;

  segments.forEach((segment, index) => {
    acc += (index === 0 && /^[A-Za-z]:$/.test(segment)) ? segment + '\\'
         : (index === 0 && segment === homeLabel) ? segment
         : (acc.endsWith('\\') ? '' : '\\') + segment;
    const full = index === 0 && segment === homeLabel ? segment : acc;
    const isLast = index === segments.length - 1;
    const button = document.createElement('button');
    button.className = 'crumb' + (isLast ? ' current' : '');
    button.type = 'button';
    button.textContent = segment === homeLabel ? 'Home' : segment;
    if (!isLast) button.addEventListener('click', () => navigate(full));
    crumbs.append(button);
    if (!isLast) crumbs.append(sep());
  });

  crumbsLabel.remove();
  el.browserCrumbs.replaceChildren(crumbs);
}

function sep() {
  const span = document.createElement('span');
  span.className = 'crumb-sep';
  span.textContent = '\u203a';
  return span;
}

function renderBrowser(entries) {
  const config = BROWSER_MODES[browser.mode];
  if (!entries.length) {
    const empty = document.createElement('div');
    empty.className = 'browser-empty';
    empty.textContent = 'This folder is empty.';
    el.browserList.replaceChildren(empty);
    return;
  }

  const isSave = browser.mode === 'save';

  const rows = entries.map((entry) => {
    const pickable = config.pickable === 'dir' ? entry.is_dir : !entry.is_dir;
    const isDir = entry.is_dir;

    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'browser-row' + (browser.picked.has(entry.path) ? ' selected' : '');
    row.dataset.path = entry.path;
    row.dataset.isDir = String(isDir);
    /* Protected entries stay navigable and selectable. The shred step is what
       refuses them, so the browser must not hide or lock anything. */

    const lead = document.createElement('span');
    lead.className = isDir ? 'lead folder' : 'lead';
    lead.innerHTML = svg(isDir ? ICON.folder : ICON.file,
                         isDir ? 'fill="currentColor"' : '');

    const name = document.createElement('span');
    name.className = 'br-name';
    name.textContent = (entry.hidden ? '. ' : '') + entry.name;

    const size = document.createElement('span');
    size.className = 'br-size';
    size.textContent = isDir ? '' : bytes(entry.size);

    row.append(lead, name, size);

    if (entry.shreddable === false) {
      const lock = document.createElement('span');
      lock.className = 'br-lock';
      lock.title = 'Shredding is blocked here, but you can still browse and queue it';
      lock.innerHTML = svg('<rect x="5" y="11" width="14" height="9" rx="2"/>'
                          + '<path d="M8 11V8a4 4 0 0 1 8 0v3"/>');
      row.append(lock);
    }

    // Navigable folders get a dedicated "enter" target, so navigating never
    // depends on a modifier key the user has to discover.
    const canEnter = isDir && !isSave;
    if (canEnter) {
      const go = document.createElement('span');
      go.className = 'br-go';
      go.title = 'Open this folder';
      go.setAttribute('aria-hidden', 'true');
      go.innerHTML = svg(ICON.chevron);
      go.addEventListener('click', (event) => {
        event.stopPropagation();
        enter(entry.path);
      });
      row.append(go);
    }

    row.addEventListener('click', (event) => {
      browser.cursor = entry.path;
      if (!pickable) {
        // Not selectable in this mode, so a plain click means "go deeper".
        if (canEnter) enter(entry.path);
        return;
      }
      if (isSave) {
        browser.picked.clear();
        browser.picked.add(entry.path);
        renderBrowser(entries);
        return;
      }
      if (event.ctrlKey || event.metaKey) {
        browser.picked.has(entry.path) ? browser.picked.delete(entry.path)
                                       : browser.picked.add(entry.path);
      } else {
        browser.picked.clear();
        browser.picked.add(entry.path);
      }
      renderBrowser(entries);
    });

    // Double-click always opens a folder, matching Explorer, so folders stay
    // reachable even when a plain click is needed for selection.
    if (canEnter) {
      row.addEventListener('dblclick', (event) => {
        event.preventDefault();
        event.stopPropagation();
        enter(entry.path);
      });
    } else if (!pickable) {
      // Neither selectable here nor a folder worth opening: e.g. a file shown
      // in Add Folders. Navigable-but-unpickable entries stay enabled because
      // clicking them opens them.
      row.disabled = true;
    }
    return row;
  });

  el.browserList.replaceChildren(...rows);
}

function enter(path) {
  navigate(path);
}

/* Keyboard navigation for the listing. */
el.browserList.addEventListener('keydown', (event) => {
  if (event.key === 'Backspace') {
    event.preventDefault();
    if (browser.parent) navigate(browser.parent);
    return;
  }
  if (event.key !== 'Enter') return;
  const target = browser.cursor
    || (browser.picked.size === 1 ? [...browser.picked][0] : null);
  if (!target) return;
  const row = el.browserList.querySelector(
    '.browser-row[data-path="' + CSS.escape(target) + '"]');
  if (row && row.dataset.isDir === 'true') {
    event.preventDefault();
    enter(target);
  }
});

el.browserPath.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') navigate(el.browserPath.value.trim());
});

el.browserConfirm.addEventListener('click', async () => {
  const typed = el.browserPath.value.trim();
  const config = BROWSER_MODES[browser.mode];
  let chosen = [...browser.picked];

  if (browser.mode === 'save') {
    if (browser.picked.size) chosen = [...browser.picked];
    else if (typed) chosen = [typed];
    if (!chosen.length) { toast('Choose a destination path.'); return; }
  } else if (!chosen.length) {
    if (typed && typed !== browser.path) chosen = [typed];
    else { toast('Select at least one entry.'); return; }
  }

  closeBrowser();
  try {
    if (browser.mode === 'save') {
      const data = await api(API.save, { path: chosen[0] });
      toast('Saved ' + data.count + ' paths to ' + data.saved);
    } else if (browser.mode === 'load') {
      const data = await api(API.load, { path: chosen[0] });
      const summary = summariseAdd(data);
      toast('Read ' + data.read + ' paths \u2014 ' + (summary || 'nothing valid'));
    } else {
      const data = await api(API.add, { paths: chosen });
      const summary = summariseAdd(data);
      toast(summary ? 'Queue: ' + summary : 'nothing added');
    }
    await refresh();
  } catch (err) { toast(err.message); }
});

/* ------------------------------------------------------------- actions -- */
el.btnAddFiles.addEventListener('click', () => openBrowser('files'));
el.btnAddFolders.addEventListener('click', () => openBrowser('folders'));
el.btnLoad.addEventListener('click', () => openBrowser('load'));
el.btnSave.addEventListener('click', () => openBrowser('save'));

el.btnRemove.addEventListener('click', async () => {
  const indices = [...state.selection].sort((a, b) => a - b);
  if (!indices.length) return;
  try {
    const data = await api(API.remove, { indices });
    state.selection.clear();
    apply(data);
    toast('Removed ' + data.removed.length + ' item' + (data.removed.length === 1 ? '' : 's'));
  } catch (err) { toast(err.message); }
});

el.btnClear.addEventListener('click', async () => {
  const ok = await askConfirm({
    title: 'Clear the queue?',
    body: 'This only empties the queue. Nothing on disk is touched.',
    ok: 'Clear List', danger: false,
  });
  if (!ok) return;
  try {
    const data = await api(API.clear, {});
    state.selection.clear();
    apply(data);
    toast('Queue cleared.');
  } catch (err) { toast(err.message); }
});

el.btnOpen.addEventListener('click', async () => {
  const indices = [...state.selection].sort((a, b) => a - b);
  if (!indices.length) return;
  try {
    const data = await api(API.open, { indices });
    const message = [];
    const opened = list(data.opened);
    const failed = list(data.failed);
    if (opened.length) message.push(opened.length + ' opened');
    if (failed.length) message.push(failed.length + ' failed');
    toast(message.join(', ') || 'Nothing to open.');
  } catch (err) { toast(err.message); }
});

/* -------------------------------------------------------------- shred -- */
el.btnShred.addEventListener('click', async () => {
  const indices = [...state.selection].sort((a, b) => a - b);
  if (!indices.length) return;

  const chosen = state.queue.filter((e) => state.selection.has(e.index));
  const blocked = chosen.filter((e) => e.shreddable === false);
  const live = chosen.filter((e) => e.shreddable !== false);

  if (!live.length) {
    toast('Shredding is blocked for protected locations. You can still '
        + 'browse and queue them, but they will not be destroyed.');
    return;
  }

  const mode = state.modes[state.mode];
  const passCount = mode ? mode.passes : 1;
  const body = 'You are about to permanently delete ' + live.length + ' item' +
    (live.length === 1 ? '' : 's') + ' using ' + (mode ? mode.label : 'the current mode') +
    '. Every byte is overwritten ' + passes(passCount) +
    ' before the file is removed. This cannot be undone.' +
    (blocked.length ? ' ' + blocked.length + ' protected item(s) in the selection '
      + 'will be skipped.' : '');

  const ok = await askConfirm({
    title: 'Confirm Permanent Deletion',
    body,
    ok: 'Shred ' + live.length + ' Item' + (live.length === 1 ? '' : 's'),
    danger: true,
  });
  if (!ok) return;

  try {
    const data = await api(API.shred, {
      indices, confirm: true, allow_partial: true,
    });
    state.selection.clear();
    state.busy = true;
    render();
    if (data.refused && data.refused.length) {
      toast('Skipping ' + data.refused.length + ' protected item(s): '
          + data.refused[0].path);
    }
    showProgress(data.job);
  } catch (err) { toast(err.message); }
});

el.progCancel.addEventListener('click', async () => {
  try { await api(API.cancel, {}); } catch (err) { toast(err.message); }
});

function showProgress() {
  el.dlgProgress.hidden = false;
  document.body.classList.add('busy');
  poll();
}

function hideProgress() {
  el.dlgProgress.hidden = true;
  document.body.classList.remove('busy');
  clearInterval(state.pollTimer);
  state.pollTimer = null;
}

function paintProgress(job) {
  el.progItem.textContent = job.item_name || 'Working...';
  el.progOverallVal.textContent = job.files_done + ' / ' + job.total_files;
  el.progOverallBar.style.width = job.overall_pct + '%';
  el.progItemVal.textContent = Math.round(job.item_pct) + '% \u00b7 ' + passes(job.passes);
  el.progItemBar.style.width = job.item_pct + '%';
  el.progPct.replaceChildren(
    document.createTextNode(String(Math.round(job.overall_pct))),
    Object.assign(document.createElement('span'),
                  { className: 'pct-sign', textContent: '%' })
  );
}

async function poll() {
  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    try {
      const data = await api(API.state);
      apply(data, { skipPoll: true });
      if (data.job) {
        paintProgress(data.job);
        if (!data.job.running) {
          clearInterval(state.pollTimer);
          state.pollTimer = null;
          setTimeout(finishJob, 400);
        }
      }
    } catch (err) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      hideProgress();
      toast('Lost contact with the server: ' + err.message);
    }
  }, 400);
  try {
    const data = await api(API.state);
    apply(data, { skipPoll: true });
    if (data.job && !data.job.running) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      hideProgress();
      finishJob();
    }
  } catch (err) { toast(err.message); }
}

async function finishJob() {
  const job = state.job;
  hideProgress();
  if (!job) return;

  const succeeded = list(job.succeeded);
  const failed = list(job.failed);
  const trimmed = list(job.trimmed);
  const trimFailed = list(job.trim_failed);

  if (job.cancelled) {
    toast('Shredding stopped. ' + succeeded.length + ' item(s) already gone.');
  } else if (failed.length) {
    toast('Finished with ' + failed.length + ' failure(s); '
          + succeeded.length + ' destroyed. See the browser console for detail.');
  } else {
    let message = 'Destroyed ' + succeeded.length + ' item(s) in ' + job.elapsed + 's.';
    if (trimmed.length) message += ' TRIM: ' + trimmed.join(', ') + '.';
    else if (trimFailed.length) message += ' TRIM failed on ' + trimFailed.join(', ') + '.';
    toast(message);
  }

  if (failed.length) {
    console.warn('Shred failures:', failed);
  }
  await refresh();
}

/* ---------------------------------------------------------- drag drop -- */
let dragDepth = 0;
window.addEventListener('dragenter', (event) => {
  if (!event.dataTransfer || !Array.from(event.dataTransfer.types).includes('Files')) return;
  event.preventDefault();
  dragDepth += 1;
  el.dropTarget.classList.add('show');
  el.dropVeil.classList.add('show');
});
window.addEventListener('dragover', (event) => {
  if (!event.dataTransfer || !Array.from(event.dataTransfer.types).includes('Files')) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = 'copy';
});
window.addEventListener('dragleave', () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) {
    el.dropTarget.classList.remove('show');
    el.dropVeil.classList.remove('show');
  }
});
window.addEventListener('drop', (event) => {
  event.preventDefault();
  dragDepth = 0;
  el.dropTarget.classList.remove('show');
  el.dropVeil.classList.remove('show');

  const files = Array.from(event.dataTransfer.files || []).map((f) => f.path || f.name);
  if (files.length) addPaths(files);
});

/* A browser cannot reveal real paths for security, so /api/add falls back to
   names. Surface that honestly instead of silently failing. */
async function addPaths(paths) {
  try {
    const data = await api(API.add, { paths });
    const added = list(data.added);
    const skipped = list(data.skipped);
    if (!added.length && skipped.length === paths.length) {
      toast('Dropped names could not be resolved to disk paths. '
          + 'Browsers withhold real paths for security \u2014 use Add Files instead.');
    } else {
      const summary = summariseAdd(data);
      toast(summary ? 'Queue: ' + summary : 'nothing added');
    }
    await refresh();
  } catch (err) { toast(err.message); }
}

/* ------------------------------------------------------------- refresh -- */
function apply(data, options) {
  state.queue = data.queue || [];
  state.count = data.count || 0;
  state.totalBytes = data.total_bytes || 0;
  state.mode = data.mode || state.mode;
  state.trim = data.trim !== undefined ? data.trim : state.trim;
  state.home = data.home || state.home;
  if (data.job) state.job = data.job;
  state.busy = Boolean(data.busy);

  const valid = new Set(state.queue.map((e) => e.index));
  state.selection.forEach((index) => { if (!valid.has(index)) state.selection.delete(index); });

  el.trimToggle.setAttribute('aria-checked', state.trim ? 'true' : 'false');
  if (data.mode_label) el.modeValue.textContent = data.mode_label;
  const mode = state.modes[state.mode];
  if (mode) el.modeHint.textContent = mode.hint;

  render();
  if (state.busy && !(options && options.skipPoll) && !state.pollTimer) showProgress();
}

async function refresh() {
  try {
    const data = await api(API.state);
    apply(data);
    setConnected(true, 'Connected');
  } catch (err) {
    setConnected(false, 'Offline');
    setStatus('Server unreachable', err.message, 'bad');
  }
}

/* ---------------------------------------------------------------- boot -- */
async function boot() {
  try {
    await loadModes();
  } catch (err) {
    // Leave the dropdown usable: it renders a reload action instead of an
    // empty list, and the shred path falls back to a safe single pass.
    el.modeValue.textContent = 'Unavailable';
    el.modeHint.textContent = 'Could not load shredding modes';
    setConnected(false, 'Degraded');
  }

  try {
    const ssds = await fetch('/api/ssds', { headers: { 'X-Shredder-Token': TOKEN } })
      .then((r) => r.json());
    el.winSsd.textContent = Object.keys(ssds.ssds || {}).join(' ') || 'no SSD';
  } catch (_err) { el.winSsd.textContent = 'ssd ?'; }

  await refresh();

  setInterval(() => {
    if (!state.busy) refresh();
  }, 5000);

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      if (!el.dlgConfirm.hidden) closeConfirm(false);
      else if (!el.dlgBrowser.hidden) closeBrowser();
      else closeMenu();
    }
  });
}

boot();
