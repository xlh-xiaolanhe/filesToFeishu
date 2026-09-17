import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_document_links_and_iteration_status_are_consistent():
    paths = [ROOT / "README.md", ROOT / "THIRD_PARTY.md", *ROOT.joinpath("docs").rglob("*.md")]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("# "), path
        assert text.count("```") % 2 == 0, path
        for link in re.findall(r"\]\(([^)]+)\)", text):
            if "://" not in link and not link.startswith("#"):
                assert (path.parent / link.split("#")[0]).is_file(), (path, link)
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
