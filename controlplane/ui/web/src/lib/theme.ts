// The only thing the UI stores in the browser: the colour-theme preference. Storage can be
// blocked (private windows), so every access is guarded and the page works without it.
export type Theme = 'system' | 'light' | 'dark';
const KEY = 'mlp.theme';
const ORDER: Theme[] = ['system', 'light', 'dark'];

export function getTheme(): Theme {
  try {
    const v = localStorage.getItem(KEY);
    return ORDER.includes(v as Theme) ? (v as Theme) : 'system';
  } catch {
    return 'system';
  }
}

export function applyTheme(theme: Theme = getTheme()): Theme {
  const root = document.documentElement;
  if (theme === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', theme);
  return theme;
}

export function cycleTheme(): Theme {
  const next = ORDER[(ORDER.indexOf(getTheme()) + 1) % ORDER.length] as Theme;
  try {
    localStorage.setItem(KEY, next);
  } catch {
    /* not persisted; still applied for this visit */
  }
  return applyTheme(next);
}

export const THEME_LABEL: Record<Theme, string> = {
  system: 'Theme: system',
  light: 'Theme: light',
  dark: 'Theme: dark',
};
export const THEME_ICON: Record<Theme, string> = { system: '◐', light: '☀', dark: '☾' };
