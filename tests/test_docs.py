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
    for relative in ("requirements/v0.1-mvp.md", "plans/v0.1-mvp.md"):
        text = (ROOT / "docs" / relative).read_text(encoding="utf-8")
        assert "| 状态 | 实施中 |" in text
    assert "| v0.1-mvp | 实施中 |" in (ROOT / "docs/README.md").read_text(encoding="utf-8")
