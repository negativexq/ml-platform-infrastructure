import { useQuery } from '@tanstack/react-query';
import { api, type S } from '../api/client';
import { num, pct } from './format';

export type Health = S['Health'];

/** Health as the status palette: reserved colours, always with a symbol and a word. */
export const HEALTH: Record<Health, { color: string; symbol: string; word: string }> = {
  ok: { color: 'var(--status-good)', symbol: '✓', word: 'Healthy' },
  warning: { color: 'var(--status-warning)', symbol: '▲', word: 'Warning' },
  critical: { color: 'var(--status-critical)', symbol: '✕', word: 'Critical' },
  no_data: { color: 'var(--status-neutral)', symbol: '○', word: 'No data' },
};

export const SEVERITY: Record<Health, number> = { critical: 3, warning: 2, no_data: 1, ok: 0 };

/** The platform's health over the last `minutes`; the sidebar and the Monitor page share it. */
export const usePlatformHealth = (minutes: number, refetchInterval = 30_000) => useQuery({
  queryKey: ['platform-health', minutes],
  queryFn: () => api.get<S['PlatformHealthOut']>(`/platform/health?minutes=${minutes}`),
  refetchInterval,
  retry: false,
});

export function formatUnit(unit: string, v: number | null | undefined): string {
  if (v == null) return '—';
  if (unit === 'ratio') return pct(v, v !== 0 && v < 0.01 ? 2 : 1);
  if (unit === 'ms') return `${num(v, 0)} ms`;
  if (unit === 'req/s') return `${num(v, v < 10 ? 1 : 0)} req/s`;
  return `${num(v, 1)}${unit}`;
}

/** "warn above 1%, critical above 5%": the rule a value is judged by, in words. */
export function thresholdText(s: Pick<S['SignalHealthOut'], 'unit' | 'warn' | 'critical' | 'lower_is_worse'>): string {
  const side = s.lower_is_worse ? 'below' : 'above';
  const parts = [
    s.warn != null ? `warn ${side} ${formatUnit(s.unit, s.warn)}` : null,
    s.critical != null ? `critical ${side} ${formatUnit(s.unit, s.critical)}` : null,
  ].filter(Boolean);
  return parts.length ? parts.join(', ') : 'none (context only)';
}

/** "pipeline_runs" reads as "pipeline runs"; an ungrouped signal is about the whole platform. */
export const scopeName = (name: string) => (name ? name.replace(/_/g, ' ') : 'all');
