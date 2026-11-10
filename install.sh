#!/bin/sh
# Install Friday:  curl -fsSL https://raw.githubusercontent.com/thesnarkitecht/friday/main/install.sh | sh
#
# Puts one binary in ~/.local/bin and changes nothing else on your system:
# - a prebuilt binary for this machine when a release has one, checksum-verified;
# - otherwise it builds from source with a private Rust toolchain in a temporary folder
#   (never rustup's usual ~/.cargo or ~/.rustup), and deletes the toolchain afterwards.
# The on-device models, the embedding model and (if needed) Chromium download on first use,
# into ~/.cache and ~/.friday.
set -eu

REPO="thesnarkitecht/friday"
DEST="${FRIDAY_INSTALL_DIR:-$HOME/.local/bin}"
say() { printf '\033[38;2;142;162;255mfriday\033[0m %s\n' "$*"; }
fail() { printf '\033[31mfriday\033[0m %s\n' "$*" >&2; exit 1; }

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)               TARGET=x86_64-unknown-linux-gnu ;;
  Linux-aarch64|Linux-arm64)  TARGET=aarch64-unknown-linux-gnu ;;
  Darwin-arm64)               TARGET=aarch64-apple-darwin ;;
  Darwin-x86_64)              TARGET=x86_64-apple-darwin ;;
  *)                          TARGET="" ;;
esac

mkdir -p "$DEST"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ASSET="https://github.com/$REPO/releases/latest/download/friday-$TARGET.tar.gz"

if [ -n "$TARGET" ] && [ -z "${FRIDAY_FROM_SOURCE:-}" ] && curl -fsSL "$ASSET" -o "$TMP/friday.tar.gz" 2>/dev/null; then
  say "downloaded the $TARGET build"
  curl -fsSL "$ASSET.sha256" -o "$TMP/sum" || fail "the release has no checksum; refusing to install"
  want="$(cut -d' ' -f1 "$TMP/sum")"
  got="$( (sha256sum "$TMP/friday.tar.gz" 2>/dev/null || shasum -a 256 "$TMP/friday.tar.gz") | cut -d' ' -f1)"
  [ "$want" = "$got" ] || fail "checksum mismatch; refusing to install"
  tar -xzf "$TMP/friday.tar.gz" -C "$TMP"
  install -m 755 "$TMP/friday" "$DEST/friday"
else
  say "no prebuilt binary for this machine; building from source (5-10 minutes)"
  command -v cc >/dev/null 2>&1 || command -v gcc >/dev/null 2>&1 || command -v clang >/dev/null 2>&1 || fail \
    "building needs a C compiler. macOS: xcode-select --install   Debian/Ubuntu: sudo apt install build-essential"
  # A private toolchain: rustup's own installer, pointed at a temporary folder, with no PATH edits.
  # Build-time downloads (the ONNX runtime) go there too, so nothing is left behind.
  export RUSTUP_HOME="$TMP/rustup" CARGO_HOME="$TMP/cargo" XDG_CACHE_HOME="$TMP/cache"
  say "fetching a private Rust toolchain (removed when done)"
  curl -fsSL https://sh.rustup.rs | sh -s -- -y --quiet --no-modify-path --profile minimal --default-toolchain stable >/dev/null
  "$CARGO_HOME/bin/cargo" install --quiet --locked --root "$TMP/build" \
    --git "https://github.com/$REPO" --branch "${FRIDAY_BRANCH:-main}" friday
  install -m 755 "$TMP/build/bin/friday" "$DEST/friday"
fi

say "installed $DEST/friday"
case ":$PATH:" in
  *":$DEST:"*) ;;
  *) say "add it to your PATH:  echo 'export PATH=\"$DEST:\$PATH\"' >> ~/.profile" ;;
esac
say "run \`friday\` to pick your models"
