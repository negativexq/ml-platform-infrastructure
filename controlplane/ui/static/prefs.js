// The only place the UI stores anything in the browser: the colour-theme preference.
// Storage can be blocked (private windows), so every access is guarded and the page works without it.

const KEY = 'mlp.theme';
const ORDER = ['system', 'light', 'dark'];

export function getTheme() {
  try { return ORDER.includes(localStorage.getItem(KEY)) ? localStorage.getItem(KEY) : 'system'; } catch { return 'system'; }
}

export function applyTheme(theme = getTheme()) {
  const root = document.documentElement;
  if (theme === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', theme);
  return theme;
}

export function cycleTheme() {
  const next = ORDER[(ORDER.indexOf(getTheme()) + 1) % ORDER.length];
  try { localStorage.setItem(KEY, next); } catch { /* not persisted; still applied for this visit */ }
  return applyTheme(next);
}

export const THEME_LABEL = { system: 'Theme: system', light: 'Theme: light', dark: 'Theme: dark' };
export const THEME_ICON = { system: '◐', light: '☀', dark: '☾' };
