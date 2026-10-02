/**
 * A check's trend in a table cell: one 1.5px line, no axes, the threshold as a faint rule when
 * it falls inside the range. The exact numbers are in the row; this only says "which way".
 */
export function Sparkline({ points, threshold, label, width = 120, height = 28 }: {
  points: { t: number; v: number }[]; threshold?: number | null; label: string; width?: number; height?: number;
}) {
  if (points.length < 2) return <span className="muted small">no trend</span>;
  const t0 = points[0]!.t, t1 = points[points.length - 1]!.t;
  const values = points.map((p) => p.v);
  const lo = Math.min(...values, 0);
  const hi = Math.max(...values, threshold ?? 0) * 1.08 || 1;
  const x = (t: number) => 1 + ((t - t0) / (t1 - t0 || 1)) * (width - 2);
  const y = (v: number) => 2 + (height - 4) * (1 - (v - lo) / (hi - lo || 1));
  const d = points.map((p, i) => `${i ? 'L' : 'M'}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join('');
  const last = points[points.length - 1]!;
  return (
    <svg className="spark" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label}>
      {threshold != null && threshold <= hi && <line className="spark-ref" x1={0} x2={width} y1={y(threshold)} y2={y(threshold)} />}
      <path d={d} fill="none" stroke="var(--series-1)" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={x(last.t)} cy={y(last.v)} r={2.5} fill="var(--series-1)" />
    </svg>
  );
}
