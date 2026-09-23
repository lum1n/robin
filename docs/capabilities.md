# Capabilities

The core is the airlock, accounts, the model route, and a registry. A new way for the assistant to help is a new module. Mail is the IMAP and SMTP connector. Calendar, groceries, and the screen fixture are examples. They are not imported by the core.

The mail account secret is stored with `broker.put(account_id, "mailbox", mailbox_secret(...))`. The JSON holds `imap_host`, `smtp_host`, `user`, and `password`. `list_messages` reads the newest messages. `send_message` is external, so it waits for a confirm. A missing or unreadable secret yields no messages and does not connect. Tests pass `open_imap` and `open_smtp`.

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

Credentials go in the `Broker` (`assistant.broker.put`). Tool code may `reveal` them at execution time. The decide path does not.

External tools return `{"status": "confirm"}` until `invoke(..., confirmed=True)`.

A downloaded plugin would sit inside the trust boundary, so the registry does not load third-party packages. The interface above is the one it will implement.
