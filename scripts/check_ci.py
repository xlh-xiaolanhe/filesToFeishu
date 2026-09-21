"""Run the same isolated checks locally and on GitHub; never use live Feishu credentials."""

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "output" / "ci"


def isolated_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Environment values override .env, including when this command is run locally."""
    result = {key: value for key, value in environ.items() if not key.upper().startswith("FEISHU_")}
    result.update(
        FEISHU_APP_ID="",
        FEISHU_APP_SECRET="",
        FEISHU_PARENT_URL="",
        FEISHU_NOTIFY_ENABLED="false",
        FEISHU_NOTIFY_RECEIVE_ID="",
        FEISHU_NOTIFY_RECEIVE_ID_TYPE="open_id",
        RUN_LIVE_TESTS="0",
        RUN_PARSER_TESTS="0",
        RUN_OCR_TESTS="0",
        RUN_BROWSER_TESTS="0",
    )
    for key in tuple(result):
        if key.startswith("TEST_") or key.startswith("PYTEST_"):
            result.pop(key)
    return result


def run(command: list[str], env: dict[str, str], timeout: int = 600) -> None:
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True, timeout=timeout)


def require_executed_tests(report: Path) -> None:
    """An explicitly requested browser/model check cannot pass by skipping everything."""
    cases = list(ET.parse(report).getroot().iter("testcase"))
    if not cases or any(case.find("skipped") is not None for case in cases):
        raise RuntimeError(f"Required tests did not all execute: {report}")


def check_fast(env: dict[str, str], base: str) -> None:
    run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "not parser and not live",
            "--junitxml=output/ci/fast.xml",
        ],
        env,
    )
    run([sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"], env)
    run([sys.executable, "-m", "mypy"], env)
    # Check working edits as well as committed changes. A clean CI checkout alone
    # would otherwise make `git diff --check` a no-op for every submitted commit.
    run(["git", "diff", "--check"], env)
    run(["git", "diff", "--cached", "--check"], env)
    if base and set(base) != {"0"}:
        run(["git", "diff", "--check", base, "HEAD", "--"], env)
    else:
        run(["git", "show", "--format=", "--check", "HEAD", "--"], env)


@contextmanager
def preview_server(mode: str, env: dict[str, str]) -> Iterator[None]:
    """Own only the isolated test process; refuse to touch another running server."""
    with socket.socket() as probe:
        # Match uvicorn's bind semantics after the previous fixture server closes;
        # TIME_WAIT sockets are not evidence of another running instance.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", 8766))
        except OSError as exc:
            raise RuntimeError(
                "Test port 8766 is occupied; existing service was not changed"
            ) from exc
    server_env = {**env, mode: "1"}
    with (REPORTS / f"{mode.lower()}.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "tests.browser_server"],
            cwd=ROOT,
            env=server_env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"Test server exited; see {log.name}")
                try:
                    with urllib.request.urlopen("http://127.0.0.1:8766/api/health", timeout=1):
                        break
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(0.1)
            else:
                raise RuntimeError(f"Test server did not become ready; see {log.name}")
            yield
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def check_browser(env: dict[str, str]) -> None:
    env = {**env, "RUN_BROWSER_TESTS": "1"}
    run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/converters/wechat/test_browser.py",
            "--junitxml=output/ci/browser.xml",
        ],
        env,
    )
    require_executed_tests(REPORTS / "browser.xml")
    run([sys.executable, "-m", "tests.converters.pdf.samples"], env)
    for name in ("wechat", "history", "notifications"):
        run(["node", f"tests/browser_{name}_smoke.cjs"], env, timeout=180)
    for name, mode in (("code", "TEST_CODE_PREVIEW"), ("batch", "TEST_BATCH_PREVIEW")):
        with preview_server(mode, env):
            run(["node", f"tests/browser_{name}_smoke.cjs"], env, timeout=240)
    with preview_server("TEST_CODE_PREVIEW", env):
        run(["node", "tests/browser_review_smoke.cjs"], env, timeout=240)


def check_models(env: dict[str, str]) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("Model/OCR verification requires macOS Vision; it was not run")
    env = {
        **env,
        "RUN_PARSER_TESTS": "1",
        "RUN_OCR_TESTS": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "parser and not live",
            "--junitxml=output/ci/models.xml",
        ],
        env,
        timeout=1800,
    )
    require_executed_tests(REPORTS / "models.xml")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("fast", "browser", "models"), default="fast", nargs="?")
    parser.add_argument(
        "--base", default="", help="Commit SHA to check for committed whitespace errors"
    )
    options = parser.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True)
    env = isolated_environment(os.environ)
    try:
        if options.suite == "fast":
            check_fast(env, options.base)
        elif options.suite == "browser":
            check_browser(env)
        else:
            check_models(env)
    except (OSError, RuntimeError, subprocess.SubprocessError, ET.ParseError) as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
