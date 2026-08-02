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

target "riverhog" {
  context    = "."
  dockerfile = "riverhog/server/Dockerfile"
  tags       = ["riverhog-app:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "jeb" {
  context    = "."
  dockerfile = "companions/jeb/server/Dockerfile"
  tags       = ["jeb:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "mango-fish" {
  context    = "."
  dockerfile = "utilities/mango-fish/Dockerfile"
  tags       = ["mango-fish:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "munchy-server" {
  context    = "."
  dockerfile = "companions/munchy/server/Dockerfile"
  tags       = ["munchy-server:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "munchy-av1-nvenc" {
  context    = "."
  dockerfile = "companions/munchy/server/targets/av1-nvenc/Dockerfile"
  tags       = ["munchy-av1-nvenc-target:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}

target "test" {
  context    = "."
  dockerfile = "tests/Dockerfile"
  tags       = ["riverhog-test:dev"]
  args       = { SOURCE_REVISION = "unknown" }
}
