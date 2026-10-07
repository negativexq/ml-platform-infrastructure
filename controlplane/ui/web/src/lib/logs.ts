import { useEffect, useState } from 'react';
import { api, ApiError, readLogStream, type S } from '../api/client';

const MAX_TEXT = 1024 * 1024;

function delay(signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve(); };
    const timer = setTimeout(finish, 3000);
    signal.addEventListener('abort', finish, { once: true });
    if (signal.aborted) finish();
  });
}

/** Direct Go SSE while running; snapshots for finished runs and disabled deployments. */
export function useRunLogs(path: string | null, active: boolean) {
  const [data, setData] = useState('');
  const [live, setLive] = useState(false);
  useEffect(() => {
    setData(''); setLive(false);
    if (!path) return;
    const controller = new AbortController();
    const { signal } = controller;
    async function follow() {
      let snapshotOnly = !active;
      while (!signal.aborted) {
        let complete = false;
        try {
          if (!snapshotOnly) {
            const ticket = await api.post<S['LogStreamTicket']>(`${path}/stream-ticket`, {}, signal);
            await readLogStream(ticket, signal, ({ event, data: value }) => {
              if (signal.aborted) return;
              if (event === 'ready') { setData(''); setLive(true); }
              if (event === 'log' && typeof value === 'string') setData((previous) => (previous + value).slice(-MAX_TEXT));
              if (event === 'end') complete = (value as { reason?: string })?.reason === 'complete';
              if (event === 'error') throw new Error('Log connection interrupted');
            });
            setLive(false);
            if (complete) return;
          } else {
            const text = await api.text(path!, signal);
            if (!signal.aborted) setData(text.slice(-MAX_TEXT));
            if (!active) return;
          }
        } catch (error) {
          if (signal.aborted) return;
          setLive(false);
          if (error instanceof ApiError && [401, 403, 404, 410].includes(error.status)) {
            setData('(logs are not available)'); return;
          }
          if (error instanceof ApiError && error.status === 503 && error.code !== 'log_stream') {
            snapshotOnly = true;
            continue;
          }
          // 409: pod has not started. Transient disconnects get a fresh capability.
        }
        await delay(signal);
      }
    }
    void follow();
    return () => controller.abort();
  }, [path, active]);
  return { data, live };
}
