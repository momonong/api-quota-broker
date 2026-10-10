#!/bin/sh
set -eu
exec /usr/bin/python3 -I "$(dirname -- "$0")/install.py" rollback "$@"
