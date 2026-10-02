import { h, copyButton } from './dom.js';

/**
 * Log viewer with the controls people reach for: wrap, follow the tail while a run is live,
 * copy, download. `prefs` is owned by the page so choices survive its live repaints.
 */
export function logsPanel(prefs, { title, text, live, filename }) {
  const pre = h('pre', { class: `logs${prefs.wrap ? '' : ' nowrap'}`, 'data-testid': 'logs', tabindex: '0', 'aria-label': `${title} output` },
    text || '(no output yet)');
  const wrap = h('input', { type: 'checkbox', checked: prefs.wrap, 'data-testid': 'log-wrap',
    onchange: (e) => { prefs.wrap = e.target.checked; pre.classList.toggle('nowrap', !prefs.wrap); } });
  const follow = h('input', { type: 'checkbox', checked: prefs.follow, 'data-testid': 'log-follow', onchange: (e) => { prefs.follow = e.target.checked; } });
  const download = h('button', { class: 'btn small', type: 'button', onclick: () => {
    const url = URL.createObjectURL(new Blob([text || ''], { type: 'text/plain' }));
    const a = h('a', { href: url, download: filename });
    document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(url);
  } }, 'Download');
  const lines = text ? text.split('\n').length : 0;
  const panel = h('div', { class: 'section card' },
    h('div', { class: 'log-tools' }, h('h2', { style: { margin: '0', flex: '1' } }, title),
      h('span', { class: 'muted small' }, lines ? `${lines} lines` : ''),
      live ? h('label', {}, follow, 'Follow') : null,
      h('label', {}, wrap, 'Wrap'), copyButton(text || '', 'logs'), download),
    pre);
  // Follow the tail while the run is live, unless the reader scrolled up on purpose.
  queueMicrotask(() => { if (live && prefs.follow) pre.scrollTop = pre.scrollHeight; });
  return panel;
}

export const newLogPrefs = () => ({ wrap: true, follow: true });
