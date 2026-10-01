"""Turn pasted MCP configs, docs pages, and login commands into Robin draft specs."""

from __future__ import annotations

import html
import json
import re
import shlex
from typing import Any
from urllib.parse import urlparse

_LAUNCHERS = ("npx", "uvx", "docker")
_SERVER_KEYS = ("command", "url", "serverUrl", "httpUrl")
_WRAPPER_KEYS = ("mcpServers", "servers", "mcp_servers", "context_servers")
_PLACEHOLDER = re.compile(
    r"^(?:<[^>]*>|\$\{[^}]*\}|\$[A-Z_][A-Z0-9_]*|\{[^}]*\}|x{3,}|\.\.\.|changeme|replace[-_ ]?me.*|todo)$",
    re.IGNORECASE,
)
_EXAMPLE_WORDS = re.compile(r"(?:^|[^a-z])(?:your|example|placeholder|insert)(?:[^a-z]|$)", re.IGNORECASE)
_FENCE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_PRE = re.compile(r"<(?:pre|code)[^>]*>(.*?)</(?:pre|code)>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_LOGIN_WORDS = re.compile(r"\b(?:auth(?:enticate)?\s+login|login|log-in|signin|sign-in|setup|authorize)\b", re.IGNORECASE)
_SECRET_FLAG = re.compile(r"pass|token|secret|key", re.IGNORECASE)
_STDIN_FLAGS = frozenset({"--pass-stdin", "--password-stdin", "--stdin"})
_FIELD_REF = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def is_placeholder(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if _PLACEHOLDER.match(text):
        return True
    return bool(_EXAMPLE_WORDS.search(text.replace("_", " ").replace("-", " ")))


def parse_config(raw: str) -> list[dict[str, Any]]:
    """Accept {"mcpServers": {...}}, {name: {...}}, a single server object, or a bare URL."""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("config is empty")
    if re.fullmatch(r"https?://\S+", text):
        return [_spec("", {"url": text})]
    payloads: list[Any] = []
    try:
        payloads.append(json.loads(text))
    except json.JSONDecodeError:
        payloads.extend(_json_objects(text))
    for payload in payloads:
        specs = _specs(payload)
        if specs:
            return specs
    raise ValueError(
        "That is not an MCP config. Paste the JSON block from the docs "
        '(it has "command" and "args", or a "url").'
    )


def docs_source_url(url: str) -> str:
    """GitHub repo and blob links become the raw README/file so the config is readable."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if host != "github.com":
        return url
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return url
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if len(parts) >= 5 and parts[2] == "blob":
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{parts[3]}/{'/'.join(parts[4:])}"
    return f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/README.md"


def read_docs(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Find server configs and login commands in a README or docs page."""
    blocks = _code_blocks(text)
    specs: list[dict[str, Any]] = []
    for _lang, body in sorted(blocks, key=lambda block: "mcpServers" not in block[1]):
        try:
            found = parse_config(body)
        except ValueError:
            continue
        if found:
            specs = found
            break
    logins: list[dict[str, Any]] = []
    for lang, body in blocks:
        if lang.strip().casefold() in {"json", "jsonc", "toml", "yaml", "yml"}:
            continue
        for line in _shell_lines(body):
            if not _LOGIN_WORDS.search(line) or " mcp add" in line:
                continue
            login = parse_login(line)
            if login is not None and login not in logins:
                logins.append(login)
    return specs, logins


def parse_login(line: str) -> dict[str, Any] | None:
    """A one-time login command: example values and {name} become form fields; --pass-stdin pipes a password."""
    text = str(line or "").strip()
    if "|" in text:
        text = text.rsplit("|", 1)[1].strip()
    try:
        tokens = shlex.split(text)
    except ValueError:
        return None
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens.pop(0)
    if not tokens:
        return None
    command = tokens[0]
    if command not in _LAUNCHERS and not command.startswith("/opt/robin-mcp/"):
        return None
    rest = tokens[1:]
    if command == "npx" and "-y" not in rest and "--yes" not in rest:
        rest = ["-y", *rest]
    fields: list[dict[str, str]] = []
    stdin = ""
    args: list[str] = []
    index = 0
    while index < len(rest):
        token = rest[index]
        index += 1
        if token in _STDIN_FLAGS:
            stdin = "password"
            args.append(token)
            continue
        refs = _FIELD_REF.findall(token)
        if refs:
            for ref in refs:
                _add_field(fields, ref)
            args.append(token)
            continue
        if token.startswith("--") and "=" in token:
            flag, value = token.split("=", 1)
            if is_placeholder(value) or _example_value(value):
                field = _field_for(flag, value)
                _add_field(fields, field)
                args.append(f"{flag}={{{field}}}")
                continue
            args.append(token)
            continue
        if token.startswith("-") and index < len(rest) and not rest[index].startswith("-"):
            value = rest[index]
            if is_placeholder(value) or _example_value(value):
                field = _field_for(token, value)
                _add_field(fields, field)
                args.extend([token, f"{{{field}}}"])
                index += 1
                continue
        args.append(token)
    if stdin:
        _add_field(fields, stdin)
    return {"command": command, "args": args, "stdin": stdin, "fields": fields}


def login_line(login: dict[str, Any]) -> str:
    return " ".join([str(login.get("command") or ""), *[shlex.quote(str(arg)) for arg in login.get("args") or []]])


def field_kind(name: str) -> str:
    if _SECRET_FLAG.search(name):
        return "secret"
    if "email" in name:
        return "email"
    return "text"


def field_label(name: str) -> str:
    return name.replace("_", " ").capitalize()


def fill(arg: str, values: dict[str, str]) -> str:
    return _FIELD_REF.sub(lambda match: values.get(match.group(1), ""), arg)


def matches_server(login: dict[str, Any], spec: dict[str, Any]) -> bool:
    """A docs login line belongs to this server when it runs the same package."""
    server = {str(arg) for arg in spec.get("args") or [] if not str(arg).startswith("-")}
    login_args = {str(arg) for arg in login.get("args") or [] if not str(arg).startswith("-")}
    return bool(server & login_args) and login.get("command") == spec.get("command")


def guess_name(spec: dict[str, Any]) -> str:
    """mcp.sentry.dev → sentry; npx github:owner/mcp-oda → oda."""
    if spec.get("url"):
        labels = (urlparse(str(spec["url"])).hostname or "").split(".")
        words = [label for label in labels[:-1] if label not in {"www", "mcp", "api", "server", "app"}]
        return words[-1] if words else (labels[0] if labels else "")
    for arg in spec.get("args") or []:
        text = str(arg)
        if text.startswith("-"):
            continue
        base = re.split(r"[/:]", text.split("@", 1)[0] if not text.startswith("@") else text[1:].split("@", 1)[0])[-1]
        cleaned = re.sub(r"^(?:mcp|server)[-_]|[-_](?:mcp|server|mcp[-_]server)$", "", base)
        if cleaned:
            return cleaned
    return str(spec.get("command") or "").rsplit("/", 1)[-1]


def _example_value(value: str) -> bool:
    return bool(re.fullmatch(r"(?:you|user|name|me)@(?:email|example|domain)\.\w+", value, re.IGNORECASE))


def _field_for(flag: str, value: str) -> str:
    name = flag.lstrip("-").replace("-", "_").casefold()
    if name in {"user", "username", "login", "u"}:
        return "email" if "@" in value else "username"
    if name in {"pass", "p"}:
        return "password"
    return re.sub(r"[^a-z0-9_]", "_", name) or "value"


def _add_field(fields: list[dict[str, str]], name: str) -> None:
    if any(item["id"] == name for item in fields):
        return
    fields.append({"id": name, "label": field_label(name), "kind": field_kind(name)})


def _specs(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("mcp"), dict):
        payload = payload["mcp"]
    for key in _WRAPPER_KEYS:
        if isinstance(payload.get(key), dict):
            payload = payload[key]
            break
    if _is_server(payload):
        return [_spec("", payload)]
    return [_spec(str(name), value) for name, value in payload.items() if isinstance(value, dict) and _is_server(value)]


def _is_server(value: dict[str, Any]) -> bool:
    return any(isinstance(value.get(key), str) and value.get(key) for key in _SERVER_KEYS)


def _spec(name: str, value: dict[str, Any]) -> dict[str, Any]:
    needs: list[dict[str, str]] = []
    url = str(value.get("url") or value.get("serverUrl") or value.get("httpUrl") or "").strip()
    if url:
        headers: dict[str, str] = {}
        for key, raw in (value.get("headers") or {}).items():
            text = str(raw)
            prefix = ""
            if text.casefold().startswith("bearer "):
                prefix, text = text[:7], text[7:]
            if is_placeholder(text):
                needs.append({"target": "header", "key": str(key), "prefix": prefix})
            else:
                headers[str(key)] = str(raw)
        return {"name": name, "transport": "http", "url": url, "headers": headers, "needs": needs}
    command = str(value.get("command") or "").strip()
    args = [str(item) for item in value.get("args") or []]
    if " " in command and not args:
        try:
            command, *args = shlex.split(command)
        except ValueError:
            pass
    if command == "npx" and "-y" not in args and "--yes" not in args:
        args = ["-y", *args]
    env: dict[str, str] = {}
    for key, raw in (value.get("env") or {}).items():
        if is_placeholder(str(raw)):
            needs.append({"target": "env", "key": str(key), "prefix": ""})
        else:
            env[str(key)] = str(raw)
    return {"name": name, "transport": "stdio", "command": command, "args": args, "env": env, "needs": needs}


def _json_objects(text: str) -> list[Any]:
    """Balanced {...} blocks inside prose or HTML."""
    found: list[Any] = []
    start = text.find("{")
    while start != -1 and len(found) < 20:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        found.append(json.loads(text[start : index + 1]))
                    except json.JSONDecodeError:
                        pass
                    break
        start = text.find("{", start + 1)
    return found


def _code_blocks(text: str) -> list[tuple[str, str]]:
    blocks = [(lang, body) for lang, body in _FENCE.findall(text)]
    if blocks:
        return blocks
    if "<" in text and ">" in text:
        return [("", html.unescape(_TAG.sub("", body))) for body in _PRE.findall(text)]
    return [("", text)]


def _shell_lines(body: str) -> list[str]:
    joined = re.sub(r"\\\n\s*", " ", body)
    lines = []
    for raw in joined.splitlines():
        line = raw.strip()
        if line.startswith("$ "):
            line = line[2:]
        if line and not line.startswith("#"):
            lines.append(line)
    return lines
