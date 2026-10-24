"""The Brief: the day at a glance, written by the "Hermsy Brief" cron job.

The job's model answers with one JSON object; this module finds it in the
reply (models sometimes wrap it in a code fence or a sentence), checks its
shape, and trims it to what the app renders, so a sloppy reply can never
break the Brief page.
"""

from __future__ import annotations

import json
import re

# The cron job that writes it; "Frank Brief" is its name from before Hermsy.
BRIEF_JOBS = frozenset({"Hermsy Brief", "Frank Brief"})

# Sections the app knows how to draw, in their default order. Others are
# kept and shown as plain lists.
KNOWN_SECTIONS = ("today", "due", "suggestions", "coming_up", "money")
MAX_SECTIONS = 8
MAX_ITEMS = 8
_ITEM_FIELDS = {"title": 120, "detail": 400, "time": 20, "when": 40, "action": 300}


def _text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:limit] or None


def parse_brief(reply: str) -> dict:
    """The brief in [reply], cleaned up. Raises ValueError when there is
    none to be found."""
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    try:
        raw = json.loads(reply[start : end + 1])
    except json.JSONDecodeError as error:
        raise ValueError(f"not valid JSON: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("the brief is not an object")
    sections = []
    for section in raw.get("sections") or []:
        if not isinstance(section, dict):
            continue
        section_id = _text(section.get("id"), 40)
        if not section_id:
            continue
        items = []
        for item in section.get("items") or []:
            if isinstance(item, str):
                item = {"title": item}
            if not isinstance(item, dict):
                continue
            clean = {key: _text(item.get(key), limit) for key, limit in _ITEM_FIELDS.items()}
            clean = {key: value for key, value in clean.items() if value}
            if clean.get("title"):
                items.append(clean)
        if items:
            sections.append(
                {
                    "id": section_id.lower().replace(" ", "_"),
                    "title": _text(section.get("title"), 60) or section_id.replace("_", " ").title(),
                    "items": items[:MAX_ITEMS],
                }
            )
    brief = {
        "greeting": _text(raw.get("greeting"), 80),
        "headline": _text(raw.get("headline"), 240),
        "summary": _text(raw.get("summary"), 800),
        "sections": sections[:MAX_SECTIONS],
    }
    if not brief["headline"] and not brief["summary"] and not sections:
        raise ValueError("the brief is empty")
    return brief
