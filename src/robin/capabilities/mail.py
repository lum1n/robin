"""Mailbox connector. IMAP reads and SMTP sends. The secret stays in the broker."""

from __future__ import annotations

import imaplib
import json
import smtplib
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default
from email.utils import parseaddr
from typing import Any, Protocol

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Result, Tool

INBOX_LIMIT = 10
_SECRET = ("imap_host", "smtp_host", "user", "password")
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
    def search(self, criteria: str, limit: int) -> list[bytes]: ...
    def fetch_one(self, uid: str) -> bytes | None: ...
    def store_flags(self, uid: str, action: str) -> None: ...
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
        return self.search("ALL", limit)

    def search(self, criteria: str, limit: int) -> list[bytes]:
        self._imap.select("INBOX", readonly=True)
        _status, data = self._imap.search(None, criteria)
        blob = data[0] if data and data[0] else b""
        if isinstance(blob, str):
            blob = blob.encode()
        numbers = blob.split()
        found: list[bytes] = []
        for number in numbers[-limit:]:
            _status, fetched = self._imap.fetch(number, "(BODY.PEEK[])")
            found.append(_body(fetched))
        return found

    def fetch_one(self, uid: str) -> bytes | None:
        self._imap.select("INBOX", readonly=True)
        _status, fetched = self._imap.fetch(uid.encode() if isinstance(uid, str) else uid, "(BODY.PEEK[])")
        try:
            return _body(fetched)
        except RuntimeError:
            return None

    def store_flags(self, uid: str, action: str) -> None:
        self._imap.select("INBOX", readonly=False)
        if action == "read":
            self._imap.store(uid, "+FLAGS", "\\Seen")
        elif action == "archive":
            try:
                self._imap.copy(uid, "Archive")
            except Exception:
                pass
            self._imap.store(uid, "+FLAGS", "\\Deleted")
            self._imap.expunge()

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
        self._drafts: dict[str, dict[str, str]] = {}

    def connected(self, account_id: str) -> bool:
        return self._credentials(account_id) is not None

    def messages(self, account_id: str) -> list[dict[str, str]]:
        return self.search(account_id)

    def search(
        self,
        account_id: str,
        *,
        query: str = "",
        sender: str = "",
        since: str = "",
        unread: bool = False,
        folder: str = "INBOX",
    ) -> list[dict[str, str]]:
        creds = self._credentials(account_id)
        if creds is None:
            return []
        criteria = _imap_criteria(query=query, sender=sender, since=since, unread=unread)
        client: _Imap = self._open_imap(creds["imap_host"])
        try:
            _login(client, creds)
            if hasattr(client, "search"):
                raw_messages = client.search(criteria, self.limit)
            else:
                raw_messages = client.fetch_recent(self.limit)
        finally:
            client.logout()
        rows = [_message(raw, index) for index, raw in enumerate(raw_messages, start=1)]
        _ = folder
        return rows

    def read(self, account_id: str, message_id: str) -> dict[str, str] | None:
        rows = self.messages(account_id)
        for row in rows:
            if row.get("id") == message_id:
                return row
        return None

    def draft(self, account_id: str, to: str, subject: str, body: str) -> str:
        self._drafts[account_id] = {"to": to, "subject": subject, "body": body}
        return "draft saved"

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

    def mark(self, account_id: str, message_id: str, action: str) -> str:
        creds = self._credentials(account_id)
        if creds is None:
            raise RuntimeError("mailbox is not connected")
        client: _Imap = self._open_imap(creds["imap_host"])
        try:
            _login(client, creds)
            if hasattr(client, "store_flags"):
                client.store_flags(message_id, action)
        finally:
            client.logout()
        return f"marked {action}"

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
            name="mail_list",
            description="List recent messages in this account's inbox.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        ),
        Tool(
            name="mail_search",
            description="Search this account's mailbox. Optional filters: query, from, since (YYYY-MM-DD), unread, folder.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "from": {"type": "string"},
                    "since": {"type": "string"},
                    "unread": {"type": "boolean"},
                    "folder": {"type": "string"},
                },
            },
            effect=Effect.READ,
        ),
        Tool(
            name="mail_read",
            description="Read one message by id from a prior mail_list or mail_search result.",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
            effect=Effect.READ,
        ),
        Tool(
            name="mail_draft",
            description="Save a draft email for this account. Does not send.",
            parameters={
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["to", "subject", "body"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="mail_send",
            description="Send a message from this account. Waits for confirmation.",
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
        Tool(
            name="mail_reply",
            description="Reply to a message by id. Waits for confirmation.",
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["id", "body"],
            },
            effect=Effect.EXTERNAL,
        ),
        Tool(
            name="mail_mark",
            description="Mark a message read or archive it.",
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "action": {"type": "string", "enum": ["read", "archive"]},
                },
                "required": ["id", "action"],
            },
            effect=Effect.MUTATE,
        ),
    ]
    fields = [
        FieldSpec("id", FieldClass.ORDINARY),
        FieldSpec("sender", FieldClass.TOKENIZE, label="PERSON"),
        FieldSpec("subject", FieldClass.ORDINARY, free_text=True),
        FieldSpec("body", FieldClass.ORDINARY, free_text=True),
    ]

    def __init__(self, mailbox: ImapMailbox) -> None:
        self.mailbox = mailbox

    def status(self, account_id: str) -> str:
        if self.mailbox.connected(account_id):
            return "mail: connected"
        return "mail: not connected, connect it in the app"

    def records(self, account_id: str) -> list[dict[str, str]]:
        return []

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        if tool_name == "mail_list":
            return self._list(account_id)
        if tool_name == "mail_search":
            return self._search(account_id, arguments)
        if tool_name == "mail_read":
            row = self.mailbox.read(account_id, str(arguments.get("id", "")))
            if row is None:
                return "No message matched that id."
            return Result(records=[row])
        if tool_name == "mail_draft":
            return self.mailbox.draft(
                account_id,
                str(arguments.get("to", "")),
                str(arguments.get("subject", "")),
                str(arguments.get("body", "")),
            )
        if tool_name == "mail_send":
            self.mailbox.send(
                account_id,
                str(arguments.get("to", "")),
                str(arguments.get("subject", "")),
                str(arguments.get("body", "")),
            )
            return "sent"
        if tool_name == "mail_reply":
            row = self.mailbox.read(account_id, str(arguments.get("id", "")))
            if row is None:
                return "No message matched that id."
            subject = row.get("subject") or ""
            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"
            self.mailbox.send(
                account_id,
                row.get("sender") or "",
                subject,
                str(arguments.get("body", "")),
            )
            return "sent"
        if tool_name == "mail_mark":
            return self.mailbox.mark(
                account_id,
                str(arguments.get("id", "")),
                str(arguments.get("action", "read")),
            )
        raise NotImplementedError(tool_name)

    def _list(self, account_id: str) -> str | Result:
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
        return Result(text="Inbox:", records=rows)

    def _search(self, account_id: str, arguments: dict[str, Any]) -> str | Result:
        if not self.mailbox.connected(account_id):
            return "The mailbox is not connected."
        try:
            rows = self.mailbox.search(
                account_id,
                query=str(arguments.get("query", "")),
                sender=str(arguments.get("from", "")),
                since=str(arguments.get("since", "")),
                unread=bool(arguments.get("unread")),
                folder=str(arguments.get("folder", "INBOX") or "INBOX"),
            )
        except Exception as exc:
            host = ""
            creds = self.mailbox._credentials(account_id)
            if creds is not None:
                host = creds["imap_host"]
            return _mail_failure(exc, host)
        if not rows:
            return "No messages matched."
        return Result(text="Messages:", records=rows)


def _imap_criteria(*, query: str, sender: str, since: str, unread: bool) -> str:
    parts: list[str] = []
    if unread:
        parts.append("UNSEEN")
    if sender:
        parts.append(f'FROM "{sender}"')
    if since:
        parts.append(f'SINCE "{since}"')
    if query:
        parts.append(f'TEXT "{query}"')
    if not parts:
        return "ALL"
    if len(parts) == 1:
        return parts[0]
    return "(" + " ".join(parts) + ")"


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


def _message(raw: bytes, index: int = 1) -> dict[str, str]:
    parsed = message_from_bytes(raw, policy=default)
    name, address = parseaddr(str(parsed.get("from") or ""))
    subject = str(parsed.get("subject") or "")
    return {
        "id": str(index),
        "sender": name or address,
        "subject": subject,
        "body": _text(parsed),
    }


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
