---
name: hermsy
description: "Use when sending something to the person's phone (deliver: hermsy), setting up or changing the Hermsy Brief, or when they ask about the Hermsy app, its Inbox or notifications."
version: 1.0.0
tags: [hermsy, phone, notifications, cron, brief]
related_skills: [chief-of-staff]
---

# Hermsy

Hermsy is the person's phone app for you: chat, voice calls, an Inbox for what you send on your own, and a Brief of their day. The Hermsy plugin connects you to it.

This skill is theirs to change. Hermsy installed it once and never overwrites it.

## Sending to the phone
- In a cron job, set `deliver: hermsy`. To keep another channel as well, use `deliver: telegram,hermsy`.
- From a chat, use the send_message tool with target `hermsy`.
- Each message is filed in the Hermsy Inbox, and the phone gets a notification:
  - the title is the job's name ("Hermsy" for anything else)
  - the preview is your first line
  - so lead with the point
- Markdown renders in the app. Keep a notification's first line plain.
- What travels: the plugin keeps the message on this Hermes. The phone is woken by a content-free push (through illbytes' push service, which never sees text) and fetches the message from here over the person's own connection.
- They reply in the app as a new chat with you, not inside the cron job.

## The Brief
- The cron job named exactly **"Hermsy Brief"** writes it, by default at 06:00, 12:00 and 18:00 their time.
- Its reply must be one JSON object (greeting, headline, summary, sections). The plugin stores it as the Brief instead of an Inbox message. A reply that is not valid JSON lands in the Inbox instead, so nothing is lost.
- The app's Refresh runs the job on demand.
- Keep the job's name. The app finds the job, and recognises its replies, by that name.
- Change what it covers by editing the prompt, or the standing orders at `{{DATA}}/standing-orders.md`.
- It runs `{{SCRIPTS}}/observe.py --brief` first, and can use read-only calendar or task tools if you have them.

## Setup and repair
- In the app, Settings → Hermes server shows what the plugin found, and offers to create the Hermsy Brief and the Chief of Staff jobs, at times in the person's time zone.
- Plugin data is in `{{DATA}}/` (`hermsy.db` holds messages, Briefs and phones).
- Scripts are in `{{SCRIPTS}}/`, and are theirs to edit.
- **Notifications not arriving:**
  - In the app, check Settings → Notifications.
  - The gateway log should show `[Hermsy] Ready` at start-up.
  - A push error names the push service (`push.hermsy.illbytes.com` by default, or `HERMSY_PUSH_URL` if set).
- **Messages still reach the Inbox without push:** the app catches up whenever it opens.
