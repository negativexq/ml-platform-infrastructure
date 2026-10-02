/** What a team asks of a pipeline or job: does it usually work, and how long does it take? */
export type RunLike = { status: string; duration_seconds?: number | null; created_at: string; finished_at?: string | null };

export function runStats(runs: RunLike[]) {
  const finished = runs.filter((r) => ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(r.status));
  const succeeded = finished.filter((r) => r.status === 'SUCCEEDED');
  const durations = succeeded.map((r) => r.duration_seconds).filter((d): d is number => d != null).sort((a, b) => a - b);
  const median = durations.length ? durations[Math.floor((durations.length - 1) / 2)] ?? null : null;
  return {
    total: runs.length,
    finished: finished.length,
    successRate: finished.length ? succeeded.length / finished.length : null,
    medianSeconds: median,
    last: runs[0] ?? null,
    lastSuccess: succeeded[0] ?? null,
    lastFailure: finished.find((r) => r.status === 'FAILED') ?? null,
  };
}
