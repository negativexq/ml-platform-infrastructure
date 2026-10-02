import { num, pct } from '../../lib/format';

type Verdict = { color: string; symbol: string; word: string };
const WITHIN: Verdict = { color: 'var(--status-good)', symbol: '✓', word: 'within gate' };
const NEAR: Verdict = { color: 'var(--status-warning)', symbol: '!', word: 'near the limit' };
const OVER: Verdict = { color: 'var(--status-critical)', symbol: '✕', word: 'over the gate' };
const ENOUGH: Verdict = { color: 'var(--status-good)', symbol: '✓', word: 'enough traffic' };
const GATHERING: Verdict = { color: 'var(--status-neutral)', symbol: '○', word: 'gathering' };

/** A ceiling: lower is better, the gate fails above `limit`. */
export function ceiling(value: number | null | undefined, limit: number): { ratio: number; verdict: Verdict } {
  if (value == null) return { ratio: 0, verdict: GATHERING };
  const ratio = value / limit;
  return { ratio, verdict: ratio > 1 ? OVER : ratio > 0.8 ? NEAR : WITHIN };
}

/** A floor: the gate needs at least `min` before it may decide. */
export function floor(value: number | null | undefined, min: number): { ratio: number; verdict: Verdict } {
  const ratio = (value ?? 0) / Math.max(min, 1);
  return { ratio, verdict: ratio >= 1 ? ENOUGH : GATHERING };
}

/**
 * One value against one limit. The limit is a mark on the track, the fill is the value, and
 * the verdict is a symbol and a word, so state never rests on colour alone. The track runs to
 * 125% of the limit, so "a little over" is visible rather than pinned at the end.
 */
export function Meter({ label, value, limitText, ratio, verdict, testid }: {
  label: string; value: string; limitText: string; ratio: number; verdict: Verdict; testid?: string;
}) {
  const span = 1.25;
  const fill = Math.min(ratio, span) / span;
  return (
    <div className="meter" data-testid={testid} data-verdict={verdict.word}>
      <div className="meter-head">
        <span>{label}</span>
        <span className="status-chip">{`${verdict.symbol} ${verdict.word}`}</span>
      </div>
      <div className="meter-head"><b>{value}</b><span className="lim">{limitText}</span></div>
      <div className="meter-track" role="meter" aria-label={`${label}: ${value}, ${limitText}, ${verdict.word}`}
        aria-valuemin={0} aria-valuemax={span} aria-valuenow={Number(Math.min(ratio, span).toFixed(3))}>
        <div className="meter-fill" style={{ width: `${Math.max(fill * 100, value === '—' ? 0 : 1.5)}%`, background: verdict.color }} />
        <div className="meter-limit" style={{ left: `calc(${(1 / span) * 100}% - 1px)` }} title={limitText} />
      </div>
    </div>
  );
}

export type Gate = { max_error_rate: number; max_p95_latency_ms: number; min_requests: number };
export type RevisionMetrics = { revision: number; p95_latency_ms?: number | null; error_rate?: number | null; requests?: number | null };

/** What the canary gate sees for one revision: the three numbers it decides on, each against its limit. */
export function GateMeters({ m, gate }: { m: RevisionMetrics; gate: Gate }) {
  const err = ceiling(m.error_rate, gate.max_error_rate);
  const lat = ceiling(m.p95_latency_ms, gate.max_p95_latency_ms);
  const req = floor(m.requests, gate.min_requests);
  return (
    <div className="meters" data-testid="gate-meters">
      <Meter testid="meter-errors" label="5xx rate" value={pct(m.error_rate)} limitText={`gate ≤ ${pct(gate.max_error_rate)}`} {...err} />
      <Meter testid="meter-latency" label="p95 latency" value={m.p95_latency_ms == null ? '—' : `${num(m.p95_latency_ms, 0)} ms`}
        limitText={`gate ≤ ${num(gate.max_p95_latency_ms, 0)} ms`} {...lat} />
      <Meter testid="meter-requests" label="Requests this step" value={m.requests == null ? '—' : String(m.requests)}
        limitText={`needs ≥ ${gate.min_requests}`} {...req} />
    </div>
  );
}
