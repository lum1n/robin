"""Register the household connectors. The airlock does not import this module."""

from __future__ import annotations

from robin.capabilities.browser import Browser, Desk, open_chromium
from robin.capabilities.calendar import CalDAV, Calendar
from robin.capabilities.groceries import Groceries
from robin.capabilities.mail import ImapMailbox, Mail
from robin.session import Assistant


def install(assistant: Assistant) -> None:
    assistant.add(Mail(ImapMailbox(assistant.broker)))
    assistant.add(Calendar(CalDAV(assistant.broker)))
    assistant.add(Groceries(store=assistant.store))
    assistant.add(Browser(desk=Desk(open_chromium)))
