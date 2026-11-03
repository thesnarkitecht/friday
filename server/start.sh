#!/bin/bash
# The FRIDAY server. Ears and voice are any device running the `friday` CLI with headphones paired.
set -e
mkdir -p "$FRIDAY_SKILLS_DIR"
cp -rn /app/skills.seed/. "$FRIDAY_SKILLS_DIR"/  # starter skills; never overwrites FRIDAY's own edits
exec uvicorn friday.app:app --host 0.0.0.0 --port 8080
