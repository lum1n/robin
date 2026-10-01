"""Robin reads MCP docs/configs itself and asks for logins and secrets through secure forms."""

from __future__ import annotations

import json

from robin.capabilities import mcp_config
from robin.capabilities.mcp import Mcp

ODA_README = """
# mcp-oda

### Initial Setup

```bash
read -rsp "Oda password: " ODA_PASSWORD; printf '\\n'
printf '%s' "$ODA_PASSWORD" | npx github:gbbirkisson/mcp-oda auth login --user your@email.com --pass-stdin
unset ODA_PASSWORD
```

```bash
npx github:gbbirkisson/mcp-oda auth user
```

```bash
# Authentication
read -rsp "Oda password: " ODA_PASSWORD; printf '\\n'
printf '%s' "$ODA_PASSWORD" | npx github:gbbirkisson/mcp-oda auth login --user your@email.com --pass-stdin
```

#### Claude Desktop

```json
{
  "mcpServers": {
    "oda": {
      "command": "npx",
      "args": ["-y", "github:gbbirkisson/mcp-oda", "mcp"]
    }
  }
}
```
"""

TOOLS = [
    {"name": "search", "description": "Search", "readOnlyHint": True, "inputSchema": {"type": "object"}},
    {"name": "cart_clear", "description": "Clear", "destructiveHint": True, "inputSchema": {"type": "object"}},
]


class Session:
    def __init__(self) -> None:
        self.configs: list[dict] = []

    def list_tools(self) -> list[dict]:
        return TOOLS

    def call_tool(self, name: str, arguments: dict) -> str:
        return "ok"


def make(admins=("ada",), runs=None, code=0, output="Logged in"):
    session = Session()
    calls = runs if runs is not None else []

    def run(account, command, args, env, stdin):
        calls.append((account, command, args, env, stdin))
        return code, output

    def open_session(config):
        session.configs.append(config)
        return session

    mcp = Mcp(open_session=open_session, admins=set(admins), fetch=lambda url: ODA_README, run_command=run)
    return mcp, calls


def test_read_docs_finds_oda_server_and_its_login() -> None:
    specs, logins = mcp_config.read_docs(ODA_README)
    assert specs[0]["name"] == "oda"
    assert specs[0]["command"] == "npx"
    assert specs[0]["args"] == ["-y", "github:gbbirkisson/mcp-oda", "mcp"]
    assert len(logins) == 1
    login = logins[0]
    assert login["command"] == "npx"
    assert login["stdin"] == "password"
    assert [field["id"] for field in login["fields"]] == ["email", "password"]
    assert mcp_config.matches_server(login, specs[0])


def test_docs_source_url_reads_github_readme() -> None:
    raw = mcp_config.docs_source_url("https://github.com/gbbirkisson/mcp-oda#installation")
    assert raw.startswith("https://raw.githubusercontent.com/gbbirkisson/mcp-oda/")
    assert raw.endswith("README.md")


def test_parse_config_accepts_common_shapes() -> None:
    claude = mcp_config.parse_config(json.dumps({"mcpServers": {"x": {"command": "npx", "args": ["-y", "x"]}}}))
    assert claude[0]["transport"] == "stdio" and claude[0]["name"] == "x"
    vscode = mcp_config.parse_config(json.dumps({"servers": {"s": {"type": "http", "url": "https://mcp.example/mcp"}}}))
    assert vscode[0]["transport"] == "http" and vscode[0]["url"] == "https://mcp.example/mcp"
    with_env = mcp_config.parse_config(
        json.dumps({"mcpServers": {"t": {"command": "npx", "args": ["t"], "env": {"TAVILY_API_KEY": "your-api-key"}}}})
    )
    assert with_env[0]["needs"][0]["key"] == "TAVILY_API_KEY"


def test_guess_name() -> None:
    assert mcp_config.guess_name({"transport": "http", "url": "https://mcp.sentry.dev/mcp"}) == "sentry"
    assert mcp_config.guess_name({"transport": "stdio", "command": "npx", "args": ["-y", "github:o/mcp-oda"]}) == "oda"


def test_admin_adds_oda_from_docs_with_login_form_and_password_on_stdin() -> None:
    mcp, runs = make()
    started = mcp.invoke("ada", "mcp_setup_start", {"docs_url": "https://github.com/gbbirkisson/mcp-oda#installation"})
    assert "Draft MCP oda saved" in started and "one-time login" in started
    asked = mcp.invoke("ada", "mcp_setup_test", {"name": "oda"})
    assert "secure form" in asked
    form = mcp.pending_input("ada", "c1")
    assert form is not None
    assert [(field.id, field.kind) for field in form.fields] == [("email", "email"), ("password", "secret")]
    assert "auth login" in form.reason
    result = mcp.accept_input("ada", "c1", form.request_id, {"email": "a@b.no", "password": "hunter2"})
    assert result is not None and result.resume
    assert runs[0][1] == "npx"
    assert "a@b.no" in runs[0][2] and "hunter2" not in runs[0][2]
    assert runs[0][4] == "hunter2"
    draft = mcp._drafts["ada"]["oda"]
    assert draft["logged_in"] is True
    assert "hunter2" not in json.dumps(draft) and "a@b.no" not in json.dumps(draft)
    tested = mcp.invoke("ada", "mcp_setup_test", {"name": "oda"})
    assert "search" in tested


def test_failed_login_keeps_form_open_and_scrubs_values() -> None:
    mcp, _ = make(code=1, output="bad password hunter2 for a@b.no")
    mcp.invoke("ada", "mcp_setup_start", {"docs_url": "https://github.com/gbbirkisson/mcp-oda"})
    mcp.invoke("ada", "mcp_setup_test", {"name": "oda"})
    form = mcp.pending_input("ada", "c1")
    result = mcp.accept_input("ada", "c1", form.request_id, {"email": "a@b.no", "password": "hunter2"})
    assert result.input_again
    again = mcp.pending_input("ada", "c1")
    assert "Login failed" in again.reason
    assert "hunter2" not in again.reason and "a@b.no" not in again.reason


