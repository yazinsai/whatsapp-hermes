from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_phone(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = str(value)
    text = text.split("@", 1)[0]
    text = text.split(":", 1)[0]
    return re.sub(r"\D", "", text)


def get_path(data: Any, *paths: str) -> Any:
    for path in paths:
        cur = data
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return None


def flatten_json(value: Any) -> list[Any]:
    out: list[Any] = []
    if isinstance(value, dict):
        for item in value.values():
            out.extend(flatten_json(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(flatten_json(item))
    else:
        out.append(value)
    return out


def extract_text(payload: dict[str, Any]) -> str:
    candidates = [
        "event.Message.extendedTextMessage.text",
        "event.Message.conversation",
        "event.Message.imageMessage.caption",
        "event.Message.videoMessage.caption",
        "event.Message.documentMessage.caption",
        "event.RawMessage.extendedTextMessage.text",
        "event.RawMessage.conversation",
        "event.body",
        "event.text",
        "event.message",
        "Message.extendedTextMessage.text",
        "Message.conversation",
        "Message.imageMessage.caption",
        "Message.videoMessage.caption",
        "Message.documentMessage.caption",
        "RawMessage.extendedTextMessage.text",
        "RawMessage.conversation",
        "body",
        "text",
        "message",
        "Info.Message",
        "Data.Message.Conversation",
        "Data.Message.ExtendedTextMessage.Text",
        "data.Message.Conversation",
        "data.Message.ExtendedTextMessage.Text",
    ]
    for path in candidates:
        val = get_path(payload, path)
        if isinstance(val, str) and val.strip():
            return val.strip()

    ignored_fragments = ("@s.whatsapp.net", "@g.us", "@lid")
    for val in flatten_json(payload):
        if not isinstance(val, str):
            continue
        text = val.strip()
        if len(text) < 2 or len(text) > 4000:
            continue
        if text.lower() in {"text", "image", "video", "audio", "document", "sticker"}:
            continue
        if any(fragment in text for fragment in ignored_fragments):
            continue
        if re.fullmatch(r"[A-Za-z0-9._:-]{12,}", text):
            continue
        return text
    return ""


def message_identity(payload: dict[str, Any]) -> dict[str, Any]:
    event_payload = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    info = get_path(event_payload, "Info", "data.Info", "Data.Info") or {}
    if not isinstance(info, dict):
        info = {}

    chat_id = (
        get_path(event_payload, "chatId", "chat_id", "from", "Chat", "data.chatId", "data.from")
        or info.get("Chat")
        or info.get("Sender")
        or ""
    )
    is_from_me = bool(
        get_path(event_payload, "fromMe", "IsFromMe", "data.fromMe")
        or info.get("IsFromMe")
        or get_path(event_payload, "Info.IsFromMe", "data.Info.IsFromMe", "Data.Info.IsFromMe")
    )
    sender_id = (
        info.get("RecipientAlt") if is_from_me else info.get("SenderAlt")
    ) or (
        get_path(event_payload, "senderId", "sender", "Sender", "data.senderId", "data.sender")
        or info.get("Sender")
        or chat_id
        or ""
    )
    phone = normalize_phone(sender_id or chat_id)
    message_id = (
        get_path(event_payload, "messageId", "id", "ID", "data.messageId", "Data.ID")
        or info.get("ID")
        or hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    )
    timestamp = (
        get_path(event_payload, "timestamp", "Timestamp", "time", "created_at", "data.timestamp")
        or info.get("Timestamp")
        or now_iso()
    )
    sender_name = (
        get_path(event_payload, "senderName", "pushName", "Name", "data.senderName", "Data.PushName")
        or get_path(event_payload, "Info.PushName", "data.Info.PushName", "Data.Info.PushName")
        or phone
        or "Unknown"
    )
    return {
        "id": str(message_id),
        "direction": "outbound" if is_from_me else "inbound",
        "phone": phone,
        "chat_id": str(chat_id or ""),
        "sender_name": str(sender_name or ""),
        "timestamp": normalize_timestamp(timestamp),
        "text": extract_text(payload) or extract_text(event_payload),
    }


def normalize_timestamp(value: Any) -> str:
    if value in (None, ""):
        return now_iso()
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds = seconds / 1000
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    text = str(value)
    if text.isdigit():
        return normalize_timestamp(int(text))
    return text


def harvest_message_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return

        data_json = node.get("data_json")
        if isinstance(data_json, str) and data_json.strip().startswith(("{", "[")):
            try:
                visit(json.loads(data_json))
            except json.JSONDecodeError:
                pass

        identity = message_identity(node)
        if identity["phone"] and ("Message" in node or "RawMessage" in node or identity["text"]):
            found.append(node)
            if "Message" in node or "RawMessage" in node:
                return

        for child in node.values():
            if isinstance(child, (dict, list)):
                visit(child)

    visit(value)

    unique: dict[str, dict[str, Any]] = {}
    for item in found:
        unique[message_identity(item)["id"]] = item
    return list(unique.values())
