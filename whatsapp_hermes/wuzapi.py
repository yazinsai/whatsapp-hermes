from __future__ import annotations

import json
import os
import base64
import binascii
import hashlib
import re
import subprocess
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .normalize import harvest_message_dicts


HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def default_env_path() -> Path:
    return Path(os.environ.get("WUZAPI_ENV_FILE") or HERMES_HOME / ".env")


def load_dotenv(path: Path | None = None, *, override: bool = False) -> None:
    path = path or default_env_path()
    if not path.exists():
        return
    override = override or os.environ.get("WUZAPI_ENV_FILE_OVERRIDE") == "1"
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and (override or key not in os.environ):
            os.environ[key] = value


@dataclass(frozen=True)
class EndpointResult:
    endpoint: str
    status: int
    ok: bool
    message_count: int = 0
    error: str | None = None


class WuzAPIClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        load_dotenv()
        configured_base_url = base_url or os.environ.get("WUZAPI_BASE_URL")
        if not configured_base_url:
            raise RuntimeError(
                f"WUZAPI_BASE_URL is not set. Run: whatsapp configure --base-url <url> --token <token>"
            )
        self.base_url = configured_base_url.rstrip("/")
        self.token = (token or os.environ.get("WUZAPI_TOKEN") or "").strip()
        if not self.token:
            raise RuntimeError(
                f"WUZAPI_TOKEN is not set. Run: whatsapp configure --base-url {self.base_url} --token <token>"
            )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: int = 45,
    ) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = self.base_url + path + query
        command = [
            "curl",
            "-sS",
            "--max-time",
            str(timeout),
            "-w",
            "\n%{http_code}",
            "-X",
            method.upper(),
            url,
            "-H",
            f"Token: {self.token}",
            "-H",
            "Content-Type: application/json",
        ]
        if payload is not None:
            command.extend(["--data-binary", json.dumps(payload)])

        result = subprocess.run(command, text=True, capture_output=True)
        stdout = result.stdout or ""
        stderr = (result.stderr or "").replace(self.token, "***")
        if result.returncode != 0:
            raise WuzAPIHTTPError(0, stderr or stdout)

        body, status_text = split_curl_status(stdout)
        status = int(status_text) if status_text.isdigit() else 0
        if status >= 400 or status == 0:
            raise WuzAPIHTTPError(status, body or stderr)
        if not body.strip():
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"raw": body}

    def send_text(self, phone: str, body: str) -> dict[str, Any]:
        message_id = uuid.uuid4().hex.upper()
        data = self.request(
            "POST",
            "/chat/send/text",
            payload={"Phone": phone, "Body": body, "Id": message_id},
        )
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"WuzAPI send failed: {data.get('error') or data}")
        if isinstance(data, dict):
            data.setdefault("client_message_id", message_id)
        return data if isinstance(data, dict) else {"data": data, "client_message_id": message_id}

    def download_media(self, attachment: dict[str, Any], *, root: Path | None = None) -> dict[str, Any]:
        root = root or HERMES_HOME / "attachments"
        media_bytes = b""
        mime_type = attachment.get("mime_type") or "application/octet-stream"

        if attachment.get("base64"):
            media_bytes, detected_mime = decode_base64_media(str(attachment["base64"]))
            mime_type = detected_mime or mime_type
        elif attachment.get("s3"):
            media_bytes = fetch_s3_media(attachment["s3"])
        else:
            endpoint = {
                "image": "/chat/downloadimage",
                "document": "/chat/downloaddocument",
                "video": "/chat/downloadvideo",
                "audio": "/chat/downloadaudio",
            }.get(str(attachment.get("kind")))
            if not endpoint:
                raise RuntimeError(f"unsupported attachment kind: {attachment.get('kind')}")
            payload = dict(attachment.get("download") or {})
            missing = [key for key in ("Url", "MediaKey", "Mimetype", "FileSHA256", "FileLength") if not payload.get(key)]
            if missing:
                raise RuntimeError(f"missing WuzAPI media fields: {', '.join(missing)}")
            data = self.request("POST", endpoint, payload=payload, timeout=90)
            media_value = extract_media_value(data)
            media_bytes, detected_mime = decode_base64_media(media_value)
            mime_type = detected_mime or mime_type

        if not media_bytes:
            raise RuntimeError("downloaded attachment was empty")

        phone = safe_path_part(str(attachment.get("phone") or "unknown"))
        message_id = safe_path_part(str(attachment.get("message_id") or attachment.get("id") or "message"))
        filename = safe_filename(str(attachment.get("filename") or attachment.get("id") or "attachment"), mime_type)
        directory = root / phone / message_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        path.write_bytes(media_bytes)

        return {
            "local_path": str(path),
            "size_bytes": len(media_bytes),
            "sha256": hashlib.sha256(media_bytes).hexdigest(),
            "mime_type": mime_type,
        }

    def fetch_history(
        self,
        *,
        limit: int = 1000,
        endpoint: str | None = None,
        workers: int = 8,
    ) -> tuple[list[dict[str, Any]], list[EndpointResult]]:
        if endpoint is None:
            messages, results = self.fetch_per_chat_history(limit=limit, workers=workers)
            if messages or any(result.ok for result in results):
                return messages, results

        endpoints = [endpoint] if endpoint else [
            "/chat/history",
            "/chat/messages",
            "/messages",
            "/history",
            "/message/history",
            "/chat/list/messages",
        ]
        results: list[EndpointResult] = []
        for path in endpoints:
            if not path:
                continue
            try:
                data = self.request("GET", path, params={"limit": limit})
            except WuzAPIHTTPError as exc:
                results.append(EndpointResult(path, exc.status, False, error=exc.safe_message))
                if exc.status in {404, 405} and endpoint is None:
                    continue
                raise RuntimeError(f"{path} failed with HTTP {exc.status}: {exc.safe_message}") from exc
            except Exception as exc:
                results.append(EndpointResult(path, 0, False, error=str(exc)))
                if endpoint is not None:
                    raise
                continue

            messages = harvest_message_dicts(data)
            results.append(EndpointResult(path, 200, True, len(messages)))
            if messages:
                return messages[:limit], results
            if endpoint is not None:
                return [], results

        return [], results

    def fetch_per_chat_history(self, *, limit: int = 1000, workers: int = 8) -> tuple[list[dict[str, Any]], list[EndpointResult]]:
        results: list[EndpointResult] = []
        try:
            contacts = self.request("GET", "/user/contacts")
        except WuzAPIHTTPError as exc:
            return [], [EndpointResult("/user/contacts", exc.status, False, error=exc.safe_message)]

        data = contacts.get("data") if isinstance(contacts, dict) else None
        if not isinstance(data, dict):
            return [], [EndpointResult("/user/contacts", 200, True, 0, error="response data is not an object")]

        results.append(EndpointResult("/user/contacts", 200, True, len(data)))
        messages: list[dict[str, Any]] = []

        def fetch_one(chat_jid: str) -> tuple[list[dict[str, Any]], EndpointResult]:
            try:
                history = self.request(
                    "GET",
                    "/chat/history",
                    params={"chat_jid": chat_jid, "limit": limit},
                )
            except WuzAPIHTTPError as exc:
                return [], EndpointResult(f"/chat/history?chat_jid={chat_jid}", exc.status, False, error=exc.safe_message)

            history_data = history.get("data") if isinstance(history, dict) else history
            found = harvest_message_dicts(history_data)
            return found, EndpointResult(f"/chat/history?chat_jid={chat_jid}", 200, True, len(found))

        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = [executor.submit(fetch_one, chat_jid) for chat_jid in data]
            for future in as_completed(futures):
                found, result = future.result()
                results.append(result)
                messages.extend(found)

        unique: dict[str, dict[str, Any]] = {}
        for message in messages:
            key = json.dumps(message, sort_keys=True, default=str)
            unique[key] = message
        return list(unique.values()), results


class WuzAPIHTTPError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body[:500]}")

    @property
    def safe_message(self) -> str:
        return self.body[:500]


def split_curl_status(output: str) -> tuple[str, str]:
    if "\n" not in output:
        return "", output.strip()
    body, status = output.rsplit("\n", 1)
    return body, status.strip()


def extract_media_value(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("base64", "Base64", "media", "Media", "data", "Data", "raw"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = extract_media_value(value)
                if nested:
                    return nested
    return ""


def decode_base64_media(value: str) -> tuple[bytes, str | None]:
    text = value.strip()
    mime_type = None
    if text.startswith("data:"):
        header, _, body = text.partition(",")
        text = body
        if ";base64" in header:
            mime_type = header.removeprefix("data:").split(";", 1)[0] or None
    try:
        return base64.b64decode(text, validate=True), mime_type
    except binascii.Error:
        return base64.b64decode(text), mime_type


def fetch_s3_media(s3: dict[str, Any]) -> bytes:
    url = str(s3.get("url") or "")
    key = str(s3.get("key") or "")
    if key and url and not url.endswith(key):
        url = f"{url.rstrip('/')}/{key.lstrip('/')}"
    if not url:
        raise RuntimeError("S3 media payload did not include a URL")
    result = subprocess.run(
        ["curl", "-sS", "--max-time", "90", "-L", url],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or b"failed to download S3 media").decode("utf-8", errors="replace"))
    return result.stdout


def safe_path_part(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return clean or "unknown"


def safe_filename(filename: str, mime_type: str) -> str:
    clean = safe_path_part(Path(filename).name)
    if "." in clean:
        return clean
    extension_by_mime = {
        "application/pdf": ".pdf",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    return clean + extension_by_mime.get(mime_type.split(";", 1)[0].lower(), "")
