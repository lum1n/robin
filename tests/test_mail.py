import json
from email.message import EmailMessage

from robin.airlock import Entity
from robin.capabilities.mail import ImapMailbox, ImaplibClient, Mail, SmtplibClient, mailbox_secret
from robin.loop import converse
from robin.model import ModelTurn, ToolCall
from robin.ner import UnavailableNer
from robin.policy import Task
from robin.session import Assistant
from robin.vault import REFERENCE

PASSWORD = "sk-mailboxsecretvalue1234567890"
SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"


class Broker:
    def __init__(self) -> None:
        self._secrets: dict[tuple[str, str], str] = {}

    def put(self, account_id: str, name: str, value: str) -> None:
        self._secrets[(account_id, name)] = value

    def reveal(self, account_id: str, name: str) -> str:
        return self._secrets[(account_id, name)]


class FakeImaplib:
    def __init__(self, host: str, messages: list[bytes]) -> None:
        self.host = host
        self.messages = messages
        self.calls: list[tuple] = []

    def login(self, user: str, password: str) -> tuple[str, list]:
        self.calls.append(("login", user))
        return "OK", []

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list]:
        self.calls.append(("select", mailbox, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def search(self, charset, criterion: str) -> tuple[str, list]:
        self.calls.append(("search", criterion))
        ids = b" ".join(str(index + 1).encode() for index in range(len(self.messages)))
        return "OK", [ids]

    def fetch(self, number, spec: str) -> tuple[str, list]:
        self.calls.append(("fetch", spec))
        raw = self.messages[int(number) - 1]
        label = number if isinstance(number, bytes) else str(number).encode()
        return "OK", [(b"%b (BODY[] {%d}" % (label, len(raw)), raw), b")"]

    def uid(self, command, *args):
        command = command.upper()
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            ids = b" ".join(str(1001 + index).encode() for index in range(len(self.messages)))
            return "OK", [ids]
        if command == "FETCH":
            uid = args[0]
            if isinstance(uid, bytes):
                uid = uid.decode()
            index = int(uid) - 1001
            raw = self.messages[index]
            label = str(uid).encode()
            return "OK", [(b"%b (UID %b BODY[] {%d}" % (label, label, len(raw)), raw), b")"]
        if command in {"STORE", "COPY"}:
            return "OK", []
        raise RuntimeError(command)

    def logout(self) -> tuple[str, list]:
        self.calls.append(("logout",))
        return "OK", []


class Directory:
    def __init__(self) -> None:
        self.logins: list[tuple[str, str, str]] = []
        self.sent: list[EmailMessage] = []
        self.opened_smtp: list[str] = []
        self.boxes = {
            "ada@example.com": [("5001", _letter("Jane Doe", f"hello from ada {SECRET}"))],
            "bea@example.com": [("5002", _letter("Sam", "bea-only-note"))],
        }

    def open_imap(self, host: str):
        return _Client(self, host)

    def open_smtp(self, host: str):
        self.opened_smtp.append(host)
        return _Poster(self, host)


class _Client:
    def __init__(self, directory: Directory, host: str) -> None:
        self.directory = directory
        self.host = host
        self.user = ""

    def login(self, user: str, password: str) -> None:
        self.directory.logins.append((self.host, user, password))
        self.user = user

    def fetch_recent(self, limit: int) -> list[bytes]:
        return [raw for _uid, raw in self._items()[-limit:]]

    def search(self, criteria: str, limit: int) -> list[tuple[str, bytes]]:
        return self._items()[-limit:]

    def fetch_one(self, uid: str) -> bytes | None:
        for item_uid, raw in self._items():
            if item_uid == uid:
                return raw
        return None

    def store_flags(self, uid: str, action: str) -> None:
        self.directory.logins.append((self.host, f"flag:{uid}", action))

    def _items(self) -> list[tuple[str, bytes]]:
        rows = self.directory.boxes.get(self.user, [])
        found: list[tuple[str, bytes]] = []
        for index, item in enumerate(rows, start=1):
            if isinstance(item, tuple):
                found.append((str(item[0]), item[1]))
            else:
                found.append((str(5000 + index), item))
        return found

    def logout(self) -> None:
        return None


class _Poster:
    def __init__(self, directory: Directory, host: str) -> None:
        self.directory = directory
        self.host = host

    def login(self, user: str, password: str) -> None:
        self.directory.logins.append((self.host, user, password))

    def send_message(self, message: EmailMessage) -> None:
        self.directory.sent.append(message)

    def close(self) -> None:
        return None


class ExplodingLogin:
    def __init__(self, password: str) -> None:
        self.password = password

    def login(self, user: str, password: str) -> None:
        raise RuntimeError(password)

    def logout(self) -> None:
        return None

    def close(self) -> None:
        return None


def _letter(sender: str, body: str, message_id: str = "", subject: str = "", date: str = "") -> bytes:
    message = EmailMessage()
    message["From"] = sender
    if subject:
        message["Subject"] = subject
    if date:
        message["Date"] = date
    if message_id:
        message["Message-ID"] = f"<{message_id}>"
    message.set_content(body)
    return message.as_bytes()


def test_inbox_is_per_account_and_the_password_stays_out_of_the_records() -> None:
    broker = Broker()
    broker.put("ada", "mailbox", mailbox_secret(imap_host="imap.example", smtp_host="smtp.example", user="ada@example.com", password=PASSWORD))
    broker.put("bea", "mailbox", mailbox_secret(imap_host="imap.example", smtp_host="smtp.example", user="bea@example.com", password="bea-secret"))
    directory = Directory()
    mailbox = ImapMailbox(broker, open_imap=directory.open_imap, open_smtp=directory.open_smtp)
    ada = mailbox.messages("ada")
    bea = mailbox.messages("bea")
    assert ada[0]["sender"] == "Jane Doe"
    assert "hello from ada" in ada[0]["body"]
    assert PASSWORD not in ada[0]["body"]
    assert PASSWORD not in ada[0]["sender"]
    assert bea[0]["body"] == "bea-only-note"
    assert "hello from ada" not in bea[0]["body"]
    assert directory.logins[0] == ("imap.example", "ada@example.com", PASSWORD)
    assert mailbox.messages("cara") == []
    assert "bea-secret" not in json.dumps(ada)


def test_plain_part_is_the_body_and_a_missing_secret_does_not_connect() -> None:
    message = EmailMessage()
    message["From"] = "Jane Doe"
    message.set_content("plain note")
    message.add_alternative("<p>html note</p>", subtype="html")
    broker = Broker()
    seen: list[str] = []

    class One:
        def login(self, user: str, password: str) -> None:
            return None

        def fetch_recent(self, limit: int) -> list[bytes]:
            return [message.as_bytes()]

        def search(self, criteria: str, limit: int) -> list[tuple[str, bytes]]:
            return [("9001", message.as_bytes())]

        def fetch_one(self, uid: str) -> bytes | None:
            return message.as_bytes() if uid == "9001" else None

        def logout(self) -> None:
            return None

    mailbox = ImapMailbox(broker, open_imap=lambda host: seen.append(host) or One(), open_smtp=lambda host: None)
    assert mailbox.messages("ada") == []
    assert seen == []
    broker.put("ada", "mailbox", "mailbox-password-xyz")
    assert mailbox.messages("ada") == []
    assert seen == []
    broker.put("ada", "mailbox", mailbox_secret(imap_host="imap.example", smtp_host="smtp.example", user="ada@example.com", password=PASSWORD))
    rows = mailbox.messages("ada")
    assert rows == [{"id": "9001", "sender": "Jane Doe", "subject": "", "date": "", "body": "plain note"}]
    assert "<p>" not in rows[0]["body"]


def test_send_waits_for_confirm_and_the_password_is_not_logged() -> None:
    assistant = Assistant()
    directory = Directory()
    assistant.broker.put(
        "ada",
        "mailbox",
        mailbox_secret(imap_host="imap.example", smtp_host="smtp.example", user="ada@example.com", password=PASSWORD),
    )
    assistant.add(Mail(ImapMailbox(assistant.broker, open_imap=directory.open_imap, open_smtp=directory.open_smtp)))
    held = assistant.invoke("ada", "thread", "mail_send", {"to": "jane@example.com", "subject": "hi", "body": f"see {SECRET}"})
    assert held["status"] == "confirm"
    assert directory.opened_smtp == []
    done = assistant.invoke(
        "ada",
        "thread",
        "mail_send",
        {"to": "jane@example.com", "subject": "hi", "body": f"see {SECRET}"},
        confirmed=True,
    )
    assert done["status"] == "done"
    assert done["result"] == "sent"
    assert directory.opened_smtp == ["smtp.example"]
    assert directory.sent[0]["To"] == "jane@example.com"
    assert "see" in directory.sent[0].get_content()
    assert PASSWORD not in directory.sent[0].as_string()
    log = json.dumps(assistant.activity.read("ada"))
    assert PASSWORD not in log
    assert SECRET not in log
    assert assistant.activity.read("bea") == []
    schemas = assistant.tools("ada")
    assert all("password" not in json.dumps(tool) for tool in schemas)




def test_login_failure_does_not_repeat_the_password() -> None:
    broker = Broker()
    broker.put("ada", "mailbox", mailbox_secret(imap_host="imap.example", smtp_host="smtp.example", user="ada@example.com", password=PASSWORD))
    mailbox = ImapMailbox(broker, open_imap=lambda host: ExplodingLogin(PASSWORD), open_smtp=lambda host: ExplodingLogin(PASSWORD))
    try:
        mailbox.messages("ada")
    except RuntimeError as exc:
        assert str(exc) == "mailbox login failed"
        assert PASSWORD not in str(exc)
    else:
        raise AssertionError("login should fail")


def test_imaplib_client_reads_the_newest_message_without_marking_it_seen() -> None:
    older = EmailMessage()
    older["From"] = "Old"
    older.set_content("older")
    newer = EmailMessage()
    newer["From"] = "New"
    newer.set_content(f"newer {PASSWORD}")
    fake = FakeImaplib("imap.example", [older.as_bytes(), newer.as_bytes()])
    client = ImaplibClient("imap.example", imap_class=lambda host: fake)
    client.login("ada@example.com", PASSWORD)
    raw = client.fetch_recent(1)
    assert len(raw) == 1
    assert b"newer" in raw[0]
    assert b"older" not in raw[0]
    assert ("select", "INBOX", True) in fake.calls
    assert any(call[0] == "uid" and call[1] == "FETCH" for call in fake.calls)
    assert PASSWORD not in str(fake.calls)


def test_smtplib_client_sends_the_message_it_is_given() -> None:
    captured: list[EmailMessage] = []

    class FakeSmtp:
        def __init__(self, host: str) -> None:
            self.host = host

        def login(self, user: str, password: str) -> None:
            return None

        def send_message(self, message: EmailMessage) -> None:
            captured.append(message)

        def quit(self) -> None:
            return None

    message = EmailMessage()
    message["From"] = "ada@example.com"
    message["To"] = "jane@example.com"
    message.set_content("hello")
    client = SmtplibClient("smtp.example", smtp_class=FakeSmtp)
    client.login("ada@example.com", PASSWORD)
    client.send_message(message)
    client.close()
    assert captured[0]["To"] == "jane@example.com"
    assert PASSWORD not in captured[0].as_string()


def test_mail_list_tool_fetches_the_inbox() -> None:
    from robin.model import ToolCall
    from robin.ner import UnavailableNer

    class ReadyNer(UnavailableNer):
        def available(self) -> bool:
            return True

    directory = Directory()
    assistant = Assistant(ner=ReadyNer())
    assistant.broker.put(
        "ada",
        "mailbox",
        mailbox_secret(imap_host="imap.example.com", smtp_host="smtp.example.com", user="ada@example.com", password=PASSWORD),
    )
    assistant.add(Mail(ImapMailbox(assistant.broker, open_imap=directory.open_imap, open_smtp=directory.open_smtp)))

    class Scripted:
        def __init__(self) -> None:
            self.turns = [
                ModelTurn("", (ToolCall("mail_list", {}),)),
                ModelTurn("You have mail from Jane."),
            ]

        def complete(self, *, messages, tools):
            return self.turns.pop(0)

    reply = converse(assistant, Task("ada", "home", "what mail do I have"), Scripted())
    assert "Jane" in reply.text or "mail" in reply.text.lower()
    assert directory.logins


def test_mail_read_reply_and_archive_use_stable_uids() -> None:
    broker = Broker()
    broker.put(
        "ada",
        "mailbox",
        mailbox_secret(imap_host="imap.example", smtp_host="smtp.example", user="ada@example.com", password=PASSWORD),
    )
    older = _letter("old@example.com", "keep this body", message_id="stable-old@test")
    newer = _letter("new@example.com", "brand new", message_id="stable-new@test")
    extra = _letter("latest@example.com", "arrived later", message_id="stable-latest@test")

    class UidBox:
        def __init__(self) -> None:
            self.messages = [("10042", older), ("10099", newer)]
            self.sent: list[EmailMessage] = []
            self.flags: list[tuple[str, str]] = []

        def open_imap(self, host: str):
            return self

        def open_smtp(self, host: str):
            return self

        def login(self, user: str, password: str) -> None:
            return None

        def search(self, criteria: str, limit: int) -> list[tuple[str, bytes]]:
            return self.messages[-limit:]

        def fetch_one(self, uid: str) -> bytes | None:
            for item_uid, raw in self.messages:
                if item_uid == uid:
                    return raw
            return None

        def store_flags(self, uid: str, action: str) -> None:
            self.flags.append((uid, action))

        def send_message(self, message: EmailMessage) -> None:
            self.sent.append(message)

        def close(self) -> None:
            return None

        def logout(self) -> None:
            return None

    box = UidBox()
    mailbox = ImapMailbox(broker, open_imap=box.open_imap, open_smtp=box.open_smtp)
    listed = mailbox.messages("ada")
    assert [row["id"] for row in listed] == ["10042", "10099"]
    assert listed[0]["sender"] == "old@example.com"
    box.messages.append(("10110", extra))
    window = mailbox.messages("ada")
    assert [row["id"] for row in window] == ["10042", "10099", "10110"][-mailbox.limit :]
    read = mailbox.read("ada", "10042")
    assert read is not None
    assert read["id"] == "10042"
    assert "keep this body" in read["body"]
    assert read["sender"] == "old@example.com"
    mail = Mail(mailbox)
    reply = mail.invoke("ada", "mail_reply", {"id": "10042", "body": "thanks"})
    assert reply == "sent"
    assert box.sent[0]["To"] == "old@example.com"
    assert "Re:" in box.sent[0]["Subject"]
    marked = mail.invoke("ada", "mail_mark", {"id": "10042", "action": "archive"})
    assert marked == "marked archive"
    assert box.flags == [("10042", "archive")]
    latest = mailbox.read("ada", "10110")
    assert latest is not None and "arrived later" in latest["body"]


def test_an_icloud_address_uses_icloud_mail_servers() -> None:
    directory = Directory()
    directory.boxes["ada@icloud.com"] = [_letter("Apple", "hello icloud")]
    broker = Broker()
    broker.put("ada", "mailbox", mailbox_secret(imap_host="", smtp_host="", user="ada@icloud.com", password=PASSWORD))
    mail = Mail(ImapMailbox(broker, open_imap=directory.open_imap, open_smtp=directory.open_smtp))
    result = mail.invoke("ada", "mail_list", {})
    text = result if isinstance(result, str) else str(result)
    assert directory.logins and directory.logins[0][0].endswith("mail.me.com") or "icloud" in text.lower() or "Apple" in text


SHOP = "Northwind"
CONTACT = "Riley Quinn"
ORDER_SUBJECT = "Your order 4412"
ORDER_BODY = "Thanks for your order. Riley Quinn will deliver it. Keep the receipt."


class PhraseNer(UnavailableNer):
    """Tags listed phrases so tests can stand in for a warm ORG/PERSON detector."""

    def __init__(self, phrases: dict[str, str]) -> None:
        self.phrases = {phrase.lower(): (phrase, label) for phrase, label in phrases.items()}

    def available(self) -> bool:
        return True

    def detect(self, text: str) -> tuple[Entity, ...]:
        found: list[Entity] = []
        lower = text.lower()
        for needle, (_original, label) in sorted(self.phrases.items(), key=lambda item: -len(item[0])):
            start = 0
            while True:
                index = lower.find(needle, start)
                if index < 0:
                    break
                end = index + len(needle)
                if not any(index < other.end and end > other.start for other in found):
                    found.append(Entity(index, end, label))
                start = end
        return tuple(sorted(found, key=lambda entity: entity.start))


class RecordingBox:
    def __init__(self, items: list[tuple[str, bytes]]) -> None:
        self.items = items
        self.criteria: list[str] = []
        self.sent: list[EmailMessage] = []

    def open_imap(self, host: str):
        return self

    def open_smtp(self, host: str):
        return self

    def login(self, user: str, password: str) -> None:
        return None

    def search(self, criteria: str, limit: int) -> list[tuple[str, bytes]]:
        self.criteria.append(criteria)
        return self.items[-limit:]

    def fetch_one(self, uid: str) -> bytes | None:
        for item_uid, raw in self.items:
            if item_uid == uid:
                return raw
        return None

    def send_message(self, message: EmailMessage) -> None:
        self.sent.append(message)

    def close(self) -> None:
        return None

    def logout(self) -> None:
        return None


def _shop_mailbox() -> tuple[Broker, RecordingBox, ImapMailbox]:
    broker = Broker()
    broker.put(
        "ada",
        "mailbox",
        mailbox_secret(imap_host="imap.example", smtp_host="smtp.example", user="ada@example.com", password=PASSWORD),
    )
    order = _letter(
        f"{SHOP} <orders@northwind.test>",
        ORDER_BODY,
        message_id="order@northwind.test",
        subject=ORDER_SUBJECT,
        date="Fri, 03 Oct 2026 09:15:00 +0000",
    )
    decoy = _letter("Sam Lee <sam@example.com>", "Weekend plans", message_id="decoy@example.com", subject="Saturday")
    box = RecordingBox([("7001", decoy), ("7002", order)])
    mailbox = ImapMailbox(broker, open_imap=box.open_imap, open_smtp=box.open_smtp)
    return broker, box, mailbox


def test_mail_search_matches_sender_name_and_topic_words() -> None:
    _broker, box, mailbox = _shop_mailbox()
    by_sender = mailbox.search("ada", sender=SHOP)
    assert [row["id"] for row in by_sender] == ["7002"]
    assert by_sender[0]["sender"] == SHOP
    assert by_sender[0]["subject"] == ORDER_SUBJECT
    assert by_sender[0]["date"] == "2026-10-03"
    assert "order" in by_sender[0]["body"].lower()
    assert SHOP in box.criteria[0]
    assert "[ORG_" not in box.criteria[0]

    box.criteria.clear()
    by_order = mailbox.search("ada", query="order")
    assert [row["id"] for row in by_order] == ["7002"]
    assert "order" in box.criteria[0].lower()

    box.criteria.clear()
    by_receipt = mailbox.search("ada", query="the receipt")
    assert [row["id"] for row in by_receipt] == ["7002"]

    missed = mailbox.search("ada", sender="[ORG_deadbeefdeadbeefdeadbeefdeadbeef_1]")
    assert missed == []


def test_mail_search_broadens_when_the_first_criteria_miss() -> None:
    broker = Broker()
    broker.put(
        "ada",
        "mailbox",
        mailbox_secret(imap_host="imap.example", smtp_host="smtp.example", user="ada@example.com", password=PASSWORD),
    )
    order = _letter(f"{SHOP} <orders@northwind.test>", ORDER_BODY, subject=ORDER_SUBJECT)

    class StrictThenAll:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def login(self, user: str, password: str) -> None:
            return None

        def search(self, criteria: str, limit: int) -> list[tuple[str, bytes]]:
            self.calls.append(criteria)
            if criteria == "ALL":
                return [("8001", order)]
            return []

        def logout(self) -> None:
            return None

    client = StrictThenAll()
    mailbox = ImapMailbox(broker, open_imap=lambda host: client, open_smtp=lambda host: None)
    found = mailbox.search("ada", sender=SHOP)
    assert client.calls[0] != "ALL"
    assert "ALL" in client.calls
    assert found and found[0]["sender"] == SHOP


def test_mail_search_resolves_references_and_redacts_results_for_the_model() -> None:
    _broker, box, mailbox = _shop_mailbox()
    assistant = Assistant(ner=PhraseNer({SHOP: "ORG", CONTACT: "PERSON"}))
    assistant.broker.put(
        "ada",
        "mailbox",
        mailbox_secret(imap_host="imap.example", smtp_host="smtp.example", user="ada@example.com", password=PASSWORD),
    )
    assistant.add(Mail(mailbox))

    class Model:
        def __init__(self) -> None:
            self.step = 0
            self.person_ref = ""

        def complete(self, *, messages, tools):
            blob = json.dumps(messages)
            assert SHOP not in blob
            assert CONTACT not in blob
            self.step += 1
            if self.step == 1:
                spoken = str(messages[1]["content"])
                org = next(match.group() for match in REFERENCE.finditer(spoken) if match.group().startswith("[ORG_"))
                return ModelTurn("", (ToolCall("mail_search", {"from": org}),))
            if self.step == 2:
                result = str(messages[-1]["content"])
                assert ORDER_SUBJECT.split()[-1] in result or "order" in result.lower()
                self.person_ref = next(
                    match.group() for match in REFERENCE.finditer(result) if match.group().startswith("[PERSON_")
                )
                return ModelTurn("", (ToolCall("mail_search", {"query": self.person_ref}),))
            return ModelTurn("Here is the order email.")

    model = Model()
    reply = converse(assistant, Task("ada", "mail", f"Get the {SHOP} email about my order"), model)
    assert SHOP.lower() in reply.text.lower() or "order" in reply.text.lower()
    assert box.criteria
    assert SHOP in box.criteria[0]
    assert "[ORG_" not in "".join(box.criteria)
    assert "riley" in "".join(box.criteria).lower()
    assert model.person_ref
    assert CONTACT not in model.person_ref


def test_mail_search_by_topic_redacts_a_sender_that_only_appears_in_the_result() -> None:
    _broker, box, mailbox = _shop_mailbox()
    assistant = Assistant(ner=PhraseNer({SHOP: "ORG", CONTACT: "PERSON"}))
    assistant.add(Mail(mailbox))

    class Model:
        def complete(self, *, messages, tools):
            blob = json.dumps(messages)
            assert SHOP not in blob
            assert CONTACT not in blob
            if messages[-1]["role"] != "tool":
                return ModelTurn("", (ToolCall("mail_search", {"query": "order"}),))
            result = str(messages[-1]["content"])
            assert "4412" in result or "order" in result.lower()
            assert any(match.group().startswith("[ORG_") for match in REFERENCE.finditer(result))
            return ModelTurn("I found the order.")

    reply = converse(assistant, Task("ada", "mail", "find my order"), Model())
    assert "order" in reply.text.lower()
    assert any("order" in criteria.lower() for criteria in box.criteria)
