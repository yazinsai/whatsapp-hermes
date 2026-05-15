from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from io import StringIO
from pathlib import Path

from whatsapp_hermes import cli
from whatsapp_hermes.db import Store
from whatsapp_hermes.normalize import extract_attachments, message_identity, normalize_phone
from whatsapp_hermes.wuzapi import WuzAPIClient


class CliHelpTests(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        stdout = StringIO()
        with redirect_stdout(stdout):
            try:
                code = cli.main(argv)
            except SystemExit as exc:
                code = int(exc.code or 0)
        return code, stdout.getvalue()

    def test_root_help_lists_commands_and_examples(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as cm:
            cli.main(["--help"])

        self.assertEqual(cm.exception.code, 0)
        output = stdout.getvalue()
        self.assertIn("usage: whatsapp", output)
        self.assertIn("whatsapp configure", output)
        self.assertIn("whatsapp check --json", output)
        self.assertIn("--db", output)

    def test_configure_writes_wuzapi_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "wuzapi.env"
            code, output = self.run_cli(
                [
                    "--env-file",
                    str(env_path),
                    "configure",
                    "--base-url",
                    "https://wuzapi.example.com/",
                    "--token",
                    "secret-token",
                ]
            )

            self.assertEqual(code, 0)
            self.assertIn("saved WuzAPI config", output)
            self.assertEqual(
                env_path.read_text(encoding="utf-8"),
                "# whatsapp-hermes WuzAPI connection\n"
                "WUZAPI_BASE_URL=https://wuzapi.example.com\n"
                "WUZAPI_TOKEN=secret-token\n",
            )

    def test_doctor_no_network_validates_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "wuzapi.env"
            self.run_cli(
                [
                    "--env-file",
                    str(env_path),
                    "configure",
                    "--base-url",
                    "https://wuzapi.example.com",
                    "--token",
                    "secret-token",
                ]
            )

            code, output = self.run_cli(["--env-file", str(env_path), "doctor", "--no-network"])

            self.assertEqual(code, 0)
            self.assertIn("network: skipped", output)

    def test_prompt_prints_generalized_command(self) -> None:
        code, output = self.run_cli(["prompt", "--command", "~/.hermes/bin/whatsapp"])

        self.assertEqual(code, 0)
        self.assertIn("human operator", output)
        self.assertIn("~/.hermes/bin/whatsapp check --json", output)
        self.assertNotIn("Yazin", output)

    def test_contact_help_is_specific(self) -> None:
        code, output = self.run_cli(["contact", "--help"])

        self.assertEqual(code, 0)
        self.assertIn("usage: whatsapp contact", output)
        self.assertIn("whatsapp contact set", output)
        self.assertIn("--json", output)
        self.assertNotIn("contact_args", output)

    def test_nested_contact_set_help_is_specific(self) -> None:
        code, output = self.run_cli(["contact", "set", "--help"])

        self.assertEqual(code, 0)
        self.assertIn("usage: whatsapp contact set", output)
        self.assertIn("--needs-review", output)
        self.assertIn("--evidence", output)

    def test_nested_facts_add_help_is_specific(self) -> None:
        code, output = self.run_cli(["facts", "add", "--help"])

        self.assertEqual(code, 0)
        self.assertIn("usage: whatsapp facts add", output)
        self.assertIn("--type", output)
        self.assertIn("--value", output)


class WuzAPIClientTests(unittest.TestCase):
    def test_request_does_not_spawn_curl_subprocesses(self) -> None:
        client = WuzAPIClient(base_url="https://wuzapi.example.com", token="token")

        with patch("whatsapp_hermes.wuzapi.subprocess.run") as run:
            with patch("urllib.request.urlopen") as urlopen:
                response = urlopen.return_value.__enter__.return_value
                response.status = 200
                response.read.return_value = b'{"ok": true}'

                result = client.request("GET", "/health", params={"limit": 1})

        self.assertEqual(result, {"ok": True})
        run.assert_not_called()


class NormalizeTests(unittest.TestCase):
    def test_normalize_phone_strips_jid(self) -> None:
        self.assertEqual(normalize_phone("97333601374@s.whatsapp.net"), "97333601374")

    def test_message_identity_extracts_wuzapi_shape(self) -> None:
        payload = {
            "Info": {
                "ID": "ABC",
                "Chat": "97333601374@s.whatsapp.net",
                "Sender": "97333601374@s.whatsapp.net",
                "PushName": "Y",
                "IsFromMe": False,
                "Timestamp": 1760000000,
            },
            "Message": {"conversation": "hello"},
        }
        identity = message_identity(payload)
        self.assertEqual(identity["id"], "ABC")
        self.assertEqual(identity["phone"], "97333601374")
        self.assertEqual(identity["direction"], "inbound")
        self.assertEqual(identity["text"], "hello")

    def test_outbound_lid_history_uses_recipient_alt_phone(self) -> None:
        payload = {
            "Info": {
                "ID": "OUT1",
                "Chat": "221204036251673@lid",
                "Sender": "139162023903404:1@lid",
                "RecipientAlt": "97336449025@s.whatsapp.net",
                "IsFromMe": True,
                "Timestamp": 1760000000,
            },
            "Message": {"conversation": "on my way"},
        }
        identity = message_identity(payload)
        self.assertEqual(identity["phone"], "97336449025")
        self.assertEqual(identity["direction"], "outbound")

    def test_extracts_wuzapi_document_attachment(self) -> None:
        payload = {
            "Info": {
                "ID": "DOC1",
                "Chat": "97333601374@s.whatsapp.net",
                "Sender": "97333601374@s.whatsapp.net",
                "IsFromMe": False,
                "Timestamp": 1760000000,
            },
            "Message": {
                "documentMessage": {
                    "caption": "please review",
                    "fileName": "invoice.pdf",
                    "mimetype": "application/pdf",
                    "URL": "https://mmg.whatsapp.net/d/f/file.enc",
                    "mediaKey": "media-key",
                    "fileSHA256": "file-sha",
                    "fileEncSHA256": "enc-sha",
                    "fileLength": 2039,
                }
            },
        }

        attachments = extract_attachments(payload)

        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["id"], "DOC1:0")
        self.assertEqual(attachments[0]["kind"], "document")
        self.assertEqual(attachments[0]["mime_type"], "application/pdf")
        self.assertEqual(attachments[0]["filename"], "invoice.pdf")
        self.assertEqual(attachments[0]["download"]["Url"], "https://mmg.whatsapp.net/d/f/file.enc")

    def test_extracts_webhook_image_attachment(self) -> None:
        payload = {
            "event": {
                "Info": {
                    "ID": "IMG1",
                    "Chat": "97333601374@s.whatsapp.net",
                    "Sender": "97333601374@s.whatsapp.net",
                    "IsFromMe": False,
                    "Timestamp": 1760000000,
                },
                "Message": {},
            },
            "base64": "aGVsbG8=",
            "mimeType": "image/png",
            "fileName": "photo.png",
        }

        attachments = extract_attachments(payload)

        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["kind"], "image")
        self.assertEqual(attachments[0]["base64"], "aGVsbG8=")


