import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { api, enc, type S } from '../api/client';
import { num, pct } from '../lib/format';
import { useSearchState } from '../lib/search';
import { TrendChart, type TrendSeries } from './charts/TrendChart';

const RANGES: [string, string, number][] = [['15m', 'Last 15 minutes', 15], ['1h', 'Last hour', 60], ['6h', 'Last 6 hours', 360], ['24h', 'Last 24 hours', 1440]];
// Colour follows the revision's role, never its order: stable is slot 1, the canary slot 2.
const COLOR: Record<string, string> = { stable: 'var(--series-1)', serving: 'var(--series-1)', canary: 'var(--series-2)' };

/**
 * How serving has gone over time, per revision, against the canary's gate: the trend the
 * meters above cannot show. One range control scopes all three charts; changing it keeps the
 * current charts on screen (dimmed) until the new data arrives.
 */
export function MetricTrends({ project, endpoint }: { project: string; endpoint: string }) {
  const [{ range }, update] = useSearchState({ range: '1h' });
  const minutes = RANGES.find(([key]) => key === range)?.[2] ?? 60;
  const query = useQuery({
    queryKey: ['history', project, endpoint, minutes],
    queryFn: () => api.get<S['EndpointHistoryOut']>(`/projects/${enc(project)}/endpoints/${enc(endpoint)}/metrics/history?minutes=${minutes}`),
    placeholderData: keepPreviousData,
    refetchInterval: 30_000,
  });
  const h = query.data;
  const series = (pick: (p: S['MetricsPointOut']) => number | null | undefined): TrendSeries[] =>
    (h?.revisions ?? []).map((r) => ({
      key: String(r.revision),
      label: `r${r.revision} ${r.role === 'serving' ? '' : `${r.role} `}${r.model} v${r.model_version}`,
      short: r.role === 'serving' ? `r${r.revision}` : r.role,
      color: COLOR[r.role] ?? 'var(--series-1)',
      points: r.points.map((p) => ({ t: Date.parse(p.at), v: pick(p) ?? null })),
    }));
  const start = h ? Date.parse(h.start) : Date.now() - minutes * 60000;
  const end = h ? Date.parse(h.end) : Date.now();
  const markers = (h?.markers ?? []).map((m) => ({ t: Date.parse(m.at), label: m.label }));
  const empty = h && h.available && h.revisions.every((r) => r.points.length === 0);

  return (
    <div className="section card" data-testid="trends">
      <div className="section-head">
        <h2>Serving over time</h2>
        <div className="seg" role="group" aria-label="Time range">
          {RANGES.map(([key, label]) => (
            <button key={key} type="button" title={label} aria-pressed={key === range} data-range={key} onClick={() => update({ range: key })}>{key}</button>))}
        </div>
      </div>
      {!h ? <p className="muted">Loading…</p>
        : !h.available ? <div className="alert">{`Metrics history unavailable: ${h.error}`}</div>
        : empty ? <p className="muted">No traffic recorded in this range.</p>
        : (
          <div className={query.isPlaceholderData ? 'refetching' : ''}>
            <div className="trends">
              <TrendChart testid="trend-p95" title="p95 latency" series={series((p) => p.p95_latency_ms)} format={(v) => `${num(v, 0)} ms`}
                reference={h.max_p95_latency_ms != null ? { value: h.max_p95_latency_ms, label: `gate ${num(h.max_p95_latency_ms, 0)} ms` } : undefined}
                markers={markers} start={start} end={end} />
              <TrendChart testid="trend-errors" title="5xx rate" series={series((p) => p.error_rate)} format={(v) => pct(v, v < 0.01 ? 2 : 1)}
                reference={h.max_error_rate != null ? { value: h.max_error_rate, label: `gate ${pct(h.max_error_rate, 1)}` } : undefined}
                markers={markers} start={start} end={end} />
              <TrendChart testid="trend-rps" title="Requests per second" series={series((p) => p.requests_per_second)} format={(v) => num(v, v < 10 ? 1 : 0)}
                markers={markers} start={start} end={end} />
            </div>
            <DataTable history={h} />
          </div>)}
    </div>
  );
}

/** The charts' table twin: every point, so no value is reachable only by hovering. */
function DataTable({ history }: { history: S['EndpointHistoryOut'] }) {
  const rows = history.revisions.flatMap((r) => r.points.map((p) => ({ r, p }))).sort((a, b) => b.p.at.localeCompare(a.p.at));
  return (
    <details className="snippet" data-testid="trend-table">
      <summary>{`Show as a table (${rows.length} rows)`}</summary>
      <div className="data-table">
        <table className="t">
          <thead><tr><th>Time</th><th>Revision</th><th className="num">p95</th><th className="num">5xx rate</th><th className="num">Requests/s</th></tr></thead>
          <tbody>
            {rows.map(({ r, p }) => (
              <tr key={`${r.revision}-${p.at}`}>
                <td>{new Date(p.at).toLocaleString()}</td><td>{`r${r.revision} ${r.role}`}</td>
                <td className="num">{p.p95_latency_ms == null ? '—' : `${num(p.p95_latency_ms, 0)} ms`}</td>
                <td className="num">{pct(p.error_rate)}</td><td className="num">{num(p.requests_per_second, 1)}</td>
              </tr>))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
