# Capabilities

The core is the airlock, accounts, the model route, and a registry. A new way for the assistant to help is a new module. Mail is the IMAP and SMTP connector. Calendar is the CalDAV connector. The browser is the computer-use connector for websites on that instance. Naming a site, such as `vg.no`, opens that page on this machine and returns a structured text snapshot: URL, numbered Interactive refs (links, buttons, textboxes, and other controls), and Content from the main region. When a click opens another window, the snapshot lists Pages and `switch_page` focuses that popup. A download is listed and saved under that account's browser profile when possible. Desktop apps use the same snapshot shape over AT-SPI; `submit`, `pay`, and `delete_item` wait for a confirm. Screenshots are not taken. With the `ner` extra installed, `robin boot` loads local NER so the airlock view of that snapshot can go to the model. The model may answer from Content or keep clicking and typing until the task is done. Without NER, Robin shows a cleaned local extract and does not send the page to a model. `click` and `type_text` take an Interactive ref or a visible name and return a fresh snapshot. The model chooses `browser_open` when a page is needed. Trigger words are not required. Another account does not see it. Robin can read files owned by the operating-system user it runs as, anywhere on that machine. A folder owned by another user is not opened. Another Robin account's directory stays hidden. When the process is root, `ROBIN_USER` names the person whose files may be read. “Find photos with dogs” compares that user's pictures to those words on this machine. `delete_file` still waits for a confirm and only deletes inside that account's directory. `run_command` waits too, and the confirm text shows the command after redaction. The command then runs as that account's login, in that directory, with a minimal environment. Groceries is the household list. The screen fixture is an example. They are not imported by the core.

The mail account secret is stored with `broker.put(account_id, "mailbox", mailbox_secret(...))`. The JSON holds `imap_host`, `smtp_host`, `user`, and `password`. The model calls mail_list or mail_search when it needs the inbox. An iCloud, Gmail, or Outlook address supplies its mail servers when the host was left blank. The model does not receive the message text. A mailbox that is not connected, or that refuses the sign-in, is said in the conversation. `list_messages` returns those messages. `send_message` is external, so it waits for a confirm. Tests pass `open_imap` and `open_smtp`.

The calendar secret is `broker.put(account_id, "calendar", calendar_secret(...))` with an `https` URL, user, and password. The model calls calendar_list when it needs this account's CalDAV calendar. Website bookings use the browser tools. A calendar that is not connected is said in the conversation. `list_events` returns those events. `add_event` waits for a confirm. Tests pass `fetch` and `put`.

The grocery list lives on this server. `add_private` changes only that account's list. `add_shared` and `add_member` wait for a confirm, because another account will see the result. A private item is not in another account's records. Pass a `HouseholdStore` and the list is encrypted across a restart.

A website sign-in belongs to that account and to the site's host. The first time a page asks for a password, or for an email before the password, Robin returns status `input` with a form (username and password). The Mac and iPhone apps show SecureField and password-manager autofill. Older clients still accept `username name@example.com password …` or the username and password on two lines. That reply is stored in the broker, encrypted with the household key, and is not sent to a model. Robin may try one automatic fill, then hands the live login form to the operator loop with Interactive refs. The model adapts with `fill_saved_username` and `fill_saved_password` (password waits for confirm), clicks, and submits for that site's flow. A wrong-password message from the site drops the stored secret and asks again. A blocked or unfinished automatic fill keeps the secret. A verification code uses an `otp` input form on every sign-in and is not stored. Another account cannot read it. `POST /v1/secrets` still accepts only mailbox and calendar names. Personal details for ordinary forms (name, email, phone, address) live in the same broker under `profile`, edited from the apps via `GET`/`POST /v1/profile`. The model fills them with `browser_fill_profile` (`field` + `target` only); the values never enter tool arguments or the prompt. When a page or an inbox was fetched and the model says the fetch failed, the fetched text is the reply.

## Input requests

When Robin needs details from the person — a Home Assistant token, an MCP bearer secret, a one-time code, a choice — the turn returns `status: input` with an `InputRequest` (`request_id`, title, reason, fields). Field kinds are `text`, `secret`, `url`, `email`, `username`, `otp`, `number`, and `choice`. The app posts `{"input": {"request_id", "values"}}` or `{"input": {"request_id", "cancel": true}}` on `POST /v1/messages`. Secret values go straight into the broker and never enter the model or thread history (history only records field labels). `ask_person` is for non-secret fields only; capabilities that own a secret destination raise their own forms.

## Home Assistant

Connect in chat: `home_connect` with the base URL, then a secure form for the long-lived token, then `home_connect_test`. The connection is saved only after the test passes. Tools cover `home_list` (area/domain/query filters), `home_state`, `home_set` (brightness, climate, covers, volume), `home_scene`, `home_script`, `home_media`, and `home_secure` for locks, alarms, sirens, and garage/gate covers (`external`, waits for confirm). `home_rule_add` / `list` / `cancel` watch entity state changes; `Home.due` polls about once a minute and runs the action text as a conversation turn.

## MCP servers

Attach servers in chat with `mcp_setup_start` → `mcp_setup_ask_secret` (secure form) → `mcp_setup_test` → `mcp_setup_finish` (confirm). Tools become `mcp_<server>_<tool>` (at most 40 per account). Results are untrusted data. Remote HTTP tools are egress. Destructive tools always confirm. A changed tool list hides the server until re-approval. Household admins may add stdio servers from `/etc/robin/mcp.json` (`--mcp-config`); stdio runs as that account's login. `GET /v1/mcp` is read-only (names, status, tool counts). Optional extra: `robin[mcp]`.

