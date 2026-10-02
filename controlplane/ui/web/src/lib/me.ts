import { useQuery } from '@tanstack/react-query';
import { api, type S } from '../api/client';

export type Role = 'invoker' | 'viewer' | 'operator' | 'admin';
const RANK: Record<Role, number> = { invoker: 0, viewer: 1, operator: 2, admin: 3 };

/** Who is signed in and their role in each project; refreshed when memberships change. */
export const useMe = () => useQuery({ queryKey: ['me'], queryFn: () => api.get<S['MeOut']>('/me'), retry: false, staleTime: 30_000 });

export function roleIn(me: S['MeOut'] | undefined, project: string): Role | null {
  if (!me) return null;
  if (me.platform_admin) return 'admin';
  return (me.roles[project] as Role | undefined) ?? null;
}

/**
 * `may('operator')`: can the signed-in person do this in the project? `why('operator')` is the
 * tooltip for a control that is shown but switched off, so it says what is missing.
 * Unknown (still loading) counts as allowed: the API is the authority and refuses anyway.
 */
export function useAccess(project: string) {
  const me = useMe();
  const role = roleIn(me.data, project);
  const may = (needed: Role) => me.data === undefined || (role !== null && RANK[role] >= RANK[needed]);
  const why = (needed: Role) => (may(needed) ? undefined : `Needs the ${needed} role in this project${role ? ` (you are ${role})` : ''}`);
  return { role, may, why };
}
