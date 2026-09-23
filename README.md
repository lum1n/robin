# Robin

Robin is a secure, privacy-oriented, extendable assistant for a household.

The joint assistant runs on the in-house Linux server. Each family member has an account. Someone who wants a private assistant gets their own instance on an [exe.dev](https://exe.dev/docs/api) VM, created from the joint server with no manual setup. Native macOS and iOS apps are clients. They do not embed this runtime, and they do not call a model provider themselves.

Mail, calendar, and groceries are example capabilities. Using the computer is another capability. The core does not know those domains.

This repository is the privacy core: detection, the account boundary, the model route, and the capability protocol. It is not the apps, and it does not drive a live browser yet.

## Routes

The local model is the default. A cloud model runs only when the task sets `allow_cloud` and the redaction report is clean. A clean scan never upgrades a task by itself. Secrets, national IDs, and payment data are dropped and are not restored. Unresolved free text stays on the local model.

Details, including the threat model and private VMs, are in [docs/privacy.md](docs/privacy.md). Adding a capability is described in [docs/capabilities.md](docs/capabilities.md).

## Run the tests

```bash
uv sync --group dev
uv run pytest
```

The tests use fixtures. They do not open a network connection.

## Inspect one string

```bash
uv run robin redact --text 'mail jane@example.com'
uv run robin decide --text 'buy milk' --allow-cloud
```

`redact` can write an encrypted vault with `--vault-out` and `--key-out`. `restore` reads that vault back. The key file is the vault key. Keep it on the machine that runs Robin.
