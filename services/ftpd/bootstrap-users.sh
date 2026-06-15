#!/usr/bin/env sh
set -eu

FTP_UID="${FTP_UID:-1000}"
FTP_GID="${FTP_GID:-1000}"
FTP_ROOT="${FTP_ROOT:-/home/ftpusers}"
FTPD_USERS="${FTPD_USERS:-}"
FTPD_MAX_CLIENTS="${FTPD_MAX_CLIENTS:-40}"
FTPD_MAX_CONNECTIONS="${FTPD_MAX_CONNECTIONS:-8}"

password_var_for_user() {
    printf 'FTPD_PASSWORD_%s' "$(printf '%s' "$1" | tr '[:lower:].-' '[:upper:]__' | tr -c 'A-Z0-9_' '_')"
}

create_user() {
    username="$1"
    password_var="$(password_var_for_user "$username")"
    eval "password=\${$password_var:-}"

    if [ -z "$username" ] || [ -z "$password" ]; then
        echo "missing username/password for $username ($password_var)" >&2
        exit 1
    fi

    home="$FTP_ROOT/$username"
    mkdir -p "$home"
    chown -R "$FTP_UID:$FTP_GID" "$home"

    if pure-pw show "$username" >/dev/null 2>&1; then
        printf '%s\n%s\n' "$password" "$password" | pure-pw passwd "$username" >/dev/null
    else
        printf '%s\n%s\n' "$password" "$password" |
            pure-pw useradd "$username" -u "$FTP_UID" -g "$FTP_GID" -d "$home" >/dev/null
    fi
}

if [ -z "$FTPD_USERS" ]; then
    echo "FTPD_USERS must list at least one FTP user" >&2
    exit 1
fi

old_ifs="$IFS"
IFS=","
for username in $FTPD_USERS; do
    username="$(printf '%s' "$username" | sed 's/^ *//; s/ *$//')"
    [ -z "$username" ] && continue
    create_user "$username"
done
IFS="$old_ifs"

pure-pw mkdb
exec /run.sh \
    -c "$FTPD_MAX_CLIENTS" \
    -C "$FTPD_MAX_CONNECTIONS" \
    -l puredb:/etc/pure-ftpd/pureftpd.pdb \
    -E \
    -j \
    -R \
    -P "$PUBLICHOST"
