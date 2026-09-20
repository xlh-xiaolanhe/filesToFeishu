import re
import tomllib
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]


def test_document_links_and_iteration_status_are_consistent():
    paths = [ROOT / "README.md", ROOT / "THIRD_PARTY.md", *ROOT.joinpath("docs").rglob("*.md")]
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
        text = (ROOT / name).read_text()
        declared = re.search(r"(?:当前|适用)版本：\*{0,2}v([\d.]+)", text)
        assert declared and declared[1] == version, name
    page = (ROOT / "src/files_to_feishu/templates/index.html").read_text()
    assert f"本地转换 · v{version}</span>" in page
    assert (ROOT / "docs/validation" / f"v{version}.md").is_file()


def test_usage_confirmation_matches_the_page():
    page = (ROOT / "src/files_to_feishu/templates/index.html").read_text()
    label = re.search(r'id="confirmed"[^>]*>([^<]+)', page)
    assert label
    guide = (ROOT / "docs/guides/pdf-to-feishu.md").read_text()
    assert label[1] in guide


def test_full_validation_instructions_enable_models_and_ocr():
    readme = (ROOT / "README.md").read_text()
    assert re.search(r"RUN_PARSER_TESTS=1 RUN_OCR_TESTS=1 .*pytest", readme)
