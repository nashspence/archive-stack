group "default" {
  targets = [
    "riverhog",
    "jeb",
    "mango-fish",
    "munchy-server",
    "munchy-av1-nvenc",
    "test",
  ]
}

// Update a readable image version and its digest together, then run `make build`.
target "image-common" {
  platforms = ["linux/amd64"]
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

target "jeb" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "companions/jeb/server/Dockerfile"
  tags       = ["jeb:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "mango-fish" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "utilities/mango-fish/Dockerfile"
  tags       = ["mango-fish:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "munchy-server" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "companions/munchy/server/Dockerfile"
  tags       = ["munchy-server:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "munchy-av1-nvenc" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "companions/munchy/server/targets/av1-nvenc/Dockerfile"
  tags       = ["munchy-av1-nvenc-target:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "test" {
  inherits   = ["image-common"]
  context    = "."
  dockerfile = "tests/Dockerfile"
  tags       = ["riverhog-test:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}
