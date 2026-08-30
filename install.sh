#!/usr/bin/env sh
set -eu

source_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec python3 "$source_root/scripts/install.py" "$source_root" "$@"
