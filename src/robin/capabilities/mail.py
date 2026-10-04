"""Mailbox connector. IMAP reads and SMTP sends. The secret stays in the broker."""

from __future__ import annotations

import hashlib
import imaplib
import json
import re
import smtplib
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Protocol

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Result, Tool

INBOX_LIMIT = 10
SEARCH_LIMIT = 50
_SECRET = ("imap_host", "smtp_host", "user", "password")
_SEARCH_STOP = frozenset(
    {
        "a",
        "an",
        "the",
        "my",
        "your",
        "our",
        "me",
        "us",
        "please",
        "get",
        "find",
        "show",
        "fetch",
        "read",
        "mail",
        "email",
        "message",
        "inbox",
        "letter",
        "from",
        "about",
        "for",
        "to",
        "of",
        "on",
        "in",
        "min",
        "mitt",
        "din",
        "ditt",
        "fra",
        "om",
        "epost",
        "post",
    }
)
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
    def fetch_recent(self, limit: int) -> list[bytes] | list[tuple[str, bytes]]: ...
    def search(self, criteria: str, limit: int) -> list[bytes] | list[tuple[str, bytes]]: ...
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
        return [raw for _uid, raw in self.search("ALL", limit)]

    def search(self, criteria: str, limit: int) -> list[tuple[str, bytes]]:
        self._imap.select("INBOX", readonly=True)
        _status, data = self._imap.uid("SEARCH", None, criteria)
        blob = data[0] if data and data[0] else b""
        if isinstance(blob, str):
            blob = blob.encode()
        uids = blob.split()
        found: list[tuple[str, bytes]] = []
        for uid in uids[-limit:]:
            label = uid.decode() if isinstance(uid, (bytes, bytearray)) else str(uid)
            _status, fetched = self._imap.uid("FETCH", uid, "(BODY.PEEK[])")
            found.append((label, _body(fetched)))
        return found

    def fetch_one(self, uid: str) -> bytes | None:
        self._imap.select("INBOX", readonly=True)
        token = uid.encode() if isinstance(uid, str) else uid
        _status, fetched = self._imap.uid("FETCH", token, "(BODY.PEEK[])")
        try:
            return _body(fetched)
        except RuntimeError:
            return None

    def store_flags(self, uid: str, action: str) -> None:
        self._imap.select("INBOX", readonly=False)
        token = uid.encode() if isinstance(uid, str) else uid
        if action == "read":
            self._imap.uid("STORE", token, "+FLAGS", "\\Seen")
        elif action == "archive":
            try:
                self._imap.uid("COPY", token, "Archive")
            except Exception:
                pass
            self._imap.uid("STORE", token, "+FLAGS", "\\Deleted")
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
        search_limit: int = SEARCH_LIMIT,
    ) -> None:
        self.secrets = secrets
        self._open_imap = open_imap or (lambda host: ImaplibClient(host))
        self._open_smtp = open_smtp or (lambda host: SmtplibClient(host))
        self.limit = limit
        self.search_limit = search_limit
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
        filtered = bool(query or sender or since or unread)
        fetch_limit = self.search_limit if filtered else self.limit
        criteria = _imap_criteria(query=query, sender=sender, since=since, unread=unread)
        client: _Imap = self._open_imap(creds["imap_host"])
        matched: list[dict[str, str]] = []
        try:
            _login(client, creds)
            rows = self._rows(client, criteria, fetch_limit)
            matched = [row for row in rows if _message_matches(row, query=query, sender=sender)]
            if not matched and filtered and criteria != "ALL":
                rows = self._rows(client, "ALL", fetch_limit)
                matched = [row for row in rows if _message_matches(row, query=query, sender=sender)]
        finally:
            client.logout()
        _ = folder
        return matched

    def _rows(self, client: _Imap, criteria: str, limit: int) -> list[dict[str, str]]:
        if hasattr(client, "search"):
            raw_messages = client.search(criteria, limit)
        else:
            raw_messages = client.fetch_recent(limit)
        return [_row_from_found(item, index) for index, item in enumerate(raw_messages, start=1)]

    def read(self, account_id: str, message_id: str) -> dict[str, str] | None:
        creds = self._credentials(account_id)
        if creds is None:
            return None
        client: _Imap = self._open_imap(creds["imap_host"])
        try:
            _login(client, creds)
            if hasattr(client, "fetch_one"):
                raw = client.fetch_one(message_id)
                if raw:
                    return _message(raw, message_id)
            if hasattr(client, "search"):
                found = client.search("ALL", self.limit)
            else:
                found = client.fetch_recent(self.limit)
            for index, item in enumerate(found, start=1):
                row = _row_from_found(item, index)
                if row.get("id") == message_id:
                    return row
        finally:
            client.logout()
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
            description=(
                "Search this account's mailbox. from matches a sender name or address; "
                "query matches words in the subject or a short preview, not only an exact subject. "
                "Copy conversation references verbatim. Optional: since (YYYY-MM-DD), unread, folder."
            ),
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
        FieldSpec("date", FieldClass.ORDINARY),
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
    if since:
        parts.append(f"SINCE {_imap_quote(since)}")
    needles: list[str] = []
    if sender.strip():
        needles.append(f"FROM {_imap_quote(sender.strip())}")
        for token in _search_needles(sender):
            needles.append(f"SUBJECT {_imap_quote(token)}")
            needles.append(f"TEXT {_imap_quote(token)}")
    for token in _search_needles(query):
        needles.append(f"SUBJECT {_imap_quote(token)}")
        needles.append(f"TEXT {_imap_quote(token)}")
    if needles:
        parts.append(_imap_or(needles))
    if not parts:
        return "ALL"
    if len(parts) == 1:
        return parts[0]
    return "(" + " ".join(parts) + ")"


