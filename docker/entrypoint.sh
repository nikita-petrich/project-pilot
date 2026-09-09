#!/bin/sh
# Apply migrations, then exec the requested command (default: daemon).
# The button poller talks to Telegram only and has no database to migrate.
set -e

if [ "${1:-daemon}" != "telegram-bot" ]; then
    project-pilot init-db
fi
exec project-pilot "$@"
