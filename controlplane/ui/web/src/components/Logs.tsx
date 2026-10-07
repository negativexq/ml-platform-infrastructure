import { useEffect, useMemo, useRef, useState } from 'react';
import { CopyButton } from './bits';
import { fieldText, logTime, parseLogs, type LogLevel } from '../lib/log-format';

/**
 * Log viewer with the controls people reach for: search, wrap, follow the tail while a run is
 * live, copy, download. Lines are numbered and the ones that look like errors stand out.
 */
export function Logs({ title, text, live, filename }: { title: string; text: string; live: boolean; filename: string }) {
  const [wrap, setWrap] = useState(true);
  const [follow, setFollow] = useState(true);
  const [needle, setNeedle] = useState('');
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [severity, setSeverity] = useState<LogLevel | ''>('');
  const pre = useRef<HTMLDivElement>(null);

  const lines = useMemo(() => (text ? parseLogs(text) : []), [text]);
  const shown = useMemo(() => {
    const n = needle.toLowerCase();
    return lines.filter((l) => (!n || l.raw.toLowerCase().includes(n)) && (!errorsOnly || l.error) && (!severity || l.level === severity));
  }, [lines, needle, errorsOnly, severity]);
  const errors = useMemo(() => lines.filter((l) => l.error).length, [lines]);

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
  const filtered = Boolean(needle) || errorsOnly || Boolean(severity);

  return (
    <div className="section card">
      <div className="log-tools">
        <h2 style={{ margin: 0, flex: 1 }}>{title}</h2>
        {live && <span className="log-live"><span aria-hidden="true" />Live</span>}
        <input type="search" className="log-search" placeholder="Find in logs" aria-label="Find in logs" data-testid="log-search"
          value={needle} onChange={(e) => setNeedle(e.target.value)} />
        <select aria-label="Log level" data-testid="log-level" value={severity} onChange={(e) => setSeverity(e.target.value as LogLevel | '')}>
          <option value="">All levels</option>
          {(['DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL', 'LOG'] as LogLevel[]).map((level) => <option key={level} value={level}>{level === 'LOG' ? 'Plain output' : level}</option>)}
        </select>
        <span className="muted small" data-testid="log-count">
          {filtered ? `${shown.length} of ${lines.length} events` : lines.length ? `${lines.length} event${lines.length === 1 ? '' : 's'}` : ''}
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
      <div ref={pre} className={`logs log-events${wrap ? '' : ' nowrap'}`} data-testid="logs" tabIndex={0} role="table" aria-label={`${title} output`}>
        {lines.length === 0 ? <span className="log-empty">(no output yet)</span>
          : shown.length === 0 ? <span className="log-empty">(no line matches)</span>
          : <>
            <div className="log-columns" role="row">
              <span role="columnheader">#</span><span role="columnheader" title="Browser local time; hover an event for its full timestamp">Time</span>
              <span role="columnheader">Level</span><span role="columnheader">Source</span><span role="columnheader">Message</span>
            </div>
            {shown.map((l) => (
              <div key={l.n} className={`ln log-line level-${l.level.toLowerCase()}${l.error ? ' err' : ''}`} role="row">
                <span className="log-number" role="cell">{l.n}</span>
                <time className="log-time" dateTime={l.timestamp ?? undefined} title={l.timestamp ?? 'Timestamp not provided'} role="cell">{logTime(l.timestamp)}</time>
                <span role="cell"><span className={`log-severity severity-${l.level.toLowerCase()}`}>{l.level}</span></span>
                <span className="log-source" title={l.source || 'Source not provided'} role="cell">{l.source || '—'}</span>
                <div className="log-body" role="cell">
                  <span className="log-message">{l.message}</span>
                  {l.repeat > 1 && <span className="log-repeat" title="Consecutive repetitions">{`×${l.repeat}`}</span>}
                  {l.detail !== l.message && <details className="log-fields log-detail" open={Boolean(needle && !l.message.toLowerCase().includes(needle.toLowerCase()))}>
                    <summary>Details</summary><pre>{l.detail}</pre>
                  </details>}
                  {Object.keys(l.fields).length > 0 && <details className="log-fields">
                    <summary>{`${Object.keys(l.fields).length} field${Object.keys(l.fields).length === 1 ? '' : 's'}`}</summary>
                    <dl>{Object.entries(l.fields).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{fieldText(value)}</dd></div>)}</dl>
                  </details>}
                </div>
              </div>))}
          </>}
      </div>
    </div>
  );
}
