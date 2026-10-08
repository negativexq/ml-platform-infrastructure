import { useQuery } from '@tanstack/react-query';
import { api, type S } from '../api/client';
import { routes } from './format';
import { useMe } from './me';

export type Notification = S['NotificationOut'];
/** Home and the header share one authoritative, permission-scoped lifecycle inbox. */
export function useNotifications() {
  const me = useMe();
  return useQuery({
    queryKey: ['notifications', me.data?.username, me.data?.roles],
    queryFn: () => api.get<S['NotificationList']>('/me/notifications'),
    enabled: Boolean(me.data), refetchInterval: 15_000, retry: false,
  });
}

export function notificationHref(n: Notification) {
  if (n.resource_type === 'pipeline_run') return routes.pipelineRun(n.project, n.resource_id);
  if (n.resource_type === 'monitoring_report') return `${routes.project(n.project)}/model-monitoring/reports/${n.resource_id}`;
  if (n.resource_type === 'run') return routes.jobRun(n.project, n.resource_id);
  if (n.resource_type === 'deployment') return routes.deployment(n.project, n.resource_name);
  if (n.resource_type === 'endpoint') return routes.endpoint(n.project, n.resource_name);
  return routes.project(n.project);
}
