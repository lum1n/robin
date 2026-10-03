"""Register the household connectors. The airlock does not import this module."""

from __future__ import annotations

import os
from pathlib import Path

from robin.capabilities.ask import Ask
from robin.capabilities.bills import Bills
from robin.capabilities.browser import Browser, Desk, open_chromium
from robin.capabilities.calendar import CalDAV, Calendar
from robin.capabilities.contacts import CardDAV, Contacts
from robin.capabilities.desktop import Desktop
from robin.capabilities.files import Files, Workspace
from robin.capabilities.groceries import Lists
from robin.capabilities.history import History
from robin.capabilities.home import Home
from robin.capabilities.jobs import Jobs
from robin.capabilities.mail import ImapMailbox, Mail
from robin.capabilities.mcp import Mcp, load_household_mcp
from robin.capabilities.memory import Memory
from robin.capabilities.notify import Notify
from robin.capabilities.photos import Photos
from robin.capabilities.shell import ShellBox, Terminal
from robin.capabilities.skills import Skills
from robin.capabilities.transit import Transit
from robin.capabilities.weather import Weather
from robin.capabilities.web import Web
from robin.learning import Learning
from robin.session import Assistant

_HOUSE_FILES = Path("/var/lib/robin/files")


def files_root(assistant: Assistant) -> Path:
    """Directory for browser profiles, files, and the shell home.

    ROBIN_FILES wins. Otherwise files sit beside the household store, so a
    user boot with --store ~/robin-data/house.sqlite uses ~/robin-data/files
    and the house unit keeps /var/lib/robin/files. Without a store, prefer
    the house path when writable, else a per-user share.
    """
    override = os.environ.get("ROBIN_FILES")
    if override:
        return Path(override)
    if assistant.store is not None:
        return assistant.store.path.parent / "files"
    if _can_use(_HOUSE_FILES):
        return _HOUSE_FILES
    return Path.home() / ".local/share/robin/files"


def _can_use(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(path, os.W_OK)


def install(assistant: Assistant, *, mcp_config: str = "") -> None:
    root = files_root(assistant)
    root.mkdir(parents=True, exist_ok=True)
    workspace = Workspace(root, volume=Path("/"))
    mail = Mail(ImapMailbox(assistant.broker))
    assistant.add(mail)
    browser = Browser(desk=Desk(open_chromium, profiles=root), broker=assistant.broker)
    assistant.add(Calendar(CalDAV(assistant.broker)))
    assistant.add(Contacts(CardDAV(assistant.broker)))
    assistant.add(Lists(store=assistant.store))
    assistant.add(Jobs(store=assistant.store))
    assistant.add(Ask())
    memory = Memory(store=assistant.store)
    assistant.add(memory)
    skills = Skills(store=assistant.store)
    assistant.add(skills)
    assistant.add(Learning(store=assistant.store, memory=memory, skills=skills))
    assistant.add(History(store=assistant.store))
    assistant.add(Notify(store=assistant.store))
    assistant.add(browser)
    assistant.add(Desktop())
    assistant.add(Files(workspace, store=assistant.store))
    assistant.add(Photos(workspace, store=assistant.store))
    assistant.add(Terminal(ShellBox(root)))
    web = Web(broker=assistant.broker)
    browser.find_site = web.find_site
    assistant.add(web)
    assistant.add(Weather())
    assistant.add(
        Transit(
            locate=lambda account_id: (
                context.location
                if (context := assistant.client_context(account_id)) is not None
                else None
            ),
            profile=assistant.get_profile,
        )
    )
    assistant.add(Home(broker=assistant.broker, store=assistant.store))
    assistant.add(Bills(mail=mail, broker=assistant.broker, store=assistant.store))
    household, admins = load_household_mcp(mcp_config) if mcp_config else ([], set())
    mcp = Mcp(
        broker=assistant.broker,
        store=assistant.store,
        registry=assistant.registry,
        household=household,
        admins=admins,
    )
    assistant.add(mcp)
    skills.known_tools = set(assistant.registry._names)
