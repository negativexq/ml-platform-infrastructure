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

/** Split a command line the way a shell would for simple cases: spaces separate, quotes group. */
export function splitCommand(line: string): string[] {
  const out: string[] = [];
  let current = '';
  let quote: string | null = null;
  let started = false;
  for (const ch of line) {
    if (quote) {
      if (ch === quote) quote = null;
      else current += ch;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
      started = true;
    } else if (/\s/.test(ch)) {
      if (started || current) out.push(current);
      current = '';
      started = false;
    } else {
      current += ch;
      started = true;
    }
  }
  if (quote) throw new Error('unclosed quote');
  if (started || current) out.push(current);
  return out;
}

/** `KEY=value` lines to an object; blank lines and `#` comments are ignored. */
export function parsePairs(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const at = line.indexOf('=');
    if (at <= 0) throw new Error(`expected KEY=value, got "${line}"`);
    out[line.slice(0, at).trim()] = line.slice(at + 1).trim();
  }
  return out;
}

export const shellQuote = (arg: string) => (/^[\w@%+=:,./-]+$/.test(arg) ? arg : `'${arg.replace(/'/g, `'\\''`)}'`);

export type Thresholds = Record<string, { min?: number | null; max?: number | null }>;

/** `auc >= 0.9` / `rmse <= 0.3` lines to thresholds; a metric may have both bounds. */
export function parseThresholds(text: string): Thresholds {
  const out: Thresholds = {};
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const m = line.match(/^([A-Za-z_][\w.-]*)\s*(>=|<=)\s*(-?\d+(?:\.\d+)?(?:e-?\d+)?)$/i);
    if (!m) throw new Error(`expected "metric >= number" or "metric <= number", got "${line}"`);
    const [, metric, op, value] = m as unknown as [string, string, string, string];
    const entry = (out[metric] ??= {});
    if (op === '>=') entry.min = Number(value); else entry.max = Number(value);
  }
  return out;
}

export const formatThresholds = (t: Thresholds) => Object.entries(t).flatMap(([m, b]) => [
  ...(b.min != null ? [`${m} >= ${b.min}`] : []), ...(b.max != null ? [`${m} <= ${b.max}`] : []),
]).join('\n');

/** A metric with only an upper bound (latency, error, loss) is better when lower. */
export const lowerIsBetter = (metric: string, t: Thresholds) =>
  (t[metric]?.max != null && t[metric]?.min == null) || /(loss|error|rmse|mae|mse|latency)/i.test(metric);
