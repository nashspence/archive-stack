# Resumable Uploads Reference

Fetch uploads use two layers:

- the JSON API binds uploads to fetch-manifest entries
- the returned upload resource uses tus-compatible resumable upload semantics

## Fetch Entry Upload Session

`POST /v1/fetches/{fetch_id}/entries/{entry_id}/upload` creates or resumes the upload resource for one logical file.

The response exposes at least:

- `entry`
- `protocol` — always `tus`
- `upload_url`
- `offset`
- `length`
- `expires_at`
- `checksum_algorithm`

## Required Transport Semantics

The upload resource must support:

- offset-based resume
- expiration
- checksum validation on streamed chunks

The upload resource is for one logical file only. Split files do not create one upload per disc part; `arc-disc`
streams parts into the same logical-file upload in ascending order.

## Direct Streaming

`arc-disc` streams recovered plaintext directly from disc to the upload resource:

- unsplit files stream as one continuous upload
- split files stream sequentially into the same upload resource as successive plaintext spans become available

If an implementation uses temporary buffering internally, that buffering is conventional temporary storage and not a
user-managed API or CLI surface.
