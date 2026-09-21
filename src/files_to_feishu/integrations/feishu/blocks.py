"""Feishu block mapping and semantic readback comparison."""

from ...models import Element, TextRun


def text_blocks(text: str, kind: str = "text", level: int = 1) -> list[dict]:
    block_type, field = {
        "text": (2, "text"),
        "bullet": (12, "bullet"),
        "ordered": (13, "ordered"),
        "quote": (15, "quote"),
        "heading": (2 + level, f"heading{level}"),
    }[kind]
    return [
        {
            "block_type": block_type,
            field: {"elements": [{"text_run": {"content": text[start : start + 1500]}}]},
        }
        for start in range(0, max(len(text), 1), 1500)
    ]


def block_text(block: dict) -> str:
    for field in TEXT_FIELDS:
        if field in block:
            return "".join(
                e.get("text_run", {}).get("content", "") for e in block[field].get("elements", [])
            )
    return ""


TEXT_FIELDS = ["text", "bullet", "ordered", "code", "quote"] + [f"heading{i}" for i in range(1, 10)]


def rich_text_elements(runs: list[TextRun]) -> list[dict]:
    """Map the shared, safe text model without flattening formatting or links."""
    elements = []
    for run in runs:
        style: dict = {
            key: value
            for key, value in {
                "bold": run.bold,
                "italic": run.italic,
                "strikethrough": run.strike,
                "inline_code": run.inline_code,
            }.items()
            if value
        }
        if run.link:
            style["link"] = {"url": run.link}
        for start in range(0, max(len(run.text), 1), 1500):
            text_run: dict = {"content": run.text[start : start + 1500]}
            if style:
                text_run["text_element_style"] = style
            elements.append({"text_run": text_run})
    return elements


def element_blocks(element: Element) -> list[dict]:
    if element.kind == "code":
        return code_blocks(element)
    # Keep the legacy PDF block splitting stable for persisted publication journals.
    if not element.runs and element.list_start is None:
        return text_blocks(element.text, element.kind, element.level)
    field = f"heading{element.level}" if element.kind == "heading" else element.kind
    block_type = {
        "text": 2,
        "bullet": 12,
        "ordered": 13,
        "quote": 15,
        "heading": 2 + element.level,
    }[element.kind]
    content: dict = {"elements": rich_text_elements(element.runs or [TextRun(text=element.text)])}
    if element.kind == "ordered" and element.list_start is not None:
        content["style"] = {"sequence": str(element.list_start)}
    return [{"block_type": block_type, field: content}]


def text_signature(block: dict) -> list[dict] | None:
    """Normalize API defaults and run splits while preserving semantic formatting."""
    content = next((block[field] for field in TEXT_FIELDS if field in block), None)
    if content is None:
        return None
    result: list[dict] = []
    for element in content.get("elements", []):
        if set(element) != {"text_run"}:
            return None
        run = element["text_run"]
        style = run.get("text_element_style") or {}
        normalized: dict = {
            key: bool(style.get(key)) for key in ("bold", "italic", "strikethrough", "inline_code")
        }
        normalized["link"] = (style.get("link") or {}).get("url", "")
        text = run.get("content", "")
        if not text:
            continue
        if result and result[-1]["style"] == normalized:
            result[-1]["text"] += text
        else:
            result.append({"text": text, "style": normalized})
    return result


def text_matches(actual: dict, wanted: dict) -> bool:
    if actual.get("block_type") != wanted.get("block_type"):
        return False
    signature = text_signature(wanted)
    if signature is None or text_signature(actual) != signature:
        return False
    if "code" in wanted:
        language = wanted["code"].get("style", {}).get("language")
        if (
            language is not None
            and actual.get("code", {}).get("style", {}).get("language") != language
        ):
            return False
    sequence = wanted.get("ordered", {}).get("style", {}).get("sequence")
    return (
        sequence is None or actual.get("ordered", {}).get("style", {}).get("sequence") == sequence
    )


def plain_text_block(block: dict) -> bool:
    """Do not mistake mentions or other non-text runs for an empty placeholder."""
    return (
        block.get("block_type") == 2
        and "text" in block
        and all(
            set(run) == {"text_run"} and isinstance(run["text_run"].get("content"), str)
            for run in block["text"].get("elements", [])
        )
    )


def empty_text_block(block: dict) -> bool:
    return plain_text_block(block) and block_text(block) == ""


def code_blocks(element: Element) -> list[dict]:
    # Verified against Feishu's blocks/convert response; omit unknown language.
    language = {"javascript": 30, "typescript": 63}.get(element.language)
    style: dict = {"wrap": False}
    if language is not None:
        style["language"] = language
    # Keep each logical code snippet in one block; split only text runs for API limits.
    return [
        {
            "block_type": 14,
            "code": {
                "style": style,
                "elements": [
                    {"text_run": {"content": element.text[start : start + 1500]}}
                    for start in range(0, len(element.text), 1500)
                ],
            },
        }
    ]
