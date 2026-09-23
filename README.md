# Robin

Robin is a secure, privacy-oriented, extendable assistant for a household.

The joint assistant runs on the in-house Linux server. Each family member has an account. Someone who wants a private assistant gets their own instance on an [exe.dev](https://exe.dev/docs/api) VM, created from the joint server with no manual setup. Native macOS and iOS apps are clients. They do not embed this runtime, and they do not call a model provider themselves.

Mail is an IMAP and SMTP connector. Calendar is a CalDAV connector. The host, user, and password for each stay in the broker, and the model tools do not take them. The browser on the instance reads accessible text through Playwright. A screenshot is not taken. Groceries are a list on this server: the household list for members, and a private list for its owner. The core does not know those domains.

This repository is the privacy core: detection, the account boundary, the model route, and the capability protocol. It is not the apps, and it does not drive a live browser yet.

## Routes

The local model is the default. A cloud model runs only when the task sets `allow_cloud` and the redaction report is clean. A clean scan never upgrades a task by itself. Secrets, national IDs, and payment data are dropped and are not restored. Unresolved free text stays on the local model.

`converse` is the turn. It shows the model that account's view, runs a tool the registry allows, and stops when the tool is external so the person can confirm. `ChatModel` posts to a local OpenAI-compatible server at `http://127.0.0.1:8080` unless you pass another address. The tests use a stand-in model and do not open a connection.

Pass a `HouseholdStore` when constructing `Assistant` and the house server keeps accounts, threads, vaults, vocabulary, the activity log, and broker secrets across a restart. Those records are encrypted with a key the process holds. Another account cannot read them. The database file does not contain the raw message text or a mailbox password.

`serve` speaks HTTP on `127.0.0.1:8787` unless `--host` says otherwise. `deploy/robin-house.service` listens on `0.0.0.0` so a Mac or iPhone on the house network can sign in. A session is still required. It reads `/etc/robin/advertise.url` and passes that URL to a new private VM. A missing file leaves private instances unconfigured. `POST /v1/export` seals that account's vault with a passphrase and returns the sealed bundle. `POST /v1/import` opens it on another instance for the same account. The passphrase is not stored, and another account's session cannot take the vault. `POST /v1/accounts` sets a password once. `POST /v1/sessions` checks it and returns a bearer token. `POST /v1/messages` runs one turn for that account and a thread. The response is a reply, or a confirmation when the model asked for an external tool. A second post with `"confirm": true` runs that tool. `POST /v1/secrets` stores a mailbox or calendar secret for that session. The response names the connection and does not return the secret. `GET /v1/secrets` lists which of those are connected. `POST /v1/schedule` turns a scheduled check on or off for that account. It stays off until they do. `robin tick` runs the check locally and holds an external action for confirmation. `deploy/robin-house.service` and `deploy/robin-tick.timer` share `/var/lib/robin/house.sqlite` and `/etc/robin/store.key`. The journal line is a count, not the mail. The private VM unit stays `robin boot` with no store, so it can enroll before a house key exists. The Mac and iPhone screen can connect a mailbox, connect a calendar, and turn that check on. Passwords are cleared from the form before the request returns. `GET /v1/threads`, `GET /v1/threads/{id}`, and `GET /v1/activity` require the same session. A token for another account is rejected. The VM enroll route stays a one-time token, not a person's session.

`POST /v1/private` asks for a private exe.dev instance and does nothing until `"confirm": true`. `"delete": true` asks to remove that instance and does nothing until confirm as well. The recorded call is `rm robin-{account} --json`. The exe.dev token stays in the broker. `robin exe-token --store … --key … --token-file …` reads that file once, encrypts it for the household, and deletes the file. The app cannot set it, and the command does not print it. `robin boot` is what the VM service starts, on `127.0.0.1:8000`, which is where exe.dev sends the public hostname. The create call marks that proxy public and then enrolls with `POST /v1/enroll` when a token file is present, deletes that file, and serves. A session is still required. `GET /v1/private` reports the address once the instance is ready.

The private image is `deploy/Dockerfile`. It is based on exeuntu, installs this package, and leaves the Robin unit disabled until the setup script starts it. Build it with `docker build -t nimul/robin:pinned -f deploy/Dockerfile .` and publish that tag. The create call pulls `nimul/robin:pinned` and does not carry registry credentials.

`clients/RobinKit` is what the Mac and iPhone shells share. `Shell` signs in to one instance, shows a reply, and stops on a confirmation until the person confirms. Leaving drops that session before another instance is opened. `RobinApp` is the SwiftUI screen for both. It builds on a Mac. It does not redact, and it does not call a model provider.

Details, including the threat model and private VMs, are in [docs/privacy.md](docs/privacy.md). Adding a capability is described in [docs/capabilities.md](docs/capabilities.md).

## Run the tests

```bash
uv sync --group dev
uv run pytest
```

The tests stand in for IMAP, SMTP, and the model. They do not open a network connection.

## Inspect one string

```bash
uv run robin redact --text 'mail jane@example.com'
uv run robin decide --text 'buy milk' --allow-cloud
```

`redact` can write an encrypted vault with `--vault-out` and `--key-out`. `restore` reads that vault back. The key file is the vault key. Keep it on the machine that runs Robin.
