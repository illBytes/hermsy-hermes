"""Unit tests for the hermsy plugin with Hermes's gateway modules stubbed out.

Run from server/relay: `.venv/bin/pytest ../hermes-plugin/tests` (in the
plugin's own repo: `pytest tests`, with httpx installed). The path
through a real Hermes gateway is covered by the end-to-end check in
the plugin's README.
"""

import asyncio
import base64
import importlib.util
import json
import subprocess
import sys
import types
from dataclasses import dataclass, field
from datetime import date, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest


def _stub_gateway():
    config = types.ModuleType("gateway.config")
    base = types.ModuleType("gateway.platforms.base")

    class Platform(str):
        API_SERVER = "api_server"

    @dataclass
    class PlatformConfig:
        enabled: bool = False
        extra: dict = field(default_factory=dict)

    @dataclass
    class SendResult:
        success: bool
        message_id: str | None = None
        error: str | None = None
        retryable: bool = False

    class BasePlatformAdapter:
        def __init__(self, config, platform):
            self.config = config
            self.name = str(platform)

        def _mark_connected(self):
            pass

        def _mark_disconnected(self):
            pass

    config.Platform, config.PlatformConfig = Platform, PlatformConfig
    base.BasePlatformAdapter, base.SendResult = BasePlatformAdapter, SendResult
    sys.modules.update({
        "gateway": types.ModuleType("gateway"),
        "gateway.platforms": types.ModuleType("gateway.platforms"),
        "gateway.config": config,
        "gateway.platforms.base": base,
    })


