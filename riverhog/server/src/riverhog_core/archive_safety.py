from __future__ import annotations

ARCHIVE_DATA_LOSS_WARNING = (
    "DANGER: These encrypted Riverhog archive objects are the sole durable copies "
    "Riverhog relies on for accepted collections. Deleting, moving, overwriting, "
    "or expiring them can permanently destroy the only recoverable copy. Do not "
    "change them unless you know exactly what they contain and deliberately accept "
    "that loss."
)


def archive_agents_guidance() -> str:
    return f"""# Riverhog Archive Agent Instructions

{ARCHIVE_DATA_LOSS_WARNING}

Treat this archive root as read-only unless the operator explicitly authorizes
an exact mutation and confirms the resulting loss. Opaque names are intentional
and do not mean that an object is unused.

- Listing and reading objects are safe inspection operations.
- Never infer cleanup from object age, provider attributes, opaque naming, or an
  unfamiliar encrypted format.
- Deletion, movement, renaming, overwriting, lifecycle expiration, provider
  attribute changes, and object-version removal are mutations.
- Use each `archives/ARCHIVE_ID/metadata.json.age` to identify an opaque archive.
- Use Riverhog's guarded archive workflows for authorized collection or archive-copy
  retirement. Do not mutate archive objects directly as a shortcut.

Do not write collection identities, personal information, credentials, private
topology, or decrypted archive content into this plaintext file or nearby logs.
"""


def archive_recovery_readme() -> str:
    return f"""# Encrypted Riverhog Archive Recovery

{ARCHIVE_DATA_LOSS_WARNING}

Riverhog archives are independently recoverable with standard object-provider
export or download tools, `age`, `sha256sum`, and `tar`.
The matching `riverhog-recover` release artifact is the maintained reference
implementation. Preserve every relative object path exactly during export.

You need read access to the object tree and the separately safeguarded passphrase named by
the archive's plaintext `recovery.json`. The descriptor contains an opaque passphrase ID and
the encrypted root's exact stored identity; it never contains the secret.
Prepare cold objects for reading through the provider before exporting them.
Treat every object as read-only unless an exact mutation is explicitly authorized.

Use `archives/ARCHIVE_ID/metadata.json.age` to identify an opaque archive, then
recover from `recovery.json` and `manifest.json.age`. The canonical plaintext
archive root binds the exact ordered sequence of bounded, self-validating volume
documents and its authenticated terminator. Verify that complete recovery closure
before accepting the archive.
"""


__all__ = ["ARCHIVE_DATA_LOSS_WARNING", "archive_agents_guidance", "archive_recovery_readme"]
