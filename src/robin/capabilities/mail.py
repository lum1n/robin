"""Mailbox connector. IMAP reads and SMTP sends. The secret stays in the broker."""

from __future__ import annotations

import imaplib
import json
import re
import smtplib
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default
from email.utils import parseaddr
from typing import Any, Protocol

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Tool

INBOX_LIMIT = 10
_SECRET = ("imap_host", "smtp_host", "user", "password")
_MAIL_ASK = re.compile(r"\b(?:e-?mails?|inbox|mailbox|unread|icloud)\b|\bmail\b", re.IGNORECASE)
_HOSTS = {
    "icloud.com": ("imap.mail.me.com", "smtp.mail.me.com"),
    "me.com": ("imap.mail.me.com", "smtp.mail.me.com"),
    "mac.com": ("imap.mail.me.com", "smtp.mail.me.com"),
    "gmail.com": ("imap.gmail.com", "smtp.gmail.com"),
    "googlemail.com": ("imap.gmail.com", "smtp.gmail.com"),
    "outlook.com": ("outlook.office365.com", "smtp.office365.com"),
    "hotmail.com": ("outlook.office365.com", "smtp.office365.com"),
    "live.com": ("outlook.office365.com", "smtp.office365.com"),
}


class SecretStore(Protocol):
    def reveal(self, account_id: str, name: str) -> str: ...


class _Imap(Protocol):
    def login(self, user: str, password: str) -> None: ...
    def fetch_recent(self, limit: int) -> list[bytes]: ...
    def logout(self) -> None: ...


class _Smtp(Protocol):
    def login(self, user: str, password: str) -> None: ...
    def send_message(self, message: EmailMessage) -> None: ...
    def close(self) -> None: ...


def mailbox_secret(*, imap_host: str, smtp_host: str, user: str, password: str) -> str:
    return json.dumps(
        {"imap_host": imap_host, "smtp_host": smtp_host, "user": user, "password": password},
        sort_keys=True,
    )


class ImaplibClient:
    def __init__(self, host: str, *, imap_class: Any = imaplib.IMAP4_SSL) -> None:
        self._imap = imap_class(host)

    def login(self, user: str, password: str) -> None:
        self._imap.login(user, password)

    def fetch_recent(self, limit: int) -> list[bytes]:
        self._imap.select("INBOX", readonly=True)
        _status, data = self._imap.search(None, "ALL")
        blob = data[0] if data and data[0] else b""
        if isinstance(blob, str):
            blob = blob.encode()
        numbers = blob.split()
        found: list[bytes] = []
        for number in numbers[-limit:]:
            _status, fetched = self._imap.fetch(number, "(BODY.PEEK[])")
            found.append(_body(fetched))
        return found

    def logout(self) -> None:
        self._imap.logout()


class SmtplibClient:
    def __init__(self, host: str, *, smtp_class: Any = smtplib.SMTP_SSL) -> None:
        self._smtp = smtp_class(host)

    def login(self, user: str, password: str) -> None:
        self._smtp.login(user, password)

    def send_message(self, message: EmailMessage) -> None:
        self._smtp.send_message(message)

    def close(self) -> None:
        self._smtp.quit()


class ImapMailbox:
    def __init__(
        self,
        secrets: SecretStore,
        *,
        open_imap: Any = None,
        open_smtp: Any = None,
        limit: int = INBOX_LIMIT,
    ) -> None:
        self.secrets = secrets
        self._open_imap = open_imap or (lambda host: ImaplibClient(host))
        self._open_smtp = open_smtp or (lambda host: SmtplibClient(host))
        self.limit = limit

    def connected(self, account_id: str) -> bool:
        return self._credentials(account_id) is not None

    def messages(self, account_id: str) -> list[dict[str, str]]:
        creds = self._credentials(account_id)
        if creds is None:
            return []
        client: _Imap = self._open_imap(creds["imap_host"])
        try:
            _login(client, creds)
            raw_messages = client.fetch_recent(self.limit)
        finally:
            client.logout()
        return [_message(raw) for raw in raw_messages]

    def send(self, account_id: str, to: str, subject: str, body: str) -> None:
        creds = self._credentials(account_id)
        if creds is None:
            raise RuntimeError("mailbox is not connected")
        message = EmailMessage()
        message["From"] = creds["user"]
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        client: _Smtp = self._open_smtp(creds["smtp_host"])
        try:
            _login(client, creds)
            client.send_message(message)
        finally:
            client.close()

    def _credentials(self, account_id: str) -> dict[str, str] | None:
        try:
            raw = self.secrets.reveal(account_id, "mailbox")
        except KeyError:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        user = parsed.get("user")
        password = parsed.get("password")
        imap_host = parsed.get("imap_host", "")
        smtp_host = parsed.get("smtp_host", "")
        if not isinstance(user, str) or not user or not isinstance(password, str) or not password:
            return None
        if not isinstance(imap_host, str) or not isinstance(smtp_host, str):
            return None
        return _complete_hosts({"imap_host": imap_host, "smtp_host": smtp_host, "user": user, "password": password})


