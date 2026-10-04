import { useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { ApiError } from '../api/client';
import { useOverlays } from '../components/overlays';
import { Empty, Skeleton } from '../components/bits';
import { useLive } from './chrome';

/**
 * Load, then reload every few seconds while `isActive(data)` says something is in flight.
 * Failures keep the last good data on screen with a banner, and retry with backoff.
 */
export function useLiveQuery<T>(
  key: readonly unknown[],
  load: () => Promise<T>,
  isActive: (data: T) => boolean = () => false,
  interval = 3000,
) {
  const query = useQuery({
    queryKey: key,
    queryFn: load,
    refetchInterval: (q) => (q.state.data !== undefined && isActive(q.state.data as T) ? interval : false),
    // Retry outages, never answers: a 4xx (not found, not allowed, signed out) will not change by asking again.
    retry: (count, error) => !(error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 429) && count < 50,
    retryDelay: (count) => Math.min(15000, 3000 * (count + 1)),
    staleTime: 0,
  });
  useLive(query.data !== undefined && isActive(query.data));
  return query;
}

/** Skeleton while loading, a not-found page for 404, a retry banner for anything else. */
export function QueryView<T>({ query, children }: { query: UseQueryResult<T>; children: (data: T) => ReactNode }) {
  const error = query.error;
  if (query.data === undefined) {
    if (error instanceof ApiError && error.status === 404) return <Empty>Not found: {error.message}</Empty>;
    if (error instanceof ApiError && error.status === 403) {
      return (
        <div className="empty no-access" data-testid="no-access">
          <b>You don't have access to this.</b>
          <p className="muted">{error.message}. Ask the project admin to review Settings → Members, or the platform admin for platform access.</p>
          <a href="#/help?topic=trouble">Access and permissions help</a>
        </div>);
    }
    if (error instanceof ApiError && error.status === 401) return <Skeleton />; // the sign-in screen takes over
    if (error) return <div className="alert bad net" role="alert">{error.message || 'Something went wrong'}. Retrying.</div>;
    return <Skeleton />;
  }
  return (
    <>
      {error && <div className="alert bad net" role="alert">{error.message || 'Something went wrong'}. Retrying.</div>}
      {children(query.data)}
    </>
  );
}

/** Run a mutating call, report the outcome, then refresh whatever is on screen. */
export function useAct() {
  const { toast } = useOverlays();
  const client = useQueryClient();
  return async (call: () => Promise<unknown>, success: string) => {
    try {
      await call();
      toast(success);
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Action failed', 'bad');
    }
    await client.invalidateQueries();
  };
}
