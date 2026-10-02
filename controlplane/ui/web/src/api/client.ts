// The only way the UI talks to anything: same-origin requests to the Platform API.
import type { components } from './schema';

export type S = components['schemas'];

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  if (!path.startsWith('/') || path.startsWith('//')) {
    throw new Error(`refusing non-relative API path: ${path}`);
  }
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers: body === undefined ? {} : { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
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
    const err = (data as { error?: { code: string; message: string } } | null)?.error;
    throw new ApiError(
      response.status,
      err ? err.code : 'http_error',
      err ? err.message : `${response.status} ${response.statusText}`,
    );
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body: unknown = {}) => request<T>('POST', path, body),
  put: <T>(path: string, body: unknown) => request<T>('PUT', path, body),
  /** Plain-text endpoints (logs). */
  text: async (path: string): Promise<string> => {
    const r = await fetch(path);
    if (!r.ok) throw new ApiError(r.status, 'http_error', `${r.status} ${r.statusText}`);
    return r.text();
  },
};

export const enc = encodeURIComponent;