class StoreTests(unittest.TestCase):
    def insert_inbound(
        self,
        store: Store,
        message_id: str,
        phone: str = "97333601374",
        text: str = "where are you?",
    ) -> None:
        store.upsert_message(
            {
                "Info": {
                    "ID": message_id,
                    "Chat": f"{phone}@s.whatsapp.net",
                    "Sender": f"{phone}@s.whatsapp.net",
                    "PushName": "Mannan",
                    "IsFromMe": False,
                    "Timestamp": 1760000000,
                },
                "Message": {"conversation": text},
            }
        )

    def test_unread_thread_contact_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "whatsapp.db")
            store.init()
            self.insert_inbound(store, "MSG1")

            unread = store.unread_groups()
            self.assertEqual(len(unread), 1)
            self.assertEqual(unread[0]["messages"][0]["id"], "MSG1")

            contact = store.set_contact(
                "97333601374",
                {"display_name": "Mannan", "confidence": 0.8, "needs_review": 0},
                ["MSG1"],
            )
            self.assertEqual(contact["contact"]["name"], "Mannan")
            self.assertFalse(contact["contact"]["needs_review"])

            thread = store.thread("97333601374", 100)
            self.assertEqual(thread["messages"][0]["text"], "where are you?")

            self.assertTrue(store.mark_read("MSG1"))
            self.assertEqual(store.unread_groups(), [])

    def test_thread_includes_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "whatsapp.db")
            store.init()
            store.upsert_message(
                {
                    "Info": {
                        "ID": "DOC1",
                        "Chat": "97333601374@s.whatsapp.net",
                        "Sender": "97333601374@s.whatsapp.net",
                        "PushName": "Mannan",
                        "IsFromMe": False,
                        "Timestamp": 1760000000,
                    },
                    "Message": {
                        "documentMessage": {
                            "caption": "please review",
                            "fileName": "invoice.pdf",
                            "mimetype": "application/pdf",
                            "URL": "https://mmg.whatsapp.net/d/f/file.enc",
                            "mediaKey": "media-key",
                            "fileSHA256": "file-sha",
                            "fileEncSHA256": "enc-sha",
                            "fileLength": 2039,
                        }
                    },
                }
            )

            thread = store.thread("97333601374", 100)

            attachment = thread["messages"][0]["attachments"][0]
            self.assertEqual(attachment["id"], "DOC1:0")
            self.assertEqual(attachment["filename"], "invoice.pdf")
            self.assertEqual(attachment["status"], "pending")

    def test_download_pending_attachments_updates_store(self) -> None:
        class FakeClient:
            def download_media(self, attachment: dict[str, object]) -> dict[str, object]:
                return {
                    "local_path": "/tmp/photo.jpg",
                    "size_bytes": 12,
                    "sha256": "abc123",
                    "mime_type": "image/jpeg",
                }

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "whatsapp.db")
            store.init()
            store.upsert_message(
                {
                    "Info": {
                        "ID": "IMG1",
                        "Chat": "97333601374@s.whatsapp.net",
                        "Sender": "97333601374@s.whatsapp.net",
                        "PushName": "Mannan",
                        "IsFromMe": False,
                        "Timestamp": 1760000000,
                    },
                    "Message": {
                        "imageMessage": {
                            "caption": "look",
                            "mimetype": "image/jpeg",
                            "URL": "https://mmg.whatsapp.net/d/f/image.enc",
                            "mediaKey": "media-key",
                            "fileSHA256": "file-sha",
                            "fileEncSHA256": "enc-sha",
                            "fileLength": 12,
                        }
                    },
                }
            )

            details = cli.download_pending_attachments(store, FakeClient())  # type: ignore[arg-type]
            thread = store.thread("97333601374", 100)
            attachment = thread["messages"][0]["attachments"][0]

            self.assertEqual(details, {"attempted": 1, "downloaded": 1, "failed": 0})
            self.assertEqual(attachment["status"], "ready")
            self.assertEqual(attachment["local_path"], "/tmp/photo.jpg")

    def test_download_pending_attachments_includes_processed_messages(self) -> None:
        class FakeClient:
            def download_media(self, attachment: dict[str, object]) -> dict[str, object]:
                return {
                    "local_path": "/tmp/invoice.pdf",
                    "size_bytes": 12,
                    "sha256": "abc123",
                    "mime_type": "application/pdf",
                }

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "whatsapp.db")
            store.init()
            store.upsert_message(
                {
                    "Info": {
                        "ID": "DOC1",
                        "Chat": "97333601374@s.whatsapp.net",
                        "Sender": "97333601374@s.whatsapp.net",
                        "PushName": "Mannan",
                        "IsFromMe": False,
                        "Timestamp": 1760000000,
                    },
                    "Message": {
                        "documentMessage": {
                            "fileName": "invoice.pdf",
                            "mimetype": "application/pdf",
                            "URL": "https://mmg.whatsapp.net/d/f/file.enc",
                            "mediaKey": "media-key",
                            "fileSHA256": "file-sha",
                            "fileEncSHA256": "enc-sha",
                            "fileLength": 12,
                        }
                    },
                }
            )
            self.assertTrue(store.mark_read("DOC1"))

            details = cli.download_pending_attachments(store, FakeClient())  # type: ignore[arg-type]
            attachment = store.thread("97333601374", 100)["messages"][0]["attachments"][0]

            self.assertEqual(details, {"attempted": 1, "downloaded": 1, "failed": 0})
            self.assertEqual(attachment["status"], "ready")
            self.assertEqual(attachment["local_path"], "/tmp/invoice.pdf")

    def test_mark_all_read_marks_every_unprocessed_inbound_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "whatsapp.db")
            store.init()
            self.insert_inbound(store, "MSG1")
            self.insert_inbound(store, "MSG2", phone="97336449025")

            self.assertEqual(store.mark_all_read(), 2)
            self.assertEqual(store.unread_groups(), [])

    def test_cli_mark_read_all_outputs_marked_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "whatsapp.db"
            store = Store(db_path)
            store.init()
            self.insert_inbound(store, "MSG1")
            self.insert_inbound(store, "MSG2", phone="97336449025")

            stdout = StringIO()
            with redirect_stdout(stdout):
                code = cli.main(["--db", str(db_path), "mark-read", "--all", "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue()), {"marked": 2, "ok": True})
            self.assertEqual(store.unread_groups(), [])


if __name__ == "__main__":
    unittest.main()
