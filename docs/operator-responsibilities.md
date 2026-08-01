# Operator responsibilities

Riverhog's configured archive stores are its durable storage authority. Readiness therefore
includes human and provider controls that application tests cannot establish.

## Maintain access

Keep account recovery, multi-factor authentication, payment, billing alerts, credentials,
bucket permissions, provider contacts, and archive-passphrase safeguards current.
Periodically exercise object listing, metadata reads, retrieval requests, and downloads in
every store.

After account, credential, provider, or storage-class changes, inspect the affected store
with `riverhog archive store show` and retrieve known files through the application
interface. A storage summary or object listing alone does not establish recoverability.
Periodically follow [Recovery without Riverhog](recovery-without-riverhog.md) against a
disposable collection without using the Riverhog database or server.

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
