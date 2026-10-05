"""Shared SDK transport limits for every control-plane Kubernetes provider."""

from typing import Any

from kubernetes import client

REQUEST_TIMEOUT = (3, 20)  # connect / socket-read seconds; not a whole-reconcile deadline


class BoundedApiClient(client.ApiClient):  # type: ignore[misc]
    def __init__(self) -> None:
        configuration = client.Configuration.get_default_copy()
        # Hidden transport retries multiply latency and can repeat ambiguous mutations.
        configuration.retries = 0
        super().__init__(configuration=configuration)

    def call_api(self, *args: Any, **kwargs: Any) -> Any:
        # Generated SDK methods pass this by keyword; support positional callers too.
        if len(args) > 14:
            args = (*args[:14], args[14] or REQUEST_TIMEOUT, *args[15:])
        elif kwargs.get("_request_timeout") is None:
            kwargs["_request_timeout"] = REQUEST_TIMEOUT
        return super().call_api(*args, **kwargs)