def _imap_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _imap_or(parts: list[str]) -> str:
    if not parts:
        return "ALL"
    expr = parts[0]
    for part in parts[1:]:
        expr = f"OR {expr} {part}"
    return expr


def _search_needles(text: str) -> list[str]:
    cleaned = text.strip()
    if not cleaned:
        return []
    tokens = [word for word in re.findall(r"[a-z0-9]+", cleaned.lower()) if word not in _SEARCH_STOP and len(word) > 1]
    return tokens or [cleaned.lower()]


def _message_matches(row: dict[str, str], *, query: str, sender: str) -> bool:
    haystack = " ".join(
        (row.get("sender") or "", row.get("subject") or "", (row.get("body") or "")[:500])
    ).lower()
    for needle in _search_needles(sender):
        if needle not in haystack:
            return False
    for needle in _search_needles(query):
        if needle not in haystack:
            return False
    return True


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


def _row_from_found(item: bytes | tuple[str, bytes], index: int) -> dict[str, str]:
    if isinstance(item, tuple) and len(item) == 2:
        uid, raw = item
        return _message(raw, str(uid))
    return _message(item, _stable_mail_id(item, index))


def _stable_mail_id(raw: bytes, fallback: int) -> str:
    parsed = message_from_bytes(raw, policy=default)
    mid = str(parsed.get("message-id") or "").strip().strip("<>")
    if mid:
        return mid
    return hashlib.sha1(raw).hexdigest()[:12]


def _message(raw: bytes, message_id: str | int = 1) -> dict[str, str]:
    parsed = message_from_bytes(raw, policy=default)
    name, address = parseaddr(str(parsed.get("from") or ""))
    subject = str(parsed.get("subject") or "")
    return {
        "id": str(message_id),
        "sender": name or address,
        "subject": subject,
        "date": _date(parsed),
        "body": _text(parsed),
    }


def _date(parsed: EmailMessage) -> str:
    raw = str(parsed.get("date") or "").strip()
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except (TypeError, ValueError, IndexError, OverflowError):
        return raw


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
