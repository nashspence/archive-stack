group "default" {
  targets = [
    "riverhog",
    "riverhog-ftp-adapter",
    "riverhog-storage-adapter-aws",
    "riverhog-storage-adapter-backblaze",
    "stove0",
    "stove0-exiftool-observer",
    "stove0-ffprobe-sampling-observer",
    "stove0-nvenc-av1-opus-target",
    "stove0-opus-target",
    "stove0-review-materialize-target",
    "stove0-review-rclone-effect-target",
    "mango-fish",
    "test",
  ]
}

// Update a readable image version and its digest together, then run `make build`.
target "image-common" {
  platforms = ["linux/amd64"]
  args = {
    SOURCE_DATE_EPOCH = "0"
  }
  attest = [
    "type=sbom,generator=docker.io/docker/buildkit-syft-scanner:stable-1@sha256:79e7b013cbec16bbb436f312819a49a4a57752b2270c1a9332ae1a10fcc82a68",
  ]
}

target "riverhog" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "riverhog/server/Dockerfile"
  tags       = ["riverhog-app:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "riverhog-ftp-adapter" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "reference/riverhog/ingress/ftp/Dockerfile"
  tags       = ["riverhog-ftp-adapter:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "riverhog-storage-adapter-aws" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "reference/riverhog/storage/aws/Dockerfile"
  tags       = ["riverhog-storage-adapter-aws:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "riverhog-storage-adapter-backblaze" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "reference/riverhog/storage/backblaze/Dockerfile"
  tags       = ["riverhog-storage-adapter-backblaze:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "stove0" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "companions/stove0/server/Dockerfile"
  tags       = ["stove0:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "stove0-exiftool-observer" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "reference/stove0/observers/exiftool/Dockerfile"
  tags       = ["stove0-exiftool-observer:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "stove0-ffprobe-sampling-observer" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "reference/stove0/observers/ffprobe-sampling/Dockerfile"
  tags       = ["stove0-ffprobe-sampling-observer:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "stove0-nvenc-av1-opus-target" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "reference/stove0/targets/nvenc-av1-opus/Dockerfile"
  tags       = ["stove0-nvenc-av1-opus-target:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "stove0-opus-target" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "reference/stove0/targets/opus/Dockerfile"
  tags       = ["stove0-opus-target:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "stove0-review-materialize-target" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "reference/stove0/targets/review/materialize-target/Dockerfile"
  tags       = ["stove0-review-materialize-target:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "stove0-review-rclone-effect-target" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "reference/stove0/targets/review/rclone-effect-target/Dockerfile"
  tags       = ["stove0-review-rclone-effect-target:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "mango-fish" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "utilities/mango-fish/Dockerfile"
  tags       = ["mango-fish:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "test" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "tests/Dockerfile"
  tags       = ["riverhog-test:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}
