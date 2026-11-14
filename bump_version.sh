#!/usr/bin/env bash
# Set the release version in every file that carries it.
#
#   ./bump_version.sh 1.0.1     write 1.0.1 into every target
#   ./bump_version.sh --check   exit 1 unless every target carries the same version
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TARGET_FILES=(
    pyproject.toml
    uv.lock
    core/common.py
    .claude-plugin/marketplace.json
    .cursor-plugin/marketplace.json
    tracing/claude_code/pyproject.toml
    tracing/claude_code/.claude-plugin/plugin.json
    tracing/cursor/pyproject.toml
    tracing/cursor/.cursor-plugin/plugin.json
)
TARGET_KINDS=(pyproject uvlock constant manifest manifest pyproject manifest pyproject manifest)

SEMVER_RE='^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'

usage() {
    echo "usage: bump_version.sh <MAJOR.MINOR.PATCH> | --check" >&2
    exit 2
}

version_lines() {
    local kind="$1" file="$2"
    case "$kind" in
        pyproject) grep -n '^version = "' "$file" | cut -d: -f1 || true ;;
        manifest) grep -n '"version": "' "$file" | cut -d: -f1 || true ;;
        constant) grep -n '^AGENT_VERSION = "' "$file" | cut -d: -f1 || true ;;
        uvlock) awk 'prev == "name = \"atatus-coding-harness-tracing\"" && /^version = "/ { print NR } { prev = $0 }' "$file" ;;
    esac
}

version_on_line() {
    local kind="$1" file="$2" line="$3"
    case "$kind" in
        pyproject | uvlock) sed -n "${line}s/^version = \"\([^\"]*\)\".*/\1/p" "$file" ;;
        manifest) sed -n "${line}s/.*\"version\": \"\([^\"]*\)\".*/\1/p" "$file" ;;
        constant) sed -n "${line}s/^AGENT_VERSION = \"\([^\"]*\)\".*/\1/p" "$file" ;;
    esac
}

LINES=()
VERSIONS=()

# Every target is located before any is written, so a broken one aborts with nothing changed.
locate_all() {
    local i file lines count
    for i in "${!TARGET_FILES[@]}"; do
        file="$ROOT/${TARGET_FILES[$i]}"
        if [[ ! -f "$file" ]]; then
            echo "bump_version.sh: ${TARGET_FILES[$i]}: file not found" >&2
            exit 1
        fi
        lines="$(version_lines "${TARGET_KINDS[$i]}" "$file")"
        count="$(printf '%s' "$lines" | grep -c . || true)"
        if [[ "$count" != 1 ]]; then
            echo "bump_version.sh: ${TARGET_FILES[$i]}: expected exactly one version, found $count" >&2
            exit 1
        fi
        LINES[i]="$lines"
        VERSIONS[i]="$(version_on_line "${TARGET_KINDS[$i]}" "$file" "$lines")"
        if [[ -z "${VERSIONS[$i]}" ]]; then
            echo "bump_version.sh: ${TARGET_FILES[$i]}: version is empty" >&2
            exit 1
        fi
    done
}

check() {
    locate_all
    local i width=0 distinct
    for i in "${!TARGET_FILES[@]}"; do
        if ((${#TARGET_FILES[$i]} > width)); then width=${#TARGET_FILES[$i]}; fi
    done
    for i in "${!TARGET_FILES[@]}"; do
        printf "%-${width}s  %s\n" "${TARGET_FILES[$i]}" "${VERSIONS[$i]}"
    done
    distinct="$(printf '%s\n' "${VERSIONS[@]}" | sort -u)"
    if [[ "$(printf '%s\n' "$distinct" | wc -l | tr -d ' ')" != 1 ]]; then
        echo "MISMATCH: found $(printf '%s' "$distinct" | tr '\n' ',' | sed 's/,/, /g')"
        return 1
    fi
    echo "OK: ${#TARGET_FILES[@]} files at $distinct"
}

bump() {
    local new="$1" i file old tmp
    locate_all
    for i in "${!TARGET_FILES[@]}"; do
        file="$ROOT/${TARGET_FILES[$i]}"
        old="${VERSIONS[$i]//./\\.}"
        tmp="$(mktemp)"
        sed "${LINES[$i]}s/${old}\"/${new}\"/" "$file" >"$tmp"
        # cat, not mv: keeps the target's own permissions and inode.
        cat "$tmp" >"$file"
        rm -f "$tmp"
        echo "${TARGET_FILES[$i]}: ${VERSIONS[$i]} -> $new"
    done
    check
}

VERSION=""
MODE=""
while (($#)); do
    case "$1" in
        --check) MODE=check ;;
        --root)
            [[ $# -ge 2 ]] || usage
            ROOT="$(cd "$2" && pwd)"
            shift
            ;;
        -h | --help) usage ;;
        *)
            [[ -z "$VERSION" ]] || usage
            VERSION="$1"
            [[ "$VERSION" =~ $SEMVER_RE ]] || {
                echo "bump_version.sh: '$VERSION' is not MAJOR.MINOR.PATCH (e.g. 1.0.1)" >&2
                exit 2
            }
            MODE=bump
            ;;
    esac
    shift
done

case "$MODE" in
    check) [[ -z "$VERSION" ]] || usage; check ;;
    bump) bump "$VERSION" ;;
    *) usage ;;
esac
