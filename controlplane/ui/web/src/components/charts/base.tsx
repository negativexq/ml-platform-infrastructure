import { useEffect, useRef, useState, type ReactNode } from 'react';

/** Width of a container, kept current, so charts fill their card at any size. */
export function useWidth<T extends HTMLElement>(fallback = 600): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(el.clientWidth || fallback);
    // A hidden or collapsing container reports 0: keep the last real width rather than draw at 0.
    const observer = new ResizeObserver(([entry]) => entry && entry.contentRect.width > 0 && setWidth(entry.contentRect.width));
    observer.observe(el);
    return () => observer.disconnect();
  }, [fallback]);
  return [ref, width];
}

export type TipRow = { value: string; label?: string; color?: string };
export type Tip = { x: number; y: number; head?: string; rows: TipRow[] } | null;

/** One tooltip per chart: the value leads, the name follows, a short line keys the series.
 * Content is React text, so names from the API can never become markup. */
export function Tooltip({ tip, width }: { tip: Tip; width: number }) {
  if (!tip) return null;
  const left = Math.min(Math.max(tip.x + 12, 0), Math.max(0, width - 200));
  return (
    <div className="tooltip" role="status" style={{ left, top: Math.max(0, tip.y - 8) }}>
      {tip.head && <div className="t-head">{tip.head}</div>}
      {tip.rows.map((r, i) => (
        <div className="t-row" key={i}>
          {r.color && <span className="k" style={{ background: r.color }} />}
          <b>{r.value}</b>{r.label && <span className="lbl">{r.label}</span>}
        </div>))}
    </div>
  );
}

/** Pointer and keyboard both open the tooltip: focus shows what hover shows. */
export function useTip() {
  const [tip, setTip] = useState<Tip>(null);
  const bind = (content: Omit<NonNullable<Tip>, 'x' | 'y'>, anchor: { x: number; y: number }) => ({
    onPointerMove: (e: React.PointerEvent<SVGElement>) => {
      const box = (e.currentTarget.ownerSVGElement ?? (e.currentTarget as unknown as SVGSVGElement)).getBoundingClientRect();
      setTip({ ...content, x: e.clientX - box.left, y: e.clientY - box.top });
    },
    onPointerLeave: () => setTip(null),
    onFocus: () => setTip({ ...content, ...anchor }),
    onBlur: () => setTip(null),
  });
  return { tip, bind };
}

/** "Nice" tick values for 0..max: 1, 2 or 5 times a power of ten (or of 60 for durations). */
export function niceTicks(max: number, count = 4, base: 'decimal' | 'seconds' = 'decimal'): number[] {
  if (!(max > 0)) return [0];
  const steps = base === 'seconds'
    ? [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 28800, 86400]
    : [1, 2, 5].flatMap((m) => [-3, -2, -1, 0, 1, 2, 3, 4, 5, 6].map((p) => m * 10 ** p)).sort((a, b) => a - b);
  const step = steps.find((s) => max / s <= count) ?? steps[steps.length - 1]!;
  const ticks: number[] = [];
  for (let v = 0; v <= max + step * 0.0001; v += step) ticks.push(Number(v.toFixed(10)));
  if (ticks[ticks.length - 1]! < max) ticks.push(ticks[ticks.length - 1]! + step);
  return ticks;
}

/** A column whose data end is rounded (4px) and whose baseline end is square. */
export function columnPath(x: number, y: number, w: number, h: number, r = 4): string {
  const rr = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + rr}Q${x},${y} ${x + rr},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${y + h}Z`;
}

export const Legend = ({ items }: { items: { label: ReactNode; color: string; line?: boolean }[] }) => (
  <div className="legend">{items.map((it, i) => (
    <span key={i}><i className={it.line ? 'line' : ''} style={{ background: it.color }} />{it.label}</span>))}
  </div>
);

/** Run state as the status palette: never a series colour, always with a symbol and a word. */
export const STATUS: Record<string, { color: string; symbol: string; word: string }> = {
  SUCCEEDED: { color: 'var(--status-good)', symbol: '✓', word: 'succeeded' },
  FAILED: { color: 'var(--status-critical)', symbol: '✕', word: 'failed' },
  CANCELLED: { color: 'var(--status-neutral)', symbol: '–', word: 'cancelled' },
  SKIPPED: { color: 'var(--status-neutral)', symbol: '–', word: 'skipped' },
  RUNNING: { color: 'var(--run)', symbol: '●', word: 'running' },
  SUBMITTED: { color: 'var(--run)', symbol: '●', word: 'submitted' },
  PENDING: { color: 'var(--status-neutral)', symbol: '○', word: 'pending' },
};
export const statusOf = (s: string) => STATUS[s] ?? { color: 'var(--status-neutral)', symbol: '•', word: s.toLowerCase() };