class Mail(Capability):
    id = "post"
    tools = [
        Tool(
            name="list_messages",
            description="List messages for this account.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        ),
        Tool(
            name="send_message",
            description="Send a message from this account.",
            parameters={
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["to", "subject", "body"],
            },
            effect=Effect.EXTERNAL,
        ),
    ]
    fields = [
        FieldSpec("sender", FieldClass.TOKENIZE, label="PERSON"),
        FieldSpec("body", FieldClass.ORDINARY, free_text=True),
    ]

    def __init__(self, mailbox: ImapMailbox) -> None:
        self.mailbox = mailbox
        self._direct: dict[str, str] = {}

    def prepare(self, account_id: str, task: str) -> str:
        self._direct.pop(account_id, None)
        if not _MAIL_ASK.search(task):
            return ""
        if not self.mailbox.connected(account_id):
            self._direct[account_id] = "The mailbox is not connected. Connect it from the app, then ask again."
            return ""
        try:
            rows = self.mailbox.messages(account_id)
        except Exception as exc:
            host = ""
            creds = self.mailbox._credentials(account_id)
            if creds is not None:
                host = creds["imap_host"]
            self._direct[account_id] = _mail_failure(exc, host)
            return ""
        if not rows:
            self._direct[account_id] = "The inbox is empty."
            return ""
        self._direct[account_id] = _listing(rows)
        return ""

    def take_direct(self, account_id: str) -> str:
        return self._direct.pop(account_id, "")

    def records(self, account_id: str) -> list[dict[str, str]]:
        return []

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "list_messages":
            if not self.mailbox.connected(account_id):
                return "The mailbox is not connected."
            try:
                rows = self.mailbox.messages(account_id)
            except Exception as exc:
                host = ""
                creds = self.mailbox._credentials(account_id)
                if creds is not None:
                    host = creds["imap_host"]
                return _mail_failure(exc, host)
            if not rows:
                return "The inbox is empty."
            return _listing(rows)
        if tool_name == "send_message":
            self.mailbox.send(
                account_id,
                str(arguments.get("to", "")),
                str(arguments.get("subject", "")),
                str(arguments.get("body", "")),
            )
            return "sent"
        raise NotImplementedError(tool_name)


def _login(client: Any, creds: dict[str, str]) -> None:
    try:
        client.login(creds["user"], creds["password"])
    except Exception as exc:
        if creds["password"] in str(exc):
            raise RuntimeError("mailbox login failed") from None
        raise


def _complete_hosts(creds: dict[str, str]) -> dict[str, str] | None:
    domain = creds["user"].rsplit("@", 1)[-1].lower()
    pair = _HOSTS.get(domain)
    if pair is not None:
        if _blank_host(creds["imap_host"], domain):
            creds["imap_host"] = pair[0]
        if _blank_host(creds["smtp_host"], domain):
            creds["smtp_host"] = pair[1]
    if not creds["imap_host"] or not creds["smtp_host"]:
        return None
    return creds


def _blank_host(host: str, domain: str) -> bool:
    cleaned = host.strip().lower().rstrip(".")
    if not cleaned or "@" in cleaned:
        return True
    return cleaned in {domain, domain.split(".")[0], "icloud", "me", "mac", "gmail", "googlemail", "outlook", "hotmail"}


def _mail_failure(exc: Exception, host: str = "") -> str:
    if "login failed" in str(exc).lower():
        if host.endswith("mail.me.com"):
            return (
                "iCloud did not accept the password. "
                "Use an app-specific password from appleid.apple.com, then connect the mailbox again."
            )
        return "The mailbox did not accept the sign-in."
    return "The mailbox did not answer."


def _listing(rows: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for row in rows:
        sender = row.get("sender") or "unknown"
        subject = row.get("subject") or ""
        body = " ".join(row.get("body", "").split())
        if len(body) > 400:
            body = body[:400]
        head = f"{subject}. {body}" if subject else body
        lines.append(f"From {sender}: {head}".rstrip())
    return "Inbox:\n" + "\n".join(lines)


def _message(raw: bytes) -> dict[str, str]:
    parsed = message_from_bytes(raw, policy=default)
    name, address = parseaddr(str(parsed.get("from") or ""))
    subject = str(parsed.get("subject") or "")
    return {"sender": name or address, "subject": subject, "body": _text(parsed)}


def _text(parsed: EmailMessage) -> str:
    if parsed.is_multipart():
        part = parsed.get_body(preferencelist=("plain",))
        content = "" if part is None else part.get_content()
    else:
        content = parsed.get_content()
    if isinstance(content, bytes):
        text = content.decode("utf-8", errors="replace")
    else:
        text = str(content)
    return text.strip("\n")


def _body(fetched: list) -> bytes:
    for item in fetched or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    raise RuntimeError("imap response had no message")
