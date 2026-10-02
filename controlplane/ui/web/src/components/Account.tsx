import { useEffect, useRef, useState } from 'react';
import { api, ApiError, SIGNED_OUT_EVENT } from '../api/client';
import { useMe } from '../lib/me';

/** `null` while signed in; otherwise where to sign in (undefined: this deployment has no browser sign-in). */
export function useSignedOut() {
  const me = useMe();
  const [event, setEvent] = useState<ApiError | null>(null);
  useEffect(() => {
    const on = (e: Event) => setEvent((e as CustomEvent<ApiError | undefined>).detail ?? new ApiError(401, 'unauthenticated', 'signed out'));
    window.addEventListener(SIGNED_OUT_EVENT, on);
    return () => window.removeEventListener(SIGNED_OUT_EVENT, on);
  }, []);
  const error = me.error instanceof ApiError && me.error.status === 401 ? me.error : event;
  return error ? { signInUrl: error.signInUrl } : null;
}

export function SignIn({ signInUrl }: { signInUrl?: string }) {
  const next = `/ui/${location.hash}`;
  return (
    <div className="sign-in" data-testid="sign-in">
      <h1>Sign in to ML Platform</h1>
      {signInUrl ? (
        <>
          <p className="muted">Use your organisation account. You will come back to the page you asked for.</p>
          <a className="btn primary big" href={`${signInUrl}?next=${encodeURIComponent(next)}`} data-testid="sign-in-button">Sign in</a>
        </>
      ) : (
        <p className="muted">This platform accepts API tokens only: there is no browser sign-in configured. Ask your platform team.</p>
      )}
    </div>
  );
}

/** The signed-in person, their groups, and signing out. */
export function AccountMenu() {
  const me = useMe();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === 'Escape' : !ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', close);
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', close); };
  }, [open]);
  if (!me.data) return null;
  const m = me.data;
  if (m.auth === 'none') {
    return <span className="chip local-mode" title="Running without sign-in: everyone is an anonymous platform admin" data-testid="local-mode">local mode</span>;
  }
  const name = m.display_name || m.username;
  const initials = name.split(/[\s._@-]+/).filter(Boolean).slice(0, 2).map((p) => p[0]!.toUpperCase()).join('');

  async function signOut() {
    try {
      const { logout_url: url } = await api.post<{ logout_url: string | null }>('/auth/logout', {});
      location.href = url ?? '/ui/';
    } catch { location.href = '/ui/'; }
  }

  return (
    <div className="account" ref={ref}>
      <button className="avatar" type="button" aria-haspopup="menu" aria-expanded={open} data-testid="account-button"
        title={name} onClick={() => setOpen(!open)}>{initials || '?'}</button>
      {open && (
        <div className="account-menu" role="menu" data-testid="account-menu">
          <div className="who"><b>{name}</b><span className="muted small">{m.email ?? m.username}</span></div>
          {m.platform_admin && <span className="chip">platform admin</span>}
          {m.groups.length > 0 && <div className="small muted">Groups: {m.groups.join(', ')}</div>}
          {m.can_sign_out && <button className="btn small" type="button" role="menuitem" data-testid="sign-out" onClick={signOut}>Sign out</button>}
        </div>)}
    </div>
  );
}
