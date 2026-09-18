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
        [str(checkout / entry)], cwd=tmp_path, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 1
    assert "uv sync --extra parser --locked" in result.stderr
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
        [str(checkout / entry)],
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
