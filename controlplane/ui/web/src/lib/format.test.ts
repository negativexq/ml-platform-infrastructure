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

import { parsePairs, splitCommand } from './format';

describe('command and env parsing', () => {
  it('splits like a shell for simple cases', () => {
    expect(splitCommand('python -m train --name "credit risk" --x \'\'')).toEqual(['python', '-m', 'train', '--name', 'credit risk', '--x', '']);
    expect(() => splitCommand('echo "oops')).toThrow();
  });
  it('reads KEY=value lines', () => {
    expect(parsePairs('# c\nA=1\n\nB = two=2\n')).toEqual({ A: '1', B: 'two=2' });
    expect(() => parsePairs('nope')).toThrow();
  });
});

import { formatThresholds, parseThresholds } from './format';

describe('thresholds', () => {
  it('round-trips', () => {
    const t = parseThresholds('auc >= 0.9\nrmse <= 0.3\nauc <= 0.999\n');
    expect(t).toEqual({ auc: { min: 0.9, max: 0.999 }, rmse: { max: 0.3 } });
    expect(parseThresholds(formatThresholds(t))).toEqual(t);
    expect(() => parseThresholds('auc > 0.9')).toThrow();
  });
});
