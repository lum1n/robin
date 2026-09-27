# Text control

Robin steers a computer with text. A screenshot is not taken. That is the difference from Grok and Muse, which look at the viewport and click pixels. Mail and calendar already go further than a picture can: they use a connector, and the model never sees the password. The browser is the fallback for a site that has no connector. The text path can match a screenshot agent on ordinary websites. It does not yet.

A screenshot agent stays in a loop. It looks at the viewport, acts, looks again, and continues until the task is done or a person approves a sensitive step. The picture shows every visible control, including an unlabeled icon, the dialog on top, the layout, and whether the last click worked. Robin can open a page and click or type by a numbered ref. The turn stops after the first look, and that look throws away most of the page.

## Where the turn stops

A task that names a site never becomes computer use. `Browser.prepare` opens the page, then `converse` used to call the model with no tools. The answer prompt only asked for a summary.

That stop is gone. A page snapshot from `prepare` seeds the operator loop with tools still available. The system prompt tells the model to keep using tools until the task is done or it needs the person. The step cap is 24. Each step sees the latest snapshot and a short action log, not every previous page. Only the first tool call of a step runs. Mail, calendar, photos, and file fetches that are not a page still use the answer-only path.

`submit` and `type_password` wait for a confirm. After the person confirms, `resume` runs that action and continues the same loop with the new snapshot, so a checkout can take the next step without the person repeating the task.

## Where the snapshot stops

The collector used to flatten the page and cut every string at 120 characters, including the body. That content cut is gone. Interactive refs now include checkbox, radio, combobox, tab, menuitem, option, and switch, with region and state when present. An open dialog is listed first. Duplicate names stay as separate refs. An unlabeled control gets a neighbor label.

What a screenshot still shows, and this snapshot still drops:

- A full accessibility-tree walk. Collection is still query selectors, not the browser a11y tree.
- Iframes and shadow DOM. Login fields are searched in frames. The snapshot is not.
- Which DOM node a ref points at. A ref is still resolved to role plus name, then clicked with `get_by_role` or `get_by_text`. A second "Delete" can be listed twice and still hit the first node.
- Proof that the last action worked beyond the next snapshot (no one-line diff yet).

If local NER is off, `release` replaces that entire snapshot with `[UNRESOLVED]`. The model then has nothing to click. Tool arguments are already restored before `invoke`, so a placeholder in a name can still be resolved at click time once the ref itself survives.

## What has to change

The loop has to act after it looks, and the snapshot has to contain the dialog and the controls a picture would show. Until those two are true, extra actions will not be used.

1. Make the turn an operator. Keep tools available until the model says the task is done or asks the person something. Use `prepare` only to attach the first snapshot, and leave `click` and `type_text` on that same turn. Raise the step cap into the range a real errand takes, on the order of 20–40. Send the latest snapshot plus a short action log. Screenshot agents send the latest frame, not every previous one. After a confirm, resume the same loop with the new snapshot.

2. Replace the snapshot with a viewport accessibility tree. Walk the visible tree. An open dialog comes first, then iframes and open shadow roots. For each control record role, accessible name, states, value, and a stable ref. Keep duplicate names as separate refs. Give an unlabeled control a ref and a short neighbor label, such as `button, unnamed, near "Cart"`. Put a region on each ref (dialog, header, main) so "the button on the right of the dialog" has a text equivalent. Render content as lines from the viewport, with headings intact, and one line that says more is below when the page continues. That is one screenshot, as text, and it is more precise on ordinary HTML.

3. Click the ref, not the name. Stamp the ref on the node, or keep a locator per ref, and act on that node. After each action, wait until navigation or the DOM settles, then return a fresh snapshot and a one-line diff: URL, dialog opened or closed, and text that appeared. Add the actions a pointer gets without being asked: scroll, select an option, toggle a checkbox, press a key, hover, go back, and type into the focused ref. A popup and a download are their own page.

