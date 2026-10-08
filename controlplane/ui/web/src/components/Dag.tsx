import { fmtDuration } from '../lib/format';

export type DagStep = {
  step: string; status: string; depends_on: string[]; duration_seconds?: number | null;
  /** Second line of the node; defaults to the status and duration. */
  detail?: string; reason?: string | null; label?: string;
};

const NODE_W = 172, NODE_H = 56, GAP_X = 60, GAP_Y = 20, PAD = 10;

/** Layers by longest dependency chain, so parallel branches sit in the same column. */
export function layout(steps: DagStep[]) {
  const byName = new Map(steps.map((s) => [s.step, s]));
  const level = new Map<string, number>();
  const depth = (name: string): number => {
    const known = level.get(name);
    if (known !== undefined) return known;
    const deps = (byName.get(name)?.depends_on ?? []).filter((d) => byName.has(d));
    const value = deps.length ? 1 + Math.max(...deps.map(depth)) : 0;
    level.set(name, value);
    return value;
  };
  steps.forEach((s) => depth(s.step));
  const columns: DagStep[][] = [];
  steps.forEach((s) => {
    const l = level.get(s.step) ?? 0;
    (columns[l] ??= []).push(s);
  });
  const pos = new Map<string, { x: number; y: number }>();
  columns.forEach((col, x) =>
    col.forEach((s, y) => pos.set(s.step, { x: PAD + x * (NODE_W + GAP_X), y: PAD + y * (NODE_H + GAP_Y) })));
  const width = PAD * 2 + columns.length * NODE_W + (columns.length - 1) * GAP_X;
  const height = PAD * 2 + Math.max(...columns.map((c) => c.length)) * (NODE_H + GAP_Y) - GAP_Y;
  return { pos, width, height };
}

export function Dag({ steps, selected, onSelect, label = 'Pipeline steps and their dependencies' }: {
  steps: DagStep[]; selected: string | null; onSelect: (step: string) => void; label?: string;
}) {
  const { pos, width, height } = layout(steps);
  return (
    <div className="dag-wrap">
      <svg className="dag" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img"
        aria-label={label}>
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX={9} refY={5} markerWidth={7} markerHeight={7} orient="auto-start-reverse">
            <path d="M0 0 L10 5 L0 10 z" />
          </marker>
        </defs>
        {steps.flatMap((s) => s.depends_on.map((dep) => {
          const a = pos.get(dep), b = pos.get(s.step);
          if (!a || !b) return null;
          const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2;
          const mid = (x2 - x1) / 2;
          return <path key={`${dep}>${s.step}`} className="edge" markerEnd="url(#arrow)"
            d={`M${x1} ${y1} C${x1 + mid} ${y1}, ${x2 - mid} ${y2}, ${x2} ${y2}`} />;
        }))}
        {steps.map((s) => {
          const p = pos.get(s.step);
          if (!p) return null;
          const status = s.status.toLowerCase();
          return (
            <g key={s.step} className={`node st-${s.status}${selected === s.step ? ' sel' : ''}`} transform={`translate(${p.x} ${p.y})`}
              tabIndex={0} role="button" aria-label={`${s.label ?? s.step}: ${status}`} data-step={s.step}
              onClick={() => onSelect(s.step)}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(s.step); } }}>
              <title>{s.reason ? `${s.label ?? s.step}: ${s.reason}` : (s.label ?? s.step)}</title>
              <rect width={NODE_W} height={NODE_H} />
              <text x={12} y={23}>{s.label ?? s.step}</text>
              <text className="st" x={12} y={42}>{s.detail ?? `${status}${s.duration_seconds != null ? ` · ${fmtDuration(s.duration_seconds)}` : ''}`}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
