#!/bin/sh
# Refuse to start without a real SECRET_KEY.
#
# app.py falls back to "dev-secret-change-me" when SECRET_KEY is unset. That is
# fine on a laptop and dangerous in a container: the key signs Flask session
# cookies, and a known key means anyone can forge them. Fail loudly at startup
# rather than serving traffic with a public secret.
set -eu

if [ -z "${SECRET_KEY:-}" ] || [ "${SECRET_KEY}" = "dev-secret-change-me" ]; then
    echo "FATAL: SECRET_KEY is unset or still the development default." >&2
    echo "       Generate one with:  python3 -c 'import secrets; print(secrets.token_hex(32))'" >&2
    echo "       and pass it in, e.g. --env-file .env or a secrets manager." >&2
    exit 1
fi

# exec so gunicorn is PID 1 and receives SIGTERM directly.
exec "$@"