4. Keep the browser for the account. One headless tab with no stored profile forgets login cookies when the process restarts. A site that opens a second window has nowhere to go. Persist the Playwright context in that account's directory. Keep more than one page when the site opens one.

5. Redact spans inside the snapshot. Refs, roles, URL, and control states always reach the model. Names and body text still go through the airlock, as placeholders for a detected person, email, phone, or address. Replacing the whole snapshot with `[UNRESOLVED]` makes the text path blind.

6. Size the context for one viewport. A structured viewport tree plus the action log needs a budget of its own, separate from history. Drop older snapshots once the action log has them. The 6,000-character cap exists so a turn fits a 4096-token local model together with the tool list and the reply. Raising it means checking that model, or splitting the snapshot so the local route still fits.

7. Treat the desktop as the same protocol, later. Mail and calendar stay connectors. The browser stays the fallback for websites. Grok and Muse also drive other applications. On the Linux VM the text equivalent is the accessibility bus (AT-SPI): windows, roles, names, and actions, with the same refs and the same confirm rule for anything that sends, pays, or deletes.

## Todo

Work top to bottom. The first two groups are the gap. Later groups depend on them.

### The turn is an operator

- [x] A task that names a site and asks for an action still has `click` and `type_text` after the page is open
- [x] `prepare` attaches the first snapshot and does not switch the turn onto the answer-only prompt
- [x] Remove the instruction that forbids another tool after `open_page` or `read_screen`
- [x] Remove the `answer_next` stop in `converse` for those two tools
- [x] Raise the step cap into the range of a real errand (about 20–40), and stop early when the model is done or needs the person
- [x] Each step sees the latest snapshot and a short action log, not every previous snapshot
- [x] Only the first tool call of a step runs for now; several calls in one reply stay deferred until the operator loop is proven on longer tasks
- [x] After the person confirms `submit` or `type_password`, resume the same loop with the snapshot from that action

### The snapshot is one viewport

- [x] Stop cutting page content at 120 characters in `_SNAPSHOT_JS`
- [ ] Build the snapshot from the visible accessibility tree, not from query selectors alone
- [x] If a dialog is open, snapshot that dialog first
- [ ] Include iframes and open shadow roots
- [x] Record role, accessible name, states, value, and a numbered ref for each control
- [x] Keep two controls with the same name as two refs
- [x] Give an unlabeled control a ref and a neighbor label
- [x] Mark each ref with a region: dialog, header, nav, main, or page
- [ ] Render viewport content as lines, keep headings, and add one line when more content is below
- [x] Include checkbox, radio, combobox, tab, menuitem, option, and switch refs (still missing full tree walk)

### Actions hit the ref

- [ ] Stamp the ref on the node, or keep a locator, and click that node
- [ ] After each action, wait until navigation or the DOM settles
- [ ] Return a fresh snapshot plus a one-line diff: URL, dialog opened or closed, text that appeared
- [ ] Add scroll, select, checkbox and radio, key press, hover, and back
- [ ] Type into the focused ref, including a field that is not a labeled textbox
- [ ] When a site opens a popup or starts a download, keep that as its own page

### The browser stays with the account

- [ ] Persist the Playwright context in that account's directory so cookies survive a restart
- [ ] Keep more than one page when a flow opens a second window

### The airlock redacts spans

- [ ] Refs, roles, URL, and control states always reach the model
- [ ] Names and body text still become placeholders for a person, email, phone, or address
- [ ] A snapshot is never replaced wholesale with `[UNRESOLVED]`
- [ ] A placeholder in a control name is restored before the click, which `invoke` already does for tool arguments

### The viewport fits the context

- [ ] Budget the current snapshot separately from conversation history
- [ ] Drop older snapshots once the action log records them
- [ ] Check the 6,000-character cap against the 4096-token local model before raising it

### The desktop, later

- [ ] Add an AT-SPI capability with the same refs, the same snapshot shape, and the same confirm rule for send, pay, and delete
- [ ] Leave mail and calendar on their connectors
