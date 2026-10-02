import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { applyTheme, cycleTheme, getTheme, THEME_ICON, THEME_LABEL } from '../lib/theme';
import { useChrome } from '../lib/chrome';
import { go, routes } from '../lib/format';
import { Palette } from './Palette';
import { AccountMenu, SignIn, useSignedOut } from './Account';
import { Modal } from './overlays';

const SHORTCUTS: [string, string][] = [
  ['Ctrl/⌘ K  or  /', 'Search everything'],
  ['g then p', 'Go to projects'],
  ['?', 'Show this help'],
  ['Esc', 'Close a dialog'],
];

export function Layout({ children }: { children: ReactNode }) {
  const { crumbs, live } = useChrome();
  const signedOut = useSignedOut();
  const [theme, setTheme] = useState(() => applyTheme());
  const [palette, setPalette] = useState(false);
  const [help, setHelp] = useState(false);
  const main = useRef<HTMLElement>(null);
  const pendingG = useRef(0);

  useEffect(() => { setTheme(getTheme()); }, []);

  const anyDialogOpen = () => document.querySelector('dialog[open]') !== null;
  const search = useCallback(() => { if (!anyDialogOpen()) setPalette(true); }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const t = event.target as HTMLElement;
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); search(); return; }
      if (typing || event.ctrlKey || event.metaKey || event.altKey || anyDialogOpen()) return;
      if (event.key === '/') {
        event.preventDefault();
        const own = document.querySelector<HTMLElement>('[data-search]');
        if (own) own.focus(); else search();
      } else if (event.key === '?') setHelp(true);
      else if (event.key === 'g') pendingG.current = Date.now();
      else if (event.key === 'p' && Date.now() - pendingG.current < 1200) { go(routes.projects()); pendingG.current = 0; }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [search]);

  return (
    <>
      <a className="skip" href="#view" onClick={(e) => { e.preventDefault(); main.current?.focus(); }}>Skip to content</a>
      <header className="topbar">
        <a className="brand" href={routes.projects()}>ML Platform</a>
        <nav id="crumbs" aria-label="Breadcrumb">
          {crumbs.map((c, i) => (
            <span key={i} className="crumb">
              <span className="sep" />
              {c.href ? <a href={c.href}>{c.label}</a> : <span aria-current="page">{c.label}</span>}
            </span>
          ))}
        </nav>
        <span id="live" className={`live${live > 0 ? ' on' : ''}`} title="Auto-refreshing" role="status" aria-label="Auto-refreshing">●</span>
        <button id="search-btn" className="btn small ghost" type="button" title="Search (Ctrl+K)" data-testid="search-btn" onClick={search}>
          <span aria-hidden="true">⌕</span> Search <kbd>Ctrl K</kbd>
        </button>
        <button id="theme-btn" className="btn small ghost" type="button" data-testid="theme-btn"
          title={`${THEME_LABEL[theme]} (click to change)`} aria-label={THEME_LABEL[theme]} onClick={() => setTheme(cycleTheme())}>
          <span aria-hidden="true">{THEME_ICON[theme]}</span>
        </button>
        <button id="help-btn" className="btn small ghost" type="button" title="Keyboard shortcuts (?)" aria-label="Keyboard shortcuts"
          onClick={() => { if (!anyDialogOpen()) setHelp(true); }}>?</button>
        <AccountMenu />
      </header>
      <main id="view" ref={main} tabIndex={-1}>{signedOut ? <SignIn signInUrl={signedOut.signInUrl} /> : children}</main>
      <Modal open={help} onClose={() => setHelp(false)}>
        <h2>Keyboard shortcuts</h2>
        <dl className="kv">
          {SHORTCUTS.map(([k, d]) => (<span key={k} style={{ display: 'contents' }}><dt><kbd>{k}</kbd></dt><dd>{d}</dd></span>))}
        </dl>
        <div className="dlg-actions">
          <button className="btn primary" type="button" onClick={(e) => (e.currentTarget.closest('dialog') as HTMLDialogElement).close()}>Close</button>
        </div>
      </Modal>
      <Modal open={palette} className="palette" onClose={() => setPalette(false)}>
        <Palette onDone={(e) => (e as HTMLElement).closest('dialog')?.close('ok')} />
      </Modal>
    </>
  );
}
