import json
from email.message import EmailMessage

from robin.capabilities.mail import ImapMailbox, ImaplibClient, Mail, SmtplibClient, mailbox_secret
from robin.session import Assistant

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

    def logout(self) -> tuple[str, list]:
        self.calls.append(("logout",))
        return "OK", []


class Directory:
    def __init__(self) -> None:
        self.logins: list[tuple[str, str, str]] = []
        self.sent: list[EmailMessage] = []
        self.opened_smtp: list[str] = []
        self.boxes = {
            "ada@example.com": [_letter("Jane Doe", f"hello from ada {SECRET}")],
            "bea@example.com": [_letter("Sam", "bea-only-note")],
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
        return self.directory.boxes.get(self.user, [])[-limit:]

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


def _letter(sender: str, body: str) -> bytes:
    message = EmailMessage()
    message["From"] = sender
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
    assert rows == [{"sender": "Jane Doe", "body": "plain note"}]
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
    held = assistant.invoke("ada", "thread", "send_message", {"to": "jane@example.com", "subject": "hi", "body": f"see {SECRET}"})
    assert held["status"] == "confirm"
    assert directory.opened_smtp == []
    done = assistant.invoke(
        "ada",
        "thread",
        "send_message",
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
    assert ("fetch", "(BODY.PEEK[])") in fake.calls
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
