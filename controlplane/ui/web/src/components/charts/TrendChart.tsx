import { useState } from 'react';
import { Legend, niceTicks, useWidth } from './base';

/** `short` labels the line's end ("canary"); `label` is the full name in legend and tooltip. */
export type TrendSeries = { key: string; label: string; short: string; color: string; points: { t: number; v: number | null }[] };

const H = 150, PAD_L = 52, PAD_R = 64, PAD_T = 10, PAD_B = 22;

function timeTicks(start: number, end: number, count = 5): number[] {
  const spanMin = (end - start) / 60000;
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440];
  const step = (steps.find((s) => spanMin / s <= count) ?? 1440) * 60000;
  const first = Math.ceil(start / step) * step;
  const out: number[] = [];
  for (let t = first; t <= end; t += step) out.push(t);
  return out;
}

/** End labels closer than a line of text would overlap: then the legend carries identity
 * alone (never nudge labels apart, which detaches them from their lines). */
function endsCollide(ends: ({ v: number | null } | undefined)[], y: (v: number) => number) {
  const ys = ends.filter((e): e is { v: number } => e?.v != null).map((e) => y(e.v)).sort((a, b) => a - b);
  return ys.some((v, i) => i > 0 && v - ys[i - 1]! < 13);
}

/** Events closer than ~80px share one label ("canary started, step 25%") instead of colliding. */
function mergeMarkers(markers: { t: number; label: string }[], x: (t: number) => number) {
  const out: { t: number; label: string }[] = [];
  for (const m of [...markers].sort((a, b) => a.t - b.t)) {
    const prev = out[out.length - 1];
    if (prev && x(m.t) - x(prev.t) < 80) prev.label = `${prev.label}, ${m.label}`;
    else out.push({ ...m });
  }
  return out;
}

const clock = (t: number) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

/**
 * One measure over time: 2px lines, one axis, a crosshair that snaps to the nearest moment
 * and lists every series there (pointer or arrow keys). A gate is a reference line, events
 * are labelled verticals, missing data is a gap. Colour follows the revision's role.
 */
