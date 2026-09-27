# Capabilities

The core is the airlock, accounts, the model route, and a registry. A new way for the assistant to help is a new module. Mail is the IMAP and SMTP connector. Calendar is the CalDAV connector. The browser is the computer-use connector on that instance. Naming a site, such as `vg.no`, opens that page on this machine and returns a structured text snapshot: URL, numbered Interactive refs (links, buttons, textboxes, and other controls), and Content from the main region. When a click opens another window, the snapshot lists Pages and `switch_page` focuses that popup. A download is listed and saved under that account's browser profile when possible. Screenshots are not taken. With the `ner` extra installed, `robin boot` loads local NER so the airlock view of that snapshot can go to the model. The model may answer from Content or keep clicking and typing until the task is done. Without NER, Robin shows a cleaned local extract and does not send the page to a model. `click` and `type_text` take an Interactive ref or a visible name and return a fresh snapshot. A general question does not start the browser. `open_page` is how a task opens an http or https page for that account. Another account does not see it. Robin can read files owned by the operating-system user it runs as, anywhere on that machine. A folder owned by another user is not opened. Another Robin account's directory stays hidden. When the process is root, `ROBIN_USER` names the person whose files may be read. “Find photos with dogs” compares that user's pictures to those words on this machine. `delete_file` still waits for a confirm and only deletes inside that account's directory. `run_command` waits too, and the confirm text shows the command after redaction. The command then runs as that account's login, in that directory, with a minimal environment. Groceries is the household list. The screen fixture is an example. They are not imported by the core.

The mail account secret is stored with `broker.put(account_id, "mailbox", mailbox_secret(...))`. The JSON holds `imap_host`, `smtp_host`, `user`, and `password`. Asking about email fetches the newest messages on this machine and shows that inbox here. An iCloud, Gmail, or Outlook address supplies its mail servers when the host was left blank. The model does not receive the message text. A mailbox that is not connected, or that refuses the sign-in, is said in the conversation. `list_messages` returns those messages. `send_message` is external, so it waits for a confirm. Tests pass `open_imap` and `open_smtp`.

The calendar secret is `broker.put(account_id, "calendar", calendar_secret(...))` with an `https` URL, user, and password. Asking about the calendar fetches the events before the model speaks. A calendar that is not connected is said in the conversation. `list_events` returns those events. `add_event` waits for a confirm. Tests pass `fetch` and `put`.

The grocery list lives on this server. `add_private` changes only that account's list. `add_shared` and `add_member` wait for a confirm, because another account will see the result. A private item is not in another account's records. Pass a `HouseholdStore` and the list is encrypted across a restart.

A website sign-in belongs to that account and to the site's host. The first time a page asks for a password, or for an email before the password, Robin asks for the username and password in the conversation. The person can reply with `username name@example.com password …` or with the username and password on two lines. That reply is stored in the broker, encrypted with the household key, and is not sent to a model. The next visit to that site signs in with the stored secret, including a form that asks for the email and the password on separate steps. A failed sign-in drops the stored secret and asks again. A browser glitch asks again and keeps the stored secret. A verification code is asked for on every sign-in and is not stored. Another account cannot read it. `POST /v1/secrets` still accepts only mailbox and calendar names. When a page or an inbox was fetched and the model says the fetch failed, the fetched text is the reply.

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

Register it on an `Assistant`. The model loop asks the registry which tools this account may use. Adding the module does not require an edit to the airlock or to a handwritten prompt.

Credentials go in the `Broker` (`assistant.broker.put`). With a `HouseholdStore`, that write is encrypted and comes back after a restart. `POST /v1/secrets` is how a signed-in app connects `mailbox` or `calendar`. The response does not include the secret. Tool code may `reveal` them at execution time. The decide path does not.

External tools return `{"status": "confirm"}` until `invoke(..., confirmed=True)`.

A downloaded plugin would sit inside the trust boundary, so the registry does not load third-party packages. The interface above is the one it will implement.
