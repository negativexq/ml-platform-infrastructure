import { useEffect, useSyncExternalStore } from 'react';

/** Page chrome state that pages set and the layout shows: breadcrumbs and the "live" dot. */
export type Crumb = { label: string; href?: string };

type State = { crumbs: Crumb[]; live: number };
let state: State = { crumbs: [], live: 0 };
const listeners = new Set<() => void>();
const set = (next: State) => {
  state = next;
  listeners.forEach((l) => l());
};
const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => listeners.delete(l);
};

export const useChrome = () => useSyncExternalStore(subscribe, () => state);

/** Set the breadcrumbs (and document title) for as long as the page is mounted. */
export function useCrumbs(crumbs: Crumb[]) {
  const key = JSON.stringify(crumbs);
  useEffect(() => {
    const parts = JSON.parse(key) as Crumb[];
    set({ ...state, crumbs: parts });
    const last = parts[parts.length - 1];
    document.title = last ? `${last.label} · ML Platform` : 'ML Platform';
  }, [key]);
}

/** Show the pulsing "auto-refreshing" dot while `active`. */
export function useLive(active: boolean) {
  useEffect(() => {
    if (!active) return;
    set({ ...state, live: state.live + 1 });
    return () => set({ ...state, live: state.live - 1 });
  }, [active]);
}