def test_non_admin_cannot_add_stdio_from_docs() -> None:
    mcp, _ = make(admins=())
    result = mcp.invoke("bob", "mcp_setup_start", {"docs_url": "https://github.com/gbbirkisson/mcp-oda"})
    assert "only household admins" in result
    assert "oda" not in mcp._drafts.get("bob", {})


def test_pasted_config_asks_for_env_secret_in_form() -> None:
    mcp, _ = make()
    config = json.dumps(
        {"mcpServers": {"tavily": {"command": "npx", "args": ["-y", "tavily-mcp"], "env": {"TAVILY_API_KEY": "<key>"}}}}
    )
    mcp.invoke("ada", "mcp_setup_start", {"config": config})
    mcp.invoke("ada", "mcp_setup_test", {"name": "tavily"})
    form = mcp.pending_input("ada", "c1")
    assert len(form.fields) == 1 and form.fields[0].kind == "secret"
    result = mcp.accept_input("ada", "c1", form.request_id, {"need0": "tvly-123"})
    assert result.resume
    assert mcp._drafts["ada"]["tavily"]["env"]["TAVILY_API_KEY"] == "tvly-123"


def test_bearer_form_for_http_and_missing_value_reasks() -> None:
    mcp, _ = make(admins=())
    mcp.invoke("bob", "mcp_setup_start", {"name": "x", "url": "https://mcp.example/mcp", "auth": "bearer"})
    mcp.invoke("bob", "mcp_setup_test", {"name": "x"})
    form = mcp.pending_input("bob", "c1")
    empty = mcp.accept_input("bob", "c1", form.request_id, {})
    assert empty.input_again
    done = mcp.accept_input("bob", "c1", form.request_id, {"need0": "tok"})
    assert done.resume and mcp._drafts["bob"]["x"]["token"] == "tok"


def test_manage_add_input_finish_tools_remove() -> None:
    mcp, runs = make()
    status, view = mcp.manage("ada", "add", {"docs_url": "https://github.com/gbbirkisson/mcp-oda"})
    assert status == 200 and view["name"] == "oda" and view["input"]["fields"]
    request_id = view["input"]["request_id"]
    status, view = mcp.manage(
        "ada", "input", {"request_id": request_id, "values": {"email": "a@b.no", "password": "pw"}}
    )
    assert status == 200 and view["tested"] and view["input"] is None
    assert {tool["name"] for tool in view["tools"]} == {"search", "cart_clear"}
    status, view = mcp.manage("ada", "finish", {"name": "oda", "enabled_tools": ["search"], "trust": "ask"})
    assert status == 200 and view["status"] == "active"
    assert [tool["enabled"] for tool in view["tools"]] == [True, False]
    status, view = mcp.manage("ada", "tools", {"name": "oda", "enable": ["cart_clear"], "trust": "auto"})
    assert status == 200 and view["trust"] == "auto" and all(tool["enabled"] for tool in view["tools"])
    status, _ = mcp.manage("ada", "input", {"request_id": "gone", "values": {}})
    assert status == 409
    status, view = mcp.manage("ada", "remove", {"name": "oda"})
    assert status == 200 and view["status"] == "removed"
    assert not mcp.summary("ada")


def test_manage_add_reports_errors() -> None:
    mcp, _ = make(admins=())
    status, body = mcp.manage("bob", "add", {"docs_url": "https://github.com/gbbirkisson/mcp-oda"})
    assert status == 400 and "admins" in body["error"]


def test_http_mcp_routes_require_session_and_drive_setup(tmp_path) -> None:
    from robin.auth import Auth
    from robin.http import Service, dispatch
    from robin.session import Assistant
    from robin.store import HouseholdStore
    from robin.vault import new_key

    store = HouseholdStore(tmp_path / "house.sqlite", new_key())
    auth = Auth(store, token_factory=lambda: "tok-ada")
    assistant = Assistant(store=store)
    mcp, runs = make()
    assistant.add(mcp)
    service = Service(assistant, object(), auth=auth)
    dispatch(service, "POST", "/v1/accounts", body={"account_id": "ada", "password": "correct-horse-battery"})
    dispatch(service, "POST", "/v1/sessions", body={"account_id": "ada", "password": "correct-horse-battery"})
    headers = {"authorization": "Bearer tok-ada"}
    body = {"account_id": "ada", "docs_url": "https://github.com/gbbirkisson/mcp-oda"}

    denied, _ = dispatch(service, "POST", "/v1/mcp/add", body=body)
    assert denied == 401
    status, view = dispatch(service, "POST", "/v1/mcp/add", body=body, headers=headers)
    assert status == 200 and view["input"]["fields"]
    status, view = dispatch(
        service,
        "POST",
        "/v1/mcp/input",
        body={"account_id": "ada", "request_id": view["input"]["request_id"], "values": {"email": "a@b.no", "password": "pw"}},
        headers=headers,
    )
    assert status == 200 and view["tested"]
    status, view = dispatch(
        service, "POST", "/v1/mcp/finish", body={"account_id": "ada", "name": "oda", "enabled_tools": ["search"]}, headers=headers
    )
    assert status == 200 and view["status"] == "active"
    status, listed = dispatch(service, "GET", "/v1/mcp", query={"account_id": "ada"}, headers=headers)
    assert status == 200 and listed["admin"] is True
    assert [row["name"] for row in listed["servers"]] == ["oda"]
    store.close()
