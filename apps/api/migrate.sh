#!/bin/sh
# Pre-deploy migration runner for Render.
# Render splits preDeployCommand on spaces (no shell quote handling), which
# mangles multi-flag commands like "alembic -c alembic.ini upgrade head".
# Wrapping the real command in a script sidesteps that: Render only runs
# "sh migrate.sh", and the shell here parses the command correctly.
set -e
cd /app
exec python -m alembic -c alembic.ini upgrade head
