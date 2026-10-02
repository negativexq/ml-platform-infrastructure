import { Legend, Tooltip, useTip, useWidth } from './base';

/** `ink`: the text colour for a label inside this part, chosen by the fill's luminance. */
export type Share = { key: string; label: string; detail: string; percent: number; color: string; ink: string };

const H = 20, GAP = 2;

/**
 * Where traffic goes right now: one stacked bar, a 2px surface gap between parts, rounded
 * only at the outer ends. Series colours follow the role (stable is always slot 1, canary
 * always slot 2), never the order on screen. A label sits inside a part only when it fits.
 */
export function TrafficSplit({ shares }: { shares: Share[] }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const { tip, bind } = useTip();
  const parts = shares.filter((s) => s.percent > 0);
  const usable = Math.max(0, width - GAP * Math.max(0, parts.length - 1));
  let x = 0;
  return (
    <figure className="chart" ref={ref} data-testid="traffic-split">
      <svg width={width} height={H} role="img" aria-label={`Traffic: ${shares.map((s) => `${s.label} ${s.percent}%`).join(', ')}`}>
        <defs>
          <clipPath id="traffic-round"><rect x={0} y={0} width={width} height={H} rx={4} /></clipPath>
        </defs>
        <g clipPath="url(#traffic-round)">
          {parts.map((s) => {
            const w = Math.max(0, (s.percent / 100) * usable);
            const at = x;
            x += w + GAP;
            const text = `${s.label} ${s.percent}%`;
            const fits = text.length * 6.6 + 16 < w;
            return (
              <g key={s.key} className="mark" tabIndex={0} role="img" aria-label={`${s.label}: ${s.percent}% of traffic, ${s.detail}`}
                data-testid={`${s.key}-share`} {...bind({ head: s.detail, rows: [{ value: `${s.percent}%`, label: s.label, color: s.color }] }, { x: at + w / 2, y: 0 })}>
                <rect x={at} y={0} width={w} height={H} fill={s.color} />
                {fits && <text x={at + 8} y={H / 2 + 4} style={{ fill: s.ink, font: '600 12px system-ui, sans-serif' }}>{text}</text>}
                <title>{text}</title>
              </g>);
          })}
        </g>
      </svg>
      <Tooltip tip={tip} width={width} />
      <Legend items={shares.map((s) => ({ color: s.color, label: `${s.label} ${s.percent}%, ${s.detail}` }))} />
    </figure>
  );
}
