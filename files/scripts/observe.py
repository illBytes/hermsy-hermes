#!/usr/bin/env python3
"""Hermsy observer: a compact JSON snapshot of the person's world, built
without a model, for the "Chief of Staff" and "Hermsy Brief" cron jobs.

Hermes runs it before each of those jobs and hands its output to the
model. It reads only what every Hermes has:

  - the person's time zone (set from the Hermsy app) and the time slot
  - standing orders: <hermes home>/hermsy/standing-orders.md
  - the agent's memory (memories/MEMORY.md and USER.md): dates coming up,
    and things that look like open loops
  - cron jobs that failed in the last 24 hours
  - for the Chief of Staff: what it already raised, what the person muted
    or snoozed, approvals waiting, and today's message budget

This copy is yours: Hermsy installed it once and never overwrites it. Add
your own signals (a task list, a calendar export, anything) as functions
that return a small dict, add them to `snapshot` in main(), and mention
them in the job's prompt. Keep the output under ~6 KB: it goes to the model
on every run.

Run:  python3 observe.py [--brief] [--pretty]
"""
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

# <home>/scripts/hermsy/observe.py -> <home>
HOME = Path(os.environ.get("HERMES_HOME") or Path(__file__).resolve().parents[2])
DATA = HOME / "hermsy"
STATE = DATA / "state"
STANDING_ORDERS = DATA / "standing-orders.md"
MEMORY_FILES = (HOME / "memories" / "USER.md", HOME / "memories" / "MEMORY.md")
JOBS_FILE = HOME / "cron" / "jobs.json"
SAID_FILE = STATE / "said.jsonl"
SUPPRESS_FILE = STATE / "suppressions.json"
APPROVED_FILE = STATE / "approved.jsonl"

DAILY_CAP = 3
QUIET_START, QUIET_END = 22, 7  # local time, [22:00, 07:00)
DATE_WINDOW_PAST, DATE_WINDOW_FUTURE = 2, 45  # days
RECENT_DAYS = 7

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"


def local_zone():
    """The person's time zone, as the Hermsy app set it; else Hermes's."""
    name = None
    try:
        con = sqlite3.connect(f"file:{DATA / 'hermsy.db'}?mode=ro", uri=True)
        row = con.execute("select value from meta where key='timezone'").fetchone()
        con.close()
        name = row[0] if row else None
    except sqlite3.Error:
        pass
    name = name or os.environ.get("HERMES_TIMEZONE")
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name), name
        except Exception:  # noqa: BLE001
            pass
    return timezone.utc, "UTC"


ZONE, ZONE_NAME = local_zone()
NOW = datetime.now(ZONE)
TODAY = NOW.date()


def slot_for(now):
    if 5 <= now.hour < 11:
        return "morning"
    if 11 <= now.hour < 16:
        return "midday"
    return "evening"


def in_quiet_hours(now):
    return now.hour >= QUIET_START or now.hour < QUIET_END


def standing_orders():
    try:
        text = STANDING_ORDERS.read_text(encoding="utf-8")
    except OSError:
        return None
    text = re.sub(r"^# .*\n", "", text)
    text = text.split("\n## Change log", 1)[0]
    # The template's examples, "(e.g. ...)", are not orders until filled in.
    text = re.sub(r"(?m)^\s*(?:[-*]|\d+\.)\s*\(.*\)\s*$\n?", "", text)
    # Headings left with nothing under them, and the intro, go too.
    sections = [s for s in re.split(r"(?m)^(?=## )", text) if s.startswith("## ") and s.strip().count("\n")]
    text = "\n".join(s.strip() for s in sections)
    return text[:3000] or None


# ---------------------------------------------------------------- memory
def memory_entries():
    entries = []
    for path in MEMORY_FILES:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for entry in text.split("\n§\n"):
            entry = entry.strip()
            if entry and not entry.startswith("#"):
                entries.append((path.stem.lower(), entry))
    return entries


