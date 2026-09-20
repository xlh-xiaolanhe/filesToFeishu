import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_document_links_and_iteration_status_are_consistent():
    paths = [
        ROOT / "AGENTS.md",
        ROOT / "README.md",
        ROOT / "THIRD_PARTY.md",
        *ROOT.joinpath("docs").rglob("*.md"),
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("# "), path
        assert text.count("```") % 2 == 0, path
        for link in re.findall(r"\]\(([^)]+)\)", text):
            if "://" in link:
                continue
            name, _, anchor = link.partition("#")
            target = path.parent / unquote(name) if name else path
            assert target.is_file(), (path, link)
            if anchor and target.suffix == ".md":
                headings = re.findall(r"^#{1,6} (.+)$", target.read_text(), re.MULTILINE)
                anchors = {
                    re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-")
                    for heading in headings
                }
                assert unquote(anchor) in anchors, (path, link)
    for requirement in (ROOT / "docs/requirements").glob("*.md"):
        statuses = []
        for path in (requirement, ROOT / "docs/plans" / requirement.name):
            text = path.read_text(encoding="utf-8")
            status = re.search(r"\| 状态 \| (规划中|实施中|已交付) \|", text)
            assert status, path
            statuses.append(status.group(1))
        assert len(set(statuses)) == 1
        index = (ROOT / "docs/README.md").read_text(encoding="utf-8")
        assert f"| {requirement.stem} | {statuses[0]} |" in index
        validation = (ROOT / "docs/validation" / requirement.name).read_text(encoding="utf-8")
        assert f"- 状态：{statuses[0]}" in validation


def test_current_version_matches_documentation_lock_and_page():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    version = project["version"]
    locked = tomllib.loads((ROOT / "uv.lock").read_text())
    package = next(p for p in locked["package"] if p["name"] == project["name"])
    assert package["version"] == version
    for name in (
        "README.md",
        "docs/guides/pdf-to-feishu.md",
        "docs/architecture/input-formats.md",
    ):
        if name.startswith("docs/") and not (ROOT / "docs").exists():
            continue
        text = (ROOT / name).read_text()
        declared = re.search(r"(?:当前|适用)版本：\*{0,2}v([\d.]+)", text)
        assert declared and declared[1] == version, name
    page = (ROOT / "src/files_to_feishu/templates/index.html").read_text()
    assert f"本地转换 · v{version}</span>" in page
    if (ROOT / "docs").exists():
        assert (ROOT / "docs/validation" / f"v{version}.md").is_file()


def test_usage_confirmation_matches_the_page():
    if not (ROOT / "docs").exists():
        pytest.skip("本地 docs 未随 Git 检出，跳过指南文案专项检查")
    page = (ROOT / "src/files_to_feishu/templates/index.html").read_text()
    label = re.search(r'id="confirmed"[^>]*>([^<]+)', page)
    assert label
    guide = (ROOT / "docs/guides/pdf-to-feishu.md").read_text()
    assert label[1] in guide


def test_full_validation_instructions_enable_models_and_ocr():
    readme = (ROOT / "README.md").read_text()
    assert re.search(r"RUN_PARSER_TESTS=1 RUN_OCR_TESTS=1 .*pytest", readme)


def test_documentation_checks_work_without_local_archive(tmp_path, monkeypatch):
    # Reproduce a fresh checkout that has public entry points but no private docs.
    for name in (
        "AGENTS.md",
        "README.md",
        "THIRD_PARTY.md",
        "pyproject.toml",
        "uv.lock",
        "start.sh",
        "start.command",
        "src/files_to_feishu/templates/index.html",
    ):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    monkeypatch.setattr("tests.test_docs.ROOT", tmp_path)
    test_document_links_and_iteration_status_are_consistent()
    test_current_version_matches_documentation_lock_and_page()
    test_full_validation_instructions_enable_models_and_ocr()
    with pytest.raises(pytest.skip.Exception, match="本地 docs"):
        test_usage_confirmation_matches_the_page()


def test_local_document_paths_are_not_in_git_index_or_active_history():
    if shutil.which("git") is None:
        pytest.skip("Git 不可用，源码归档不检查提交历史")
    repository = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=ROOT, capture_output=True, text=True
    )
    if repository.returncode or Path(repository.stdout.strip()).resolve() != ROOT.resolve():
        pytest.skip("当前为无 Git 元数据的源码归档")

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True)

    assert not git("ls-files", "--", "docs/", "doc/").strip()
    assert not git("log", "--all", "--format=", "--name-only", "--", "docs/", "doc/").strip()
    # Some local tools retain direct tree refs that git log does not traverse.
    for ref in git("for-each-ref", "--format=%(objectname) %(objecttype)").splitlines():
        oid, kind = ref.split()
        if kind == "tree":
            assert not git("ls-tree", "-r", "--name-only", oid, "--", "docs", "doc").strip()
