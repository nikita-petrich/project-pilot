#!/bin/sh
# Apply migrations, then exec the requested command (default: daemon).
set -e

project-pilot init-db
exec project-pilot "$@"
