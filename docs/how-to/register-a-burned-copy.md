# Register a burned copy

Once an image has been downloaded, finalized, and burned, register the physical copy so archival coverage can be
counted.

CLI example:

```bash
arc copy add img_2026-04-20_01 BR-021-A --at 'Shelf B1'
```

Equivalent API request:

```http
POST /v1/images/img_2026-04-20_01/copies
Content-Type: application/json
```

```json
{
  "id": "BR-021-A",
  "location": "Shelf B1"
}
```

Notes:

- registration is valid only after the image's first ISO download has assigned and stored its `volume_id`
- the physical copy identity is `(volume_id, id)`
- the user-supplied `id` must be unique within that finalized image/`volume_id`
- `location` is mutable metadata and is not part of the copy identity

Registering a copy does not change hot presence by itself. It updates archival coverage for files contained in the
image.
