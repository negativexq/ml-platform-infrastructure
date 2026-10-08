from dataclasses import replace

from controlplane.domain.errors import AlreadyExists, Conflict


class MemoryMonitoringAutomation:
    def __init__(self, rules, events, executions):
        self._rules, self._events, self._executions = rules, events, executions

    def rule(self, project_id, id, *, lock=False):
        value = self._rules.get(id)
        return value if value and value.project_id == project_id else None

    def rules(self, project_id):
        return sorted(
            (r for r in self._rules.values() if r.project_id == project_id),
            key=lambda r: (r.created_at, r.id),
        )

    def add_rule(self, rule):
        if any(r.name == rule.name for r in self.rules(rule.project_id)):
            raise AlreadyExists("monitoring rule", rule.name)
        self._rules[rule.id] = rule

    def update_rule(self, rule, expected_revision):
        if self._rules[rule.id].revision != expected_revision:
            raise Conflict("monitoring rule changed")
        self._rules[rule.id] = rule

    def next_event(self):
        return min(
            (e for e in self._events.values() if e.processed_at is None),
            key=lambda e: (e.created_at, e.id),
            default=None,
        )

    def finish_event(self, id, now):
        self._events[id] = replace(self._events[id], processed_at=now)

    def add_execution(self, execution):
        if self.execution(execution.rule_id, execution.observed_dataset_id):
            raise AlreadyExists("monitoring execution", execution.id)
        self._executions[execution.id] = execution

    def update_execution(self, execution):
        self._executions[execution.id] = execution

    def execution(self, rule_id, dataset_id):
        return next(
            (
                e
                for e in self._executions.values()
                if e.rule_id == rule_id and e.observed_dataset_id == dataset_id
            ),
            None,
        )

    def executions(self, project_id, rule_id, limit, offset):
        values = sorted(
            (
                e
                for e in self._executions.values()
                if e.project_id == project_id and e.rule_id == rule_id
            ),
            key=lambda e: (e.created_at, e.id),
            reverse=True,
        )
        return values[offset : offset + limit]

    def next_waiting(self, now):
        return min(
            (
                e
                for e in self._executions.values()
                if e.status == "WAITING_FEEDBACK" and e.next_attempt_at <= now
            ),
            key=lambda e: (e.next_attempt_at, e.id),
            default=None,
        )
