"""Backend routes shared by management inference and the public gateway."""

from collections.abc import Mapping


def prediction_path(name: str, payload: object) -> str:
    # MLflow's JSON handler accepts instances/predictions; V2 accepts tensors.
    if isinstance(payload, Mapping) and "instances" in payload:
        return "/invocations"
    return f"/v2/models/{name}/infer"
