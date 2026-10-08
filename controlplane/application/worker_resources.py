"""Disk reservations for managed workers, including retained files and scratch space."""

import re
from collections.abc import Mapping
from decimal import ROUND_CEILING, Decimal

from controlplane.application.namespaces import DEFAULT_LIMITS, DEFAULT_QUOTA, DEFAULT_REQUESTS
from controlplane.domain.errors import InvalidArgument

MIB = 1024**2
# Runtime files, logs, result metadata and filesystem overhead. This is a reservation,
# not a guarantee against node pressure; kubelet enforcement remains authoritative.
SCRATCH_BYTES = 512 * MIB


def storage_bytes(quantity: str) -> int:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|k|M|G|T|m)?", quantity)
    if not match:
        raise InvalidArgument("invalid ephemeral-storage quantity")
    factors = {
        "": 1,
        "m": Decimal("0.001"),
        "k": 1000,
        "M": 1000**2,
        "G": 1000**3,
        "T": 1000**4,
        "Ki": 1024,
        "Mi": MIB,
        "Gi": 1024**3,
        "Ti": 1024**4,
    }
    value = Decimal(match[1]) * factors[match[2] or ""]
    if value <= 0:
        raise InvalidArgument("ephemeral-storage must be positive")
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def disk_requirement(*, batch: Mapping | None = None, monitoring: Mapping | None = None) -> int:
    if batch:
        retained = 2 * batch["max_bytes"] + batch["max_model_bytes"]
    elif monitoring:
        feedback = bool(monitoring.get("feedback_dataset_id"))
        # SQLite rollback journal can coexist with the full bounded database.
        retained = (3 if feedback else 2) * monitoring["max_bytes"]
        retained += 2 * monitoring["max_join_bytes"] if feedback else 0
    else:
        raise InvalidArgument("managed worker specification required")
    headroom = max(SCRATCH_BYTES, (retained + 9) // 10)
    return ((retained + headroom + MIB - 1) // MIB) * MIB


def worker_resources(
    resources: Mapping[str, str] | None,
    *,
    batch: Mapping | None = None,
    monitoring: Mapping | None = None,
) -> dict[str, str]:
    result = dict(resources or {"cpu": "1", "memory": "2Gi"})
    required = disk_requirement(batch=batch, monitoring=monitoring)
    requested = (
        storage_bytes(result["ephemeral-storage"]) if "ephemeral-storage" in result else required
    )
    if requested < required:
        raise InvalidArgument(f"ephemeral-storage must reserve at least {required // MIB}Mi")
    # Argo's wait container runs alongside main; init runs before them. Reject a
    # single pod that cannot fit even an otherwise empty project quota.
    if requested + storage_bytes(DEFAULT_REQUESTS["ephemeral-storage"]) > storage_bytes(
        DEFAULT_QUOTA["requests.ephemeral-storage"]
    ) or requested + storage_bytes(DEFAULT_LIMITS["ephemeral-storage"]) > storage_bytes(
        DEFAULT_QUOTA["limits.ephemeral-storage"]
    ):
        raise InvalidArgument(
            "worker ephemeral-storage exceeds the project quota including executor"
        )
    if "ephemeral-storage" not in result:
        result["ephemeral-storage"] = f"{required // MIB}Mi"
    return result
