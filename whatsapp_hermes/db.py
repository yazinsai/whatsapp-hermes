from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .normalize import message_identity, normalize_phone, now_iso


HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
DEFAULT_DB_PATH = HERMES_HOME / "whatsapp_cli.db"


class Store:
    def __init__(self, path: Path = DEFAULT_DB_PATH) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def init(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS messages (
                  id TEXT PRIMARY KEY,
                  source TEXT NOT NULL DEFAULT 'wuzapi',
                  direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
                  phone TEXT NOT NULL,
                  chat_id TEXT,
                  sender_name TEXT,
                  timestamp TEXT NOT NULL,
                  text TEXT NOT NULL DEFAULT '',
                  raw_json TEXT NOT NULL,
                  processed INTEGER NOT NULL DEFAULT 0,
                  sent_via_cli INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_messages_unread
                  ON messages(direction, processed, timestamp);

                CREATE INDEX IF NOT EXISTS idx_messages_phone_timestamp
                  ON messages(phone, timestamp);

                CREATE TABLE IF NOT EXISTS contacts (
                  phone TEXT PRIMARY KEY,
                  display_name TEXT,
                  company TEXT,
                  role TEXT,
                  relationship TEXT,
                  summary TEXT,
                  confidence REAL,
                  needs_review INTEGER NOT NULL DEFAULT 1,
                  evidence_message_ids TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS contact_facts (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  phone TEXT NOT NULL,
                  fact_type TEXT NOT NULL,
                  value TEXT NOT NULL,
                  evidence_message_id TEXT,
                  confidence REAL,
                  created_at TEXT NOT NULL,
                  UNIQUE(phone, fact_type, value, evidence_message_id)
                );

                CREATE TABLE IF NOT EXISTS runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  command TEXT NOT NULL,
                  started_at TEXT NOT NULL,
                  finished_at TEXT,
                  status TEXT NOT NULL,
                  details_json TEXT NOT NULL DEFAULT '{}',
                  error TEXT
                );
                """
            )
            db.commit()

    def start_run(self, command: str) -> int:
        started = now_iso()
        with self.connect() as db:
            cur = db.execute(
                "INSERT INTO runs (command, started_at, status) VALUES (?, ?, 'running')",
                (command, started),
            )
            db.commit()
            return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        details: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE runs
                SET finished_at = ?, status = ?, details_json = ?, error = ?
                WHERE id = ?
                """,
                (now_iso(), status, json.dumps(details or {}, ensure_ascii=False), error, run_id),
            )
            db.commit()

    def upsert_message(
        self,
        payload: dict[str, Any],
        *,
        source: str = "wuzapi",
        processed: int | None = None,
        sent_via_cli: int = 0,
    ) -> bool:
        identity = message_identity(payload)
        if not identity["phone"]:
            return False
        created = now_iso()
        raw_json = json.dumps(payload, ensure_ascii=False, default=str)
        default_processed = 1 if identity["direction"] == "outbound" else 0
        processed_value = default_processed if processed is None else processed

        with self.connect() as db:
            cur = db.execute(
                """
                INSERT INTO messages (
                  id, source, direction, phone, chat_id, sender_name, timestamp,
                  text, raw_json, processed, sent_via_cli, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  source = excluded.source,
                  direction = excluded.direction,
                  phone = excluded.phone,
                  chat_id = excluded.chat_id,
                  sender_name = COALESCE(NULLIF(excluded.sender_name, ''), messages.sender_name),
                  timestamp = excluded.timestamp,
                  text = excluded.text,
                  raw_json = excluded.raw_json,
                  sent_via_cli = MAX(messages.sent_via_cli, excluded.sent_via_cli),
                  updated_at = excluded.updated_at
                """,
                (
                    identity["id"],
                    source,
                    identity["direction"],
                    identity["phone"],
                    identity["chat_id"],
                    identity["sender_name"],
                    identity["timestamp"],
                    identity["text"],
                    raw_json,
                    processed_value,
                    sent_via_cli,
                    created,
                    created,
                ),
            )
            self.ensure_contact(identity["phone"], identity["sender_name"], db=db)
            db.commit()
            return cur.rowcount > 0

    def record_outgoing(self, phone: str, message_id: str, text: str, raw: dict[str, Any]) -> None:
        payload = {
            "id": message_id,
            "fromMe": True,
            "sender": normalize_phone(phone),
            "chatId": f"{normalize_phone(phone)}@s.whatsapp.net",
            "timestamp": now_iso(),
            "text": text,
            "send_response": raw,
        }
        self.upsert_message(payload, source="cli", processed=1, sent_via_cli=1)

    def ensure_contact(
        self,
        phone: str,
        display_name: str | None = None,
        *,
        db: sqlite3.Connection | None = None,
    ) -> None:
        phone = normalize_phone(phone)
        if not phone:
            return
        own_db = db is None
        conn = db or self.connect()
        try:
            now = now_iso()
            conn.execute(
                """
                INSERT INTO contacts (phone, display_name, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                  display_name = COALESCE(contacts.display_name, excluded.display_name),
                  updated_at = excluded.updated_at
                """,
                (phone, display_name or phone, now, now),
            )
            if own_db:
                conn.commit()
        finally:
            if own_db:
                conn.close()

    def unread_groups(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM messages
                WHERE direction = 'inbound' AND processed = 0
                ORDER BY timestamp ASC
                """
            ).fetchall()
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = row_to_message(row)
            key = item["phone"]
            group = groups.setdefault(
                key,
                {"phone": key, "chat_id": item["chat_id"], "messages": []},
            )
            group["messages"].append(item)
        return list(groups.values())

    def thread(self, phone: str, limit: int) -> dict[str, Any]:
        phone = normalize_phone(phone)
        with self.connect() as db:
            contact = db.execute("SELECT * FROM contacts WHERE phone = ?", (phone,)).fetchone()
            facts = db.execute(
                "SELECT * FROM contact_facts WHERE phone = ? ORDER BY id ASC",
                (phone,),
            ).fetchall()
            rows = db.execute(
                """
                SELECT * FROM messages
                WHERE phone = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (phone, limit),
            ).fetchall()
        messages = [row_to_message(row) for row in reversed(rows)]
        return {
            "contact": row_to_contact(contact) if contact else default_contact(phone),
            "facts": [row_to_fact(row) for row in facts],
            "messages": messages,
        }

    def contact(self, phone: str) -> dict[str, Any]:
        phone = normalize_phone(phone)
        with self.connect() as db:
            row = db.execute("SELECT * FROM contacts WHERE phone = ?", (phone,)).fetchone()
            facts = db.execute(
                "SELECT * FROM contact_facts WHERE phone = ? ORDER BY id ASC",
                (phone,),
            ).fetchall()
        return {
            "contact": row_to_contact(row) if row else default_contact(phone),
            "facts": [row_to_fact(item) for item in facts],
        }

    def set_contact(self, phone: str, updates: dict[str, Any], evidence: list[str]) -> dict[str, Any]:
        phone = normalize_phone(phone)
        self.ensure_contact(phone)
        allowed = {
            "display_name",
            "company",
            "role",
            "relationship",
            "summary",
            "confidence",
            "needs_review",
        }
        clean_updates = {key: value for key, value in updates.items() if key in allowed and value is not None}
        with self.connect() as db:
            current = db.execute(
                "SELECT evidence_message_ids FROM contacts WHERE phone = ?",
                (phone,),
            ).fetchone()
            existing = []
            if current:
                try:
                    existing = json.loads(current["evidence_message_ids"] or "[]")
                except json.JSONDecodeError:
                    existing = []
            merged_evidence = list(dict.fromkeys([*existing, *evidence]))
            clean_updates["evidence_message_ids"] = json.dumps(merged_evidence, ensure_ascii=False)
            clean_updates["updated_at"] = now_iso()
            assignments = ", ".join(f"{key} = ?" for key in clean_updates)
            values = [*clean_updates.values(), phone]
            db.execute(f"UPDATE contacts SET {assignments} WHERE phone = ?", values)
            db.commit()
        return self.contact(phone)

    def add_fact(
        self,
        phone: str,
        fact_type: str,
        value: str,
        evidence: str | None,
        confidence: float | None,
    ) -> dict[str, Any]:
        phone = normalize_phone(phone)
        self.ensure_contact(phone)
        with self.connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO contact_facts (
                  phone, fact_type, value, evidence_message_id, confidence, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (phone, fact_type, value, evidence, confidence, now_iso()),
            )
            db.commit()
        return self.contact(phone)

    def mark_read(self, message_id: str) -> bool:
        with self.connect() as db:
            cur = db.execute(
                """
                UPDATE messages
                SET processed = 1, updated_at = ?
                WHERE id = ? AND direction = 'inbound'
                """,
                (now_iso(), message_id),
            )
            db.commit()
            return cur.rowcount > 0

    def mark_all_read(self) -> int:
        with self.connect() as db:
            cur = db.execute(
                """
                UPDATE messages
                SET processed = 1, updated_at = ?
                WHERE direction = 'inbound' AND processed = 0
                """,
                (now_iso(),),
            )
            db.commit()
            return cur.rowcount


def row_to_message(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "direction": row["direction"],
        "phone": row["phone"],
        "chat_id": row["chat_id"],
        "sender_name": row["sender_name"],
        "timestamp": row["timestamp"],
        "text": row["text"],
        "processed": bool(row["processed"]),
        "sent_via_cli": bool(row["sent_via_cli"]),
    }


def row_to_contact(row: sqlite3.Row) -> dict[str, Any]:
    try:
        evidence = json.loads(row["evidence_message_ids"] or "[]")
    except json.JSONDecodeError:
        evidence = []
    return {
        "phone": row["phone"],
        "name": row["display_name"],
        "company": row["company"],
        "role": row["role"],
        "relationship": row["relationship"],
        "summary": row["summary"],
        "confidence": row["confidence"],
        "needs_review": bool(row["needs_review"]),
        "evidence_message_ids": evidence,
    }


def row_to_fact(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "phone": row["phone"],
        "type": row["fact_type"],
        "value": row["value"],
        "evidence_message_id": row["evidence_message_id"],
        "confidence": row["confidence"],
        "created_at": row["created_at"],
    }


def default_contact(phone: str) -> dict[str, Any]:
    return {
        "phone": normalize_phone(phone),
        "name": None,
        "company": None,
        "role": None,
        "relationship": None,
        "summary": None,
        "confidence": None,
        "needs_review": True,
        "evidence_message_ids": [],
    }
