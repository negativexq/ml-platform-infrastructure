import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { api, type S } from '../api/client';
import { Sparkline } from '../components/charts/Sparkline';
import { TrendChart } from '../components/charts/TrendChart';
import { Section } from '../components/bits';
import { useCrumbs, useLive } from '../lib/chrome';
import { fmtAgo, fmtDuration, routes } from '../lib/format';
import { formatUnit, HEALTH, scopeName, SEVERITY, thresholdText, type Health } from '../lib/health';
import { useNow } from '../lib/now';
import { useSearchState } from '../lib/search';

const RANGES: [string, string, number][] = [['15m', 'Last 15 minutes', 15], ['1h', 'Last hour', 60], ['6h', 'Last 6 hours', 360], ['24h', 'Last 24 hours', 1440]];

type Check = { signal: S['SignalHealthOut']; series: S['SeriesHealthOut'] | null; status: Health };

/** Every judged thing on one row: a signal with no series still gets one, so silence shows. */
function checksOf(h: S['PlatformHealthOut']): Check[] {
  return h.signals.flatMap((signal): Check[] => signal.series.length
    ? signal.series.map((series) => ({ signal, series, status: series.status }))
    : [{ signal, series: null, status: signal.status }]);
}

export function StatusChip({ status, testid }: { status: Health; testid?: string }) {
  const h = HEALTH[status];
  return (
    <span className="status-chip" data-testid={testid} data-status={status}>
      <span aria-hidden="true" style={{ color: h.color }}>{h.symbol}</span>{h.word}
    </span>
  );
}

/**
 * The platform's own health, for whoever is on call for it: is the API answering, are the
 * reconcilers applying desired state, are the systems they call healthy, and how much work is
 * in flight. Each check is a measure, its value now, the rule it is judged by and its trend.
 */
export function MonitorPage() {
  useCrumbs([{ label: 'Monitor' }]);
  const [{ range }, update] = useSearchState({ range: '1h' });
  const minutes = RANGES.find(([key]) => key === range)?.[2] ?? 60;
  const query = useQuery({
    queryKey: ['platform-health', minutes],
    queryFn: () => api.get<S['PlatformHealthOut']>(`/platform/health?minutes=${minutes}`),
    placeholderData: keepPreviousData,
    refetchInterval: 30_000,
  });
  useLive(true);
  const now = useNow();
  const h = query.data;

  return (
    <>
      <div className="page-head">
        <h1>Platform health</h1>
        {h && <StatusChip status={h.status} testid="platform-status" />}
        <div className="actions">
          <div className="seg" role="group" aria-label="Time range">
            {RANGES.map(([key, label]) => (
              <button key={key} type="button" title={label} aria-pressed={key === range} data-range={key} onClick={() => update({ range: key })}>{key}</button>))}
          </div>
        </div>
      </div>
      <p className="sub">
        The control plane itself: its API, the reconcilers that apply desired state, and the systems they call.
        Thresholds are the same as the alert rules. {h && <span className="muted">{`Updated ${fmtAgo(h.generated_at, now)}.`}</span>}
      </p>
      {query.isError && !h && <div className="alert bad">{`Could not load platform health: ${(query.error as Error).message}`}</div>}
      {!h ? <div className="skel card" /> : (
        <div className={query.isPlaceholderData ? 'refetching' : ''}>
          {!h.available && (
            <div className="alert" data-testid="telemetry-missing">
              {`Platform metrics are unavailable (${h.error}). Workload counts below come from the platform database and are current.`}
            </div>)}
          <Attention checks={checksOf(h)} />
          <Inventory inv={h.inventory} />
          <ApiCharts h={h} />
          <Checks h={h} />
        </div>)}
    </>
  );
}

function Attention({ checks }: { checks: Check[] }) {
  const bad = checks.filter((c) => c.status === 'warning' || c.status === 'critical')
    .sort((a, b) => SEVERITY[b.status] - SEVERITY[a.status]);
  if (!bad.length) return null;
  return (
    <div className="card attention section-gap" data-testid="health-attention">
      <h2>Needs attention<span className={`count${bad.some((c) => c.status === 'critical') ? ' bad' : ''}`}>{bad.length}</span></h2>
      <ul className="timeline">
        {bad.map((c) => (
          <li key={`${c.signal.key}:${c.series?.name ?? ''}`}>
            <StatusChip status={c.status} />
            <span>
              <b>{c.signal.title}</b>{c.series?.name ? ` for ${scopeName(c.series.name)}` : ''}
              {': '}{c.series?.current == null ? 'no recent data' : formatUnit(c.signal.unit, c.series.current)}
              <span className="muted">{` (${thresholdText(c.signal)})`}</span>
            </span>
            <span className="muted small">{c.signal.help}</span>
          </li>))}
      </ul>
    </div>
  );
}

