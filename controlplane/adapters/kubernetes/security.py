"""Platform-owned Pod Security restricted settings; return fresh dictionaries."""

from typing import Any

TRAINING_ACCOUNT = "mlp-training"
SERVING_ACCOUNT = "mlp-serving"


def pod_security() -> dict[str, Any]:
    return {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}


def container_security() -> dict[str, Any]:
    return {
        **pod_security(),
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }
