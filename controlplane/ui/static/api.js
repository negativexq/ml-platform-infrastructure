// The only way the UI talks to anything: same-origin requests to the Platform API.

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function request(method, path, body) {
  if (!path.startsWith('/') || path.startsWith('//')) {
    throw new Error(`refusing non-relative API path: ${path}`);
  }
  let response;
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
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!response.ok) {
    const err = data && data.error;
    throw new ApiError(response.status, err ? err.code : 'http_error',
      err ? err.message : `${response.status} ${response.statusText}`);
  }
  return data;
}

export const api = {
  get: (path) => request('GET', path),
  post: (path, body = {}) => request('POST', path, body),
  put: (path, body) => request('PUT', path, body),
  /** Plain-text endpoints (logs). */
  text: async (path) => {
    const r = await fetch(path);
    if (!r.ok) throw new ApiError(r.status, 'http_error', `${r.status} ${r.statusText}`);
    return r.text();
  },
};

export const enc = encodeURIComponent;
