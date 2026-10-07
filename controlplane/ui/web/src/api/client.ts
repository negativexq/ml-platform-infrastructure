// Platform API requests and API-authorized, credential-free Go log streams.
import type { components } from './schema';

export type S = components['schemas'];

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly signInUrl?: string,
  ) {
    super(message);
  }
}

/** Sent with every request: the API refuses a cookie-authenticated change without it, which is
 * what stops another site from making the browser act on someone's behalf (CSRF). */
const CSRF = { 'x-mlp-csrf': '1' };

/** Fired when the session is gone, so the app can show the sign-in screen from anywhere. */
export const SIGNED_OUT_EVENT = 'mlp:signed-out';

async function request<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  if (!path.startsWith('/') || path.startsWith('//')) {
    throw new Error(`refusing non-relative API path: ${path}`);
  }
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      signal,
      headers: body === undefined ? CSRF : { ...CSRF, 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new ApiError(0, 'network', 'Cannot reach the platform API');
  }
  const text = await response.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!response.ok) {
    const err = (data as { error?: { code: string; message: string; sign_in_url?: string } } | null)?.error;
    const error = new ApiError(
      response.status,
      err ? err.code : 'http_error',
      err ? err.message : `${response.status} ${response.statusText}`,
      err?.sign_in_url,
    );
    if (response.status === 401) window.dispatchEvent(new CustomEvent(SIGNED_OUT_EVENT, { detail: error }));
    throw error;
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body: unknown = {}, signal?: AbortSignal) => request<T>('POST', path, body, signal),
  put: <T>(path: string, body: unknown) => request<T>('PUT', path, body),
  patch: <T>(path: string, body: unknown) => request<T>('PATCH', path, body),
  del: <T>(path: string) => request<T>('DELETE', path),
  /** Plain-text endpoints (logs). */
  text: async (path: string, signal?: AbortSignal): Promise<string> => {
    const r = await fetch(path, { headers: CSRF, signal });
    if (r.status === 401) window.dispatchEvent(new CustomEvent(SIGNED_OUT_EVENT));
    if (!r.ok) throw new ApiError(r.status, 'http_error', `${r.status} ${r.statusText}`);
    return r.text();
  },
};

export type LogEvent = { event: string; data: unknown };

/** Parse SSE incrementally, including UTF-8 split across network chunks. */
export async function readLogStream(
  ticket: S['LogStreamTicket'], signal: AbortSignal, receive: (event: LogEvent) => void,
): Promise<void> {
  const url = new URL(ticket.url, window.location.origin);
  if (url.username || url.password || url.search || url.hash ||
    url.origin !== window.location.origin) {
    throw new Error('Invalid log stream address');
  }
  const response = await fetch(url, {
    headers: { Authorization: `Bearer ${ticket.token}` }, signal,
    credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer',
  });
  if (!response.ok || !response.headers.get('content-type')?.startsWith('text/event-stream') || !response.body) {
    throw new ApiError(response.status, 'log_stream', 'Cannot connect to live logs');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = '';
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done) return;
      pending += decoder.decode(value, { stream: true });
      // A JSON-escaped 64 KiB line can occupy up to 384 KiB on the wire.
      if (pending.length > 512 * 1024) throw new Error('Live log event is too large');
      let boundary: number;
      while ((boundary = pending.indexOf('\n\n')) !== -1) {
        const frame = pending.slice(0, boundary);
        pending = pending.slice(boundary + 2);
        const event = frame.split('\n').find((line) => line.startsWith('event: '))?.slice(7);
        const data = frame.split('\n').filter((line) => line.startsWith('data: ')).map((line) => line.slice(6)).join('\n');
        if (event && data) receive({ event, data: JSON.parse(data) as unknown });
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export const enc = encodeURIComponent;
