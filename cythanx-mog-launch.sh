#!/data/data/com.termux/files/usr/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PREFIX:-/data/data/com.termux/files/usr}/bin/python"
CYTHANX="$SCRIPT_DIR/cythanx_mog.py"

echo "[CYTHANX MOG] Strategic fusion launcher"


find_xmrig() {
    # Explicit override wins.
    if [ -n "${CYTHANX_XMRIG:-}" ] && [ -x "${CYTHANX_XMRIG}" ]; then
        printf '%s\n' "$CYTHANX_XMRIG"
        return 0
    fi

    # Common Termux / project locations.
    for candidate in \
        "${PREFIX:-/data/data/com.termux/files/usr}/bin/xmrig" \
        "$SCRIPT_DIR/xmrig/build/xmrig" \
        "$SCRIPT_DIR/../xmrig/build/xmrig" \
        "$SCRIPT_DIR/../../xmrig/build/xmrig" \
        "${HOME:-/data/data/com.termux/files/home}/xmrig/build/xmrig"; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    # Search the Termux home tree for an executable named xmrig.
    if [ -d "${HOME:-/data/data/com.termux/files/home}" ]; then
        found="$(find "${HOME:-/data/data/com.termux/files/home}" -type f -name xmrig -perm -111 -print -quit 2>/dev/null)"
        if [ -n "$found" ]; then
            printf '%s\n' "$found"
            return 0
        fi
    fi

    # Finally check PATH.
    command -v xmrig 2>/dev/null || return 1
}

if ! command -v python >/dev/null 2>&1; then
    echo "[CYTHANX MOG] Installing Python..."
    pkg update -y || exit 1
    pkg install -y python || exit 1
fi

if ! "$PYTHON" -c 'import blake3' >/dev/null 2>&1; then
    echo "[CYTHANX MOG] Installing Python blake3..."
    "$PYTHON" -m pip install --upgrade pip || exit 1
    "$PYTHON" -m pip install blake3 || exit 1
fi

if [ ! -f "$CYTHANX" ]; then
    echo "[CYTHANX MOG] Missing $CYTHANX"
    exit 1
fi

chmod +x "$CYTHANX"

echo "[CYTHANX MOG] Structural status..."
"$PYTHON" "$CYTHANX" --mog-status || exit 1

echo "[CYTHANX MOG] Self-test..."
"$PYTHON" "$CYTHANX" --procedure validate || exit 1

if [ "$#" -eq 0 ]; then
    echo
    echo "Usage:"
    echo "  $0 --validate"
    echo "  $0 --fuse FILE [FILE ...]"
    echo "  $0 --organism [SEED]"
    echo "  $0 --mine-block [SEED]"
    echo "  $0 --auto [POOL:PORT USER [XMRIG_PATH]]"
    echo "  $0 --bip39-generate /path/to/english.txt"
    echo
    echo "No wallet, mnemonic, password, or pool credential is embedded."
    exit 2
fi

case "$1" in
    --validate)
        exec "$PYTHON" "$CYTHANX" --procedure validate
        ;;
    --fuse)
        shift
        [ "$#" -gt 0 ] || { echo "[MOG] File required."; exit 2; }
        exec "$PYTHON" "$CYTHANX" --procedure fuse --fuse "$@"
        ;;
    --organism)
        shift
        if [ "$#" -gt 0 ]; then
            exec "$PYTHON" "$CYTHANX" --procedure organism --seed "$1"
        fi
        exec "$PYTHON" "$CYTHANX" --procedure organism
        ;;
    --mine-block)
        shift
        if [ "$#" -gt 0 ]; then
            exec "$PYTHON" "$CYTHANX" --procedure mine-block --seed "$1"
        fi
        exec "$PYTHON" "$CYTHANX" --procedure mine-block
        ;;
    --bip39-generate)
        [ -n "${2:-}" ] || { echo "[BIP39] Wordlist path required."; exit 2; }
        exec "$PYTHON" "$CYTHANX" --bip39-wordlist "$2" --bip39-generate
        ;;
    --auto)
        shift
        if [ "$#" -gt 0 ]; then
            export CYTHANX_POOL="${CYTHANX_POOL:-$1}"
        fi
        if [ "$#" -gt 1 ]; then
            export CYTHANX_USER="${CYTHANX_USER:-$2}"
        fi
        if [ "$#" -gt 2 ]; then
            export CYTHANX_XMRIG="$3"
        fi

        XMRIG_FOUND="$(find_xmrig || true)"
        if [ -n "$XMRIG_FOUND" ]; then
            export CYTHANX_XMRIG="$XMRIG_FOUND"
            echo "[CYTHANX MOG] XMRig found: $CYTHANX_XMRIG"
            echo "[CYTHANX MOG] XMRig will not start until the supervisor is explicitly launched."
        else
            echo "[CYTHANX MOG] XMRig executable not found."
            echo "[CYTHANX MOG] Expected examples: xmrig/build/xmrig or $PREFIX/bin/xmrig"
            exit 1
        fi

        printf '[CYTHANX MOG] Launch XMRig supervisor now? [y/N]: '
        read -r answer
        case "$answer" in
            y|Y|yes|YES) ;;
            *) echo "[CYTHANX MOG] XMRig launch cancelled."; exit 0 ;;
        esac

        exec "$PYTHON" "$CYTHANX" --procedure auto
        ;;
    *)
        echo "[CYTHANX MOG] Unknown mode: $1"
        exit 2
        ;;
esac
