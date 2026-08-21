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
- Never infer cleanup from object age, placement metadata, opaque naming, or an
  unfamiliar encrypted format.
- Deletion, movement, renaming, overwriting, lifecycle expiration, tiering
  changes, and exact-revision removal are mutations.
- Use each `archives/ARCHIVE_ID/metadata.json.age` to identify an opaque archive.
- Use Riverhog's guarded archive workflows for authorized collection or archive-copy
  retirement. Do not mutate archive objects directly as a shortcut.

Do not write collection identities, personal information, credentials, private
topology, or decrypted archive content into this plaintext file or nearby logs.
"""
