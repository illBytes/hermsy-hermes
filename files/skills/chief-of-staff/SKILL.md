---
name: chief-of-staff
description: "Use when the person replies to a Chief of Staff text, asks to mute, snooze or approve a nudge, or tells you how to look after them (\"from now on\", \"always flag\", \"stop raising\")."
version: 1.0.0
tags: [hermsy, proactive, chief-of-staff, cron, nudges, standing-orders]
related_skills: [hermsy]
---

# Chief of Staff

Your unprompted layer, installed by the Hermsy plugin. Three times a day a cron job looks at a snapshot of the person's world and decides whether to text them through the Hermsy app. It suggests; it never acts. What they say back teaches it.

This skill is theirs to change. Hermsy installed it once and never overwrites it.

## Parts
- **Observer:** `{{SCRIPTS}}/observe.py [--pretty]` builds the snapshot without a model. It includes:
  - the time and slot in their time zone
  - standing orders
  - dates coming up and open loops from your memory (MEMORY.md, USER.md)
  - jobs that failed in the last 24 hours
  - topics already raised (`recently_said`), suppressions, and approvals waiting
  - a budget: at most 3 texts a day, none from 22:00 to 07:00 their time
- **Judge:** the cron job "Chief of Staff" (by default 07:30, 12:30 and 18:30 their time, `deliver: hermsy`). It runs the observer and replies `[SILENT]` when there is nothing worth a text; Hermes then sends nothing.
- **Standing orders:** `{{DATA}}/standing-orders.md`, their own instructions. Read on every run.
- **Bookkeeping:**
  - `{{PYTHON}} {{SCRIPTS}}/said.py <slug>...` records which topics a text raised, before it is sent.
  - `said.py --list [--days N]` shows what recent texts raised.
- **Feedback:** `{{PYTHON}} {{SCRIPTS}}/suppress.py add|remove|list|approve|done`. State lives in `{{DATA}}/state/`.

## When the person replies
Their replies to a text arrive as a new chat in the Hermsy app, or wherever they talk to you. Replies are natural language, never commands. Work out what they mean, act, then confirm in one short line:
- "got it" / "handled" / "ok" → no action, a one-line acknowledgement.
- "stop reminding me about X" / "never again" → `suppress.py add <slug> --forever --reason "asked"`
- "not now" / "later" / "remind me in N days" → `suppress.py add <slug> --days N --reason "snoozed"` (default 7)
- "yes" / "do it" / "go ahead" to a proposal → `suppress.py approve <slug> "<what was proposed>"`. Do it now if it is within what you normally do for them, then `suppress.py done <slug>`. Tell them what you did.
- Anything else → answer from context; if you lack the data, say so.

To find the slug, `said.py --list --days 2` shows what recent texts raised; match the topic they mean. If unsure, ask one short question.

## Teaching it
- **Standing orders from chat.** When they tell you how to look after them, edit `{{DATA}}/standing-orders.md` in that turn, without asking. That covers:
  - a priority ("family comes first")
  - a focus ("this month is the house move")
  - a rule ("never remind me about chores on weekends")
  - a flag ("always tell me when a bill is late")
  - how they like to be told

  Put it under the right heading in their own words, tightened. Reword or remove a line it contradicts rather than adding a conflict. Add one line to `## Change log` (date · change · their words). Confirm in one line quoting the new text. Removing a whole section, or more than two lines: confirm first.
- **What triggers it:** "from now on", "always/never tell me", "I care more about", "stop raising", "priority this month is". A one-off ("not today") is a snooze, not a standing order.
- **"Why did you raise X?"** Answer from the standing orders and the snapshot, and offer to change the order.

## Making it see more
The observer reads only what every Hermes has. To add a signal (their task app, a calendar export, a finance summary):
1. Add a function to `{{SCRIPTS}}/observe.py` that returns a small dict.
2. Add it to `snapshot` in `main()`.
3. Add one line about it under WHAT MATTERS in the job's prompt (`hermes cron edit <id> --prompt ...`).

Keep the snapshot under about 6 KB; it goes to the model on every run. Keep private data out: percentages rather than balances, no account numbers.

## Operating rules
- The text reads like a person: plain prose, no bullets, headers, tags, emoji, slugs or "reply with" footers. The job name is the notification's title and the first line is its preview, so lead with the point.
- Every topic has a stable slug; repeats are judged by slug. The budget counts `said.py` records, so a text sent without recording breaks the budget.
- Scheduled runs never act. Only replies may lead to action.
- To test the voice: `hermes cron run <id>`. Outside the morning slot, or once today's topics are recorded, a correct run is `[SILENT]`. To force a text, add a temporary line to the prompt: "TEST RUN: ignore budget.must_be_silent and recently_said."
