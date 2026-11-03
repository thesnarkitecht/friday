#!/bin/sh
# FRIDAY in one line, no sudo:
#   curl -fsSL https://raw.githubusercontent.com/thesnarkitecht/friday/main/install.sh | sh
# Installs uv (if missing) and FRIDAY into ~/.local, then runs `friday setup`.
# FRIDAY_REF picks a branch or tag (default: main); FRIDAY_SRC installs from elsewhere (e.g. ./server).
set -eu

REF="${FRIDAY_REF:-main}"
SRC="${FRIDAY_SRC:-friday-assistant @ https://github.com/thesnarkitecht/friday/archive/${REF}.tar.gz#subdirectory=server}"

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }

if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv, which brings its own Python (into ~/.local/bin, no sudo)"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
  PATH="$HOME/.local/bin:$PATH"
  export PATH
fi

say "Installing FRIDAY"
uv tool install --force --quiet --python 3.12 "$SRC"
uv tool update-shell >/dev/null 2>&1 || true  # puts ~/.local/bin on PATH for new shells

FRIDAY_BIN="$(uv tool dir --bin)/friday"
if [ "$(uname -s)" = "Linux" ] && ! ldconfig -p 2>/dev/null | grep -q libportaudio; then
  say "No PortAudio here: FRIDAY will use your system's own recorder and player (parec/paplay or arecord/aplay)."
fi

say "Done. Setting FRIDAY up…"
if [ -t 1 ] && [ -r /dev/tty ]; then
  "$FRIDAY_BIN" setup </dev/tty
else
  echo "Run: friday setup"
fi
