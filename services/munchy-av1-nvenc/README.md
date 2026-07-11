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

The image currently builds ffmpeg with `--enable-cuda-nvcc` so `scale_cuda` is
available for GPU Lanczos downscaling on Blackwell. FFmpeg classifies CUDA NVCC
builds as nonfree and unredistributable, so this service image is intended for
private deployment builds, not public binary redistribution. `scale_cuda` itself
is not inherently nonfree: an ffmpeg build using `--enable-cuda-llvm` can avoid
`--enable-nonfree` when the toolchain supports it. `scale_npp` remains a separate
nonfree `--enable-libnpp` path.

## Runtime

The default API listens on port `8000` inside the container. The compose example
binds it to `127.0.0.1` by default.

Important environment variables:

- `MUNCHY_DATA_DIR`
- `MUNCHY_MAX_PARALLEL_ENCODES`
- `MUNCHY_QCUT_VIDEO_MAX_PARALLEL_ENCODES`
- `MUNCHY_VIDEO_DECODE_MODE`
- `MUNCHY_VIDEO_SCALE_MODE`
- `MUNCHY_AV1_CQ`
- `MUNCHY_AV1_PRESET`
- `MUNCHY_AV1_TUNE`
- `MUNCHY_RIVERHOG_UPLOAD_ENABLED`
- `MUNCHY_REVIEW_UPLOAD_ENABLED`

`MUNCHY_VIDEO_DECODE_MODE=cpu` forces software decode. Hardware decode is useful
when it is verified for a source class, but archive-fidelity workflows should
prefer predictable output over theoretical throughput.

`MUNCHY_VIDEO_SCALE_MODE=cuda` uses `scale_cuda` with the profile's requested
interpolation when an archive encode only needs scaling and the source is using a
CUVID decoder. Private deployments can use this for high-quality GPU Lanczos
downscaling after verifying the source class. Review/qcut video clips use the
same hardware-frame path when their encode settings and source geometry allow it.

`MUNCHY_QCUT_VIDEO_MAX_PARALLEL_ENCODES` caps video review clip fanout without
reducing archive batch concurrency. If unset, it defaults to the smaller of
`MUNCHY_MAX_PARALLEL_ENCODES` and `4`.
