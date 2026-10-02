export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null) return '—';
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
}

export function fmtAgo(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return '—';
  const s = Math.max(0, (now - new Date(iso).getTime()) / 1000);
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export const pct = (x: number | null | undefined, digits = 2) =>
  x == null ? '—' : `${(x * 100).toFixed(digits)}%`;
export const num = (x: number | null | undefined, digits = 1) =>
  x == null ? '—' : Number(x).toFixed(digits);
export const shortId = (id: string) => String(id).slice(0, 8);

/** Subsequence match, so "crprod" finds "credit-risk-prod". Lower is better; null is no match. */
export function score(query: string, text: string): number | null {
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  if (!q) return 0;
  const at = t.indexOf(q);
  if (at >= 0) return at;
  let gaps = 0;
  let last = -1;
  for (const ch of q) {
    const found = t.indexOf(ch, last + 1);
    if (found < 0) return null;
    gaps += found - last - 1;
    last = found;
  }
  return 100 + gaps;
}

/** Routes, as the hash URLs the app lives under. */
export const routes = {
  projects: () => '#/projects',
  project: (p: string) => `#/projects/${encodeURIComponent(p)}`,
  pipelineRun: (p: string, id: string) => `#/projects/${encodeURIComponent(p)}/pipeline-runs/${id}`,
  jobRun: (p: string, id: string) => `#/projects/${encodeURIComponent(p)}/runs/${id}`,
  model: (p: string, name: string) =>
    `#/projects/${encodeURIComponent(p)}/models/${encodeURIComponent(name)}`,
  deployment: (p: string, name: string) =>
    `#/projects/${encodeURIComponent(p)}/deployments/${encodeURIComponent(name)}`,
};

export const go = (hash: string) => {
  location.hash = hash;
};
