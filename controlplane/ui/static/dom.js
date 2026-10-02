// DOM helpers. Everything is built with createElement/textContent, never innerHTML,
// so data from the API cannot inject markup.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'style') {
      // The CSP forbids inline style *attributes* (a string would be silently ignored), but
      // allows setting properties through the CSSOM, so styles are passed as objects.
      if (typeof value !== 'object') throw new Error('style must be an object, e.g. { width: "75%" }');
      Object.assign(el.style, value);
    } else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? '' : String(value));
  }
  append(el, children);
  return el;
}

export function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, String(value));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

const SYMBOLS = {
  READY: '✓', SUCCEEDED: '✓', CHAMPION: '★', PASSED: '✓', APPLIED: '✓',
  RUNNING: '●', PROGRESSING: '●', SUBMITTED: '●', DEPLOYING: '●', EVALUATING: '●', PROVISIONING: '●',
  PENDING: '○', REGISTERED: '○', CANDIDATE: '◐',
  DEGRADED: '!', DRIFTED: '!', UNAVAILABLE: '!', CANCELLED: '–', DELETING: '!',
  FAILED: '✕', REJECTED: '✕', ROLLED_BACK: '↺', SKIPPED: '–', ARCHIVED: '–', DELETED: '–',
};

/** A status pill. Colour is never the only signal: there is always a symbol and the word. */
export function badge(status) {
  const label = String(status).replace('_', ' ').toLowerCase();
  return h('span', { class: `badge st-${status}`, 'data-status': status },
    h('span', { class: 'sym', 'aria-hidden': 'true' }, SYMBOLS[status] || '•'), label);
}

export function fmtDuration(seconds) {
  if (seconds == null) return '—';
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
}

export function fmtAgo(iso) {
  if (!iso) return '—';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function timeEl(iso) {
  return h('time', { datetime: iso || '', title: iso ? new Date(iso).toLocaleString() : '' }, fmtAgo(iso));
}

export const pct = (x, digits = 2) => (x == null ? '—' : `${(x * 100).toFixed(digits)}%`);
export const num = (x, digits = 1) => (x == null ? '—' : Number(x).toFixed(digits));
export const shortId = (id) => String(id).slice(0, 8);

/** A small "copy" control for ids and URLs. Falls back silently where the clipboard is blocked. */
export function copyButton(text, what = 'value') {
  const btn = h('button', { class: 'copy', type: 'button', title: `Copy ${what}`, 'aria-label': `Copy ${what}` }, '⧉');
  btn.addEventListener('click', async (event) => {
    event.stopPropagation();
    try { await navigator.clipboard.writeText(text); btn.textContent = '✓'; } catch { btn.textContent = '!'; }
    setTimeout(() => { btn.textContent = '⧉'; }, 1200);
  });
  return btn;
}
