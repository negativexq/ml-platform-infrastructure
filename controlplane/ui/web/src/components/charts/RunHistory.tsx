import { fmtAgo, fmtDuration, go, shortId } from '../../lib/format';
import { columnPath, Legend, niceTicks, statusOf, Tooltip, useTip, useWidth } from './base';

export type HistoryRun = { id: string; status: string; created_at: string; duration_seconds?: number | null; href: string; label?: string };

const H = 120, PAD_L = 44, PAD_B = 18, PAD_T = 8, BAR_MAX = 24, GAP = 2;

/**
 * The last runs of a pipeline or job, oldest to newest: column height is how long it took,
 * colour is how it ended (status palette, with symbol and word in the legend and tooltip).
 * Answers "is it getting slower?" and "is it flaky?" at a glance. The runs table below is
 * its table view. A click opens the run.
 */
export function RunHistory({ runs, title = 'Run history' }: { runs: HistoryRun[]; title?: string }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const { tip, bind } = useTip();
  const ordered = [...runs].reverse(); // the API lists newest first; time reads left to right
  if (ordered.length < 2) return null;
  const finished = ordered.map((r) => r.duration_seconds ?? 0);
  const ticks = niceTicks(Math.max(...finished, 1), 3, 'seconds');
  const top = ticks[ticks.length - 1]!;
  const plotW = Math.max(120, width - PAD_L);
  const band = plotW / ordered.length;
  const barW = Math.min(BAR_MAX, Math.max(3, band - GAP));
  const y = (v: number) => PAD_T + (H - PAD_T) * (1 - v / top);
  const durations = ordered.filter((r) => r.status === 'SUCCEEDED' && r.duration_seconds != null).map((r) => r.duration_seconds!).sort((a, b) => a - b);
  const median = durations.length ? durations[Math.floor((durations.length - 1) / 2)]! : null;
  const present = [...new Set(ordered.map((r) => r.status))];

  return (
    <figure className="chart" ref={ref} data-testid="run-history" aria-label={`${title}: ${ordered.length} runs, oldest first`}>
      <svg width={width} height={H + PAD_B} role="img" aria-label={`${title}. The runs table lists the same runs.`}>
        <g className="grid">{ticks.map((t) => <line key={t} x1={PAD_L} x2={width} y1={y(t)} y2={y(t)} />)}</g>
        {ticks.map((t) => <text key={t} className="tick" x={PAD_L - 6} y={y(t) + 4} textAnchor="end">{fmtDuration(t)}</text>)}
        <line className="baseline" x1={PAD_L} x2={width} y1={H} y2={H} />
        {median != null && (
          <g>
            <line className="ref" x1={PAD_L} x2={width} y1={y(median)} y2={y(median)} />
            <text className="ref-label" x={width - 2} y={y(median) - 4} textAnchor="end">{`typical ${fmtDuration(median)}`}</text>
          </g>)}
        {ordered.map((r, i) => {
          const st = statusOf(r.status);
          const d = r.duration_seconds ?? 0;
          const h = Math.max(3, H - y(d)); // a run that has not finished still gets a visible stub
          const x = PAD_L + i * band + (band - barW) / 2;
          const label = `${r.label ?? shortId(r.id)} ${st.word}${r.duration_seconds != null ? ` in ${fmtDuration(d)}` : ''}, ${fmtAgo(r.created_at)}`;
          return (
            <g key={r.id} className="mark" tabIndex={0} role="link" aria-label={label} data-status={r.status}
              onClick={() => go(r.href)} onKeyDown={(e) => { if (e.key === 'Enter') go(r.href); }}
              {...bind({ head: `${r.label ?? shortId(r.id)} · ${fmtAgo(r.created_at)}`,
                rows: [{ value: r.duration_seconds != null ? fmtDuration(d) : 'in progress', label: `${st.symbol} ${st.word}`, color: st.color }] },
              { x: x + barW / 2, y: H - h })}>
              <rect className="hit" x={PAD_L + i * band} y={PAD_T} width={band} height={H - PAD_T} />
              <path d={columnPath(x, H - h, barW, h)} fill={st.color} />
            </g>);
        })}
        <text className="tick" x={PAD_L} y={H + 14}>older</text>
        <text className="tick" x={width} y={H + 14} textAnchor="end">latest</text>
      </svg>
      <Tooltip tip={tip} width={width} />
      <Legend items={present.map((s) => ({ color: statusOf(s).color, label: `${statusOf(s).symbol} ${statusOf(s).word}` }))} />
    </figure>
  );
}
