import { num } from '../../lib/format';
import { Legend, Tooltip, useTip, useWidth } from './base';

export type VersionPoint = { id: string; version: number; status: string; value: number };
type Bound = { min?: number | null; max?: number | null };

const ROW = 46, PAD_L = 92, PAD_R = 16;

/**
 * Every version's score on one metric, on one axis, with the acceptance threshold drawn in:
 * "is the candidate better than the champion, and by how much against the bar?" The champion
 * carries the accent, the rest are context (emphasis, not a rainbow of versions); the selected
 * version wears a ring. The versions table above is its table view.
 */
export function MetricStrip({ metric, points, bound, lowerIsBetter, selected, onSelect }: {
  metric: string; points: VersionPoint[]; bound?: Bound; lowerIsBetter: boolean;
  selected: string | null; onSelect: (id: string) => void;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const { tip, bind } = useTip();
  if (!points.length) return null;
  const refs = [bound?.min, bound?.max].filter((v): v is number => v != null);
  const values = [...points.map((p) => p.value), ...refs];
  let lo = Math.min(...values), hi = Math.max(...values);
  const pad = (hi - lo || Math.abs(hi) || 1) * 0.15;
  lo -= pad; hi += pad;
  const x = (v: number) => PAD_L + ((v - lo) / (hi - lo)) * (width - PAD_L - PAD_R);
  const cy = ROW / 2 + 2;
  const champion = points.find((p) => p.status === 'CHAMPION');
  const labelled = new Set([champion?.id, points[0]?.id].filter(Boolean)); // the champion and the newest

  return (
    <figure className="chart" ref={ref} data-testid={`metric-strip-${metric}`}>
      <svg width={width} height={ROW} role="img" aria-label={`${metric} by version${bound?.min != null ? `, must be at least ${bound.min}` : ''}${bound?.max != null ? `, must be at most ${bound.max}` : ''}`}>
        <text className="tick" x={0} y={cy - 2} style={{ fontWeight: 600, fill: 'var(--text)' }}>{metric}</text>
        <text className="tick" x={0} y={cy + 12}>{lowerIsBetter ? 'lower is better' : 'higher is better'}</text>
        <line className="baseline" x1={PAD_L} x2={width - PAD_R} y1={cy} y2={cy} />
        {refs.map((r) => (
          <g key={r}>
            <line className="ref" x1={x(r)} x2={x(r)} y1={cy - 14} y2={cy + 14} />
            <text className="ref-label" x={x(r)} y={cy + 24} textAnchor="middle">{`${r === bound?.min ? '≥' : '≤'} ${r}`}</text>
          </g>))}
        {[...points].sort((a, b) => (a.status === 'CHAMPION' ? 1 : 0) - (b.status === 'CHAMPION' ? 1 : 0)).map((p) => {
          const isChampion = p.status === 'CHAMPION';
          const color = isChampion ? 'var(--series-1)' : 'var(--series-dim)';
          const label = `v${p.version}${isChampion ? ' ★' : ''}`;
          return (
            <g key={p.id} className="mark" tabIndex={0} role="button" aria-label={`v${p.version} ${p.status.toLowerCase()}: ${metric} ${num(p.value, 3)}`}
              data-version={p.version} onClick={() => onSelect(p.id)} onKeyDown={(e) => { if (e.key === 'Enter') onSelect(p.id); }}
              {...bind({ head: `v${p.version} · ${p.status.toLowerCase()}`, rows: [{ value: num(p.value, 3), label: metric, color }] }, { x: x(p.value), y: cy - 10 })}>
              <circle className="hit" cx={x(p.value)} cy={cy} r={12} />
              <circle cx={x(p.value)} cy={cy} r={isChampion ? 6 : 5} fill={color} stroke="var(--surface)" strokeWidth={2} />
              {p.id === selected && <circle cx={x(p.value)} cy={cy} r={9.5} fill="none" stroke="var(--text)" strokeWidth={1.5} />}
              {labelled.has(p.id) && <text className="tick" x={x(p.value)} y={cy - 12} textAnchor="middle" style={{ fill: 'var(--text)' }}>{label}</text>}
            </g>);
        })}
      </svg>
      <Tooltip tip={tip} width={width} />
    </figure>
  );
}

export const MetricLegend = () => (
  <Legend items={[{ color: 'var(--series-1)', label: 'champion' }, { color: 'var(--series-dim)', label: 'other versions' },
    { color: 'var(--ink-muted)', label: 'acceptance threshold', line: true }]} />
);
