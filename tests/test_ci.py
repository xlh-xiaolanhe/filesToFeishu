"""Guard CI isolation and evidence checks; no real model, browser or Feishu required."""

import socket
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import check_ci


def test_ci_overrides_live_credentials_and_flags_without_changing_parent_environment():
    original = {
        "FEISHU_APP_ID": "actual-app",
        "FEISHU_APP_SECRET": "actual-secret",
        "feishu_app_secret": "lowercase-secret",
        "FEISHU_PARENT_URL": "https://real.feishu.cn/wiki/parent",
        "FEISHU_NOTIFY_ENABLED": "true",
        "FEISHU_NOTIFY_RECEIVE_ID": "actual-user",
        "RUN_LIVE_TESTS": "1",
        "RUN_OCR_TESTS": "1",
        "RUN_PARSER_TESTS": "1",
        "TEST_BATCH_PREVIEW": "1",
        "PYTEST_ADDOPTS": "--override-ini=testpaths=live",
        "NODE_PATH": "/isolated/node_modules",
    }
    safe = check_ci.isolated_environment(original)
    assert safe["FEISHU_APP_ID"] == safe["FEISHU_APP_SECRET"] == ""
    assert "feishu_app_secret" not in safe
    assert safe["FEISHU_PARENT_URL"] == safe["FEISHU_NOTIFY_RECEIVE_ID"] == ""
    assert safe["FEISHU_NOTIFY_ENABLED"] == "false"
    assert all(
        safe[name] == "0" for name in ("RUN_LIVE_TESTS", "RUN_OCR_TESTS", "RUN_PARSER_TESTS")
    )
    assert "TEST_BATCH_PREVIEW" not in safe and "PYTEST_ADDOPTS" not in safe
    assert safe["NODE_PATH"] == "/isolated/node_modules"
    assert original["FEISHU_NOTIFY_ENABLED"] == "true"


@pytest.mark.parametrize("body", ["", '<testcase><skipped message="no model"/></testcase>'])
def test_explicit_optional_suite_cannot_pass_if_empty_or_skipped(tmp_path, body):
    report = tmp_path / "result.xml"
    report.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>", encoding="utf-8")
    with pytest.raises(RuntimeError, match="did not all execute"):
        check_ci.require_executed_tests(report)
    report.write_text(
        "<testsuites><testsuite><testcase/></testsuite></testsuites>", encoding="utf-8"
    )
    check_ci.require_executed_tests(report)


def test_fast_checks_committed_diff_not_just_empty_checkout(monkeypatch):
    run = Mock()
    monkeypatch.setattr(check_ci, "run", run)
    check_ci.check_fast({}, "abc123")
    commands = [call.args[0] for call in run.call_args_list]
    assert ["git", "diff", "--check", "abc123", "HEAD", "--"] in commands
    assert any("not parser and not live" in command for command in commands)
    run.reset_mock()
    check_ci.check_fast({}, "0" * 40)
    assert ["git", "show", "--format=", "--check", "HEAD", "--"] in [
        call.args[0] for call in run.call_args_list
    ]


def test_browser_check_never_starts_on_an_occupied_port(monkeypatch):
    probe = Mock()
    probe.__enter__ = Mock(return_value=probe)
    probe.__exit__ = Mock(return_value=False)
    probe.bind.side_effect = OSError("address already in use")
    monkeypatch.setattr(socket, "socket", Mock(return_value=probe))
    start = Mock()
    monkeypatch.setattr(check_ci.subprocess, "Popen", start)
    with pytest.raises(RuntimeError, match="existing service was not changed"):
        with check_ci.preview_server("TEST_CODE_PREVIEW", {}):
            pytest.fail("must not run tests against an existing service")
    start.assert_not_called()


def test_model_suite_cannot_claim_ocr_passed_on_linux(monkeypatch):
    monkeypatch.setattr(check_ci.sys, "platform", "linux")
    run = Mock()
    monkeypatch.setattr(check_ci, "run", run)
    with pytest.raises(RuntimeError, match="requires macOS Vision"):
        check_ci.check_models({})
    run.assert_not_called()


def test_required_workflow_never_consumes_credentials_or_untrusted_target_context():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/quality.yml").read_text(
        encoding="utf-8"
    )
    assert "pull_request_target" not in workflow
    assert "secrets." not in workflow
    assert "needs: [fast, browser]" in workflow
    assert 'test "$FAST_RESULT" = success' in workflow
    assert 'test "$BROWSER_RESULT" = success' in workflow
