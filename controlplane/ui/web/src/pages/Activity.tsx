import { api, enc, type S } from '../api/client';
import { CopyButton, Empty, Time } from '../components/bits';
import { describeAction, entityHref, isFailure, isRoutine } from '../lib/audit';
import { useCrumbs } from '../lib/chrome';
import { routes } from '../lib/format';
import { QueryView, useLiveQuery } from '../lib/query';
import { useSearchState } from '../lib/search';

const LIMIT = 500;

/** The audit trail: who changed what, when, and the trace it happened in. */
export function ActivityPage({ project }: { project: string }) {
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: project, href: routes.project(project) }, { label: 'Activity' }]);
  const p = enc(project);
  const [{ type, q, problems, all }, update] = useSearchState({ type: '', q: '', problems: '', all: '' });

  const query = useLiveQuery(['activity', project], async () => {
    const [audit, deployments, models] = await Promise.all([
      api.get<S['AuditOut']>(`/projects/${p}/audit?limit=${LIMIT}`),
      api.get<S['DeploymentList']>(`/projects/${p}/deployments`),
      api.get<S['ModelList']>(`/projects/${p}/models`),
    ]);
    const names = new Map<string, string>([...deployments.items, ...models.items].map((x) => [x.id, x.name]));
    return { events: audit.items, names };
  }, () => true, 10_000);

  return (
    <QueryView query={query}>
      {({ events, names }) => {
        const types = [...new Set(events.map((e) => e.entity_type))].sort();
        const needle = q.toLowerCase();
        const routine = events.filter(isRoutine).length;
        const shown = events.filter((e) => (all || type || !isRoutine(e)) && (!type || e.entity_type === type) && (!problems || isFailure(e.action))
          && (!needle || `${e.action} ${describeAction(e.action)} ${e.actor} ${e.trace_id ?? ''} ${e.entity_id} ${JSON.stringify(e.payload)}`.toLowerCase().includes(needle)));
        const days = new Map<string, S['AuditEventOut'][]>();
        shown.forEach((e) => {
          const day = new Date(e.occurred_at).toLocaleDateString(undefined, { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
          (days.get(day) ?? days.set(day, []).get(day)!).push(e);
        });
        return (
          <>
            <div className="page-head"><h1>Activity</h1></div>
            <p className="sub">{`Every state change in this project, newest first (last ${LIMIT}). Paste a trace id into your tracing tool to see the whole request.`}</p>
            <div className="toolbar">
              <input type="search" className="grow" placeholder="Search actions, actors, ids, trace ids…  ( / )" aria-label="Search activity" data-search
                data-testid="activity-search" value={q} onChange={(e) => update({ q: e.target.value })} />
              <label className="inline">Kind
                <select value={type} data-testid="activity-type" onChange={(e) => update({ type: e.target.value })}>
                  <option value="">Everything</option>
                  {types.map((t) => <option key={t} value={t}>{t.replace('_', ' ')}</option>)}
                </select>
              </label>
              <label className="inline"><input type="checkbox" checked={Boolean(problems)} data-testid="activity-problems"
                onChange={(e) => update({ problems: e.target.checked ? '1' : '' })} />Problems only</label>
              <label className="inline" title="Per-step progress, canary ticks and state hand-offs"><input type="checkbox" checked={Boolean(all)} data-testid="activity-all"
                onChange={(e) => update({ all: e.target.checked ? '1' : '' })} />{`Include routine (${routine})`}</label>
            </div>
            {shown.length === 0 ? <Empty>{events.length ? 'Nothing matches these filters.' : 'No activity recorded yet.'}</Empty> : (
              [...days].map(([day, list]) => (
                <div className="section" key={day}>
                  <h3 className="day">{day}</h3>
                  <ul className="timeline audit" data-testid="activity-list">
                    {list.map((e) => {
                      const href = entityHref(project, e, names);
                      return (
                        <li key={e.id} className={isFailure(e.action) ? 'bad' : ''} data-action={e.action}>
                          <time dateTime={e.occurred_at} title={new Date(e.occurred_at).toLocaleString()}>{new Date(e.occurred_at).toLocaleTimeString()}</time>
                          <span className="what">{href ? <a href={href}>{describeAction(e.action)}</a> : describeAction(e.action)}
                            <Details payload={e.payload} /></span>
                          <span className="muted small">{e.actor}</span>
                          <span className="trace">{e.trace_id ? <>{e.trace_id.slice(0, 8)}<CopyButton text={e.trace_id} what="trace id" /></> : null}</span>
                        </li>);
                    })}
                  </ul>
                </div>)))}
            <p className="muted small">{`Showing ${shown.length} of ${events.length} events. Most recent `}<Time iso={events[0]?.occurred_at} />.</p>
          </>
        );
      }}
    </QueryView>
  );
}

/** The interesting part of an event's payload, inline: reason, from → to, revision, ... */
function Details({ payload }: { payload: Record<string, unknown> }) {
  const parts: string[] = [];
  if (payload.from && payload.to) parts.push(`${payload.from} → ${payload.to}`);
  for (const key of ['reason', 'revision', 'version', 'canary_percent', 'exit_code']) {
    const v = payload[key];
    if (v != null && typeof v !== 'object') parts.push(key === 'reason' ? String(v) : `${key.replace('_', ' ')} ${v}`);
  }
  return parts.length ? <span className="muted small detail">{parts.join(' · ')}</span> : null;
}
