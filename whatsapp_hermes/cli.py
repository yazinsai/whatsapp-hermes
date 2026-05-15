from __future__ import annotations

import argparse
import json
import os
import sys
from importlib import resources
from pathlib import Path
from typing import Any

from .attachment_text import extract_pdf_text
from .db import DEFAULT_DB_PATH, Store
from .normalize import normalize_phone
from .wuzapi import WuzAPIClient, default_env_path, load_dotenv


ROOT_HELP = """examples:
  whatsapp configure --base-url https://wuzapi.example.com --token "$WUZAPI_TOKEN"
  whatsapp doctor
  whatsapp check --json
  whatsapp sync --limit 1000
  whatsapp unread --json
  whatsapp thread 97333601374 --json --limit 100
  whatsapp contact 97333601374 --json
  whatsapp contact set 97333601374 --name "Mannan" --confidence 0.8 --evidence MSG1
  whatsapp facts add 97333601374 --type identity --value "Met at LEAP" --evidence MSG1
  whatsapp send 97333601374 "on my way"
  whatsapp mark-read MSG1
  whatsapp mark-read --all
"""

CONFIG_HELP = """examples:
  whatsapp configure --base-url https://wuzapi.example.com --token "$WUZAPI_TOKEN"
  whatsapp configure --base-url http://localhost:8080 --token dev-token --env-file ./wuzapi.env
"""

CONTACT_HELP = """examples:
  whatsapp contact 97333601374
  whatsapp contact 97333601374 --json
  whatsapp contact set 97333601374 --name "Mannan" --company "Acme" --confidence 0.8
"""

