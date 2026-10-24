#!/usr/bin/env python3
"""Chief of Staff: what the person said about its texts.

  suppress.py add <slug> [--days N | --forever] [--reason "..."]   # mute or snooze
  suppress.py remove <slug>                                        # unmute
  suppress.py list
  suppress.py approve <slug> "<what was proposed>"                 # they said yes
  suppress.py done <slug>                                          # the yes was acted on

State: <hermes home>/hermsy/state/suppressions.json and approved.jsonl.
"""
import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from observe import APPROVED_FILE, NOW, STATE, SUPPRESS_FILE, TODAY  # noqa: E402


def load():
    try:
        return json.loads(SUPPRESS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save(items):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = SUPPRESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1), encoding="utf-8")
    os.replace(tmp, SUPPRESS_FILE)


def main(argv):
    if len(argv) < 2 or argv[1] not in ("add", "remove", "list", "approve", "done"):
        print(__doc__)
        return 1
    command = argv[1]
    if command == "list":
        print(json.dumps(load(), indent=1))
        return 0
    if len(argv) < 3:
        print(__doc__)
        return 1
    slug = argv[2].lstrip("#").lower()
    if command == "approve":
        STATE.mkdir(parents=True, exist_ok=True)
        with open(APPROVED_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": NOW.isoformat(timespec="minutes"), "slug": slug,
                                "text": " ".join(argv[3:]), "processed": False}) + "\n")
        print(f"approved #{slug}")
        return 0
    if command == "done":
        rows = []
        if APPROVED_FILE.exists():
            for line in APPROVED_FILE.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("slug") == slug:
                    row["processed"] = True
                rows.append(row)
            APPROVED_FILE.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        print(f"#{slug} done")
        return 0
    items = [i for i in load() if i.get("slug") != slug]
    if command == "remove":
        save(items)
        print(f"unmuted #{slug}")
        return 0
    until = None
    if "--forever" not in argv:
        days = int(argv[argv.index("--days") + 1]) if "--days" in argv else 7
        until = (TODAY + timedelta(days=days)).isoformat()
    reason = argv[argv.index("--reason") + 1] if "--reason" in argv else ""
    items.append({"slug": slug, "until": until, "reason": reason, "added": TODAY.isoformat()})
    save(items)
    print(f"muted #{slug} " + ("for good" if until is None else f"until {until}"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
