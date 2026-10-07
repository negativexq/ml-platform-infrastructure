import { describe, expect, it } from 'vitest';
import { parseLog, parseLogs } from './log-format';

describe('readable log events', () => {
  it('normalizes structlog behind Kubernetes timestamps and preserves diagnostic fields', () => {
    const raw = '2026-10-07T18:00:00.123456789Z {"level":"warning","service":"trainer","event":"Missing optional metadata","run_id":"42","nested":{"safe":"<script>"}}';
    const entry = parseLog(raw, 1);
    expect(entry.level).toBe('WARN');
    expect(entry.error).toBe(false);
    expect(entry.source).toBe('trainer');
    expect(entry.message).toBe('Missing optional metadata');
    expect(entry.timestamp).toBe('2026-10-07T18:00:00.123456789Z');
    expect(entry.fields).toEqual({ run_id: '42', nested: { safe: '<script>' } });
    expect(entry.raw).toBe(raw);
  });
  it('turns the repeated MLflow Git wall into one short warning with full details', () => {
    const body = 'Failed to import Git (the Git executable is probably not on your PATH). Error: Bad git executable.\nThe git executable must be specified.\nUse the GIT_PYTHON_GIT_EXECUTABLE environment variable.';
    const entries = parseLogs(`2026/10/07 16:35:05 WARNING mlflow.utils.git_utils: ${body}\n2026/10/07 16:35:06 WARNING mlflow.utils.git_utils: ${body}\nrun_id=42\nmetrics={r2:0.99}\n`);
    expect(entries).toHaveLength(3);
    expect(entries[0]?.repeat).toBe(2);
    expect(entries[0]?.message).toBe('Git is unavailable; commit metadata could not be recorded.');
    expect(entries[0]?.detail).toContain('GIT_PYTHON_GIT_EXECUTABLE');
    expect(entries[0]?.level).toBe('WARN');
    expect(entries[0]?.error).toBe(false);
    expect(entries[1]?.message).toBe('run_id=42');
  });
  it('puts the exception cause first and keeps the traceback in details', () => {
    const entries = parseLogs('Traceback (most recent call last):\n  File "train.py", line 2\n    row["income_band"]\nKeyError: income_band\n');
    expect(entries).toHaveLength(1);
    expect(entries[0]?.message).toBe('KeyError: income_band');
    expect(entries[0]?.detail).toContain('File "train.py"');
    expect(entries[0]?.error).toBe(true);
  });
  it('groups Git SHA, branch and repository diagnostics without losing their details', () => {
    const text = ['SHA', 'branch', 'repository URL'].map((name) =>
      `2026/10/07 16:35:05 WARNING mlflow.utils.git_utils: Failed to import Git, so Git ${name} is not available. Error: Bad git executable.\nThe git executable must be specified.\n`).join('\n');
    const entries = parseLogs(text);
    expect(entries).toHaveLength(1);
    expect(entries[0]?.repeat).toBe(3);
    expect(entries[0]?.detail).toContain('Git SHA');
    expect(entries[0]?.detail).toContain('Git branch');
    expect(entries[0]?.detail).toContain('Git repository URL');
  });
  it('keeps normal stdout and distinct structured fields as separate events', () => {
    const entries = parseLogs('INFO ready\nplain output\n{"level":"info","event":"step","step":1}\n{"level":"info","event":"step","step":2}');
    expect(entries).toHaveLength(4);
    expect(entries[1]?.level).toBe('LOG');
  });
  it('caps a visible message but preserves the entire diagnostic', () => {
    const entry = parseLogs('x'.repeat(1000))[0]!;
    expect(entry.message.length).toBe(238);
    expect(entry.detail.length).toBe(1000);
  });
  it('shows a successful Argo logfmt exit as INFO instead of a false error', () => {
    const entry = parseLog('time="2026-10-07T16:36:07.640Z" level=info msg="sub-process exited" argo=true error="<nil>"', 1);
    expect(entry.level).toBe('INFO');
    expect(entry.error).toBe(false);
    expect(entry.source).toBe('argo');
    expect(entry.message).toBe('sub-process exited');
    expect(entry.fields).toEqual({ argo: 'true', error: '<nil>' });
  });
});
