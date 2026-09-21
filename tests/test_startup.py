"""Exercise the real shell entry points without starting or publishing real jobs."""

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def checkout(tmp_path):
    project = tmp_path / "project with spaces"
    project.mkdir()
    for name in ("start.sh", "start.command"):
        shutil.copy2(ROOT / name, project / name)
    return project


@pytest.mark.parametrize("entry", ["start.sh", "start.command"])
def test_missing_environment_reports_setup_without_modifying_config(checkout, tmp_path, entry):
    config = checkout / ".env"
    config.write_text("EXISTING_CONFIG=keep\n")
    result = subprocess.run(
        [str(checkout / entry), "--no-setup", "--no-open"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "uv sync --extra parser --locked" in result.stderr
    assert "配置方法见 README.md" in result.stderr
    assert config.read_text() == "EXISTING_CONFIG=keep\n"
    assert not (checkout / ".data").exists()


@pytest.mark.parametrize("entry", ["start.sh", "start.command"])
def test_launcher_uses_project_python_cwd_and_preserves_exit_status(checkout, tmp_path, entry):
    # Use a real interpreter and a harmless stand-in for the application entry point.
    binary = checkout / ".venv/bin"
    binary.mkdir(parents=True)
    (binary / "python").symlink_to(sys.executable)
    package = checkout / "files_to_feishu"
    package.mkdir()
    (package / "__init__.py").touch()
    (package / "__main__.py").write_text(
        "import os, signal, sys\n"
        "from pathlib import Path\n"
        "print('CHECK_CWD=' + str(Path.cwd()), flush=True)\n"
        "print('CHECK_PYTHON=' + sys.executable, flush=True)\n"
        "print('CHECK_ENV=' + os.environ['STARTUP_TEST_VALUE'], flush=True)\n"
        "os.kill(os.getpid(), signal.SIGTERM)\n"
    )
    result = subprocess.run(
        [str(checkout / entry), "--no-setup", "--no-open"],
        cwd=tmp_path,
        env={**os.environ, "STARTUP_TEST_VALUE": "preserved"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert f"CHECK_CWD={checkout}" in result.stdout
    assert f"CHECK_PYTHON={binary / 'python'}" in result.stdout
    assert "CHECK_ENV=preserved" in result.stdout
    # exec keeps the application as the foreground process, with no surviving wrapper.
    assert result.returncode == -signal.SIGTERM


def test_one_click_syncs_locked_dependencies_and_preserves_existing_config(checkout, tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls"
    (checkout / ".env").write_text("FEISHU_APP_ID=keep\n", encoding="utf-8")
    (checkout / ".env.example").write_text("FEISHU_APP_ID=replace\n", encoding="utf-8")
    uv = fake_bin / "uv"
    uv.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" >> "$TEST_CALLS"\n'
        "mkdir -p .venv/bin\n"
        "cat > .venv/bin/python <<'EOF'\n"
        '#!/bin/bash\nprintf "%s\\n" "$*" >> "$TEST_CALLS"\nEOF\n'
        "chmod +x .venv/bin/python\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    result = subprocess.run(
        [str(checkout / "start.command"), "--wechat-only", "--no-open"],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin", "TEST_CALLS": str(calls)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == [
        "sync --locked",
        "-m files_to_feishu --wechat-only --no-open",
    ]
    assert (checkout / ".env").read_text() == "FEISHU_APP_ID=keep\n"


def test_default_preparation_and_failure_do_not_start_application(checkout, tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text('#!/bin/bash\necho "$*"\nexit 7\n', encoding="utf-8")
    uv.chmod(0o755)
    result = subprocess.run(
        [str(checkout / "start.sh"), "--no-open"],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 7
    assert "sync --extra parser --locked" in result.stdout
    assert not (checkout / ".env").exists()
    assert not (checkout / ".data").exists()


def test_resource_preparation_downloads_only_missing_resources(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from files_to_feishu.config import Settings
    from files_to_feishu.launcher import prepare_resources

    executable = tmp_path / "chromium"
    calls = []

    class Playwright:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(executable_path=str(executable)))

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("playwright.sync_api.sync_playwright", Playwright)
    monkeypatch.setattr("subprocess.run", lambda args, **kwargs: calls.append(args))
    models = tmp_path / "models"
    settings = Settings(docling_artifacts_path=models)
    prepare_resources(settings, wechat_only=False)
    assert calls[0][1:] == ["-m", "playwright", "install", "chromium"]
    assert calls[1][-1].endswith("scripts/download_models.py")
    calls.clear()
    executable.touch()
    prepare_resources(settings, wechat_only=True)
    assert not calls
    models.mkdir()
    (models / ".ready").touch()
    prepare_resources(settings, wechat_only=False)
    assert not calls


def test_launcher_reuses_matching_server_but_rejects_other_or_old_instance(monkeypatch):
    import io
    import json

    from files_to_feishu.config import Settings
    from files_to_feishu.launcher import running_here, server_identity

    settings = Settings()
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: io.BytesIO())

    class Opener:
        def open(self, *args, **kwargs):
            return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("files_to_feishu.launcher.build_opener", lambda *a: Opener())
    payload = server_identity(settings)
    assert running_here(settings) is True
    for change in ({"version": "old"}, {"data_id": "different"}, {"service": "other"}):
        payload = {**server_identity(settings), **change}
        with pytest.raises(RuntimeError, match="不会自动终止"):
            running_here(settings)


def test_unknown_start_flag_does_not_prepare_anything(checkout):
    result = subprocess.run(
        [str(checkout / "start.sh"), "--unknown"], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 2
    assert not (checkout / ".venv").exists()


def test_repeat_start_checks_existing_service_before_touching_dependencies(checkout, tmp_path):
    binary = checkout / ".venv/bin"
    binary.mkdir(parents=True)
    python = binary / "python"
    calls = tmp_path / "calls"
    python.write_text('#!/bin/bash\nprintf "%s\\n" "$*" > "$TEST_CALLS"\nexit 0\n')
    python.chmod(0o755)
    # No uv in PATH: success must come from read-only preflight, not dependency sync.
    result = subprocess.run(
        [str(checkout / "start.sh"), "--wechat-only", "--no-open"],
        cwd=tmp_path,
        env={**os.environ, "PATH": "/usr/bin:/bin", "TEST_CALLS": str(calls)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (
        calls.read_text().strip() == f"{checkout}/scripts/check_running.py --wechat-only --no-open"
    )
    assert not (checkout / ".env").exists()
