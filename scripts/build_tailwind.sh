#!/usr/bin/env bash
# Build the Tailwind CSS bundle using the standalone CLI binary.
#
# On first run, downloads the appropriate `tailwindcss` binary for the host
# OS/arch into ./bin/tailwindcss and caches it there. Subsequent runs reuse
# the cached binary.
#
# Usage:
#   ./scripts/build_tailwind.sh          # one-shot production build (minified)
#   ./scripts/build_tailwind.sh --watch  # dev watch mode

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

TAILWIND_VERSION="${TAILWIND_VERSION:-v3.4.14}"
BIN_DIR="$ROOT_DIR/bin"
TAILWIND_BIN="$BIN_DIR/tailwindcss"

detect_target() {
    local os arch
    os="$(uname -s | tr '[:upper:]' '[:lower:]')"
    arch="$(uname -m)"

    case "$os" in
        linux)  os="linux" ;;
        darwin) os="macos" ;;
        *)      echo "Unsupported OS: $os" >&2; exit 1 ;;
    esac

    case "$arch" in
        x86_64|amd64) arch="x64" ;;
        arm64|aarch64) arch="arm64" ;;
        *) echo "Unsupported arch: $arch" >&2; exit 1 ;;
    esac

    echo "tailwindcss-${os}-${arch}"
}

download_tailwind() {
    local target url
    target="$(detect_target)"
    url="https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/${target}"
    mkdir -p "$BIN_DIR"
    echo "Downloading $url -> $TAILWIND_BIN"
    curl -fsSL -o "$TAILWIND_BIN" "$url"
    chmod +x "$TAILWIND_BIN"
}

if [[ ! -x "$TAILWIND_BIN" ]]; then
    download_tailwind
fi

INPUT="app/static/css/input.css"
OUTPUT="app/static/css/app.css"

if [[ "${1:-}" == "--watch" ]]; then
    exec "$TAILWIND_BIN" -c tailwind.config.js -i "$INPUT" -o "$OUTPUT" --watch
else
    exec "$TAILWIND_BIN" -c tailwind.config.js -i "$INPUT" -o "$OUTPUT" --minify
fi
