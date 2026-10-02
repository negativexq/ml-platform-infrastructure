"""Instrumented provider ports (Argo, KServe, MLflow, Prometheus, Kubernetes).

Every call is **counted and timed**. Only *state-changing* calls get a **span** (submit,
deploy, apply, set alias, ...), because reads such as `get_status` run on every reconcile
pass: spanning them would flood the trace of the request that started the work with
identical polling spans. Reads stay visible as metrics and as errors.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Collection
from contextlib import nullcontext
from time import perf_counter
from typing import Any, cast

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from controlplane.application.context import bound_origin
from controlplane.domain.errors import DomainError
from controlplane.observability.metrics import record_provider_call
from controlplane.observability.tracing import parent_context

_TRACER = "controlplane.providers"

# The calls that change something outside the platform, per provider.
WORKFLOW_MUTATIONS = frozenset({"submit", "cancel"})
SERVING_MUTATIONS = frozenset({"deploy", "set_traffic", "delete", "predict"})
CLUSTER_MUTATIONS = frozenset({"apply", "delete"})
EXPERIMENT_MUTATIONS = frozenset({"ensure_experiment", "set_model_alias", "delete_model_alias"})
NO_MUTATIONS: frozenset[str] = frozenset()


class ObservedProvider:
    def __init__(self, inner: Any, name: str, traced: Collection[str]) -> None:
        self._inner = inner
        self._name = name
        self._traced = frozenset(traced)

    def __getattr__(self, attr: str) -> Any:
        target = getattr(self._inner, attr)
        if attr.startswith("_") or not callable(target):
            return target
        return self._wrap(attr, target)

    def _wrap(self, operation: str, target: Callable[..., Any]) -> Callable[..., Any]:
        provider, traced = self._name, operation in self._traced

        @functools.wraps(target)
        def call(*args: Any, **kwargs: Any) -> Any:
            started = perf_counter()
            span_cm: Any = nullcontext()
            if traced:
                # In a reconciler there is no active span; the entity's origin trace is the
                # parent. In an API request there is no origin and the active span is used.
                origin = bound_origin()
                span_cm = trace.get_tracer(_TRACER).start_as_current_span(
                    f"{provider}.{operation}",
                    context=parent_context(origin) if origin else None,
                    attributes={
                        "mlp.provider": provider,
                        "mlp.operation": operation,
                        **_target(args),
                    },
                    record_exception=False,
                    set_status_on_exception=False,
                )
            outcome = "ok"
            with span_cm as span:
                try:
                    return target(*args, **kwargs)
                except DomainError as exc:  # an expected answer ("not found"), not an outage
                    outcome = "not_found" if type(exc).__name__ == "NotFound" else "rejected"
                    raise
                except Exception as exc:
                    outcome = "error"
                    if span is not None:
                        span.record_exception(exc)
                        span.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
                    raise
                finally:
                    record_provider_call(provider, operation, outcome, perf_counter() - started)

        return call


def _target(args: tuple[Any, ...]) -> dict[str, str]:
    """What the call acts on, taken from its first argument (a ref string or a spec)."""
    if not args:
        return {}
    first = args[0]
    if isinstance(first, str):
        return {"mlp.target.ref": first[:200]}
    out = {}
    for attr in ("namespace", "name"):
        value = getattr(first, attr, None)
        if isinstance(value, str):
            out[f"mlp.target.{attr}"] = value
    return out


def observe[T](provider: T, name: str, traced: Collection[str]) -> T:
    """Wrap a provider; the result is used exactly like the original."""
    return cast(T, ObservedProvider(provider, name, traced))