def _load_plugin():
    """Imports the plugin as `hermsy`: from the folder above in this repo,
    or from the repo root in the published plugin repo (whatever its name)."""
    root = Path(__file__).resolve().parents[1]
    if not (root / "plugin.yaml").exists():
        sys.path.insert(0, str(root))
        return
    spec = importlib.util.spec_from_file_location(
        "hermsy", root / "__init__.py", submodule_search_locations=[str(root)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermsy"] = module
    spec.loader.exec_module(module)


_stub_gateway()
_load_plugin()
from hermsy import adapter  # noqa: E402
from hermsy.core import Core, split_cron_wrapper  # noqa: E402
from hermsy.push import Waker  # noqa: E402
from hermsy.setup import FILES, Setup, cron_at  # noqa: E402
from hermsy.store import Store  # noqa: E402

PHONE = "phone-token-" + "x" * 20
BRIEF = {
    "greeting": "Good morning",
    "headline": "The dentist at ten.",
    "summary": "A light day.",
    "sections": [{"id": "today", "title": "Today", "items": [{"title": "Dentist", "time": "10:00"}]}],
}


def wrapped(name, body):
    return (
        f"Cronjob Response: {name}\n(job_id: abc123)\n-------------\n\n{body}\n\n"
        f'To stop or manage this job, send me a new message (e.g. "stop reminder {name}").'
    )


def run(coro):
    return asyncio.run(coro)


class FakeJobs:
    def __init__(self, existing=()):
        self.records = [dict(r) for r in existing]

    def list_jobs(self, include_disabled=False):
        return self.records

    def create_job(self, **kwargs):
        record = {"id": f"job{len(self.records)}", **kwargs, "schedule_display": kwargs["schedule"]}
        self.records.append(record)
        return record


@pytest.fixture
def woken():
    return []


@pytest.fixture
def hermsy(tmp_path, woken):
    (tmp_path / "hermsy").mkdir()
    store = Store(str(tmp_path / "hermsy" / "hermsy.db"))

    async def push(request: httpx.Request) -> httpx.Response:
        token = json.loads(request.content)["token"]
        woken.append(token)
        return httpx.Response(410 if token.startswith("gone") else 200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(push))
    waker = Waker("https://push.test", store.device_tokens, store.remove_device, client=client)
    keys = {"OPENAI_API_KEY": "sk-test"}
    setup = Setup(tmp_path, store, jobs=FakeJobs(), hermes_zone=timezone.utc, python="/usr/bin/python3")
    return Core(store, waker, secret=lambda name: keys.get(name, ""), setup=setup, client=client)


# -- what Hermes delivers -------------------------------------------------------


def test_cron_wrapper_becomes_title_and_clean_body():
    assert split_cron_wrapper(wrapped("Morning brief", "Good morning.\n\nThree things today.")) == {
        "text": "Good morning.\n\nThree things today.",
        "job_name": "Morning brief",
        "job_id": "abc123",
        "source": "cron",
    }
    assert split_cron_wrapper("Cronjob Response: not really a header")["source"] == "agent"


def test_a_delivery_is_filed_and_wakes_every_phone(hermsy, woken):
    async def go():
        hermsy.store.upsert_device(PHONE, "android")
        hermsy.store.upsert_device("gone-" + "y" * 20, "android")
        result = hermsy.deliver(wrapped("Chief of Staff", "The dentist is at ten."))
        await hermsy.waker.idle()
        return result

    result = run(go())
    assert result["success"]
    message = hermsy.store.get_message(result["message_id"])
    assert (message.title, message.text, message.source) == ("Chief of Staff", "The dentist is at ten.", "cron")
    assert sorted(woken) == sorted([PHONE, "gone-" + "y" * 20])
    # Google said the second phone no longer has the app.
    assert hermsy.store.device_tokens() == [PHONE]


def test_a_message_without_a_job_is_titled_hermsy(hermsy):
    result = hermsy.deliver("Your build finished.")
    assert hermsy.store.get_message(result["message_id"]).title == "Hermsy"
    assert hermsy.deliver("   ") == {"error": "hermsy: empty message"}


def test_the_brief_job_becomes_the_brief_and_a_broken_one_a_message(hermsy):
    hermsy.deliver(wrapped("Hermsy Brief", json.dumps(BRIEF)))
    assert hermsy.store.latest_brief()["headline"] == "The dentist at ten."
    assert hermsy.store.messages_since(0, 10) == []
    hermsy.deliver(wrapped("Frank Brief", "Sorry, no brief today."))
    [kept] = hermsy.store.messages_since(0, 10)
    assert kept.title == "Frank Brief"
    # The notification's first line is words, not JSON; the reply is below.
    assert kept.text.startswith("This Brief could not be read")
    assert "Sorry, no brief today." in kept.text


def test_a_slipped_bracket_does_not_cost_the_brief():
    from hermsy.brief import parse_brief

    # Shaped like a real reply: the last section closed with "]" not "}".
    slipped = (
        '{"headline": "H", "sections": [\n'
        '  {"id": "today", "items": [{"title": "Dentist", "time": "10:00"}]},\n'
        '  {"id": "money", "items": [{"title": "Rent due"}]\n'
        "  ]\n}"
    )
    brief = parse_brief(slipped)
    assert [s["id"] for s in brief["sections"]] == ["today", "money"]

    # Cut off before its last closers.
    assert parse_brief('{"headline": "H", "sections": [{"id": "today", "items": [{"title": "A"}')["headline"] == "H"

    # A broken object followed by a code fence and a sentence.
    fenced = "```json\n" + slipped + "\n```\nHope this helps!"
    assert parse_brief(fenced)["headline"] == "H"

    # Brackets inside text are left alone.
    quoted = '{"headline": "Call [Ada] about {rent}", "sections": [{"id": "due", "items": [{"title": "x"}]]}'
    assert parse_brief(quoted)["headline"] == "Call [Ada] about {rent}"

    with pytest.raises(ValueError):
        parse_brief('{"headline": "H", "sections": [{"id": oops}]}')


# -- what the app asks ----------------------------------------------------------


def test_hello_says_what_is_there(hermsy):
    hello = run(hermsy.handle({"op": "hello"}))
    assert hello["plugin"] == "hermsy"
    assert hello["store"] == hermsy.store.id
    assert hello["push"] is True
    assert hello["voices"] == ["openai"]
    assert hello["setup"] == {"brief": None, "chief_of_staff": None}


def test_phones_register_and_messages_page_through(hermsy):
    assert run(hermsy.handle({"op": "register_device", "token": PHONE})) == {"ok": True}
    assert hermsy.store.device_tokens() == [PHONE]

    async def deliver_three():
        for n in range(3):
            hermsy.deliver(f"Message {n}")
        await hermsy.waker.idle()

    run(deliver_three())
    page = run(hermsy.handle({"op": "messages", "since": 0, "limit": 2}))
    assert [m["text"] for m in page["messages"]] == ["Message 0", "Message 1"]
    rest = run(hermsy.handle({"op": "messages", "since": page["next"]}))
    assert [m["text"] for m in rest["messages"]] == ["Message 2"]
    one = run(hermsy.handle({"op": "message", "id": rest["messages"][0]["id"]}))
    assert one["text"] == "Message 2"
    assert run(hermsy.handle({"op": "remove_device", "token": PHONE})) == {"ok": True}
    assert hermsy.store.device_tokens() == []


def test_bad_requests_say_why(hermsy):
    assert run(hermsy.handle({"op": "nope"}))["status"] == 400
    assert run(hermsy.handle({"op": "__init__"}))["status"] == 400
    assert run(hermsy.handle({"op": "register_device", "token": "short"}))["status"] == 400
    assert run(hermsy.handle({"op": "messages", "since": "x"}))["status"] == 400
    assert run(hermsy.handle({"op": "message", "id": "missing"}))["status"] == 404
    assert run(hermsy.handle({"op": "brief"})) == {"brief": None}


def test_cloud_speech_comes_back_as_base64_pcm(hermsy):
    async def openai(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-test"
        return httpx.Response(200, content=b"\x01\x02\x03\x04")

    hermsy._client = httpx.AsyncClient(transport=httpx.MockTransport(openai))
    spoken = run(hermsy.handle({"op": "speak", "provider": "openai", "voice": "marin", "text": "Hi."}))
    assert base64.b64decode(spoken["audio"]) == b"\x01\x02\x03\x04"
    assert spoken["sample_rate"] == 24000
    missing = run(hermsy.handle({"op": "speak", "provider": "elevenlabs", "voice": "v", "text": "Hi."}))
    assert missing["status"] == 404


# -- setup ----------------------------------------------------------------------


def test_files_are_installed_once_and_never_overwritten(tmp_path, hermsy):
    placed = hermsy.setup.install_files()
    skill = tmp_path / "skills" / "hermsy" / "chief-of-staff" / "SKILL.md"
    assert skill in map(Path, placed)
    text = skill.read_text()
    assert "{{" not in text
    assert f"{tmp_path}/scripts/hermsy/said.py" in text
    hermsy_skill = (tmp_path / "skills" / "hermsy" / "hermsy" / "SKILL.md").read_text()
    assert "{{" not in hermsy_skill and f"{tmp_path}/logs/agent.log" in hermsy_skill
    assert (tmp_path / "scripts" / "hermsy" / "observe.py").exists()
    assert (tmp_path / "hermsy" / "standing-orders.md").exists()
    skill.write_text("my own version")
    assert hermsy.setup.install_files() == []
    assert skill.read_text() == "my own version"


def test_a_chief_of_staff_of_their_own_is_left_alone(tmp_path, hermsy):
    (tmp_path / "skills" / "autonomous-ai-agents" / "proactive-chief-of-staff").mkdir(parents=True)
    hermsy.setup.install_files()
    assert not (tmp_path / "skills" / "hermsy" / "chief-of-staff").exists()
    assert (tmp_path / "skills" / "hermsy" / "hermsy" / "SKILL.md").exists()


def test_job_times_are_converted_to_hermes_time():
    lagos, utc = ZoneInfo("Africa/Lagos"), timezone.utc
    assert cron_at(((6, 0), (12, 0), (18, 0)), lagos, utc) == "0 5,11,17 * * *"
    assert cron_at(((7, 30), (12, 30), (18, 30)), ZoneInfo("Asia/Kolkata"), utc) == "0 2,7,13 * * *"
    # Across midnight the hours are listed in order.
    assert cron_at(((6, 0), (18, 0)), ZoneInfo("Pacific/Auckland"), lagos).split()[1] in ("6,18", "5,17")


def test_setup_creates_the_jobs_asked_for_once(hermsy):
    result = run(hermsy.handle({"op": "setup", "brief": True, "chief_of_staff": True, "timezone": "Africa/Lagos"}))
    assert result["created"] == ["Hermsy Brief", "Chief of Staff"]
    jobs = {job["name"]: job for job in hermsy.setup.jobs().records}
    assert jobs["Hermsy Brief"]["schedule"] == "0 5,11,17 * * *"
    assert jobs["Hermsy Brief"]["deliver"] == "hermsy"
    assert jobs["Hermsy Brief"]["script"] == "hermsy/observe_brief.py"
    assert "/usr/bin/python3" in jobs["Chief of Staff"]["prompt"]
    assert "{{" not in jobs["Chief of Staff"]["prompt"]
    assert hermsy.store.get("timezone") == "Africa/Lagos"
    again = run(hermsy.handle({"op": "setup", "brief": True, "chief_of_staff": True}))
    assert again["created"] == []
    assert run(hermsy.handle({"op": "setup", "brief": True, "timezone": "Mars/Base"}))["status"] == 400


def test_jobs_under_their_old_names_count(tmp_path):
    jobs = FakeJobs([{"id": "old", "name": "Chief of Staff (Proactive)", "deliver": "hermsy"}])
    setup = Setup(tmp_path, Store(":memory:"), jobs=jobs, hermes_zone=timezone.utc)
    assert setup.status()["chief_of_staff"]["id"] == "old"
    assert setup.run(brief=False, chief_of_staff=True, zone=None)["created"] == []


# -- the observer ---------------------------------------------------------------


def observe(home, *args):
    script = home / "scripts" / "hermsy" / "observe.py"
    out = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, check=True,
                         env={"HERMES_HOME": str(home), "PATH": "/usr/bin:/bin"})
    return json.loads(out.stdout)


def test_the_observer_reads_memory_orders_and_its_own_state(tmp_path, hermsy):
    hermsy.setup.install_files()
    hermsy.store.put("timezone", "Africa/Lagos")
    soon = date.today() + timedelta(days=10)
    (tmp_path / "memories").mkdir()
    (tmp_path / "memories" / "USER.md").write_text(
        f"Passport renewal appointment on {soon.isoformat()}.\n§\nWaiting on the landlord about the deposit.\n"
    )
    said = tmp_path / "scripts" / "hermsy" / "said.py"
    subprocess.run([sys.executable, str(said), "passport-renewal"], check=True, capture_output=True,
                   env={"HERMES_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})

    snapshot = observe(tmp_path)
    assert snapshot["timezone"] == "Africa/Lagos"
    # Only the template's real line: its examples are not orders.
    assert snapshot["standing_orders"] == "## How to tell me\n- Short and plain. Lead with what matters."
    orders = tmp_path / "hermsy" / "standing-orders.md"
    orders.write_text(orders.read_text().replace("- (e.g. Weekend chores.)", "- Weekend chores."))
    assert "## Never nag about\n- Weekend chores." in observe(tmp_path)["standing_orders"]
    assert snapshot["memory_dates_window"][0]["in_days"] == 10
    assert "landlord" in snapshot["candidate_open_loops"][0]["snippet"]
    assert "passport-renewal" in snapshot["recently_said"]
    assert snapshot["budget"]["sent_today"] == 1

    brief = observe(tmp_path, "--brief")
    assert "budget" not in brief and "memory_dates_window" in brief


def test_every_bundled_file_is_there():
    assert {p.name for p in (FILES / "skills").iterdir()} == {"hermsy", "chief-of-staff"}
    assert {p.name for p in (FILES / "jobs").iterdir()} == {"hermsy-brief.md", "chief-of-staff.md"}


# -- the adapter ----------------------------------------------------------------


def test_the_app_must_send_the_api_server_key(monkeypatch):
    monkeypatch.setattr(adapter, "secret", lambda name: "server-key-1234567890" if name == "API_SERVER_KEY" else "")
    hermsy = adapter.HermsyAdapter(adapter.PlatformConfig())
    assert hermsy.verify_http_event_request("Bearer server-key-1234567890") == (True, "")
    assert hermsy.verify_http_event_request("Bearer wrong")[0] is False
    assert hermsy.verify_http_event_request("")[0] is False


def test_push_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("HERMSY_PUSH_URL", "off")
    assert adapter.push_url() == ""
    monkeypatch.delenv("HERMSY_PUSH_URL")
    assert adapter.push_url() == "https://push.hermsy.illbytes.com"
