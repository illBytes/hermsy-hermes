# Hermsy for Hermes

[Hermsy](https://play.google.com/store/apps/details?id=com.illbytes.hermsy_app) is a phone app for your own [Hermes](https://github.com/NousResearch/hermes-agent) agent: chat, voice calls, an Inbox for what Hermes sends on its own, and a Brief of your day.

Chat and calls work with Hermes as it is. This plugin adds the rest:

- **Inbox and notifications.** Cron jobs and messages with `deliver: hermsy` arrive on your phone.
- **The Hermsy Brief.** Your day at a glance, as cards in the app and a home-screen widget.
- **A Chief of Staff.** Texts you when something needs you, and stays quiet otherwise. You teach it by replying.
- **Cloud voices for calls.** OpenAI or ElevenLabs, using the keys your Hermes already has.

There is no separate server to run. The app reaches the plugin through Hermes's own API server, with the same address and key.

## Set up

**With [hermes-spawn](https://github.com/oscarfrank/hermes-spawn)** (Hermes in Docker), one command does all of it: the API server, this plugin, an HTTPS address (Caddy, Cloudflare Tunnel or Tailscale), and a QR code for the app:

```bash
hermes-spawn connect <name>
```

Then install Hermsy, tap **Scan QR code**, and you're done.

**By hand, in three steps.** You need:
- Hermes 0.19 (v2026.7.20) or newer.
- Its API server switched on (`API_SERVER_ENABLED=true` and `API_SERVER_KEY`).
- The API server reachable from your phone over HTTPS, for example behind Caddy or a Cloudflare tunnel.

**1. Install the plugin** where Hermes runs, then restart Hermes:

```bash
hermes plugins install illbytes/hermsy-hermes --enable
```

With Docker, run it inside the container, as the user Hermes runs as:

```bash
docker exec -u <hermes-uid> hermes hermes plugins install illbytes/hermsy-hermes --enable
docker restart hermes
```

**2. Install Hermsy** on your phone from Google Play. Open it, create a PIN, and enter your Hermes address and API key.

**3. Turn on what you want** in Hermsy, under **Settings → Hermes**:
- It shows that the plugin is connected.
- **Set up** the Hermsy Brief and the Chief of Staff. They run at times in your time zone.
- Allow notifications when asked.

That's it. To check it end to end, send yourself something:

```bash
hermes send -t hermsy "Hello from Hermes"
```

## What it puts on your Hermes

On first start the plugin copies these into your Hermes home. They are yours to edit; it never overwrites them.

| Path | What |
|---|---|
| `skills/hermsy/hermsy/SKILL.md` | Tells your agent how to reach your phone and how the Brief works |
| `skills/hermsy/chief-of-staff/SKILL.md` | How to handle replies, mutes, snoozes and standing orders |
| `scripts/hermsy/observe.py` | Builds the snapshot the Brief and the Chief of Staff read (no model involved) |
| `scripts/hermsy/said.py`, `suppress.py` | The Chief of Staff's bookkeeping |
| `hermsy/standing-orders.md` | Your instructions: focus, priorities, what to always flag, what never to nag about |
| `hermsy/hermsy.db` | Messages, the latest Briefs, and your phones' push tokens |

The two jobs are created only when you tap **Set up** in the app:
- **Hermsy Brief:** 06:00, 12:00 and 18:00.
- **Chief of Staff:** 07:30, 12:30 and 18:30.

Both are in your time zone and deliver to `hermsy`. Change them like any cron job (`hermes cron edit <id> ...`). Keep the name "Hermsy Brief": the app finds the Brief by it.

## Making the Chief of Staff yours

Tell your agent what matters, in plain words, in any chat:
- "From now on, always flag when a bill is overdue."
- "This month my focus is the house move."
- "Stop reminding me about the gym."

It updates `hermsy/standing-orders.md` and confirms in one line.

Reply to a text to steer it:
- "not now" snoozes a topic
- "never again" mutes it
- "yes, do it" approves a suggestion

Out of the box it sees the agent's own memory, upcoming dates, open loops and failed jobs. If your Hermes has calendar or task tools, it can also check them, read-only. To feed it more (your task app, a calendar export), add a function to `scripts/hermsy/observe.py`. The Chief of Staff skill explains how.

## Privacy

- Messages, Briefs and your standing orders stay on your Hermes. The app fetches them over your own HTTPS connection with your API key.
- Notifications go through Firebase Cloud Messaging. Google only accepts pushes for the Hermsy app from its publisher, so the plugin asks **illbytes' push service** (`push.hermsy.illbytes.com`) to wake your phone.
  - The push service is given your phone's push token and nothing else.
  - The push itself carries nothing: its whole content is `{"type": "wake"}`, with no text, title or message ID. Your phone then asks your Hermes, with your key, what is new, and builds the notification itself.

  | | Sees |
  |---|---|
  | Google / Firebase | That the app on your phone was woken, and when. Nothing else. |
  | illbytes' push service | Your phone's push token, and when it was woken. It stores neither. |
  | Your Hermes | The messages (it wrote them). |
  | Your lock screen | "Hermsy" only; the text shows after you unlock. |
  - Message text, titles, your Hermes address and your key never leave your server.
- To run without the push service, set `HERMSY_PUSH_URL=off`. The Inbox then fills whenever you open Hermsy.

## Settings

All optional, in Hermes's `.env`:

| Variable | Default | |
|---|---|---|
| `HERMSY_PUSH_URL` | `https://push.hermsy.illbytes.com` | The push service; `off` to turn pushes off |
| `HERMSY_HOME_CHANNEL` | `phone` | The home channel `deliver: hermsy` uses |
| `HERMSY_RESTART_NOTICES` | off | `on` to get Hermes's "shutting down / restarting" notices on the phone |
| `OPENAI_API_KEY`, `ELEVENLABS_API_KEY` | | Turn on that provider's voices for calls |

## When something is off

- **Hermsy says "Not installed".**
  - Check `hermes plugins list` shows `hermsy` as enabled, and that Hermes was restarted.
  - The API server must be on: Hermsy talks to the plugin through it.
- **No notifications.**
  - Check Hermsy's Settings → Notifications.
  - Check Hermes's `logs/agent.log` (in its home folder) shows `[Hermsy] Ready` at start-up.
  - A push error in the log names the push service and what it answered.
  - Messages still reach the Inbox when you open the app.
- **No Brief.**
  - Set it up in the app, or check `hermes cron list` for "Hermsy Brief".
  - A reply that isn't valid JSON lands in the Inbox instead, so nothing is lost.

## Removing it

```bash
hermes plugins disable hermsy
```

Then delete the jobs (`hermes cron list`, then `hermes cron remove <id>`) and, if you like, the files above.
