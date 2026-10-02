import { useEffect, useRef, useState } from 'react';
import { CopyButton } from './bits';

/**
 * Log viewer with the controls people reach for: wrap, follow the tail while a run is live,
 * copy, download. Choices live in state, so they survive the page's live refreshes.
 */
export function Logs({ title, text, live, filename }: { title: string; text: string; live: boolean; filename: string }) {
  const [wrap, setWrap] = useState(true);
  const [follow, setFollow] = useState(true);
  const pre = useRef<HTMLPreElement>(null);
  useEffect(() => {
    if (live && follow && pre.current) pre.current.scrollTop = pre.current.scrollHeight;
  }, [text, live, follow]);
  const lines = text ? text.split('\n').length : 0;
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
  return (
    <div className="section card">
      <div className="log-tools">
        <h2 style={{ margin: 0, flex: 1 }}>{title}</h2>
        <span className="muted small">{lines ? `${lines} lines` : ''}</span>
        {live && (
          <label>
            <input type="checkbox" checked={follow} data-testid="log-follow" onChange={(e) => setFollow(e.target.checked)} />Follow
          </label>
        )}
        <label>
          <input type="checkbox" checked={wrap} data-testid="log-wrap" onChange={(e) => setWrap(e.target.checked)} />Wrap
        </label>
        <CopyButton text={text} what="logs" />
        <button className="btn small" type="button" onClick={download}>Download</button>
      </div>
      <pre ref={pre} className={`logs${wrap ? '' : ' nowrap'}`} data-testid="logs" tabIndex={0} aria-label={`${title} output`}>
        {text || '(no output yet)'}
      </pre>
    </div>
  );
}
