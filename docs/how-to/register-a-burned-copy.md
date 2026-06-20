# Register a burned copy

Once an image has been explicitly finalized, downloaded, and burned, register the
physical copy so physical-copy coverage can be counted.

CLI example:

```bash
djdan disc add 20260420T040001Z --at 'Shelf B1'
```

The first positional argument is the finalized `image_id`.
Finalized image ids use compact UTC basic form `YYYYMMDDTHHMMSSZ`.

Equivalent API request:

```http
POST /v1/images/20260420T040001Z/copies
Content-Type: application/json
```

```json
{
  "location": "Shelf B1"
}
```

Notes:

- registration is valid only after explicit image finalization has created the finalized image id
- finalization creates exactly two generated copy slots by default, such as `20260420T040001Z-1` and `20260420T040001Z-2`
- the generated `copy_id` is also the exact disc label text Riverhog expects the operator to write
- the physical copy identity is `(volume_id, copy_id)`
- `location` is mutable metadata and is not part of the copy identity

Registering a copy does not change hot presence by itself. It updates physical-copy coverage for files contained in the
image.

Registering one copy does not make a finalized image physically protected by itself.
The physical protection model also requires the image's default two-copy target.
