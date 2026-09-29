import pytest
import json

from robin.capabilities.browser import Browser, Desk
from robin.http import Service, dispatch
from robin.loop import converse
from robin.model import ModelTurn, ToolCall
from robin.ner import UnavailableNer
from robin.policy import Task
from robin.session import Assistant
from robin.store import HouseholdStore
from robin.vault import new_key

PASSWORD = "correct-horse-battery"
USER = "ada@shop.com"
STORE = "https://store.example/"


class ReadyNer(UnavailableNer):
    def available(self) -> bool:
        return True


class Gate:
    def __init__(self) -> None:
        self.form = False
        self.signed_in = False
        self.username = ""
        self.password_typed = ""
        self.url = ""
        self.clicks: list[str] = []

    def open(self, url: str) -> None:
        self.url = url
        self.signed_in = False
        self.form = False
        self.password_typed = ""

    def needs_login(self) -> bool:
        return self.form and not self.signed_in

    def read(self) -> tuple[str, str]:
        if self.signed_in:
            return "Yesterday sales were 42", ""
        if self.form:
            return "Sign in", ""
        return "Welcome. Log in", ""

    def click(self, target: str) -> None:
        self.clicks.append(target)
        if target in {"Log in", "Sign in", "Login"} and not self.signed_in:
            self.form = True
            self.url = "https://accounts.store.example/login"

    def type_text(self, target: str, text: str) -> None:
        self.username = text

    def type_username(self, text: str) -> None:
        self.username = text

    def type_password(self, text: str) -> None:
        self.password_typed = text

    def submit(self) -> None:
        if self.username == USER and self.password_typed == PASSWORD:
            self.signed_in = True
            self.form = False
            self.url = "https://store.example/admin"

    def sign_in(self, user: str, password: str) -> None:
        self.type_username(user)
        self.type_password(password)
        self.submit()

    def needs_code(self) -> bool:
        return False

    def submit_code(self, code: str) -> None:
        return None

    def location(self) -> str:
        return self.url


class Scripted:
    def __init__(self) -> None:
        self.seen: list = []

    def complete(self, *, messages: list[dict], tools: list[dict]) -> ModelTurn:
        self.seen.append(messages)
        names = {tool["name"] for tool in tools}
        has_tool_result = any(message.get("role") == "tool" for message in messages)
        person = _person_text(messages)
        if (
            not has_tool_result
            and "browser_open" in names
            and _wants_browser(person)
        ):
            return ModelTurn("", (ToolCall("browser_open", {"url": _browser_url(person)}),))
        for message in reversed(messages):
            if message.get("role") == "tool":
                content = str(message.get("content") or "")
                if "Yesterday sales were 42" in content:
                    return ModelTurn("Yesterday sales were 42")
                if "market report" in content:
                    return ModelTurn(content[:200])
                if content.startswith(("Sign in", "That sign-in", "Enter the")):
                    return ModelTurn(content)
                break
        return ModelTurn("Sales were 42.")


def _person_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = str(message.get("content") or "")
        if "Person:" in content:
            return content.rsplit("Person:", 1)[-1].strip()
        return content
    return ""


def _wants_browser(text: str) -> bool:
    lowered = text.lower()
    if "http://" in lowered or "https://" in lowered:
        return True
    if "store.example" in lowered or "news.example" in lowered:
        return True
    return any(word in lowered for word in ("log in", "sign in", "login", "open ", "read ", "find "))


def _browser_url(text: str) -> str:
    for token in text.split():
        if token.startswith("http://") or token.startswith("https://"):
            return token.rstrip(".,)")
    if "news.example" in text.lower():
        return "https://news.example/story"
    return STORE


def _assistant(store: HouseholdStore | None = None) -> tuple[Assistant, Gate, Scripted]:
    page = Gate()
    assistant = Assistant(store=store, ner=ReadyNer())
    assistant.add(Browser(desk=Desk(lambda url: page), broker=assistant.broker))
    return assistant, page, Scripted()


