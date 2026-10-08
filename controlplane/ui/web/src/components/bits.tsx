import { useState, type ReactNode } from 'react';
import { fmtAgo } from '../lib/format';
import { useNow } from '../lib/now';

const SYMBOLS: Record<string, string> = {
  STABLE: '✓', INSUFFICIENT_DATA: '?', READY: '✓', SUCCEEDED: '✓', CHAMPION: '★', PASSED: '✓', APPLIED: '✓',
  RUNNING: '●', PROGRESSING: '●', SUBMITTED: '●', DEPLOYING: '●', EVALUATING: '●', PROVISIONING: '●',
  PENDING: '○', REGISTERED: '○', CANDIDATE: '◐',
  DEGRADED: '!', DRIFTED: '!', UNAVAILABLE: '!', CANCELLED: '–', DELETING: '!',
  FAILED: '✕', REJECTED: '✕', ROLLED_BACK: '↺', SKIPPED: '–', ARCHIVED: '–', DELETED: '–',
};

/** A status pill. Colour is never the only signal: there is always a symbol and the word. */
export function Badge({ status }: { status: string }) {
  return (
    <span className={`badge st-${status}`} data-status={status}>
      <span className="sym" aria-hidden="true">{SYMBOLS[status] ?? '•'}</span>
      {status.replace('_', ' ').toLowerCase()}
    </span>
  );
}

export function Time({ iso }: { iso: string | null | undefined }) {
  const now = useNow();
  return (
    <time dateTime={iso ?? ''} title={iso ? new Date(iso).toLocaleString() : ''}>
      {fmtAgo(iso, Math.max(now, Date.now()))}
    </time>
  );
}

/** A small "copy" control for ids and URLs. Falls back silently where the clipboard is blocked. */
export function CopyButton({ text, what = 'value' }: { text: string; what?: string }) {
  const [mark, setMark] = useState('⧉');
  return (
    <button
      className="copy"
      type="button"
      title={`Copy ${what}`}
      aria-label={`Copy ${what}`}
      onClick={async (event) => {
        event.stopPropagation();
        try {
          await navigator.clipboard.writeText(text);
          setMark('✓');
        } catch {
          setMark('!');
        }
        setTimeout(() => setMark('⧉'), 1200);
      }}
    >
      {mark}
    </button>
  );
}

export const Empty = ({ children, actions }: { children: ReactNode; actions?: ReactNode }) => <div className="empty"><div>{children}</div>{actions && <div className="empty-actions">{actions}</div>}</div>;

export function Section({ title, testid, children, more }: { title: string; testid?: string; children: ReactNode; more?: [string, string] }) {
  return (
    <div className="section" data-testid={testid}>
      <div className="section-head"><h2>{title}</h2>{more && <a className="small" href={more[0]}>{more[1]}</a>}</div>
      {children}
    </div>
  );
}

export function Alert({ bad, children }: { bad?: boolean; children: ReactNode }) {
  return <div className={`alert${bad ? ' bad' : ''}`}>{children}</div>;
}

export function Skeleton() {
  return (
    <div className="skeleton" aria-busy="true" aria-label="Loading">
      <div className="skel title" />
      <div className="skel line" />
      <div className="grid">
        <div className="skel card" />
        <div className="skel card" />
        <div className="skel card" />
      </div>
    </div>
  );
}

export function Table({ head, children, testid }: { head: string[]; children: ReactNode; testid?: string }) {
  return (
    <table className="t" data-testid={testid}>
      <thead>
        <tr>{head.map((c, i) => <th key={i}>{c}</th>)}</tr>
      </thead>
      <tbody>{children}</tbody>
    </table>
  );
}

/** Previous / next page controls for offset-paginated lists. */
export function Pager({ offset, limit, count, onChange }: { offset: number; limit: number; count: number; onChange: (offset: number) => void }) {
  if (offset === 0 && count < limit) return null;
  return (
    <div className="pager" role="navigation" aria-label="Pages">
      <button className="btn small" type="button" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>← Newer</button>
      <span className="muted small">{count ? `${offset + 1}–${offset + count}` : 'nothing here'}</span>
      <button className="btn small" type="button" data-testid="older" disabled={count < limit} onClick={() => onChange(offset + limit)}>Older →</button>
    </div>
  );
}

/** A labelled value in a definition grid. */
export function Kv({ entries }: { entries: [string, ReactNode][] }) {
  if (!entries.length) return <p className="muted">—</p>;
  return (
    <dl className="kv">
      {entries.map(([k, v]) => <span key={k} style={{ display: 'contents' }}><dt>{k}</dt><dd>{v}</dd></span>)}
    </dl>
  );
}

/** A copyable command, e.g. the API call behind a button, so teams can script what they click. */
export function Snippet({ label, code }: { label: string; code: string }) {
  return (
    <details className="snippet">
      <summary>{label}</summary>
      <div className="snippet-body"><pre className="mono">{code}</pre><CopyButton text={code} what="command" /></div>
    </details>
  );
}
