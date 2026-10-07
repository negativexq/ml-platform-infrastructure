"""Prepare release artifacts without invoking kubectl or starting any service."""

import hashlib
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml  # type: ignore[import-untyped]

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/controlplane_bootstrap.py"
spec = importlib.util.spec_from_file_location("bootstrap_script", SCRIPT)
assert spec is not None and spec.loader is not None
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
IMAGE = "ghcr.io/example/platform@sha256:" + "a" * 64
REVISION = "b" * 40


def files(tmp_path: Path) -> tuple[Path, Path, Path]:
    site = tmp_path / "site.yaml"
    site.write_text("config:\n  CP_AUTH_MODE: none\n")
    root = tmp_path / "repo"
    (root / "helm/controlplane").mkdir(parents=True)
    (root / "helm/controlplane/Chart.yaml").write_text("name: controlplane\nversion: 0.1.0\n")
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps(
            {
                "knative_serving_version": "1.15.2",
                "artifacts": [
                    {
                        "name": "gateway-api",
                        "file": "gateway-api.yaml",
                        "version": "v1.2.1",
                        "url": "https://example.invalid/gateway.yaml",
                        "sha256": hashlib.sha256(b"verified").hexdigest(),
                    }
                ],
            }
        )
    )
    return site, root, lock


def test_offline_bundle_pins_git_image_and_checks_before_mutation(tmp_path: Path) -> None:
    site, root, lock = files(tmp_path)
    calls: list[list[str]] = []
    out = tmp_path / "bundle"

    def fake_command(args: list[str]) -> str:
        calls.append(args)
        if args[:2] == ["helm", "package"]:
            (out / "controlplane-0.1.0.tgz").write_bytes(b"chart")
        if args[-1] == "HEAD":
            return REVISION
        return "" if args[0] == "git" else "apiVersion: v1\nkind: ConfigMap\n"

    with (
        patch.object(bootstrap, "ROOT", root),
        patch.object(bootstrap, "LOCK", lock),
        patch.object(bootstrap, "command", fake_command),
    ):
        script = bootstrap.prepare(
            out,
            site,
            IMAGE,
            REVISION,
            "chosen-context",
            mlflow_serving_image=IMAGE,
            s3_storage_initializer_image=IMAGE,
            migration_image=IMAGE,
        )
    assert all(c[0] in {"helm", "git"} for c in calls)
    app = yaml.safe_load((out / "application.yaml").read_text())
    assert app["spec"]["source"]["targetRevision"] == REVISION
    assert "automated" not in app["spec"]["syncPolicy"]
    values = yaml.safe_load((out / "values.yaml").read_text())
    assert values["image"]["digest"] == "sha256:" + "a" * 64
    assert values["migrations"]["image"]["digest"] == "sha256:" + "a" * 64
    serving = yaml.safe_load((out / "knative-serving.yaml").read_text())
    assert serving["spec"]["config"]["features"]["kubernetes.podspec-securitycontext"] == "enabled"
    plan = json.loads((out / "plan.json").read_text())
    assert plan["source_verified"] and not plan["artifacts_cached"]
    text = script.read_text()
    assert text.index("dependency checksum mismatch") < text.index("create namespace")
    assert text.index("helm version --short") < text.index("create namespace")
    assert "--context chosen-context" in text and "controlplane-0.1.0.tgz" in text
    runtime = yaml.safe_load((out / "mlflow-runtime.yaml").read_text())
    assert runtime["spec"]["containers"][0]["image"] == IMAGE
    assert runtime["spec"]["containers"][0]["securityContext"]["runAsUser"] == 1000
    assert all(f["priority"] > 1 for f in runtime["spec"]["supportedModelFormats"])
    assert text.index("mlflow-runtime.yaml") < text.index("upgrade --install mlp ")
    initializer = yaml.safe_load((out / "s3-storage-initializer.yaml").read_text())
    assert initializer["spec"]["supportedUriFormats"] == [{"prefix": "s3://"}]
    assert initializer["spec"]["container"]["image"] == IMAGE
    default_formats = json.loads((out / "default-storage-formats.json").read_text())
    assert {"prefix": "s3://"} not in default_formats["spec"]["supportedUriFormats"]
    assert {"prefix": "hf://"} in default_formats["spec"]["supportedUriFormats"]


def test_mutable_serving_image_is_rejected_before_commands(tmp_path: Path) -> None:
    site, _, _ = files(tmp_path)
    with patch.object(bootstrap, "command") as external:
        with pytest.raises(ValueError, match="MLflow serving image"):
            bootstrap.prepare(
                tmp_path / "out", site, IMAGE, REVISION, "ctx", mlflow_serving_image="server:latest"
            )
        external.assert_not_called()


@pytest.mark.parametrize("image,revision", [("platform:latest", REVISION), (IMAGE, "main")])
def test_mutable_release_inputs_are_rejected(tmp_path: Path, image: str, revision: str) -> None:
    site, _, _ = files(tmp_path)
    with patch.object(bootstrap, "command") as external:
        with pytest.raises(ValueError):
            bootstrap.prepare(tmp_path / "out", site, image, revision, "ctx")
        external.assert_not_called()


def test_corrupt_cache_fails_before_helm_render(tmp_path: Path) -> None:
    site, root, lock = files(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "gateway-api.yaml").write_bytes(b"tampered")
    with (
        patch.object(bootstrap, "ROOT", root),
        patch.object(bootstrap, "LOCK", lock),
        patch.object(bootstrap, "command") as external,
    ):
        with pytest.raises(RuntimeError, match="checksum"):
            bootstrap.prepare(tmp_path / "out", site, IMAGE, REVISION, "ctx", cache=cache)
        external.assert_not_called()


def test_dirty_checkout_produces_review_only_installer(tmp_path: Path) -> None:
    site, root, lock = files(tmp_path)
    out = tmp_path / "bundle"

    def fake_command(args: list[str]) -> str:
        if args[:2] == ["helm", "package"]:
            (out / "controlplane-0.1.0.tgz").write_bytes(b"chart")
        return REVISION if args[-1] == "HEAD" else " M changed.py" if args[0] == "git" else ""

    with (
        patch.object(bootstrap, "ROOT", root),
        patch.object(bootstrap, "LOCK", lock),
        patch.object(bootstrap, "command", fake_command),
    ):
        script = bootstrap.prepare(out, site, IMAGE, REVISION, "ctx")
    text = script.read_text()
    assert text.index("Review-only bundle") < text.index("kubectl")
    assert not json.loads((out / "plan.json").read_text())["source_verified"]
