import { afterEach, describe, expect, it, vi } from 'vitest';
import { readLogStream } from './client';

afterEach(() => vi.unstubAllGlobals());
const ticket = { url: '/log-stream', token: 'short-lived-capability', expires_at: 1 };

function stream(chunks: Uint8Array[]) {
  vi.stubGlobal('window', { location: { origin: 'https://platform.example' } });
  const fetch = vi.fn(async () => new Response(new ReadableStream({
    start(controller) { chunks.forEach((chunk) => controller.enqueue(chunk)); controller.close(); },
  }), { headers: { 'content-type': 'text/event-stream' } }));
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

describe('Go log SSE client', () => {
  it('handles split frames and UTF-8 without putting credentials in a URL or sending cookies', async () => {
    const raw = new TextEncoder().encode('event: ready\ndata: {"pod":"p"}\n\nevent: log\ndata: "işlem\\n"\n\nevent: end\ndata: {"reason":"complete"}\n\n');
    const fetch = stream(Array.from(raw, (byte) => new Uint8Array([byte])));
    const events: unknown[] = [];
    await readLogStream(ticket, new AbortController().signal, (event) => events.push(event));
    expect(events).toEqual([
      { event: 'ready', data: { pod: 'p' } }, { event: 'log', data: 'işlem\n' }, { event: 'end', data: { reason: 'complete' } },
    ]);
    const [url, init] = fetch.mock.calls[0]! as unknown as [URL, RequestInit];
    expect(url.href).toBe('https://platform.example/log-stream');
    expect(init.credentials).toBe('omit');
    expect(init.redirect).toBe('error');
    expect(init.headers).toEqual({ Authorization: `Bearer ${ticket.token}` });
  });
  it.each(['//evil.example/log-stream', '/log-stream?token=x', 'https://evil.example/log-stream'])('refuses %s', async (url) => {
    const fetch = stream([]);
    await expect(readLogStream({ ...ticket, url }, new AbortController().signal, () => {})).rejects.toThrow();
    expect(fetch).not.toHaveBeenCalled();
  });
  it('bounds an incomplete event', async () => {
    stream([new TextEncoder().encode('x'.repeat(512 * 1024 + 1))]);
    await expect(readLogStream(ticket, new AbortController().signal, () => {})).rejects.toThrow('too large');
  });
});
