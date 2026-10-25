"""Puts Hermsy's skills, scripts and jobs in place on this Hermes.

Files are copied once into the person's own folders and never overwritten,
so they can change them:

  <home>/skills/hermsy/<skill>/SKILL.md
  <home>/scripts/hermsy/*.py
  <home>/hermsy/standing-orders.md

Jobs (the Hermsy Brief and the Chief of Staff) are created only when the
person asks for them in the app, at times in their time zone.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

FILES = Path(__file__).parent / "files"

BRIEF_JOB = "Hermsy Brief"
CHIEF_JOB = "Chief of Staff"
# Older names that count as the job already being there.
KNOWN_NAMES = {
    BRIEF_JOB: (BRIEF_JOB, "Frank Brief"),
    CHIEF_JOB: (CHIEF_JOB, "Chief of Staff (Proactive)"),
}
# Local times, in the person's time zone.
BRIEF_TIMES = ((6, 0), (12, 0), (18, 0))
CHIEF_TIMES = ((7, 30), (12, 30), (18, 30))


def _zone(name: Optional[str]):
    from zoneinfo import ZoneInfo

    return ZoneInfo(name) if name else None


def cron_at(times, person_zone, hermes_zone) -> str:
    """A cron expression in Hermes's time zone for [times] in the person's.
    Today's offsets decide it; a daylight-saving change moves it an hour,
    which a Brief can live with."""
    today = datetime.now(person_zone).date()
    converted = [
        datetime(today.year, today.month, today.day, h, m, tzinfo=person_zone).astimezone(hermes_zone)
        for h, m in times
    ]
    minute = converted[0].minute
    hours = ",".join(str(t.hour) for t in sorted(converted, key=lambda t: t.hour))
    return f"{minute} {hours} * * *"


class Setup:
    """``jobs`` is Hermes's ``cron.jobs`` module (tests pass a fake);
    ``hermes_zone`` is the zone Hermes's cron runs in."""

    def __init__(
        self,
        home: Path,
        store: Any,
        *,
        jobs: Any = None,
        hermes_zone: Any = None,
        python: str = sys.executable,
    ) -> None:
        self.home = Path(home)
        self.data = self.home / "hermsy"
        self.scripts = self.home / "scripts" / "hermsy"
        self.store = store
        self._jobs = jobs
        self._hermes_zone = hermes_zone
        self.python = python

    def _fill(self, text: str) -> str:
        return (
            text.replace("{{PYTHON}}", self.python)
            .replace("{{SCRIPTS}}", str(self.scripts))
            .replace("{{DATA}}", str(self.data))
            .replace("{{HOME}}", str(self.home))
        )

    def install_files(self) -> list[str]:
        """Copies whatever is missing; returns what it put in place."""
        placed = []
        for skill in sorted((FILES / "skills").iterdir()):
            target = self.home / "skills" / "hermsy" / skill.name / "SKILL.md"
            if skill.name == "chief-of-staff" and self._own_chief_of_staff():
                continue
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(self._fill((skill / "SKILL.md").read_text(encoding="utf-8")), encoding="utf-8")
                placed.append(str(target))
        self.scripts.mkdir(parents=True, exist_ok=True)
        for script in sorted((FILES / "scripts").glob("*.py")):
            target = self.scripts / script.name
            if not target.exists():
                shutil.copyfile(script, target)
                os.chmod(target, 0o755)
                placed.append(str(target))
        orders = self.data / "standing-orders.md"
        if not orders.exists():
            self.data.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(FILES / "templates" / "standing-orders.md", orders)
            placed.append(str(orders))
        for path in placed:
            log.info("hermsy: installed %s", path)
        return placed

    def _own_chief_of_staff(self) -> bool:
        """The person already has a chief of staff skill of their own; a
        second one would only confuse their agent."""
        skills = self.home / "skills"
        return any(
            path.name.endswith("chief-of-staff") and path.parent.name != "hermsy"
            for path in skills.glob("*/*")
            if path.is_dir()
        )

    def jobs(self):
        if self._jobs is None:
            from cron import jobs

            self._jobs = jobs
        return self._jobs

    def hermes_zone(self):
        if self._hermes_zone is None:
            try:
                from hermes_time import get_timezone

                self._hermes_zone = get_timezone()
            except Exception:  # noqa: BLE001
                self._hermes_zone = None
            if self._hermes_zone is None:
                self._hermes_zone = datetime.now().astimezone().tzinfo
        return self._hermes_zone

    def _find(self, job: str) -> Optional[dict]:
        names = KNOWN_NAMES[job]
        for record in self.jobs().list_jobs(include_disabled=True):
            if record.get("name") in names:
                return record
        return None

    def status(self) -> dict:
        def describe(job: str) -> Optional[dict]:
            record = self._find(job)
            if record is None:
                return None
            return {
                "id": record.get("id"),
                "name": record.get("name"),
                "schedule": record.get("schedule_display") or record.get("schedule"),
                "deliver": record.get("deliver"),
                "enabled": record.get("enabled", True),
            }

        try:
            return {"brief": describe(BRIEF_JOB), "chief_of_staff": describe(CHIEF_JOB)}
        except Exception as error:  # noqa: BLE001
            return {"error": f"could not read cron jobs: {error}"}

    def run(self, *, brief: bool, chief_of_staff: bool, zone: Optional[str]) -> dict:
        if zone:
            try:
                person_zone = _zone(zone)
            except Exception:  # noqa: BLE001
                raise ValueError(f"unknown time zone {zone!r}") from None
            self.store.put("timezone", zone)
        else:
            zone = self.store.get("timezone")
            person_zone = _zone(zone) if zone else self.hermes_zone()
        self.install_files()
        created = []
        wanted = (
            (brief, BRIEF_JOB, BRIEF_TIMES, "hermsy-brief.md", "hermsy/observe_brief.py"),
            (chief_of_staff, CHIEF_JOB, CHIEF_TIMES, "chief-of-staff.md", "hermsy/observe.py"),
        )
        for want, name, times, prompt, script in wanted:
            if not want or self._find(name) is not None:
                continue
            record = self.jobs().create_job(
                prompt=self._fill((FILES / "jobs" / prompt).read_text(encoding="utf-8")),
                schedule=cron_at(times, person_zone, self.hermes_zone()),
                name=name,
                deliver="hermsy",
                script=script,
            )
            log.info("hermsy: created job %s (%s)", name, record.get("id"))
            created.append(name)
        return {"created": created, "setup": self.status()}
