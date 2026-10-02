import { useNavigate, useSearch } from '@tanstack/react-router';

/**
 * Page state kept in the URL (`#/projects/x/runs?status=failed`), so a filtered view can be
 * bookmarked, reloaded and pasted into a chat. Empty values are left out of the address.
 * Several keys change in one update, so "switch kind and clear the name" is a single navigation.
 */
export function useSearchState<K extends string>(defaults: Record<K, string>): [Record<K, string>, (patch: Partial<Record<K, string>>) => void] {
  const search = useSearch({ strict: false }) as Record<string, unknown>;
  const navigate = useNavigate();
  const values = Object.fromEntries(
    (Object.keys(defaults) as K[]).map((k) => [k, search[k] == null ? defaults[k] : String(search[k])]),
  ) as Record<K, string>;
  const update = (patch: Partial<Record<K, string>>) =>
    navigate({
      to: '.',
      replace: true,
      search: (prev: Record<string, unknown>) => {
        const out: Record<string, unknown> = { ...prev };
        for (const [k, v] of Object.entries(patch) as [K, string][]) {
          if (v === '' || v === defaults[k]) delete out[k];
          else out[k] = v;
        }
        return out;
      },
    } as never);
  return [values, update];
}
