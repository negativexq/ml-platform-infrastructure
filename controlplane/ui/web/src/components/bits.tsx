import { useState, type ReactNode } from 'react';
import { fmtAgo } from '../lib/format';

const SYMBOLS: Record<string, string> = {
  READY: '✓', SUCCEEDED: '✓', CHAMPION: '★', PASSED: '✓', APPLIED: '✓',
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
  return (
    <time dateTime={iso ?? ''} title={iso ? new Date(iso).toLocaleString() : ''}>
      {fmtAgo(iso)}
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

export const Empty = ({ children }: { children: ReactNode }) => <div className="empty">{children}</div>;

export function Section({ title, testid, children }: { title: string; testid?: string; children: ReactNode }) {
  return (
    <div className="section" data-testid={testid}>
      <h2>{title}</h2>
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