def _safe_date(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def extract_dates(text):
    """(date, approx, is_anniversary, span) for each date mentioned."""
    out = []
    low = text.lower()

    def anniversary(span):
        before = low[max(0, span[0] - 14):span[0]]
        return bool(re.search(r"\b(dob|b\.|born|birthday|bday|wedding|anniversary)\b", before))

    for m in re.finditer(r"\b(20\d\d)-(\d\d)-(\d\d)\b", text):
        d = _safe_date(int(m[1]), int(m[2]), int(m[3]))
        if d:
            out.append((d, False, anniversary(m.span()), m.span()))
    for m in re.finditer(r"\b(\d{1,2})\s+" + MON + r",?\s+((?:19|20)\d\d)\b", low):
        d = _safe_date(int(m[3]), MONTHS[m[2][:3]], int(m[1]))
        if d:
            out.append((d, False, anniversary(m.span()), m.span()))
    for m in re.finditer(r"\b" + MON + r"\s+(\d{1,2}),?\s+((?:19|20)\d\d)\b", low):
        d = _safe_date(int(m[3]), MONTHS[m[1][:3]], int(m[2]))
        if d:
            out.append((d, False, anniversary(m.span()), m.span()))
    for m in re.finditer(r"\b(early|mid|late)[- ]" + MON + r"\s+(20\d\d)\b", low):
        d = _safe_date(int(m[3]), MONTHS[m[2][:3]], {"early": 5, "mid": 15, "late": 25}[m[1]])
        if d:
            out.append((d, True, False, m.span()))
    # Month and year only ("June 2027") -> approximate, day 1.
    for m in re.finditer(r"\b" + MON + r"\s+(20\d\d)\b", low):
        d = _safe_date(int(m[2]), MONTHS[m[1][:3]], 1)
        if d and not any(s[0] <= m.start() < s[1] for *_, s in out):
            out.append((d, True, False, m.span()))
    return out


def memory_signals(entries):
    lo = TODAY - timedelta(days=DATE_WINDOW_PAST)
    hi = TODAY + timedelta(days=DATE_WINDOW_FUTURE)
    dated, seen = [], set()
    for source, entry in entries:
        for d, approx, is_anniversary, span in extract_dates(entry):
            kind = "date"
            if is_anniversary:
                d = _safe_date(TODAY.year, d.month, d.day) or _safe_date(TODAY.year, d.month, 28)
                if d < lo:
                    d = _safe_date(TODAY.year + 1, d.month, d.day) or d
                kind = "anniversary"
            elif d.year < TODAY.year and not approx:
                continue  # a past date, not a yearly one
            if not lo <= d <= hi:
                continue
            context = entry[max(0, span[0] - 70):span[1] + 50].replace("\n", " ").strip()
            if (context, d) in seen:
                continue
            seen.add((context, d))
            dated.append({"date": d.isoformat(), "in_days": (d - TODAY).days, "kind": kind,
                          "approx": approx, "from": source, "context": context})
    dated.sort(key=lambda r: r["in_days"])

    loop = re.compile(
        r"\b(plan(s|ning)? to|considering|intends? to|wants? to|pending|awaiting|to decide|"
        r"will (decide|review|revisit)|not yet|follow[- ]up|waiting on|next step)\b", re.I)
    loops = [{"from": source, "snippet": entry.replace("\n", " ")[:160]}
             for source, entry in entries if loop.search(entry)]
    return dated[:15], loops[:8]


# ---------------------------------------------------------------- jobs
def failed_jobs_24h():
    try:
        data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    failed = []
    for job in jobs if isinstance(jobs, list) else []:
        status = str(job.get("last_status") or "")
        if status in ("", "ok", "success", "silent", "skipped"):
            continue
        try:
            ran = datetime.fromisoformat(str(job.get("last_run_at")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ran.tzinfo is None:
            ran = ran.replace(tzinfo=timezone.utc)
        if ran >= since:
            error = job.get("last_error") or job.get("last_delivery_error") or status
            failed.append({"job": job.get("name") or job.get("id"), "error": str(error)[:120]})
    return failed[:10]


# ---------------------------------------------------------------- chief of staff state
def _jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as lines:
            for line in lines:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def recently_said():
    """{slug: last day raised} for the last week, and how many texts went out today."""
    said, sent_today = {}, 0
    cutoff = (TODAY - timedelta(days=RECENT_DAYS)).isoformat()
    for row in _jsonl(SAID_FILE):
        day = row.get("date", "")
        if day == TODAY.isoformat():
            sent_today += 1
        if day < cutoff:
            continue
        for slug in row.get("slugs", []):
            said[slug] = max(said.get(slug, ""), day)
    return said, sent_today


def suppressions():
    try:
        items = json.loads(SUPPRESS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    live = []
    for item in items:
        until = item.get("until")
        if until and until < TODAY.isoformat():
            continue
        live.append({k: item[k] for k in ("slug", "until", "reason") if k in item})
    return live


def pending_approvals():
    return [{k: row.get(k) for k in ("ts", "slug", "text")}
            for row in _jsonl(APPROVED_FILE) if not row.get("processed")][-5:]


# ---------------------------------------------------------------- main
def main():
    entries = memory_entries()
    dated, loops = memory_signals(entries)
    snapshot = {
        "now": NOW.strftime("%a %d %b %Y %H:%M"),
        "timezone": ZONE_NAME,
        "slot": slot_for(NOW),
        "standing_orders": standing_orders(),
        "memory_dates_window": dated,
        "candidate_open_loops": loops,
        "failed_jobs_24h": failed_jobs_24h(),
    }
    if "--brief" not in sys.argv:
        said, sent_today = recently_said()
        quiet = in_quiet_hours(NOW)
        snapshot.update({
            "budget": {
                "cap_per_day": DAILY_CAP, "sent_today": sent_today, "quiet_hours": quiet,
                "must_be_silent": quiet or sent_today >= DAILY_CAP,
            },
            "pending_approvals": pending_approvals(),
            "recently_said": said,
            "suppressions": suppressions(),
        })
    print(json.dumps(snapshot, indent=1 if "--pretty" in sys.argv else None, ensure_ascii=False))


if __name__ == "__main__":
    main()