def test_a_site_asks_once_then_signs_in_from_the_vault(tmp_path) -> None:
    path = tmp_path / "house.sqlite"
    key = new_key()
    store = HouseholdStore(path, key)
    assistant, page, model = _assistant(store)
    ask = converse(assistant, Task("ada", "home", "find yesterday's sales on store.example"), model)
    assert ask.text.startswith("Sign in to accounts.store.example")
    assert PASSWORD not in str(model.seen)
    assert PASSWORD not in ask.text
    given = converse(
        assistant,
        Task("ada", "home", f"username {USER} password {PASSWORD}", allow_cloud=True),
        model,
    )
    assert "Yesterday sales were 42" in given.text
    assert page.password_typed == PASSWORD
    assert page.username == USER
    assert PASSWORD not in str(model.seen)
    assert PASSWORD not in given.text
    assert USER not in given.text
    saved = json.loads(assistant.broker.reveal("ada", "site:accounts.store.example"))
    assert saved == {"password": PASSWORD, "user": USER}
    assert json.loads(assistant.broker.reveal("ada", "site:store.example")) == saved
    try:
        assistant.broker.reveal("bea", "site:store.example")
    except KeyError:
        pass
    else:
        raise AssertionError("another account read the site secret")
    turns = json.dumps(store.turns("ada", "home"))
    assert PASSWORD not in turns
    assert USER not in turns
    raw = path.read_bytes()
    assert PASSWORD.encode() not in raw
    assert USER.encode() not in raw
    store.close()

    again_page = Gate()
    restored = Assistant(store=HouseholdStore(path, key), ner=ReadyNer())
    restored.add(Browser(desk=Desk(lambda url: again_page), broker=restored.broker))
    later = Scripted()
    reply = converse(restored, Task("ada", "home", "yesterday's sales on store.example"), later)
    assert "Yesterday sales were 42" in reply.text
    assert again_page.password_typed == PASSWORD
    assert not any(item.startswith("Sign in to") for item in [reply.text])
    assert PASSWORD not in str(later.seen)
    assert USER not in str(later.seen)


def test_a_sign_in_accepts_the_name_and_password_on_two_lines() -> None:
    assistant, page, model = _assistant()
    ask = converse(assistant, Task("ada", "home", "log in to store.example"), model)
    assert ask.text.startswith("Sign in to")
    given = converse(assistant, Task("ada", "home", f"{USER}\n{PASSWORD}"), model)
    assert "Yesterday sales were 42" in given.text
    assert page.username == USER
    assert page.password_typed == PASSWORD
    assert PASSWORD not in str(model.seen)
    assert PASSWORD not in given.text


def test_sign_in_waits_for_the_form_to_leave() -> None:
    class Slow(Gate):
        def __init__(self) -> None:
            super().__init__()
            self.checks = 0

        def needs_login(self) -> bool:
            self.checks += 1
            if self.signed_in:
                # Stay on the form for the first checks so Robin must wait.
                return self.checks < 3
            return self.form

        def submit(self) -> None:
            if self.username == USER and self.password_typed == PASSWORD:
                self.signed_in = True
                self.url = "https://store.example/admin"

        def read(self) -> tuple[str, str]:
            if self.signed_in and self.checks >= 3:
                return "Yesterday sales were 42", ""
            if self.form:
                return "Sign in", ""
            return "Welcome. Log in", ""

    page = Slow()
    assistant = Assistant(ner=ReadyNer())
    assistant.add(Browser(desk=Desk(lambda url: page), broker=assistant.broker))
    model = Scripted()
    ask = converse(assistant, Task("ada", "home", "log in to store.example"), model)
    assert ask.text.startswith("Sign in to")
    done = converse(assistant, Task("ada", "home", f"username {USER} password {PASSWORD}"), model)
    assert "Yesterday sales were 42" in done.text or page.signed_in
    assert page.checks >= 3
    assert page.password_typed == PASSWORD

    assistant, page, model = _assistant()
    ask = converse(assistant, Task("ada", "home", "log in to store.example"), model)
    assert "cancel" in ask.text.lower()
    freed = converse(assistant, Task("ada", "home", "never mind, what is 2+2?"), model)
    assert not freed.text.startswith("Sign in to")
    assert not freed.text.startswith("That sign-in")
    assert model.seen
    latest = str(model.seen[-1])
    assert "2+2" in latest or "Person:" in latest


