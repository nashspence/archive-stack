group "default" {
  targets = [
    "riverhog",
    "riverhog-ftp-adapter",
    "stove0",
    "stove0-extensions",
    "stove0-nvenc-extension",
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
  dockerfile = "riverhog/ftp-adapter/Dockerfile"
  tags       = ["riverhog-ftp-adapter:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "stove0" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "companions/stove0/server/Dockerfile"
  tags       = ["stove0:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "stove0-extensions" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "companions/stove0/extensions/Dockerfile"
  tags       = ["stove0-maintained-extensions:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "stove0-nvenc-extension" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "companions/stove0/extensions/nvenc/Dockerfile"
  tags       = ["stove0-nvenc-extension:dev"]
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
