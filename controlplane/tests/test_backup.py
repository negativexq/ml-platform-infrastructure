"""Exercise restore safeguards without running PostgreSQL or containers."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/controlplane_backup.py"
spec = importlib.util.spec_from_file_location("backup_script", SCRIPT)
assert spec is not None and spec.loader is not None
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def archive(tmp_path: Path) -> Path:
    (tmp_path / "database.dump").write_bytes(b"archive")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "sha256": backup.digest(tmp_path / "database.dump"),
                "schema_heads": ["0015"],
                "counts": {t: (3 if t == "projects" else 0) for t in backup.TABLES},
            }
        )
    )
    return tmp_path


def test_restore_preview_and_nonempty_target_never_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = archive(tmp_path)
    monkeypatch.setenv("PGDATABASE", "recovery_test")
    with patch.object(backup, "run_tool", return_value="") as tool:
        backup.restore(source, False)
        assert tool.call_args.args[0][1] == "--list"
    with (
        patch.object(backup, "run_tool", return_value="") as tool,
        patch.object(backup, "query", return_value="1"),
    ):
        with pytest.raises(RuntimeError, match="not empty"):
            backup.restore(source, True)
        assert tool.call_count == 1


def test_corrupt_archive_is_rejected_before_database_access(tmp_path: Path) -> None:
    source = archive(tmp_path)
    (source / "database.dump").write_bytes(b"corrupt")
    with patch.object(backup, "run_tool") as tool:
        with pytest.raises(RuntimeError, match="checksum"):
            backup.restore(source, True)
        tool.assert_not_called()


def test_restore_uses_atomic_transaction_and_verifies_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = archive(tmp_path)
    monkeypatch.setenv("PGDATABASE", "recovery_test")
    with (
        patch.object(backup, "run_tool", return_value="") as tool,
        patch.object(backup, "query", return_value="0"),
        patch.object(
            backup,
            "metadata",
            return_value={
                "schema_heads": ["0015"],
                "counts": {t: (3 if t == "projects" else 0) for t in backup.TABLES},
            },
        ),
    ):
        backup.restore(source, True)
        args = tool.call_args.args[0]
        assert "--single-transaction" in args and "--exit-on-error" in args
        assert "--clean" not in args and "--dbname=recovery_test" in args


def test_backup_inventory_covers_every_durable_table() -> None:
    from controlplane.persistence.models import Base

    assert set(backup.TABLES) == set(Base.metadata.tables)
