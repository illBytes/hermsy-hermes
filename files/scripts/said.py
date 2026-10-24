#!/usr/bin/env python3
"""Chief of Staff: records which topics a text raised (hidden slugs), so the
text itself can read like a person and tomorrow's run does not repeat it.

  said.py <slug> [<slug> ...]   # once per text that goes out, before writing it
  said.py --list [--days 7]     # what recent texts raised

State: <hermes home>/hermsy/state/said.jsonl, one line per text.
"""
import json
import os
import re
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from observe import NOW, SAID_FILE, STATE, TODAY  # noqa: E402


def main(argv):
    args = [a for a in argv[1:] if a]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 1
    if args[0] == "--list":
        days = int(args[args.index("--days") + 1]) if "--days" in args else 7
        cutoff = (TODAY - timedelta(days=days)).isoformat()
        if SAID_FILE.exists():
            for line in SAID_FILE.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("date", "") >= cutoff:
                    print(line)
        return 0
    slugs = sorted({re.sub(r"[^a-z0-9-]", "", a.lstrip("#").lower()) for a in args})
    slugs = [s for s in slugs if len(s) >= 3]
    if not slugs:
        print("no valid slugs", file=sys.stderr)
        return 1
    STATE.mkdir(parents=True, exist_ok=True)
    with open(SAID_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": NOW.isoformat(timespec="minutes"),
                            "date": TODAY.isoformat(), "slugs": slugs}) + "\n")
    print("recorded: " + " ".join("#" + s for s in slugs))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