FACTS_HELP = """examples:
  whatsapp facts add 97333601374 --type identity --value "Met at LEAP"
  whatsapp facts add 97333601374 --type preference --value "Prefers WhatsApp" --evidence MSG1 --confidence 0.8
"""


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "env_file", None):
        os.environ["WUZAPI_ENV_FILE"] = str(Path(args.env_file).expanduser())
        os.environ["WUZAPI_ENV_FILE_OVERRIDE"] = "1"
    if args.command is None:
        parser.print_help()
        return 2
    if args.command == "configure":
        return cmd_configure(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "prompt":
        return cmd_prompt(args)
    if args.command == "contact" and wants_help(args.contact_args):
        return print_contact_help(args.contact_args)
    if args.command == "facts" and wants_help(args.facts_args):
        return print_facts_help(args.facts_args)

    store = Store(Path(args.db))
    store.init()

    try:
        if args.command == "check":
            return cmd_check(args, store)
        if args.command == "sync":
            return cmd_sync(args, store)
        if args.command == "unread":
            return cmd_unread(args, store)
        if args.command == "thread":
            return cmd_thread(args, store)
        if args.command == "contact":
            return cmd_contact(args, store)
        if args.command == "facts":
            return cmd_facts(args, store)
        if args.command == "send":
            return cmd_send(args, store)
        if args.command == "mark-read":
            return cmd_mark_read(args, store)
    except Exception as exc:
        print(f"whatsapp: error: {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whatsapp",
        description="Local WhatsApp CLI bridge for Hermes-style agents using WuzAPI.",
        epilog=ROOT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--env-file",
        help=f"Connection env file (default: {default_env_path()})",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=f"SQLite database path (default: {DEFAULT_DB_PATH})",
    )
    sub = parser.add_subparsers(dest="command")

    configure = sub.add_parser(
        "configure",
        help="Save WuzAPI connection settings",
        epilog=CONFIG_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    configure.add_argument("--base-url", required=True, help="Base URL of your WuzAPI instance")
    configure.add_argument("--token", required=True, help="WuzAPI token for the WhatsApp session")
    configure.add_argument("--json", action="store_true")

    doctor = sub.add_parser("doctor", help="Check local config and WuzAPI connectivity")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--no-network", action="store_true", help="Only validate local configuration")

    prompt = sub.add_parser("prompt", help="Print the reusable Hermes cron prompt")
    prompt.add_argument(
        "--command",
        dest="prompt_command",
        default="whatsapp",
        help="CLI command/path to put in the prompt examples (default: whatsapp)",
    )

    check = sub.add_parser("check", help="Sync and show whether any inbound messages need handling")
    check.add_argument("--json", action="store_true")
    check.add_argument("--limit", type=int, default=1000)
    check.add_argument("--workers", type=int, default=8)
    check.add_argument(
        "--no-download-attachments",
        action="store_true",
        help="Do not download pending inbound media attachments during check",
    )

    sync = sub.add_parser("sync", help="Pull new messages from WuzAPI history endpoints")
    sync.add_argument("--json", action="store_true")
    sync.add_argument("--limit", type=int, default=1000)
    sync.add_argument("--endpoint", help="Override WuzAPI history endpoint path")
    sync.add_argument("--workers", type=int, default=8)
    sync.add_argument(
        "--download-attachments",
        action="store_true",
        help="Download pending inbound media attachments after syncing",
    )
    sync.add_argument(
        "--no-download-attachments",
        action="store_true",
        help="Leave media attachments pending after syncing",
    )

    unread = sub.add_parser("unread", help="Show unprocessed inbound messages")
    unread.add_argument("--json", action="store_true")

    thread = sub.add_parser("thread", help="Show contact profile and recent conversation")
    thread.add_argument("phone")
    thread.add_argument("--json", action="store_true")
    thread.add_argument("--limit", type=int, default=100)

    contact = sub.add_parser(
        "contact",
        help="Get or set contact metadata",
        usage="whatsapp contact <phone> [--json] | whatsapp contact set <phone> [options]",
        epilog=CONTACT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    contact.add_argument("contact_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    facts = sub.add_parser(
        "facts",
        help="Manage contact facts",
        usage="whatsapp facts add <phone> --type <type> --value <value> [options]",
        epilog=FACTS_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    facts.add_argument("facts_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    send = sub.add_parser("send", help="Send a WhatsApp text message through WuzAPI")
    send.add_argument("phone")
    send.add_argument("message")
    send.add_argument("--json", action="store_true")

    mark_read = sub.add_parser("mark-read", help="Mark inbound messages handled locally")
    mark_read.add_argument("message_id", nargs="?", help="Inbound message ID to mark handled")
    mark_read.add_argument("--all", action="store_true", help="Mark all unread inbound messages handled")
    mark_read.add_argument("--json", action="store_true")

    return parser


def cmd_configure(args: argparse.Namespace) -> int:
    env_path = Path(args.env_file).expanduser() if args.env_file else default_env_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    base_url = args.base_url.rstrip("/")
    lines = [
        "# whatsapp-hermes WuzAPI connection",
        f"WUZAPI_BASE_URL={shell_quote_env_value(base_url)}",
        f"WUZAPI_TOKEN={shell_quote_env_value(args.token)}",
    ]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    output = {"ok": True, "env_file": str(env_path), "base_url": base_url, "token": mask_secret(args.token)}
    if args.json:
        print_json(output)
    else:
        print(f"saved WuzAPI config to {env_path}")
        print(f"base_url={base_url}")
        print(f"token={mask_secret(args.token)}")
        print("run `whatsapp doctor` to test the connection")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    env_path = Path(args.env_file).expanduser() if args.env_file else default_env_path()
    load_dotenv(env_path, override=bool(args.env_file))
    base_url = os.environ.get("WUZAPI_BASE_URL", "").rstrip("/")
    token = os.environ.get("WUZAPI_TOKEN", "")
    output: dict[str, Any] = {
        "ok": False,
        "env_file": str(env_path),
        "env_file_exists": env_path.exists(),
        "base_url": base_url or None,
        "token_set": bool(token),
        "network": None,
    }
    if not base_url or not token:
        output["error"] = "missing WUZAPI_BASE_URL or WUZAPI_TOKEN"
        return print_doctor(output, args.json)
    if args.no_network:
        output["ok"] = True
        return print_doctor(output, args.json)

    try:
        client = WuzAPIClient(base_url=base_url, token=token)
        data = client.request("GET", "/user/contacts", timeout=20)
        contact_count = len(data.get("data", {})) if isinstance(data, dict) and isinstance(data.get("data"), dict) else None
        output["network"] = {"ok": True, "endpoint": "/user/contacts", "contact_count": contact_count}
        output["ok"] = True
    except Exception as exc:
        output["network"] = {"ok": False, "endpoint": "/user/contacts", "error": str(exc)}
        output["error"] = str(exc)
    return print_doctor(output, args.json)


def print_doctor(output: dict[str, Any], as_json: bool) -> int:
    if as_json:
        print_json(output)
    else:
        print(f"env_file: {output['env_file']} ({'found' if output['env_file_exists'] else 'missing'})")
        print(f"base_url: {output['base_url'] or 'missing'}")
        print(f"token: {'set' if output['token_set'] else 'missing'}")
        network = output.get("network")
        if network:
            print(f"network: {'ok' if network.get('ok') else 'failed'} {network.get('endpoint')}")
            if network.get("contact_count") is not None:
                print(f"contacts: {network['contact_count']}")
            if network.get("error"):
                print(f"error: {network['error']}")
        elif output.get("ok"):
            print("network: skipped")
        if not output.get("ok") and output.get("error") and not network:
            print(f"error: {output['error']}")
    return 0 if output.get("ok") else 1


def cmd_prompt(args: argparse.Namespace) -> int:
    prompt = resources.files("whatsapp_hermes.prompts").joinpath("hermes-cron-whatsapp.md").read_text(encoding="utf-8")
    print(prompt.replace("{{WHATSAPP_COMMAND}}", args.prompt_command))
    return 0


def wants_help(values: list[str]) -> bool:
    return any(value in {"-h", "--help"} for value in values)


def print_contact_help(values: list[str]) -> int:
    if values and values[0] == "set":
        build_contact_set_parser().print_help()
    else:
        build_contact_parser().print_help()
    return 0


def print_facts_help(values: list[str]) -> int:
    if values and values[0] == "add":
        build_facts_add_parser().print_help()
    else:
        build_facts_parser().print_help()
    return 0


def cmd_check(args: argparse.Namespace, store: Store) -> int:
    sync_details = run_sync(
        store,
        limit=args.limit,
        workers=args.workers,
        download_attachments=not args.no_download_attachments,
    )
    groups = store.unread_groups()
    summaries = []
    for group in groups:
        messages = group["messages"]
        latest = messages[-1] if messages else None
        summaries.append(
            {
                "phone": group["phone"],
                "chat_id": group["chat_id"],
                "unread_count": len(messages),
                "latest_message": latest,
                "thread_command": f"~/.hermes/bin/whatsapp thread {group['phone']} --json --limit 100",
            }
        )

    output = {
        "has_unread": bool(groups),
        "unread_count": sum(item["unread_count"] for item in summaries),
        "groups": summaries,
        "sync": {
            "fetched": sync_details["fetched"],
            "upserted": sync_details["upserted"],
            "attachments": sync_details["attachments"],
        },
    }
    if args.json:
        print_json(output)
    elif not groups:
        print("[SILENT]")
    else:
        for group in summaries:
            latest = group["latest_message"] or {}
            print(f"{group['phone']} ({group['unread_count']}): {latest.get('text', '')}")
    return 0


def cmd_sync(args: argparse.Namespace, store: Store) -> int:
    details = run_sync(
        store,
        limit=args.limit,
        endpoint=args.endpoint,
        workers=args.workers,
        download_attachments=args.download_attachments and not args.no_download_attachments,
    )
    if args.json:
        print_json(details)
    else:
        endpoint = next((item["endpoint"] for item in details["endpoints"] if item["ok"]), "none")
        print(f"synced fetched={details['fetched']} upserted={details['upserted']} endpoint={endpoint}")
    return 0


def run_sync(
    store: Store,
    *,
    limit: int,
    endpoint: str | None = None,
    workers: int = 8,
    download_attachments: bool = False,
) -> dict[str, Any]:
    run_id = store.start_run("sync")
    try:
        client = WuzAPIClient()
        messages, endpoint_results = client.fetch_history(
            limit=limit,
            endpoint=endpoint,
            workers=workers,
        )
        inserted = 0
        for message in messages:
            if store.upsert_message(message):
                inserted += 1
        attachment_details = (
            download_pending_attachments(store, client)
            if download_attachments
            else {"attempted": 0, "downloaded": 0, "failed": 0}
        )
        details = {
            "fetched": len(messages),
            "upserted": inserted,
            "attachments": attachment_details,
            "endpoints": [result.__dict__ for result in endpoint_results],
        }
        store.finish_run(run_id, "ok", details)
        return details
    except Exception as exc:
        store.finish_run(run_id, "error", error=str(exc))
        raise


def download_pending_attachments(store: Store, client: WuzAPIClient) -> dict[str, int]:
    attempted = downloaded = failed = 0
    for attachment in store.pending_attachments(inbound_only=True):
        attempted += 1
        try:
            result = client.download_media(attachment)
            text = ""
            error = None
            if is_pdf_attachment(attachment, result["local_path"]):
                try:
                    text = extract_pdf_text(result["local_path"])
                except Exception as exc:
                    error = str(exc)
            store.update_attachment(
                attachment["id"],
                status="ready",
                local_path=result["local_path"],
                size_bytes=result["size_bytes"],
                sha256=result["sha256"],
                text=text,
                error=error,
            )
            downloaded += 1
        except Exception as exc:
            store.update_attachment(attachment["id"], status="error", error=str(exc))
            failed += 1
    return {"attempted": attempted, "downloaded": downloaded, "failed": failed}


def is_pdf_attachment(attachment: dict[str, Any], local_path: str) -> bool:
    mime_type = str(attachment.get("mime_type") or "").split(";", 1)[0].lower()
    return mime_type == "application/pdf" or local_path.lower().endswith(".pdf")


def cmd_unread(args: argparse.Namespace, store: Store) -> int:
    groups = store.unread_groups()
    if args.json:
        print_json({"groups": groups})
    else:
        for group in groups:
            print(f"{group['phone']} ({len(group['messages'])})")
            for message in group["messages"]:
                print(f"  {message['id']}: {message['text']}")
    return 0


def cmd_thread(args: argparse.Namespace, store: Store) -> int:
    data = store.thread(args.phone, args.limit)
    if args.json:
        print_json(data)
    else:
        contact = data["contact"]
        print(f"{contact.get('name') or contact['phone']} ({contact['phone']})")
        if contact.get("summary"):
            print(contact["summary"])
        for message in data["messages"]:
            print(f"{message['timestamp']} {message['direction']}: {message['text']}")
    return 0


def cmd_contact(args: argparse.Namespace, store: Store) -> int:
    if not args.contact_args:
        build_contact_parser().print_help()
        return 2
    if args.contact_args[0] == "set":
        parser = build_contact_set_parser()
        ns = parser.parse_args(args.contact_args[1:])
        updates = {
            "display_name": ns.display_name,
            "company": ns.company,
            "role": ns.role,
            "relationship": ns.relationship,
            "summary": ns.summary,
            "confidence": ns.confidence,
            "needs_review": ns.needs_review,
        }
        data = store.set_contact(ns.phone, updates, ns.evidence)
        print_json(data)
        return 0

    parser = build_contact_parser()
    ns = parser.parse_args(args.contact_args)
    data = store.contact(ns.phone)
    if ns.json:
        print_json(data)
    else:
        print_contact(data)
    return 0


def cmd_facts(args: argparse.Namespace, store: Store) -> int:
    if not args.facts_args or args.facts_args[0] != "add":
        build_facts_parser().print_help()
        return 2
    parser = build_facts_add_parser()
    ns = parser.parse_args(args.facts_args[1:])
    data = store.add_fact(ns.phone, ns.fact_type, ns.value, ns.evidence, ns.confidence)
    print_json(data)
    return 0


def build_contact_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whatsapp contact",
        description="Show contact metadata and learned facts.",
        epilog=CONTACT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("phone", help="Phone number or WhatsApp JID")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    return parser


def build_contact_set_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whatsapp contact set",
        description="Set contact profile metadata.",
    )
    parser.add_argument("phone", help="Phone number or WhatsApp JID")
    parser.add_argument("--name", dest="display_name", help="Display name")
    parser.add_argument("--company", help="Company or organization")
    parser.add_argument("--role", help="Role or title")
    parser.add_argument("--relationship", help="Relationship context")
    parser.add_argument("--summary", help="Short contact summary")
    parser.add_argument("--confidence", type=float, help="Confidence score from 0.0 to 1.0")
    parser.add_argument(
        "--needs-review",
        type=int,
        choices=[0, 1],
        help="Whether Hermes should review this contact",
    )
    parser.add_argument(
        "--evidence",
        action="append",
        default=[],
        help="Message ID supporting the update; can repeat",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    return parser


def build_facts_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whatsapp facts",
        description="Manage durable facts attached to a contact.",
        epilog=FACTS_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("facts_command", nargs="?", choices=["add"], help="Facts command")
    return parser


def build_facts_add_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whatsapp facts add",
        description="Add a durable fact to a contact.",
    )
    parser.add_argument("phone")
    parser.add_argument("--type", required=True, dest="fact_type", help="Fact category, e.g. identity or preference")
    parser.add_argument("--value", required=True, help="Fact text")
    parser.add_argument("--evidence", help="Message ID supporting the fact")
    parser.add_argument("--confidence", type=float, help="Confidence score from 0.0 to 1.0")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    return parser


def cmd_send(args: argparse.Namespace, store: Store) -> int:
    phone = normalize_phone(args.phone)
    client = WuzAPIClient()
    result = client.send_text(phone, args.message)
    message_id = extract_sent_id(result)
    store.record_outgoing(phone, message_id, args.message, result)
    output = {"ok": True, "phone": phone, "message_id": message_id, "wuzapi": result}
    if args.json:
        print_json(output)
    else:
        print(f"sent {message_id} to {phone}")
    return 0


def cmd_mark_read(args: argparse.Namespace, store: Store) -> int:
    if args.all:
        if args.message_id:
            raise RuntimeError("usage: whatsapp mark-read <message_id> | whatsapp mark-read --all")
        count = store.mark_all_read()
        if args.json:
            print_json({"ok": True, "marked": count})
        else:
            print(f"marked {count}")
        return 0
    if not args.message_id:
        raise RuntimeError("usage: whatsapp mark-read <message_id> | whatsapp mark-read --all")
    ok = store.mark_read(args.message_id)
    if args.json:
        print_json({"ok": ok, "message_id": args.message_id})
    else:
        print("marked" if ok else "not_found")
    return 0 if ok else 1


def extract_sent_id(result: dict[str, Any]) -> str:
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, dict):
        for key in ("Id", "id", "ID"):
            if data.get(key):
                return str(data[key])
    for key in ("Id", "id", "ID", "client_message_id"):
        if result.get(key):
            return str(result[key])
    return str(result)


def shell_quote_env_value(value: str) -> str:
    if not value:
        return '""'
    if all(ch.isalnum() or ch in "._-/:=@%" for ch in value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def print_contact(data: dict[str, Any]) -> None:
    contact = data["contact"]
    for key in ("phone", "name", "company", "role", "relationship", "summary", "confidence", "needs_review"):
        print(f"{key}: {contact.get(key)}")
    if data.get("facts"):
        print("facts:")
        for fact in data["facts"]:
            print(f"  - {fact['type']}: {fact['value']}")


if __name__ == "__main__":
    raise SystemExit(main())
