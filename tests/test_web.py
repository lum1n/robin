from urllib.error import URLError

from robin.capabilities.web import Web
from robin.loop import SYSTEM, _access_spam


def test_web_search_failure_points_at_browser_open() -> None:
    def boom(url: str, headers: dict | None = None) -> str:
        raise URLError(ConnectionRefusedError(111, "Connection refused"))

    web = Web(fetch=boom, search_url="http://127.0.0.1:9")
    result = web.invoke("ada", "web_search", {"query": "vg.no news"})
    assert "unreachable" in result.lower() or "browser_open" in result
    assert "browser_open" in result
    assert "lack" not in result.lower() or "Do not say you lack" in result


def test_web_fetch_failure_points_at_browser_open() -> None:
    def boom(url: str, headers: dict | None = None) -> str:
        raise TimeoutError()

    web = Web(fetch=boom)
    result = web.invoke("ada", "web_fetch", {"url": "https://www.vg.no/"})
    assert "browser_open" in result


def test_web_search_tool_prefers_browser_for_named_sites() -> None:
    tools = {tool.name: tool.description for tool in Web().tools}
    assert "browser_open" in tools["web_search"]
    assert "browser_open" in tools["web_fetch"]


def test_system_forbids_claiming_no_web_access() -> None:
    assert "browser_open" in SYSTEM
    assert "lack access" in SYSTEM.lower()
    assert "web_search fails" in SYSTEM.lower() or "web_search" in SYSTEM


def test_access_spam_collapses_prior_refusals() -> None:
    from robin.loop import _access_spam, _bot_block_spam

    assert _access_spam(
        "I'm currently unable to access the web to check the latest news on vg.no. "
        "You might want to visit the site directly."
    )
    assert not _access_spam("I opened https://www.vg.no with browser_open and read the headlines.")
    blocked = (
        "Access to the LOT website has been blocked due to security policies, so "
        "I'm unable to retrieve flight information directly from there."
    )
    assert not _bot_block_spam(blocked)
    captcha = (
        "A captcha or security check is blocking the page. "
        "Open the live view, solve it, then tap Done so Robin can continue."
    )
    assert _bot_block_spam(captcha)
    assert not _access_spam(captcha)


def test_find_site_returns_homepage_of_top_result() -> None:
    def fake(url: str, headers: dict | None = None) -> str:
        return '{"results": [{"title": "Shop", "url": "https://www.shop.example/no/home?x=1"}]}'

    assert Web(fetch=fake).find_site("ada", "Some Shop") == "https://www.shop.example/"
