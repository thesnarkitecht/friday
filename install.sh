#!/bin/sh
# Install Friday:  curl -fsSL https://raw.githubusercontent.com/thesnarkitecht/friday/main/install.sh | sh
# Downloads the prebuilt binary for this machine; builds from source if there isn't one.
set -eu

REPO="thesnarkitecht/friday"
DEST="${FRIDAY_INSTALL_DIR:-$HOME/.local/bin}"

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)               TARGET=x86_64-unknown-linux-gnu ;;
  Linux-aarch64|Linux-arm64)  TARGET=aarch64-unknown-linux-gnu ;;
  Darwin-arm64)               TARGET=aarch64-apple-darwin ;;
  Darwin-x86_64)              TARGET=x86_64-apple-darwin ;;
  *) echo "friday: no prebuilt binary for $(uname -s) $(uname -m)"; TARGET="" ;;
esac

mkdir -p "$DEST"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [ -n "$TARGET" ] && curl -fsSL "https://github.com/$REPO/releases/latest/download/friday-$TARGET.tar.gz" -o "$TMP/friday.tar.gz" 2>/dev/null; then
  if curl -fsSL "https://github.com/$REPO/releases/latest/download/friday-$TARGET.tar.gz.sha256" -o "$TMP/sum" 2>/dev/null; then
    want="$(cut -d' ' -f1 "$TMP/sum")"
    got="$( (sha256sum "$TMP/friday.tar.gz" 2>/dev/null || shasum -a 256 "$TMP/friday.tar.gz") | cut -d' ' -f1)"
    [ "$want" = "$got" ] || { echo "friday: checksum mismatch, refusing to install"; exit 1; }
  fi
  tar -xzf "$TMP/friday.tar.gz" -C "$TMP"
  install -m 755 "$TMP/friday" "$DEST/friday"
else
  echo "friday: building from source (a few minutes)..."
  command -v cargo >/dev/null || { echo "friday: needs Rust first: https://rustup.rs"; exit 1; }
  cargo install --git "https://github.com/$REPO" --locked --root "$TMP/build" friday
  install -m 755 "$TMP/build/bin/friday" "$DEST/friday"
fi

echo "friday: installed to $DEST/friday"
case ":$PATH:" in *":$DEST:"*) ;; *) echo "friday: add $DEST to your PATH" ;; esac
echo "friday: run \`friday\` to pick your models. The on-device models download on first use."
