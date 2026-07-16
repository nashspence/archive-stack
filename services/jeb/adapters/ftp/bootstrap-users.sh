#!/usr/bin/env sh
set -eu

FTP_UID="${JEB_FTP_UID:-1000}"
FTP_GID="${JEB_FTP_GID:-1000}"
FTP_ROOT="${JEB_LANDING_DIR:-/landing}"
PASSWD_FILE="${PASSWD_FILE:-/etc/pure-ftpd/passwd/pureftpd.passwd}"
JEB_ACCOUNTS="${JEB_ACCOUNTS:-}"
JEB_FTP_ACCOUNTS="${JEB_FTP_ACCOUNTS:-}"
JEB_FTP_MAX_CLIENTS="${JEB_FTP_MAX_CLIENTS:-40}"
JEB_FTP_MAX_CONNECTIONS="${JEB_FTP_MAX_CONNECTIONS:-8}"
PUBLICHOST="${JEB_FTP_PUBLIC_HOST:-}"

password_var_for_user() {
    printf 'JEB_ACCOUNT_%s_PASSWORD' "$(printf '%s' "$1" | tr '[:lower:].-' '[:upper:]__' | tr -c 'A-Z0-9_' '_')"
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

    printf '%s\n%s\n' "$password" "$password" |
        pure-pw useradd "$username" -f "$PASSWD_FILE" \
            -u "$FTP_UID" -g "$FTP_GID" -d "$home" >/dev/null
}

if [ -z "$JEB_FTP_ACCOUNTS" ]; then
    echo "JEB_FTP_ACCOUNTS must list at least one Jeb account" >&2
    exit 1
fi

if [ -z "$PUBLICHOST" ]; then
    echo "JEB_FTP_PUBLIC_HOST must be set" >&2
    exit 1
fi

mkdir -p "$(dirname "$PASSWD_FILE")"
rm -f "$PASSWD_FILE" /etc/pure-ftpd/pureftpd.pdb

old_ifs="$IFS"
IFS=","
for username in $JEB_FTP_ACCOUNTS; do
    username="$(printf '%s' "$username" | sed 's/^ *//; s/ *$//')"
    [ -z "$username" ] && continue
    case ",$JEB_ACCOUNTS," in
        *",$username,"*) ;;
        *)
            echo "JEB_FTP_ACCOUNTS contains unknown Jeb account: $username" >&2
            exit 1
            ;;
    esac
    create_user "$username"
done
IFS="$old_ifs"

pure-pw mkdb /etc/pure-ftpd/pureftpd.pdb -f "$PASSWD_FILE"
exec /run.sh \
    -c "$JEB_FTP_MAX_CLIENTS" \
    -C "$JEB_FTP_MAX_CONNECTIONS" \
    -l puredb:/etc/pure-ftpd/pureftpd.pdb \
    -E \
    -j \
    -R \
    -P "$PUBLICHOST"
