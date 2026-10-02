import { describe, expect, it } from 'vitest';
import { fmtAgo, fmtDuration, score } from './format';

describe('score', () => {
  it('finds a subsequence', () => expect(score('crprod', 'credit-risk-prod')).not.toBeNull());
  it('rejects letters out of order', () => expect(score('dorp', 'prod')).toBeNull());
  it('prefers a substring over a scattered match', () =>
    expect(score('prod', 'credit-risk-prod')!).toBeLessThan(score('cdt', 'credit-risk-prod')! + 100));
});

describe('format', () => {
  it('durations', () => {
    expect(fmtDuration(null)).toBe('—');
    expect(fmtDuration(59)).toBe('59s');
    expect(fmtDuration(63)).toBe('1m 03s');
    expect(fmtDuration(3900)).toBe('1h 05m');
  });
  it('relative time', () => {
    const now = Date.parse('2026-01-01T12:00:00Z');
    expect(fmtAgo('2026-01-01T11:59:50Z', now)).toBe('just now');
    expect(fmtAgo('2026-01-01T11:00:00Z', now)).toBe('1h ago');
    expect(fmtAgo(null, now)).toBe('—');
  });
});
