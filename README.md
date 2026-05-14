# whatsapp-hermes

Let your agent touch WhatsApp without giving it the keys to the kingdom.

`whatsapp-hermes` is a tiny local CLI bridge between a Hermes-style agent and a [WuzAPI](https://github.com/asternic/wuzapi) WhatsApp session. It gives your agent a boring, scriptable command line instead of a pile of HTTP endpoints, mystery payloads, and "wait, did it just reply to my dentist?" energy.

The CLI handles transport and local state. The agent handles judgment. Everybody stays in their lane. Civilization continues.

## What It Does

`whatsapp-hermes` lets an agent:

- pull WhatsApp history from WuzAPI into a local SQLite DB
- list unread inbound messages grouped by contact
- inspect the full thread before getting brave
- persist contact metadata and durable facts
- send replies through WuzAPI
- mark messages handled locally

Runtime state lives under `~/.hermes` by default:

- connection env: `~/.hermes/.env`
- SQLite DB: `~/.hermes/whatsapp_cli.db`

No hosted service. No dashboard. No "book a demo." Just a CLI and a small database.

## Install

From this repo:

```bash
python3 -m pip install -e .
```

That installs the `whatsapp` command.

Prefer a tiny sandbox, like a responsible adult?

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Then either call `.venv/bin/whatsapp` directly from your agent/cron, or symlink it somewhere memorable:

```bash
mkdir -p ~/.hermes/bin
ln -sfn "$(pwd)/.venv/bin/whatsapp" ~/.hermes/bin/whatsapp
```

## Connect WuzAPI

Point the CLI at your WuzAPI instance:

```bash
whatsapp configure \
  --base-url https://wuzapi.example.com \
  --token "$WUZAPI_TOKEN"
```

This writes:

```env
WUZAPI_BASE_URL=https://wuzapi.example.com
WUZAPI_TOKEN=...
```

to `~/.hermes/.env`.

Make sure the plumbing works:

```bash
whatsapp doctor
```

Use a different env file when you want multiple sessions or a project-local config:

```bash
whatsapp --env-file ./wuzapi.env configure --base-url http://localhost:8080 --token dev-token
whatsapp --env-file ./wuzapi.env doctor
whatsapp --env-file ./wuzapi.env check --json
```

Already managing env vars yourself? Cool, skip `configure`:

```bash
export WUZAPI_BASE_URL=https://wuzapi.example.com
export WUZAPI_TOKEN=...
whatsapp doctor
```

## Agent Prompt

Print the reusable Hermes cron prompt:

```bash
whatsapp prompt
```

If your cron uses a full path instead of `whatsapp`, bake that into the prompt:

```bash
whatsapp prompt --command ~/.hermes/bin/whatsapp
```

The source template also lives at `prompts/hermes-cron-whatsapp.md`.

The prompt is intentionally bossy. It tells the agent to read the thread before replying, preserve evidence IDs, skip junk, and return exactly `[SILENT]` when there is nothing useful to do. Agents need boundaries. We all do.

## Commands

```bash
whatsapp check --json
whatsapp sync
whatsapp unread --json
whatsapp thread <phone> --json --limit 100
whatsapp contact <phone> --json
whatsapp contact set <phone> --name "..." --company "..." --role "..." --relationship "..." --summary "..." --confidence 0.8 --evidence <message_id>
whatsapp facts add <phone> --type identity --value "..." --evidence <message_id> --confidence 0.8
whatsapp send <phone> "message"
whatsapp mark-read <message_id>
whatsapp mark-read --all
```

`check` is the cron-friendly entrypoint. It syncs new WuzAPI history, groups unprocessed inbound messages, and returns JSON:

```bash
whatsapp check --json
```

If `has_unread` is false, the agent can return exactly `[SILENT]` and stop.

Example response shape:

```json
{
  "has_unread": true,
  "unread_count": 2,
  "groups": [
    {
      "phone": "15551234567",
      "unread_count": 2,
      "thread_command": "~/.hermes/bin/whatsapp thread 15551234567 --json --limit 100"
    }
  ]
}
```

## Typical Cron Flow

1. Run `whatsapp check --json`.
2. If there are unread groups, inspect each with `whatsapp thread <phone> --json --limit 100`.
3. Read or update contact metadata with `whatsapp contact <phone> --json` and `whatsapp contact set ...`.
4. Add durable facts with `whatsapp facts add ...`.
5. Reply only when appropriate with `whatsapp send <phone> "..."`.
6. Mark handled messages with `whatsapp mark-read <message_id>`.

That gives your agent enough context to be useful, and enough friction to avoid becoming a cursed autoresponder.

## Development

```bash
python3 -m pip install -e .
python3 -m unittest discover -s tests
```
