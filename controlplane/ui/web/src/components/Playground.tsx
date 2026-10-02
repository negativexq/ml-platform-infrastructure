import { useState } from 'react';
import { api, enc, type S } from '../api/client';
import { num } from '../lib/format';
import { Snippet } from './bits';

type Turn = { role: 'user' | 'assistant'; content: string; usage?: { prompt_tokens: number; completion_tokens: number }; ms?: number };
type Completion = {
  choices: { message: { role: string; content: string } }[];
  usage?: { prompt_tokens: number; completion_tokens: number; total_tokens: number };
};

/**
 * A conversation with an LLM endpoint through the platform: the quickest "does it answer, and
 * how well?". Each reply shows the tokens it cost, because that is what quotas count.
 */
export function Playground({ project, endpoint, allowed = true }: { project: string; endpoint: S['EndpointOut']; allowed?: boolean }) {
  const [system, setSystem] = useState('You are a concise, friendly support assistant.');
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState('');
  const [maxTokens, setMaxTokens] = useState('256');
  const [temperature, setTemperature] = useState('0.7');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ready = endpoint.status === 'READY';
  const path = `/projects/${enc(project)}/endpoints/${enc(endpoint.name)}/chat`;

  async function send() {
    const text = draft.trim();
    if (!text) return;
    const history: Turn[] = [...turns, { role: 'user', content: text }];
    setTurns(history);
    setDraft('');
    setBusy(true);
    setError(null);
    const started = performance.now();
    try {
      const messages = [
        ...(system.trim() ? [{ role: 'system', content: system.trim() }] : []),
        ...history.map((t) => ({ role: t.role, content: t.content })),
      ];
      const reply = await api.post<Completion>(path, {
        messages,
        max_tokens: Number(maxTokens) || undefined,
        temperature: temperature === '' ? undefined : Number(temperature),
      });
      const content = reply.choices[0]?.message.content ?? '';
      setTurns([...history, { role: 'assistant', content, usage: reply.usage, ms: performance.now() - started }]);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'The request failed');
    } finally { setBusy(false); }
  }

  const spent = turns.reduce((s, t) => s + (t.usage ? t.usage.prompt_tokens + t.usage.completion_tokens : 0), 0);
  const why = !allowed ? 'Needs the invoker role in this project' : ready ? undefined : `The endpoint is ${endpoint.status.toLowerCase()}`;
  return (
    <div className="section card" data-testid="playground">
      <div className="section-head">
        <h2>Playground</h2>
        <span className="muted small" data-testid="chat-spent">{spent ? `${num(spent, 0)} tokens in this conversation` : ''}</span>
      </div>
      <p className="muted small">Talks to <span className="mono">{endpoint.name}</span> through the platform. Counts as real traffic during a canary. Outside callers use the gateway, which also streams.</p>
      <div className="chat-settings">
        <label className="field-label" htmlFor="chat-system">System prompt</label>
        <textarea id="chat-system" rows={2} value={system} onChange={(e) => setSystem(e.target.value)} data-testid="chat-system" />
        <div className="chat-knobs">
          <label className="inline">Max tokens <input type="number" min={1} max={32768} value={maxTokens} onChange={(e) => setMaxTokens(e.target.value)} data-testid="chat-max-tokens" /></label>
          <label className="inline">Temperature <input type="number" min={0} max={2} step={0.1} value={temperature} onChange={(e) => setTemperature(e.target.value)} /></label>
          <button className="btn small ghost" type="button" disabled={!turns.length} onClick={() => { setTurns([]); setError(null); }}>Clear conversation</button>
        </div>
      </div>
      <ol className="chat-log" data-testid="chat-log" aria-live="polite">
        {turns.length === 0 && <li className="muted chat-empty">{ready ? 'Ask something to start.' : `The endpoint is ${endpoint.status.toLowerCase()}.`}</li>}
        {turns.map((t, i) => (
          <li key={i} className={`chat-turn ${t.role}`} data-role={t.role}>
            <span className="chat-who">{t.role === 'user' ? 'You' : endpoint.name}</span>
            <div className="chat-text">{t.content}</div>
            {t.usage && (
              <span className="chat-usage muted small" data-testid="chat-usage">
                {`${t.usage.prompt_tokens} prompt + ${t.usage.completion_tokens} completion tokens${t.ms ? `, ${(t.ms / 1000).toFixed(1)} s` : ''}`}
              </span>)}
          </li>))}
        {busy && <li className="chat-turn assistant muted">Thinking…</li>}
      </ol>
      {error && <div className="alert bad" data-testid="chat-error">{error}</div>}
      <form className="chat-input" onSubmit={(e) => { e.preventDefault(); void send(); }}>
        <textarea rows={2} placeholder="Message (Enter to send, Shift+Enter for a new line)" value={draft} aria-label="Message"
          onChange={(e) => setDraft(e.target.value)} data-testid="chat-input"
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send(); } }} />
        <button className="btn primary" type="submit" disabled={!ready || busy || !allowed || !draft.trim()} title={why} data-testid="chat-send">{busy ? 'Sending…' : 'Send'}</button>
      </form>
      <Snippet label="The same request with curl" code={[
        `curl -X POST ${location.origin}${path} \\`,
        `  -H 'content-type: application/json' \\`,
        `  -d '{"messages": [{"role": "user", "content": "Hello"}], "max_tokens": ${Number(maxTokens) || 256}}'`,
      ].join('\n')} />
    </div>
  );
}
