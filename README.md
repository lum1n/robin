# Robin

Robin is a secure, privacy-oriented, extendable assistant for a household.

The joint assistant runs on the in-house Linux server. Each family member has an account. Someone who wants a private assistant gets their own instance on an [exe.dev](https://exe.dev/docs/api) VM, created from the joint server with no manual setup. Native macOS and iOS apps are clients. They do not embed this runtime, and they do not call a model provider themselves.

Mail is an IMAP and SMTP connector. The host, user, and password stay in the broker, and the model tools do not take them. The browser on the instance reads accessible text through Playwright. A screenshot is not taken. Calendar and groceries are still examples. The core does not know those domains.

This repository is the privacy core: detection, the account boundary, the model route, and the capability protocol. It is not the apps, and it does not drive a live browser yet.

## Routes

The local model is the default. A cloud model runs only when the task sets `allow_cloud` and the redaction report is clean. A clean scan never upgrades a task by itself. Secrets, national IDs, and payment data are dropped and are not restored. Unresolved free text stays on the local model.

`converse` is the turn. It shows the model that account's view, runs a tool the registry allows, and stops when the tool is external so the person can confirm. `ChatModel` posts to a local OpenAI-compatible server at `http://127.0.0.1:8080` unless you pass another address. The tests use a stand-in model and do not open a connection.

Pass a `HouseholdStore` when constructing `Assistant` and the house server keeps accounts, threads, vaults, vocabulary, and the activity log across a restart. Those records are encrypted with a key the process holds. Another account cannot read them. The database file does not contain the raw message text.

`serve` speaks HTTP on `127.0.0.1:8787`. `POST /v1/accounts` sets a password once. `POST /v1/sessions` checks it and returns a bearer token. `POST /v1/messages` runs one turn for that account and a thread. The response is a reply, or a confirmation when the model asked for an external tool. A second post with `"confirm": true` runs that tool. `GET /v1/threads`, `GET /v1/threads/{id}`, and `GET /v1/activity` require the same session. A token for another account is rejected. The VM enroll route stays a one-time token, not a person's session.

`POST /v1/private` asks for a private exe.dev instance and does nothing until `"confirm": true`. The exe.dev token stays in the broker. `robin boot` is what the VM service starts: it enrolls with `POST /v1/enroll` when a token file is present, deletes that file, and then serves. `GET /v1/private` reports the address once the instance is ready.

The private image is `deploy/Dockerfile`. It is based on exeuntu, installs this package, and leaves the Robin unit disabled until the setup script starts it. Build it with `docker build -t robin/exeuntu:pinned -f deploy/Dockerfile .` and publish that tag. The create call pulls `robin/exeuntu:pinned` and does not carry registry credentials.

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