function Inventory({ inv }: { inv: S['InventoryOut'] }) {
  const tiles: [string, string, string, string?][] = [
    ['projects', String(inv.projects), 'Projects', inv.projects_not_ready ? `${inv.projects_not_ready} not ready` : 'all ready'],
    ['active', String(inv.runs_active), 'Runs in flight', `${inv.runs_waiting} waiting to start`],
    ['waiting', inv.oldest_waiting_seconds == null ? '—' : fmtDuration(inv.oldest_waiting_seconds), 'Longest wait to start',
      inv.oldest_waiting_seconds == null ? 'nothing waiting' : 'a long wait means a stuck reconciler or a full cluster'],
    ['failed', String(inv.runs_failed_24h), 'Failed runs, 24h'],
    ['deployments', `${inv.deployments_ready}/${inv.deployments}`, 'Deployments ready', inv.deployments_failed ? `${inv.deployments_failed} failed` : undefined],
    ['rollouts', String(inv.rollouts_active), 'Rollouts in progress'],
  ];
  return (
    <Section title="Workload" testid="inventory">
      <div className="tiles">
        {tiles.map(([key, n, label, note]) => {
          const body = <><div className="n">{n}</div><div className="l">{label}</div>{note && <div className="l note">{note}</div>}</>;
          return key === 'projects'
            ? <a key={key} className="tile" href={routes.projects()} data-tile={key}>{body}</a>
            : <div key={key} className="tile" data-tile={key}>{body}</div>;
        })}
      </div>
    </Section>
  );
}

/** The three API measures as charts: one axis each, the warning threshold as a reference line. */
function ApiCharts({ h }: { h: S['PlatformHealthOut'] }) {
  const start = Date.parse(h.start), end = Date.parse(h.end);
  const charts = (['api_requests', 'api_errors', 'api_latency'] as const)
    .map((key) => h.signals.find((s) => s.key === key)).filter((s): s is S['SignalHealthOut'] => !!s);
  if (!h.available) return null;
  return (
    <div className="section card" data-testid="api-trends">
      <h2>Platform API</h2>
      <div className="trends">
        {charts.map((s) => (
          <TrendChart key={s.key} testid={`trend-${s.key}`} title={s.title} start={start} end={end} markers={[]}
            format={(v) => formatUnit(s.unit, v)}
            reference={s.warn != null ? { value: s.warn, label: `warn ${formatUnit(s.unit, s.warn)}` } : undefined}
            series={s.series.map((x) => ({
              key: x.name || s.key, label: s.title, short: '', color: 'var(--series-1)',
              points: x.points.map((p) => ({ t: Date.parse(p.at), v: p.value })),
            }))} />))}
      </div>
      <ApiTable charts={charts} />
    </div>
  );
}

/** The charts' table twin, so no value is reachable only by hovering. */
function ApiTable({ charts }: { charts: S['SignalHealthOut'][] }) {
  const byTime = new Map<string, Map<string, number>>();
  for (const s of charts) for (const p of s.series[0]?.points ?? []) {
    if (!byTime.has(p.at)) byTime.set(p.at, new Map());
    byTime.get(p.at)!.set(s.key, p.value);
  }
  const rows = [...byTime.entries()].sort((a, b) => b[0].localeCompare(a[0]));
  return (
    <details className="snippet" data-testid="api-table">
      <summary>{`Show as a table (${rows.length} rows)`}</summary>
      <div className="data-table">
        <table className="t">
          <thead><tr><th>Time</th>{charts.map((s) => <th key={s.key} className="num">{s.title}</th>)}</tr></thead>
          <tbody>
            {rows.map(([at, values]) => (
              <tr key={at}><td>{new Date(at).toLocaleString()}</td>
                {charts.map((s) => <td key={s.key} className="num">{formatUnit(s.unit, values.get(s.key))}</td>)}</tr>))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

/** Every check in a fixed order (reconcilers, API, external systems), so a row is always in the same place. */
function Checks({ h }: { h: S['PlatformHealthOut'] }) {
  const rows = checksOf(h);
  return (
    <Section title="Checks" testid="health-checks">
      <div className="table-scroll">
      <table className="t">
        <thead>
          <tr><th>Check</th><th>Scope</th><th className="num">Now</th><th>Judged by</th><th>Status</th><th>Trend</th></tr>
        </thead>
        <tbody>
          {rows.map((c, i) => {
            const first = i === 0 || rows[i - 1]!.signal.key !== c.signal.key;
            const judged = c.signal.warn != null || c.signal.critical != null;
            const pts = (c.series?.points ?? []).map((p) => ({ t: Date.parse(p.at), v: p.value }));
            return (
              <tr key={`${c.signal.key}:${c.series?.name ?? ''}`} data-check={`${c.signal.key}:${c.series?.name ?? ''}`} className={first ? 'group-start' : ''}>
                <td title={c.signal.help}>{first ? <b>{c.signal.title}</b> : <span className="sr">{c.signal.title}</span>}</td>
                <td>{c.series ? scopeName(c.series.name) : <span className="muted">{c.signal.error ? 'unavailable' : 'nothing reported'}</span>}</td>
                <td className="num" title={thresholdText(c.signal)}>{formatUnit(c.signal.unit, c.series?.current)}</td>
                <td className="muted small">{thresholdText(c.signal)}</td>
                <td>{judged || !c.series ? <StatusChip status={c.status} /> : <span className="muted small">not judged</span>}</td>
                <td><Sparkline points={pts} threshold={c.signal.warn}
                  label={`${c.signal.title} ${c.series ? scopeName(c.series.name) : ''}: ${pts.length} points, now ${formatUnit(c.signal.unit, c.series?.current)}`} /></td>
              </tr>);
          })}
        </tbody>
      </table>
      </div>
    </Section>
  );
}
