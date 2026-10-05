"""Exercise manual acceptance orchestration without starting tools/services."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_gate_uses_one_image_and_cleans_only_its_fixtures(tmp_path: Path) -> None:
    release = load("controlplane_release_check")
    calls: list[list[str]] = []
    revision, image = "b" * 40, "sha256:" + "a" * 64

    def fake(args: list[str], **kwargs: Any) -> str:
        calls.append(args)
        if args[0] == "git":
            return revision if "rev-parse" in args else ""
        if args[:3] == ["docker", "image", "inspect"]:
            return image if "{{.Id}}" in args else revision
        if "{{.State.Running}}" in args:
            return "true"
        if args[:2] == ["docker", "logs"]:
            return "reconciler started"
        if args[:2] == ["docker", "save"]:
            Path(args[args.index("-o") + 1]).write_bytes(b"image")
        if "get_heads" in " ".join(args) or "SELECT version_num" in " ".join(args):
            return "0019"
        return ""

    with (
        patch.object(release.shutil, "which", return_value="/fake/tool"),
        patch.object(release, "run", side_effect=fake),
        patch.object(release.time, "sleep"),
        patch.object(release.subprocess, "run") as cleanup,
    ):
        release.check("image:release", "postgres:test", tmp_path / "report", True)
    report = json.loads((tmp_path / "report/report.json").read_text())
    assert report["passed"] and report["image_id"] == image
    workloads = [
        args
        for args in calls
        if args[:2] == ["docker", "run"] and "POSTGRES_USER=platform" not in args
    ]
    assert workloads and all(image in args for args in workloads)
    for module in (
        "controlplane.main:app_factory",
        "controlplane.gateway_main:app_factory",
        "controlplane.reconciler_main",
        "controlplane.persistence.migrate",
    ):
        assert any(module in args for args in workloads)
    assert any(args[0] == "trivy" for args in calls)
    assert any(args[0] == "syft" for args in calls)
    assert cleanup.call_count == 6  # 4 owned containers and their network
    assert all("mlp-release-" in " ".join(call.args[0]) for call in cleanup.call_args_list)


def test_release_errors_do_not_echo_credentials() -> None:
    release = load("controlplane_release_check")
    with (
        patch.object(
            release.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, stdout="secret", stderr="secret"),
        ),
        pytest.raises(RuntimeError) as failure,
    ):
        release.run(["docker", "sensitive-credential"])
    assert "sensitive-credential" not in str(failure.value) and "secret" not in str(failure.value)


def test_cpu_acceptance_default_is_plan_only(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    cpu = load("controlplane_cpu_acceptance")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check",
            "--api",
            "https://api.invalid",
            "--gateway",
            "https://gateway.invalid",
            "--context",
            "test",
            "--prometheus",
            "https://metrics.invalid",
            "--training-image",
            "registry/train@sha256:" + "a" * 64,
            "--function-image",
            "registry/function@sha256:" + "b" * 64,
            "--out",
            "/tmp/not-written",
        ],
    )
    with patch.object(cpu, "execute") as execute:
        cpu.main()
        execute.assert_not_called()
    assert json.loads(capsys.readouterr().out)["execute"] is False


def test_recovery_refuses_same_db_and_restores_source_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = load("controlplane_backup")
    with patch.dict(sys.modules, {"controlplane_backup": backup}):
        recovery = load("controlplane_recovery_check")
    monkeypatch.setenv("PGDATABASE", "source")
    with pytest.raises(ValueError):
        recovery.drill(tmp_path / "same", "source", True)
    with (
        patch.object(
            recovery,
            "fingerprints",
            side_effect=[
                {"projects": "hash"},
                {"projects": "hash"},
                RuntimeError("target unavailable"),
            ],
        ),
        patch.object(recovery, "backup"),
        patch.object(recovery, "restore"),
        pytest.raises(RuntimeError),
    ):
        recovery.drill(tmp_path / "drill", "target", True)
    import os

    assert os.environ["PGDATABASE"] == "source"
    assert not json.loads((tmp_path / "drill/report.json").read_text())["passed"]


def test_missing_scan_tool_fails_before_build_or_fixture_creation(tmp_path: Path) -> None:
    release = load("controlplane_release_check")
    with (
        patch.object(
            release.shutil,
            "which",
            side_effect=lambda tool: None if tool == "syft" else "/fake/tool",
        ),
        patch.object(release, "run") as run,
    ):
        with pytest.raises(RuntimeError, match="syft"):
            release.check("image:release", "postgres:test", tmp_path / "report", True)
        run.assert_not_called()
    assert not json.loads((tmp_path / "report/report.json").read_text())["passed"]
