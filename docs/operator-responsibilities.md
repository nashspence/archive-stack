# Operator responsibilities

Application tests cannot establish provider-account access, payment continuity, credential
recovery, or independent recoverability. These checks remain operator work.

## Maintain access

Keep account recovery, multi-factor authentication, payment, billing alerts, credentials,
bucket permissions, provider contacts, and archive-passphrase safeguards current.
Periodically exercise object listing, metadata reads, retrieval requests, and downloads in
every store.

Treat the authorized Riverhog host as the trusted plaintext and encryption boundary. Protect
secret-bearing persistent storage with full-disk or equivalent volume encryption, restrict
administrative, network, and secret-file access, use least-privilege provider credentials, and
keep the host and runtime security-maintained. Riverhog-controlled surfaces must keep
passphrases out of source, images, logs, databases, archives, generated documentation, and
API or CLI output.

After account, credential, provider, or storage-class changes, inspect the affected store
with `riverhog archive store show` and retrieve known files through the application
interface. A storage summary or object listing alone does not establish recoverability.

Riverhog maintains plaintext `README.md` and `AGENTS.md` guidance at each archive root.
Opaque names do not mean objects are unused; encrypted collection objects may be the sole
durable copies.

## Destructive changes

Use Riverhog's guarded archive-copy retirement and collection-deletion operations. Inspect
the exact collection and store, retained verification candidates, affected objects and
bytes, blockers, warning, and short-lived challenge before confirming a destructive change.
Provider retention, object versions, minimum-storage duration, or billing timing may delay
visible cost changes.

Direct provider credentials can bypass Riverhog's ceremony. Protect those credentials and
treat provider-console or raw-object deletion as an exceptional destructive action.
