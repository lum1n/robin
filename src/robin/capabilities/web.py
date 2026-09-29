"""Web search and lightweight page fetch. Arguments leave the machine (egress)."""

from __future__ import annotations

import json
import os
import re
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Result, Tool


class SecretStore(Protocol):
    def reveal(self, account_id: str, name: str) -> str: ...


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = " ".join(data.split())
        if text:
            self._chunks.append(text)

    def text(self) -> str:
        return "\n".join(self._chunks)


class Web(Capability):
    id = "web"
    tools = [
        Tool(
            name="web_search",
            description="Search the public web for a query. The query is sent to the configured search provider.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            effect=Effect.READ,
            egress=True,
        ),
        Tool(
            name="web_fetch",
            description=(
                "Fetch readable text from a public http(s) URL without a browser session. "
                "For booking, forms, or clicking on a site, use browser_open instead."
            ),
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            effect=Effect.READ,
            egress=True,
        ),
    ]
    fields = [
        FieldSpec("title", FieldClass.ORDINARY, free_text=True),
        FieldSpec("url", FieldClass.ORDINARY),
        FieldSpec("snippet", FieldClass.ORDINARY, free_text=True),
    ]

    def __init__(
        self,
        *,
        broker: SecretStore | None = None,
        fetch: Any = None,
        search_url: str | None = None,
    ) -> None:
        self.broker = broker
        self._fetch = fetch or urllib_get
        self.search_url = search_url or os.environ.get("ROBIN_SEARCH_URL", "http://127.0.0.1:8888")

    def status(self, account_id: str) -> str:
        return f"web: search via {self.search_url}"

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        if tool_name == "web_search":
            query = str(arguments.get("query", "")).strip()
            if not query:
                return "query is required"
            rows = self._search(account_id, query)
            if not rows:
                return "No results."
            return Result(text="Search results:", records=rows)
        if tool_name == "web_fetch":
            url = str(arguments.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                if re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/.*)?", url, re.IGNORECASE):
                    url = "https://" + url
                else:
                    return "url must be http or https"
            body = self._fetch(url)
            text = _readable(body)[:8000]
            return text or "empty page"
        raise NotImplementedError(tool_name)

    def _search(self, account_id: str, query: str) -> list[dict[str, str]]:
        key = ""
        if self.broker is not None:
            try:
                key = self.broker.reveal(account_id, "web_search")
            except KeyError:
                key = ""
        if key.startswith("brave:"):
            return self._brave(query, key.removeprefix("brave:"))
        if key.startswith("kagi:"):
            return self._kagi(query, key.removeprefix("kagi:"))
        url = f"{self.search_url.rstrip('/')}/search?{urlencode({'q': query, 'format': 'json'})}"
        raw = self._fetch(url)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return [{"title": "result", "url": "", "snippet": raw[:400]}]
        results = payload.get("results") or payload.get("organic") or []
        rows: list[dict[str, str]] = []
        for item in results[:8]:
            rows.append(
                {
                    "title": str(item.get("title") or ""),
                    "url": str(item.get("url") or item.get("link") or ""),
                    "snippet": str(item.get("content") or item.get("snippet") or "")[:400],
                }
            )
        return rows

    def _brave(self, query: str, token: str) -> list[dict[str, str]]:
        url = f"https://api.search.brave.com/res/v1/web/search?q={quote_plus(query)}"
        raw = self._fetch(url, headers={"X-Subscription-Token": token, "Accept": "application/json"})
        payload = json.loads(raw)
        rows = []
        for item in (payload.get("web") or {}).get("results") or []:
            rows.append(
                {
                    "title": str(item.get("title") or ""),
                    "url": str(item.get("url") or ""),
                    "snippet": str(item.get("description") or "")[:400],
                }
            )
        return rows[:8]

    def _kagi(self, query: str, token: str) -> list[dict[str, str]]:
        url = f"https://kagi.com/api/v0/search?q={quote_plus(query)}"
        raw = self._fetch(url, headers={"Authorization": f"Bot {token}"})
        payload = json.loads(raw)
        rows = []
        for item in payload.get("data") or []:
            if item.get("t") != 0:
                continue
            rows.append(
                {
                    "title": str(item.get("title") or ""),
                    "url": str(item.get("url") or ""),
                    "snippet": str(item.get("snippet") or "")[:400],
                }
            )
        return rows[:8]


def urllib_get(url: str, headers: dict[str, str] | None = None) -> str:
    request = Request(url, headers=headers or {"User-Agent": "Robin/1.0"}, method="GET")
    with urlopen(request, timeout=20) as response:  # noqa: S310
        return response.read().decode(errors="replace")


def _readable(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return parser.text()
