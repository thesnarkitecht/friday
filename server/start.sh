#!/bin/bash
# One container: the server, plus the earbud voice loop unless FRIDAY_EARS=0.
set -e
mkdir -p "$FRIDAY_SKILLS_DIR"
cp -rn /app/skills.seed/. "$FRIDAY_SKILLS_DIR"/  # starter skills; never overwrites FRIDAY's own edits
uvicorn friday.app:app --host 0.0.0.0 --port 8080 &
if [ "${FRIDAY_EARS:-1}" != "0" ]; then
  python -m friday.ears &
fi
wait -n
