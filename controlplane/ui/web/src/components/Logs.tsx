import { useEffect, useMemo, useRef, useState } from 'react';
import { CopyButton } from './bits';

const ERROR_LINE = /\b(error|exception|traceback|fatal|failed|killed)\b/i;

/**
 * Log viewer with the controls people reach for: search, wrap, follow the tail while a run is
 * live, copy, download. Lines are numbered and the ones that look like errors stand out.
 */
export function Logs({ title, text, live, filename }: { title: string; text: string; live: boolean; filename: string }) {
  const [wrap, setWrap] = useState(true);
  const [follow, setFollow] = useState(true);
  const [needle, setNeedle] = useState('');
  const [errorsOnly, setErrorsOnly] = useState(false);
  const pre = useRef<HTMLPreElement>(null);

  const lines = useMemo(() => (text ? text.replace(/\n$/, '').split('\n') : []), [text]);
  const shown = useMemo(() => {
    const n = needle.toLowerCase();
    return lines.map((line, i) => ({ line, n: i + 1, error: ERROR_LINE.test(line) }))
      .filter((l) => (!n || l.line.toLowerCase().includes(n)) && (!errorsOnly || l.error));
  }, [lines, needle, errorsOnly]);
  const errors = useMemo(() => lines.filter((l) => ERROR_LINE.test(l)).length, [lines]);

  useEffect(() => {
    if (live && follow && !needle && pre.current) pre.current.scrollTop = pre.current.scrollHeight;
  }, [text, live, follow, needle]);

  const download = () => {
    const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.append(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };
  const filtered = Boolean(needle) || errorsOnly;

  return (
    <div className="section card">
      <div className="log-tools">
        <h2 style={{ margin: 0, flex: 1 }}>{title}</h2>
        <input type="search" className="log-search" placeholder="Find in logs" aria-label="Find in logs" data-testid="log-search"
          value={needle} onChange={(e) => setNeedle(e.target.value)} />
        <span className="muted small" data-testid="log-count">
          {filtered ? `${shown.length} of ${lines.length} lines` : lines.length ? `${lines.length} lines` : ''}
        </span>
        {errors > 0 && (
          <label title="Lines mentioning error, exception, traceback, fatal, failed or killed">
            <input type="checkbox" checked={errorsOnly} data-testid="log-errors" onChange={(e) => setErrorsOnly(e.target.checked)} />
            {`Errors (${errors})`}
          </label>)}
        {live && (
          <label><input type="checkbox" checked={follow} data-testid="log-follow" onChange={(e) => setFollow(e.target.checked)} />Follow</label>)}
        <label><input type="checkbox" checked={wrap} data-testid="log-wrap" onChange={(e) => setWrap(e.target.checked)} />Wrap</label>
        <CopyButton text={text} what="logs" />
        <button className="btn small" type="button" onClick={download}>Download</button>
      </div>
      <pre ref={pre} className={`logs numbered${wrap ? '' : ' nowrap'}`} data-testid="logs" tabIndex={0} aria-label={`${title} output`}>
        {lines.length === 0 ? '(no output yet)'
          : shown.length === 0 ? '(no line matches)'
          : shown.map((l) => (
            <span key={l.n} className={`ln${l.error ? ' err' : ''}`}><span className="n" aria-hidden="true">{l.n}</span>{l.line}{'\n'}</span>))}
      </pre>
    </div>
  );
}
