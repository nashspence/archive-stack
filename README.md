# Archive Storage MVP

A small self-hosted archive service for people with a home server, NAS, or other
shared storage box who want to move older files onto offline optical discs
without losing track of what they saved.

The main use case is very specific: a family, household, or small group with a
large pile of photos, videos, and other files that should not stay on expensive
always-online storage forever, but that still need to stay organized and
recoverable.

This project does **not** make offline disc archiving effortless. That process
is still a little awkward. The goal is to make it more manageable, less messy,
and safer than copying folders around by hand and hoping the notes you wrote
last year still make sense.

## Who this is for

This project is mainly for:

- families with a home server or NAS
- small shared households or friend groups storing files on one machine
- home lab users who want long-term offline archives
- people with growing photo and video libraries
- anyone who wants more structure than “drag files onto a disc and write the date on it”

## Why someone would want it

A lot of personal archive workflows break down in familiar ways:

- uploads fail halfway through
- the list of what was archived drifts away from what was actually saved
- once files go offline, nobody remembers what is on which disc
- bringing old files back later takes guesswork
- original files get deleted too early

This service is meant to help with that.

It gives you:

- resumable uploads so large transfers are less fragile
- a record of what each archive batch contains
- a way to still browse archived files even when the data is offline
- a way to verify that returned archive data matches what was originally saved
- cautious cleanup rules so online originals are not removed too soon

## What this project does

At a high level, the service helps you do this:

1. create an archive batch
2. upload files into it
3. organize those files into disc-sized groups
4. prepare those groups for offline storage
5. optionally create ISO images for burning
6. keep a record of what ended up in the archive
7. later bring a known archived disc back online when you need something from it

The important idea is that files do not become invisible just because their data
is no longer sitting on your server. The catalog still knows they exist and can
explain what you need to restore them.

## What it is trying to be

This is a small backend for a narrow self-hosted workflow. It is meant to be:

- understandable
- testable
- practical for home or small-group use
- honest about the rough edges of offline media

## What it is not

This project is probably not the right fit if you need:

- cloud-scale storage
- enterprise access control
- a polished consumer backup app
- a full records-management system
- an all-in-one hardware appliance
- a one-click magic archive experience

## Main capabilities

### Upload files reliably

Files are uploaded in a resumable way, which helps when transfers are large or
connections are interrupted.

### Keep a clear catalog

The system keeps a structured record of what each archive batch contains,
including folders and files that are no longer online.

### Prepare files for disc storage

When a batch is finalized, the service groups files into disc-sized sets and
writes them into archive output folders that are ready for offline storage.

### Keep offline files visible

Even after files are moved off the server, you can still browse the archive and
see what exists. If a file is offline, the system tells you that clearly instead
of pretending it is still available.

### Bring archived data back online safely

When you reconnect or re-upload a known archived disc set, the service checks
that it matches what was originally archived before accepting it.

### Avoid deleting originals too early

Online originals are kept until archive conditions are satisfied, unless you
explicitly choose a different behavior.

## How the workflow feels

A typical flow looks like this:

1. Create an archive batch.
2. Reserve upload slots for files.
3. Upload files.
4. Finalize the batch.
5. Let the service group the files into disc-sized archive sets.
6. Create an ISO if you want one.
7. Burn or store the archive offline.
8. Later, bring that archive set back online if you need to recover files.

## Quick example

Create an archive batch:

```bash
curl -X POST http://localhost:8080/v1/jobs \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "content-type: application/json" \
  -d '{
    "description": "photos from trip",
    "keep_buffer_after_archive": false
  }'
```

Reserve an upload slot:

```bash
curl -X POST http://localhost:8080/v1/jobs/20260417T060811Z/uploads \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "content-type: application/json" \
  -d '{
    "relative_path": "photos/raw/frame001.dng",
    "size_bytes": 104857600,
    "sha256": null,
    "mode": "0644",
    "mtime": "2026-04-17T06:08:11Z"
  }'
```

Finalize the batch:

```bash
curl -X POST http://localhost:8080/v1/jobs/20260417T060811Z/seal \
  -H "Authorization: Bearer $API_TOKEN"
```

Create an ISO from a finished archive set:

```bash
curl -X POST http://localhost:8080/v1/discs/20260417T091500Z/iso/create \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "content-type: application/json" \
  -d '{"volume_label":"ARCHIVE_20260417"}'
```

## A few project terms

Some internal names are still a little technical. In plain language, they mean:

- **job**: one archive batch
- **disc**: one finished archive set meant for offline storage
- **cache**: bringing archived data back onto online storage
- **seal**: finalize a batch so it can be turned into archive sets
- **buffer**: the temporary online storage area before files are fully archived

## API summary

### Archive batches
- `POST /v1/jobs`
- `POST /v1/jobs/{job_id}/directories`
- `POST /v1/jobs/{job_id}/uploads`
- `POST /v1/jobs/{job_id}/seal`
- `GET /v1/jobs/{job_id}/tree`
- `GET /v1/jobs/{job_id}/content/{relative_path}`
- `POST /v1/jobs/{job_id}/buffer/release`

### Archived disc sets
- `POST /v1/discs/flush`
- `GET /v1/discs/{disc_id}/tree`
- `GET /v1/discs/{disc_id}/content/{disc_relative_path}`
- `POST /v1/discs/{disc_id}/cache/sessions`
- `GET /v1/discs/{disc_id}/cache/sessions/{session_id}/expected`
- `POST /v1/discs/{disc_id}/cache/sessions/{session_id}/uploads`
- `POST /v1/discs/{disc_id}/cache/sessions/{session_id}/complete`
- `DELETE /v1/discs/{disc_id}/cache`
- `POST /v1/discs/{disc_id}/iso/create`
- `POST /v1/discs/{disc_id}/iso/register`
- `POST /v1/discs/{disc_id}/burn/confirm`
- `POST /v1/discs/{disc_id}/download-sessions`
- `GET /v1/discs/downloads/{session_id}/content`

### Progress streams
- `GET /v1/progress/uploads/{upload_id}/stream`
- `GET /v1/progress/jobs/{job_id}/stream`
- `GET /v1/progress/cache-sessions/{session_id}/stream`
- `GET /v1/progress/downloads/{session_id}/stream`

## Technical notes

Under the hood, the project currently uses:

- FastAPI for the API
- `tusd` for resumable uploads
- Redis for progress streams
- SQLite for the catalog
- local filesystem storage for uploaded files, archive output, restored archive data, and ISOs

## Running locally

```bash
cp .env.example .env
docker compose up --build
```

Public API calls require:

```bash
-H "Authorization: Bearer $API_TOKEN"
```

Primary local endpoints:

- OpenAPI: `http://localhost:8080/docs`
- tusd: `http://localhost:1080/files`