export function TrendChart({ title, testid, series, format, reference, markers, start, end }: {
  title: string; testid: string; series: TrendSeries[]; format: (v: number) => string;
  reference?: { value: number; label: string }; markers: { t: number; label: string }[]; start: number; end: number;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [at, setAt] = useState<number | null>(null);
  const values = series.flatMap((s) => s.points.map((p) => p.v).filter((v): v is number => v != null));
  const ticks = niceTicks(Math.max(...values, reference?.value ?? 0, 0) * 1.1 || 1, 4);
  const top = ticks[ticks.length - 1]!;
  const plotR = width - PAD_R;
  const x = (t: number) => PAD_L + ((t - start) / (end - start)) * (plotR - PAD_L);
  const y = (v: number) => PAD_T + (H - PAD_T - PAD_B) * (1 - v / top);
  const times = [...new Set(series.flatMap((s) => s.points.map((p) => p.t)))].sort((a, b) => a - b);
  const nearest = (t: number) => times.reduce((best, c) => (Math.abs(c - t) < Math.abs(best - t) ? c : best), times[0] ?? t);

  const path = (pts: TrendSeries['points']) => {
    let d = '';
    let pen = false;
    for (const p of pts) {
      if (p.v == null) { pen = false; continue; }
      d += `${pen ? 'L' : 'M'}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`;
      pen = true;
    }
    return d;
  };
  const valueAt = (s: TrendSeries, t: number) => s.points.find((p) => p.t === t)?.v ?? null;
  const last = (s: TrendSeries) => [...s.points].reverse().find((p) => p.v != null);

  const move = (clientX: number, box: DOMRect) => {
    const t = start + ((clientX - box.left - PAD_L) / (plotR - PAD_L)) * (end - start);
    setAt(times.length ? nearest(Math.min(Math.max(t, start), end)) : null);
  };
  const onKey = (e: React.KeyboardEvent) => {
    if (!times.length) return;
    const i = at == null ? times.length - 1 : times.indexOf(at);
    if (e.key === 'ArrowLeft') { setAt(times[Math.max(0, i - 1)]!); e.preventDefault(); }
    if (e.key === 'ArrowRight') { setAt(times[Math.min(times.length - 1, i + 1)]!); e.preventDefault(); }
    if (e.key === 'Escape') setAt(null);
  };
  const tipLeft = at == null ? 0 : Math.min(Math.max(x(at) + 12, 0), Math.max(0, width - 200));

  return (
    <figure className="chart trend" ref={ref} data-testid={testid}>
      <figcaption className="trend-title">{title}</figcaption>
      <svg width={width} height={H} role="img" tabIndex={0} aria-label={`${title} over time${reference ? `, ${reference.label}` : ''}. Use the arrow keys to read values; the table below lists them.`}
        onPointerMove={(e) => move(e.clientX, e.currentTarget.getBoundingClientRect())} onPointerLeave={() => setAt(null)}
        onFocus={() => setAt(times[times.length - 1] ?? null)} onBlur={() => setAt(null)} onKeyDown={onKey}>
        <g className="grid">{ticks.map((t) => <line key={t} x1={PAD_L} x2={plotR} y1={y(t)} y2={y(t)} />)}</g>
        {ticks.map((t) => <text key={t} className="tick" x={PAD_L - 6} y={y(t) + 4} textAnchor="end">{format(t)}</text>)}
        <line className="baseline" x1={PAD_L} x2={plotR} y1={y(0)} y2={y(0)} />
        {timeTicks(start, end, Math.max(2, Math.floor((plotR - PAD_L) / 90))).map((t) => <text key={t} className="tick" x={x(t)} y={H - 6} textAnchor="middle">{clock(t)}</text>)}
        {mergeMarkers(markers.filter((m) => m.t >= start && m.t <= end), x).map((m) => (
          <g key={`${m.label}${m.t}`}>
            <line className="ref" x1={x(m.t)} x2={x(m.t)} y1={PAD_T} y2={y(0)} />
            <text className="ref-label" x={x(m.t) + 4} y={PAD_T + 10}>{m.label}</text>
          </g>))}
        {reference && (
          <g>
            <line className="gate" x1={PAD_L} x2={plotR} y1={y(reference.value)} y2={y(reference.value)} />
            <text className="ref-label" x={plotR + 4} y={y(reference.value) + 4}>{reference.label}</text>
          </g>)}
        {series.map((s) => <path key={s.key} d={path(s.points)} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />)}
        {series.length > 1 && !endsCollide(series.map(last), y) && series.map((s) => {
          const l = last(s);
          return l && <text key={s.key} className="tick end-label" x={x(l.t) + 6} y={y(l.v!) + 4}>{s.short}</text>;
        })}
        {at != null && (
          <g>
            <line className="crosshair" x1={x(at)} x2={x(at)} y1={PAD_T} y2={y(0)} />
            {series.map((s) => {
              const v = valueAt(s, at);
              return v == null ? null : <circle key={s.key} cx={x(at)} cy={y(v)} r={4} fill={s.color} stroke="var(--surface)" strokeWidth={2} />;
            })}
          </g>)}
      </svg>
      {at != null && (
        <div className="tooltip" role="status" style={{ left: tipLeft, top: 24 }}>
          <div className="t-head">{new Date(at).toLocaleString()}</div>
          {series.map((s) => {
            const v = valueAt(s, at);
            return (
              <div className="t-row" key={s.key}><span className="k" style={{ background: s.color }} /><b>{v == null ? 'no data' : format(v)}</b><span className="lbl">{s.label}</span></div>);
          })}
        </div>)}
      {series.length > 1 && <Legend items={series.map((s) => ({ color: s.color, label: s.label, line: true }))} />}
    </figure>
  );
}
