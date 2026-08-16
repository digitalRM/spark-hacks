#!/usr/bin/env bash
# The Python API: routing, compiling, planning. http://$AMICUS_HOST:$AMICUS_PORT
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
exec "$PYTHON" -m api.server
