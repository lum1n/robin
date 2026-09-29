# Robin

Robin is a secure, privacy-oriented, extendable assistant for a household.

The joint assistant runs on the in-house Linux server. Each family member has an account. Someone who wants a private assistant gets their own instance on an [exe.dev](https://exe.dev/docs/api) VM, created from the joint server with no manual setup. Native macOS and iOS apps are clients. They do not embed this runtime, and they do not call a model provider themselves.

Mail is an IMAP and SMTP connector. Calendar is a CalDAV connector. The host, user, and password for each stay in the broker, and the model tools do not take them. A website sign-in is stored the same way for that site, and the password is not sent to a model. The browser on the instance returns a structured accessible-text snapshot (URL, interactive refs, main content) through Playwright. A screenshot is not taken. Robin reads files owned by the operating-system user on that machine, and leaves another user's folders closed. A confirmed command runs as that account's login, with a minimal environment. Groceries are a list on this server: the household list for members, and a private list for its owner. The core does not know those domains.

This repository is the privacy core: detection, the account boundary, the model route, and the capability protocol. Playwright is a required dependency; Chromium is installed with `playwright install chromium`. The Mac and iPhone apps are separate clients.

## Routes

The model is remote. `ChatModel` posts to an OpenAI-compatible server. `robin boot --model-url` sets that address and `--model-name` sets the model id. The default address is `http://127.0.0.1:8080`. `ROBIN_MODEL_KEY` is sent as a bearer token and is not put in the prompt. Each model prompt and answer (the airlock view, including tool calls) is printed on the server's stderr; set `ROBIN_LOG_MODEL=0` to turn that off. Each reply also carries a `timing` line (`model` is the chat completion, `local` is everything else on the server) and the same line is printed on the server's stderr. The Mac app shows that line under the answer. Setting a variable in the app or in another terminal does not affect the `robin boot` process. The airlock runs first. Secrets, national IDs, and payment data are dropped and are not restored. Detected names, emails, phones, and addresses become placeholders. Mail, page text, file contents, tool results, and earlier turns stay on the machine unless the local NER pass clears them; otherwise the model receives `[UNRESOLVED]`. `allow_cloud` does not turn that filter off.

`converse` is the turn. It shows the model that redacted view and the full tool catalog for the account; the model chooses tools, and Robin stops when a tool is external so the person can confirm. The tests use a stand-in model and do not open a connection.

Pass a `HouseholdStore` when constructing `Assistant` and the house server keeps accounts, threads, vaults, vocabulary, the activity log, and broker secrets across a restart. Those records are encrypted with a key the process holds. Another account cannot read them. The database file does not contain the raw message text or a mailbox password.

`serve` speaks HTTP on `127.0.0.1:8787` unless `--host` says otherwise. `deploy/robin-house.service` listens on `0.0.0.0` so a Mac or iPhone on the house network can sign in. A session is still required. It reads `/etc/robin/advertise.url` and passes that URL to a new private VM. A missing file leaves private instances unconfigured. `POST /v1/export` seals that account's vault with a passphrase and returns the sealed bundle. `POST /v1/import` opens it on another instance for the same account. The passphrase is not stored, and another account's session cannot take the vault. `POST /v1/accounts` sets a password once. `POST /v1/sessions` checks it and returns a bearer token. `POST /v1/messages` runs one turn for that account and a thread. The response is a reply, or a confirmation when the model asked for an external tool. A second post with `"confirm": true` runs that tool. `POST /v1/secrets` stores a mailbox or calendar secret for that session. The response names the connection and does not return the secret. `GET /v1/secrets` lists which of those are connected. `POST /v1/schedule` turns a scheduled check on or off for that account. It stays off until they do. `robin tick` runs the check locally and holds an external action for confirmation. `deploy/robin-house.service` and `deploy/robin-tick.timer` share `/var/lib/robin/house.sqlite` and `/etc/robin/store.key`. The journal line is a count, not the mail. The private VM unit stays `robin boot` with no store, so it can enroll before a house key exists. The Mac and iPhone screen can connect a mailbox, connect a calendar, and turn that check on. Passwords are cleared from the form before the request returns. `GET /v1/threads`, `GET /v1/threads/{id}`, and `GET /v1/activity` require the same session. A token for another account is rejected. The VM enroll route stays a one-time token, not a person's session.

`POST /v1/private` asks for a private exe.dev instance and does nothing until `"confirm": true`. `"delete": true` asks to remove that instance and does nothing until confirm as well. The recorded call is `rm robin-{account} --json`. The exe.dev token stays in the broker. `robin exe-token --store … --key … --token-file …` reads that file once, encrypts it for the household, and deletes the file. The app cannot set it, and the command does not print it. `robin boot` is what the VM service starts, on `127.0.0.1:8000`, which is where exe.dev sends the public hostname. The create call marks that proxy public and then enrolls with `POST /v1/enroll` when a token file is present, deletes that file, and serves. A session is still required. `GET /v1/private` reports the address once the instance is ready.

The private image is `deploy/Dockerfile`. It is based on exeuntu, installs this package and Chromium, and leaves the Robin unit disabled until the setup script starts it. Build it with `docker build -t nimul/robin:pinned -f deploy/Dockerfile .` and publish that tag. The create call pulls `nimul/robin:pinned` and does not carry registry credentials. The house server does not run this image.

`clients/RobinKit` is what the Mac and iPhone shells share. `Shell` signs in to one instance, shows a reply, and stops on a confirmation until the person confirms. Leaving drops that session before another instance is opened. `RobinApp` is the SwiftUI screen for both. It builds on a Mac. It does not redact, and it does not call a model provider.

Details, including the threat model and private VMs, are in [docs/privacy.md](docs/privacy.md). Adding a capability is described in [docs/capabilities.md](docs/capabilities.md).

## Run the tests

`robin boot --store ~/robin-data/house.sqlite` keeps browser profiles, files, and the shell home under `~/robin-data/files`. The house unit uses `/var/lib/robin/files`. `ROBIN_FILES` overrides either.

The tests stand in for IMAP, SMTP, and the model. They do not open a network connection.

## Inspect one string

```bash
uv run robin redact --text 'mail jane@example.com'
uv run robin decide --text 'buy milk' --allow-cloud
```

`redact` can write an encrypted vault with `--vault-out` and `--key-out`. `restore` reads that vault back. The key file is the vault key. Keep it on the machine that runs Robin.
