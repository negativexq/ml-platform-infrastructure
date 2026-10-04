import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api, type S } from '../api/client';
import { go, routes } from '../lib/format';
import { notificationHref, useNotifications, type Notification } from '../lib/notifications';
import { Badge, Empty, Time } from './bits';
import { Modal, useOverlays } from './overlays';

export function Notifications() {
  const [open, setOpen] = useState(false);
  const [unreadOnly, setUnreadOnly] = useState(false);
  const query = useNotifications();
  const client = useQueryClient();
  const { toast } = useOverlays();
  const mark = useMutation({
    mutationFn: (body: { ids?: string[]; all?: boolean }) => api.post<S['NotificationReadOut']>('/me/notifications/read', body),
    onSuccess: () => client.invalidateQueries({ queryKey: ['notifications'] }),
    onError: (error) => toast(`Could not mark notifications read: ${error.message}`, 'bad'),
  });
  const count = query.data?.unread_count;
  const items = query.data?.items.filter((n) => !unreadOnly || !n.read) ?? [];
  async function visit(n: Notification, href: string) {
    if (!n.read) { try { await mark.mutateAsync({ ids: [n.id] }); } catch { /* the inbox keeps it unread */ } }
    setOpen(false);
    go(href);
  }
  return <>
    <button className="btn small ghost notification-bell" type="button" data-testid="notification-bell"
      aria-label={`Notifications${query.isError ? ' (status unavailable)' : count == null ? ' (loading)' : ` (${count} unread)`}`} aria-haspopup="dialog" aria-expanded={open}
      title="Notifications" onClick={() => { if (!document.querySelector('dialog[open]')) setOpen(true); }}>
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4" /></svg>
      {count != null && count > 0 && !query.isError && <span className="notification-count" data-testid="notification-count">{count > 99 ? '99+' : count}</span>}
      {query.isError && <span aria-hidden="true">!</span>}
    </button>
    <Modal open={open} label="Notifications" className="notification-panel" onClose={() => setOpen(false)}>
      <div className="page-head"><h2>Notifications</h2><button className="btn small" type="button" onClick={(e) => e.currentTarget.closest('dialog')?.close()}>Close</button></div>
      <p className="muted small">Unresolved issues and the last 24 hours of failed runs and rollout outcomes. Reading an item does not resolve it.</p>
      <div className="toolbar">
        <div className="seg" role="group" aria-label="Notification filter"><button type="button" aria-pressed={!unreadOnly} onClick={() => setUnreadOnly(false)}>All</button><button type="button" aria-pressed={unreadOnly} onClick={() => setUnreadOnly(true)}>Unread</button></div>
        <button className="btn small" type="button" disabled={!count || mark.isPending || query.isError} onClick={() => mark.mutate({ all: true })}>Mark all read</button>
      </div>
      {query.isError && <div className="alert bad" role="alert">Notifications could not be refreshed. {query.error.message}<button className="btn small" type="button" onClick={() => query.refetch()}>Retry</button></div>}
      {!query.data && !query.isError && <p role="status">Loading notifications…</p>}
      {query.data?.truncated && <div className="alert">Showing up to 500 current issues and recent outcomes. Mark all read covers the full inbox.</div>}
      {items.length ? <ul className="notification-list" data-testid="notification-list">{items.map((n) => <li key={n.id} className={n.read ? 'read' : 'unread'} data-testid="notification-item" data-notification-id={n.id}>
        <a className="notification-title" href={notificationHref(n)} onClick={(e) => { if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return; e.preventDefault(); void visit(n, notificationHref(n)); }}>{!n.read && <span className="unread-dot" aria-label="Unread">●</span>}{n.title}</a>
        <div className="notification-meta"><span>{n.project_label}</span><Badge status={n.status} /><Time iso={n.occurred_at} /></div>
        {n.reason && <p className="muted small">{n.reason}</p>}
        {n.endpoint_name && <a href={routes.endpoint(n.project, n.endpoint_name)} onClick={(e) => { if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return; e.preventDefault(); void visit(n, routes.endpoint(n.project, n.endpoint_name!)); }}>Open unavailable endpoint</a>}
        {!n.read && <button className="btn small ghost" type="button" disabled={mark.isPending || query.isError} onClick={() => mark.mutate({ ids: [n.id] })}>Mark read</button>}
      </li>)}</ul> : query.data && !query.isError && <Empty>{unreadOnly ? 'You’re all caught up. No unread notifications.' : 'No notifications to show.'}</Empty>}
    </Modal>
  </>;
}