def test_a_blocked_sign_in_keeps_the_password_and_hands_the_form_to_the_model() -> None:
    class Blocked(Gate):
        def sign_in(self, user: str, password: str) -> None:
            self.username = user
            self.password_typed = password
            self.form = True
            self.signed_in = False
            self.url = "https://accounts.store.example/login"

        def needs_login(self) -> bool:
            return True

        def read(self) -> tuple[str, str]:
            return (
                'URL: https://accounts.store.example/login\n\n'
                'Interactive:\n[1] textbox "Email"\n[2] textbox "Password"\n\n'
                "Content:\nUnusual activity detected. Please verify you are human.",
                "",
            )

    page = Blocked()
    assistant = Assistant(ner=ReadyNer())
    assistant.add(Browser(desk=Desk(lambda url: page), broker=assistant.broker))

    class BlockedScripted:
        def __init__(self) -> None:
            self.seen: list[tuple[str, list[str]]] = []
            self.calls = 0

        def complete(self, *, messages: list[dict], tools: list[dict]) -> ModelTurn:
            self.calls += 1
            names = [tool["name"] for tool in tools]
            self.seen.append((messages, names))
            person = _person_text(messages)
            has_tool_result = any(message.get("role") == "tool" for message in messages)
            if not has_tool_result and "browser_open" in names and _wants_browser(person):
                return ModelTurn("", (ToolCall("browser_open", {"url": STORE}),))
            joined = "\n".join(str(message.get("content") or "") for message in messages)
            filled_user = any(
                message.get("role") == "tool"
                and "filled saved username" in str(message.get("content") or "").lower()
                for message in messages
            )
            if "Saved sign-in" in joined and "browser_fill_username" in names and not filled_user:
                assert "browser_fill_password" in names
                return ModelTurn("", (ToolCall("browser_fill_username", {"target": "1"}),))
            if filled_user:
                return ModelTurn("", (ToolCall("browser_fill_password", {"target": "2"}),))
            return ModelTurn("Stopped at the site challenge.")

    model = BlockedScripted()
    converse(assistant, Task("ada", "home", "log in to store.example"), model)
    held = converse(assistant, Task("ada", "home", f"username {USER} password {PASSWORD}"), model)
    # Password fill waits for confirm instead of crashing the turn.
    assert held.status == "confirm"
    assert held.tool == "browser_fill_password"
    names = [name for name in assistant.broker.names("ada") if name.startswith("site:")]
    assert names
    stored = json.loads(assistant.broker.reveal("ada", names[0]))
    assert stored["password"] == PASSWORD


def test_a_wrong_password_is_not_kept_and_a_long_page_is_not_a_login(tmp_path, monkeypatch) -> None:
    import robin.capabilities.browser as browser_mod

    monkeypatch.setattr(browser_mod, "_SIGN_IN_WAIT_MS", 400)

    class Rejected(Gate):
        def read(self) -> tuple[str, str]:
            if self.password_typed and self.password_typed != PASSWORD:
                return "Incorrect password. Please try again.", ""
            return super().read()

    page = Rejected()
    assistant = Assistant(ner=ReadyNer())
    assistant.add(Browser(desk=Desk(lambda url: page), broker=assistant.broker))
    model = Scripted()
    converse(assistant, Task("ada", "home", "open https://store.example/report"), model)
    held = converse(assistant, Task("ada", "home", f"username {USER} password wrong-password"), model)
    assert held.text.startswith("That sign-in to")
    assert PASSWORD not in str(model.seen)
    try:
        assistant.broker.reveal("ada", "site:store.example")
    except KeyError:
        pass
    else:
        raise AssertionError("a failed password was kept")
    assert PASSWORD not in held.text

    class Article:
        def __init__(self) -> None:
            self.clicks: list[str] = []
            self.text = "Log in " + ("market report " * 80)

        def open(self, url: str) -> None:
            return None

        def read(self) -> tuple[str, str]:
            return self.text, ""

        def needs_login(self) -> bool:
            return False

        def click(self, target: str) -> None:
            self.clicks.append(target)

        def location(self) -> str:
            return "https://news.example/story"

        def type_text(self, target: str, text: str) -> None:
            return None

        def type_username(self, text: str) -> None:
            return None

        def type_password(self, text: str) -> None:
            return None

        def submit(self) -> None:
            return None

        def sign_in(self, user: str, password: str) -> None:
            return None

    article = Article()
    news = Assistant(ner=ReadyNer())
    news.add(Browser(desk=Desk(lambda url: article), broker=news.broker))
    reader = Scripted()
    reply = converse(news, Task("ada", "home", "read https://news.example/story"), reader)
    assert "market report" in reply.text
    assert article.clicks == []
    assert reader.seen
    assert not reply.text.startswith("Sign in to")


