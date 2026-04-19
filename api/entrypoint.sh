#!/bin/sh
set -eu

uid="${PREFERRED_UID:-1000}"
gid="${PREFERRED_GID:-1000}"
archive_root="${ARCHIVE_ROOT:-/var/lib/archive}"
intake_root="${COLLECTION_INTAKE_ROOT:-/var/lib/uploads/collections}"
uploads_root="$(dirname "$intake_root")"
runtime_home="${archive_root}/runtime-home"
xdg_cache_home="${runtime_home}/.cache"
xdg_config_home="${runtime_home}/.config"

fix_tree() {
  root="$1"
  mkdir -p "$root"
  chown -R --no-dereference "$uid:$gid" "$root"
  find "$root" -type d -exec chmod 2775 {} +
}

umask 0002
fix_tree "$archive_root"
fix_tree "$uploads_root"
fix_tree "$runtime_home"
fix_tree "$xdg_cache_home"
fix_tree "$xdg_config_home"

export HOME="$runtime_home"
export XDG_CACHE_HOME="$xdg_cache_home"
export XDG_CONFIG_HOME="$xdg_config_home"
exec setpriv --reuid "$uid" --regid "$gid" --clear-groups "$@"
