# Munchy AV1 NVENC

`munchy-av1-nvenc` is the GPU encode target for Munchy media ingest jobs. It
receives an input directory and writes archive and review outputs according to
typed Munchy encode profiles.

The service is intentionally generic:

- no deployment-specific hostnames
- no private device names
- no private rclone destinations
- no operator webhook configuration

Private deployment configuration supplies mounts, profile files, runner URLs,
review destinations, and Riverhog credentials.

## Profile Contract

Archive encode profiles target:

```json
{"target": "munchy-av1-nvenc"}
```

The target currently supports AV1 video plus Opus audio, with archive containers
`mkv` and `webm`. Munchy validates container choices so `webm` is only used when
it will not hide streams that `mkv` can preserve directly.

Source-artifact bundles use the Munchy source-artifact contract and are written
as strongly compressed `.source-artifacts.tar.zst` sidecars.

## Runtime

The default API listens on port `8000` inside the container. The compose example
binds it to `127.0.0.1` by default.

Important environment variables:

- `MUNCHY_DATA_DIR`
- `MUNCHY_MAX_PARALLEL_ENCODES`
- `MUNCHY_VIDEO_DECODE_MODE`
- `MUNCHY_AV1_CQ`
- `MUNCHY_AV1_PRESET`
- `MUNCHY_AV1_TUNE`
- `MUNCHY_REVIEW_FONT`
- `MUNCHY_RIVERHOG_UPLOAD_ENABLED`
- `MUNCHY_REVIEW_UPLOAD_ENABLED`

`MUNCHY_VIDEO_DECODE_MODE=cpu` forces software decode. Hardware decode is useful
when it is verified for a source class, but archive-fidelity workflows should
prefer predictable output over theoretical throughput.