## Documents and bills

`files_read` extracts text from PDF (`robin[docs]`) and `.docx`. `docs_find` keyword-searches readable folders with an encrypted `doc_index`. `bills_scan` / `bills_remind` parse invoices from mail (KID/account drop for the model). `packages_scan` finds carrier tracking numbers and URLs.

## Browser stealth and bot walls

Robin opens pages with a **headed** Chromium (or Google Chrome when installed) on a per-account Xvfb display by default (`ROBIN_BROWSER_HEADLESS=0`). Automation flags are stripped. Optional engines: `ROBIN_BROWSER_ENGINE=playwright|patchright|camoufox` (patchright is preferred when installed via the `stealth` extra). Per-account persistent profiles keep cookies between visits.

When a snapshot is a captcha or WAF wall, the turn returns status `handoff` with a `live_url`. The person opens that live view (Playwright screenshots over HTTP — pixels never go to a model), solves the check, then confirms. `resume` re-reads the page and continues.

**IP reputation limit:** private exe.dev VMs use datacenter IPs that Akamai and PerimeterX often block even with a headed browser. The house box usually has a residential IP and fares better. A later option is to proxy VM browser traffic through the house.

Measure with `robin browser-probe` (add `--headless` to compare modes).

An automation is a sentence such as “create a summary of my day and deliver it to me every day at 08:00”, “check the news every hour”, or “remind me in 15 minutes”. Robin saves it for that account and runs it on this instance when the time arrives. A daily time that has already passed starts the next matching day. “Every hour” and “every 15 minutes” repeat. “In 15 minutes” runs once. “List my jobs”, “cancel the summary”, and “change the summary to 07:30” do those things. Another account does not see the list. Sending, paying, or deleting during a run still waits for a confirm.

## Declare one

Subclass `robin.capability.Capability`.

- `id` is the name in the registry.
- `tools` lists what the model may call. Each tool has a JSON schema and an effect: `read`, `mutate`, or `external`.
- `fields` lists record fields as `drop`, `tokenize`, or ordinary. Set `free_text=True` on bodies, titles, and notes. A drop field is removed before any model sees the record. A tokenize field becomes a stable placeholder when no detector already covered it.
- `visible_to` decides which accounts may see the capability. `records` returns only the rows that account may see. A shared resource checks membership here. The core does not special-case it.
- `invoke` performs the action with real values. The session restores placeholders first, and redacts the result afterward.

```python
from robin.capability import Capability, Effect, FieldClass, FieldSpec, Tool

class Notes(Capability):
    id = "notes"
    tools = [
        Tool(
            name="list_notes",
            description="List this account's notes.",
            parameters={"type": "object", "properties": {}},
            effect=Effect.READ,
        )
    ]
    fields = [FieldSpec("body", FieldClass.ORDINARY, free_text=True)]

    def __init__(self, notes):
        self.notes = notes

    def records(self, account_id):
        return list(self.notes.get(account_id, []))

    def invoke(self, account_id, tool_name, arguments):
        return f"{len(self.records(account_id))} notes"
```

Register it on an `Assistant`. The model receives the full tool catalog for this account each turn and chooses what to call. Adding the module does not require an edit to the airlock or to a handwritten prompt.

Credentials go in the `Broker` (`assistant.broker.put`). With a `HouseholdStore`, that write is encrypted and comes back after a restart. `POST /v1/secrets` is how a signed-in app connects `mailbox` or `calendar`. `GET`/`POST /v1/profile` is how the app edits personal form-fill details. The response does not leak secrets to other accounts. Tool code may `reveal` them at execution time. The decide path does not.

External tools return `{"status": "confirm"}` until `invoke(..., confirmed=True)`. Tools may also set `confirm=True` without being external (for example activating a proposed skill).

## Learning and skills

Robin remembers how this person wants things done.

- **Lessons** are one-line preferences or corrections (`lesson_save`, `lesson_list`, `lesson_update`, `lesson_forget`). Optional tags (tool names or website hosts) scope when they apply. Matching lessons are injected into the system prompt on later turns, after the airlock. Untagged preferences always apply. Secrets and national IDs are refused at save time.
- **Skills** are reusable procedures (`skill_list`, `skill_read`, `skill_propose`, `skill_activate`, `skill_retire`). A proposal is stored as a draft and is not followed until `skill_activate` (one confirm). Steps may only name tools that exist in the registry. Skills never load code.
- **Browser sessions.** A successful multi-step browser turn records a trace (controls by role and name, host and path only, no passwords). Robin drafts a site skill such as `finn.no: search used bikes` and offers to remember it. Opening that host later gets a hint to `skill_read` the skill first.
- **Steering.** When the person corrects a browser flow, a reflection pass saves a host-tagged lesson immediately and may propose a revised skill. Lessons beat skill steps when they conflict.
- **Nightly review.** Around 03:00, accounts with new turns get a `learning` thread that merges lessons and proposes skills via `learning_digest`.
- Lessons and skills are per account and encrypted in the household store. A turn that already saw page, mail, or file text must confirm before the model may write a lesson mid-turn (prompt-injection guard). The reflection pass never sees tool result bodies, so its lessons apply without that confirm.

A downloaded plugin would sit inside the trust boundary, so the registry does not load third-party packages. The interface above is the one it will implement.
