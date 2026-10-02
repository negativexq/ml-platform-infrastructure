import { describe, expect, it } from 'vitest';
import { runStats } from './stats';

const run = (status: string, duration: number | null = null) => ({ status, duration_seconds: duration, created_at: '2026-01-01T00:00:00Z' });

describe('runStats', () => {
  it('ignores runs still in flight for the success rate', () => {
    const s = runStats([run('RUNNING'), run('SUCCEEDED', 10), run('FAILED', 3), run('SUCCEEDED', 30), run('SUCCEEDED', 20)]);
    expect(s.successRate).toBe(0.75);
    expect(s.medianSeconds).toBe(20);
    expect(s.last?.status).toBe('RUNNING');
    expect(s.lastFailure?.status).toBe('FAILED');
  });
  it('has no rate before anything finished', () => expect(runStats([run('PENDING')]).successRate).toBeNull());
});
