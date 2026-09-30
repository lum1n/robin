"""Parse bills and package tracking from this account's mailbox."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from robin.capability import Capability, Effect, FieldClass, FieldSpec, Result, Tool
from robin.store import HouseholdStore

_AMOUNT = re.compile(
    r"(?i)(?:kr|nok|usd|eur|£|\$)\s*([0-9][0-9\s.,]{1,12})|([0-9][0-9\s.,]{1,12})\s*(?:kr|nok|usd|eur)"
)
_DUE = re.compile(
    r"(?i)(?:forfall|due(?:\s+date)?|betalingsfrist)[:\s]+(\d{1,4}[-./]\d{1,2}[-./]\d{1,4})"
)
_KID = re.compile(r"(?i)\bKID[:\s#-]*([0-9]{6,25})\b")
_ACCOUNT = re.compile(r"(?i)\b(?:kontonr|account)[:\s#-]*([0-9.\s]{8,20})\b")
_TRACK = (
    (re.compile(r"\b(?:POSTEN|BRING)[^\n]{0,40}?([A-Z0-9]{10,20})\b", re.I), "bring", "https://sporing.bring.no/sporing/{}"),
    (re.compile(r"\b(?:PostNord)[^\n]{0,40}?([A-Z0-9]{10,20})\b", re.I), "postnord", "https://www.postnord.no/tracking#{}"),
    (re.compile(r"\b(1Z[A-Z0-9]{16})\b"), "ups", "https://www.ups.com/track?tracknum={}"),
    (re.compile(r"\b(\d{12,22})\b.*?\bDHL\b|\bDHL\b.*?\b(\d{12,22})\b", re.I), "dhl", "https://www.dhl.com/global-en/home/tracking.html?tracking-id={}"),
    (re.compile(r"\b(\d{12,15})\b.*?\bFedEx\b|\bFedEx\b.*?\b(\d{12,15})\b", re.I), "fedex", "https://www.fedex.com/fedextrack/?trknbr={}"),
)


class Mailbox(Protocol):
    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> Any: ...


class SecretStore(Protocol):
    def reveal(self, account_id: str, name: str) -> str: ...


class Bills(Capability):
    id = "bills"
    tools = [
        Tool(
            name="bills_scan",
            description="Scan recent mail for invoices: sender, amount, due date. KID/account stay local.",
            parameters={
                "type": "object",
                "properties": {"days": {"type": "number"}},
            },
            effect=Effect.READ,
        ),
        Tool(
            name="bills_remind",
            description="Create a reminder job before a scanned bill's due date.",
            parameters={
                "type": "object",
                "properties": {"bill_id": {"type": "string"}},
                "required": ["bill_id"],
            },
            effect=Effect.MUTATE,
        ),
        Tool(
            name="packages_scan",
            description="Scan recent mail for package tracking numbers and carrier tracking URLs.",
            parameters={
                "type": "object",
                "properties": {"days": {"type": "number"}},
            },
            effect=Effect.READ,
        ),
    ]
    fields = [
        FieldSpec("id", FieldClass.ORDINARY),
        FieldSpec("sender", FieldClass.ORDINARY, free_text=True),
        FieldSpec("amount", FieldClass.ORDINARY),
        FieldSpec("currency", FieldClass.ORDINARY),
        FieldSpec("due", FieldClass.ORDINARY),
        FieldSpec("kid", FieldClass.DROP),
        FieldSpec("account", FieldClass.DROP),
        FieldSpec("carrier", FieldClass.ORDINARY),
        FieldSpec("tracking", FieldClass.ORDINARY),
        FieldSpec("url", FieldClass.ORDINARY),
    ]

    def __init__(
        self,
        *,
        mail: Any = None,
        broker: SecretStore | None = None,
        store: HouseholdStore | None = None,
        bills: dict[str, list[dict[str, str]]] | None = None,
    ) -> None:
        self.mail = mail
        self.broker = broker
        self.store = store
        self._bills = bills or {}

    def status(self, account_id: str) -> str:
        if self.broker is None:
            return ""
        try:
            self.broker.reveal(account_id, "mailbox")
        except KeyError:
            return ""
        return f"bills: {len(self._bills.get(account_id, []))} scanned"

    def available_tools(self, account_id: str) -> list[Tool]:
        if self.broker is None:
            return []
        try:
            self.broker.reveal(account_id, "mailbox")
        except KeyError:
            return []
        return list(self.tools)

    def invoke(self, account_id: str, tool_name: str, arguments: dict[str, Any]) -> str | Result:
        if tool_name == "bills_scan":
            return self._scan_bills(account_id, int(arguments.get("days") or 30))
        if tool_name == "bills_remind":
            return self._remind(account_id, str(arguments.get("bill_id") or ""))
        if tool_name == "packages_scan":
            return self._scan_packages(account_id, int(arguments.get("days") or 14))
        raise NotImplementedError(tool_name)

    def _messages(self, account_id: str, days: int) -> list[dict[str, str]]:
        if self.mail is None:
            return []
        try:
            outcome = self.mail.invoke(account_id, "mail_list", {})
        except Exception:
            return []
        records = []
        if hasattr(outcome, "records") and outcome.records is not None:
            records = list(outcome.records)
        # Prefer search when available.
        try:
            since = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).strftime("%d-%b-%Y")
            found = self.mail.invoke(account_id, "mail_search", {"query": f"SINCE {since}"})
            if hasattr(found, "records") and found.records:
                records = list(found.records)
        except Exception:
            pass
        return [{str(k): str(v) for k, v in row.items()} for row in records]

    def _scan_bills(self, account_id: str, days: int) -> str | Result:
        rows = []
        for message in self._messages(account_id, days):
            body = " ".join(str(message.get(key) or "") for key in ("subject", "from", "snippet", "body", "text"))
            amount_match = _AMOUNT.search(body)
            due_match = _DUE.search(body)
            if amount_match is None and due_match is None and "faktura" not in body.casefold() and "invoice" not in body.casefold():
                continue
            amount = ""
            currency = "NOK"
            if amount_match:
                amount = (amount_match.group(1) or amount_match.group(2) or "").replace(" ", "")
                lead = amount_match.group(0).casefold()
                if "usd" in lead or "$" in lead:
                    currency = "USD"
                elif "eur" in lead or "€" in lead:
                    currency = "EUR"
                elif "£" in lead:
                    currency = "GBP"
            bill_id = secrets.token_hex(4)
            row = {
                "id": bill_id,
                "sender": str(message.get("from") or message.get("sender") or "")[:200],
                "amount": amount,
                "currency": currency,
                "due": due_match.group(1) if due_match else "",
                "kid": (_KID.search(body).group(1) if _KID.search(body) else ""),
                "account": (_ACCOUNT.search(body).group(1) if _ACCOUNT.search(body) else ""),
            }
            rows.append(row)
        self._bills[account_id] = rows
        if not rows:
            return "No invoices found."
        public = [{key: value for key, value in row.items() if key not in {"kid", "account"}} for row in rows]
        return Result(text="Bills:", records=public)

    def _remind(self, account_id: str, bill_id: str) -> str:
        row = next((item for item in self._bills.get(account_id, []) if item.get("id") == bill_id), None)
        if row is None:
            return "bill not found — bills_scan first"
        due = row.get("due") or "soon"
        return (
            f"Remind about bill {bill_id} from {row.get('sender') or 'sender'} "
            f"amount {row.get('amount') or '?'} {row.get('currency') or ''} due {due}. "
            "Use jobs_add or calendar_add to schedule the reminder."
        )

    def _scan_packages(self, account_id: str, days: int) -> str | Result:
        found: list[dict[str, str]] = []
        seen: set[str] = set()
        for message in self._messages(account_id, days):
            body = " ".join(str(message.get(key) or "") for key in ("subject", "from", "snippet", "body", "text"))
            for pattern, carrier, template in _TRACK:
                match = pattern.search(body)
                if not match:
                    continue
                code = next((group for group in match.groups() if group), "")
                if not code or code in seen:
                    continue
                seen.add(code)
                found.append(
                    {
                        "carrier": carrier,
                        "tracking": code,
                        "url": template.format(code),
                        "sender": str(message.get("from") or "")[:200],
                    }
                )
        if not found:
            return "No tracking numbers found."
        return Result(text="Packages:", records=found)