def test_credentials_in_the_question_never_reach_the_model() -> None:
    assistant, page, model = _assistant()
    reply = converse(
        assistant,
        Task("ada", "home", f"find yesterday's sales on store.example username {USER} password {PASSWORD}"),
        model,
    )
    assert "Yesterday sales were 42" in reply.text
    assert page.password_typed == PASSWORD
    assert PASSWORD not in str(model.seen)
    assert PASSWORD not in reply.text
    assert USER not in reply.text


class TwoFactor(Gate):
    def __init__(self) -> None:
        super().__init__()
        self.challenge = False
        self.code = ""

    def open(self, url: str) -> None:
        super().open(url)
        self.challenge = False
        self.code = ""

    def needs_login(self) -> bool:
        return self.form and not self.signed_in and not self.challenge

    def needs_code(self) -> bool:
        return self.challenge and not self.signed_in

    def submit(self) -> None:
        if self.username == USER and self.password_typed == PASSWORD:
            self.form = False
            self.challenge = True
            self.url = "https://accounts.store.example/challenge"

    def submit_code(self, code: str) -> None:
        self.code = code
        if code == "482193":
            self.challenge = False
            self.signed_in = True
            self.url = "https://store.example/admin"

    def read(self) -> tuple[str, str]:
        if self.signed_in:
            return "Yesterday sales were 42", ""
        if self.challenge:
            return "Enter the verification code", ""
        return super().read()


def test_a_verification_code_is_asked_every_time_and_not_stored(tmp_path) -> None:
    path = tmp_path / "house.sqlite"
    key = new_key()
    store = HouseholdStore(path, key)
    page = TwoFactor()
    assistant = Assistant(store=store, ner=ReadyNer())
    assistant.add(Browser(desk=Desk(lambda url: page), broker=assistant.broker))
    model = Scripted()
    code = "482193"
    first = converse(assistant, Task("ada", "home", "find yesterday's sales on store.example"), model)
    assert first.text.startswith("Sign in to")
    signed = converse(assistant, Task("ada", "home", f"username {USER} password {PASSWORD}"), model)
    assert "verification code" in signed.text
    assert PASSWORD not in str(model.seen)
    assert code not in signed.text
    saved = json.loads(assistant.broker.reveal("ada", "site:store.example"))
    assert saved == {"password": PASSWORD, "user": USER}
    assert code not in json.dumps(saved)
    wrong = converse(assistant, Task("ada", "home", "code 000000", allow_cloud=True), model)
    assert wrong.text.startswith("That code for")
    assert PASSWORD not in str(model.seen)
    assert "000000" not in wrong.text
    done = converse(assistant, Task("ada", "home", f"code {code}"), model)
    assert "Yesterday sales were 42" in done.text
    assert page.code == code
    assert PASSWORD not in str(model.seen)
    assert code not in done.text
    assert PASSWORD not in done.text
    blob = json.dumps({name: assistant.broker.reveal("ada", name) for name in assistant.broker.names("ada")})
    assert code not in blob
    assert code.encode() not in path.read_bytes()
    assert code not in json.dumps(store.turns("ada", "home"))
    again = converse(assistant, Task("ada", "later", "sales on store.example"), model)
    assert "verification code" in again.text
    assert assistant.broker.reveal("ada", "site:store.example")
    store.close()


def test_the_site_secret_is_not_a_connectable_api_secret(tmp_path) -> None:
    path = tmp_path / "house.sqlite"
    key = new_key()
    assistant = Assistant(store=HouseholdStore(path, key))
    service = Service(assistant, Scripted())
    service.auth.register("ada", "ada-session-password")
    ada = {"authorization": f"Bearer {service.auth.login('ada', 'ada-session-password')}"}
    status, body = dispatch(
        service,
        "POST",
        "/v1/secrets",
        body={"account_id": "ada", "name": "site:store.example", "value": PASSWORD},
        headers=ada,
    )
    assert status == 400
    assistant.broker.put("ada", "site:store.example", json.dumps({"password": PASSWORD, "user": USER}))
    listed, listed_body = dispatch(service, "GET", "/v1/secrets", query={"account_id": "ada"}, headers=ada)
    assert listed == 200
    assert listed_body == {"connected": []}
    assert PASSWORD not in json.dumps(listed_body)
