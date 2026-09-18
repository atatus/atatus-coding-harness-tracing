#!/bin/bash
# Atatus Coding Harness Tracing — Thin shell router
#
# Handles Python discovery, repo clone/tarball, venv creation, and pip install.
# All harness-specific logic lives in tracing/<harness>/install.py.
#
# Usage:
#   curl -sSL .../install.sh | bash -s -- claude [--with-skills] [--branch NAME]
#   ./install.sh <harness> --non-interactive
#   ./install.sh status [--json]
#   ./install.sh uninstall [<harness>]
#   ./install.sh update
#
# Flag parsing and the pip invocation both run before the venv exists, so
# neither can move into core/setup/.

set -euo pipefail

REPO_URL="https://github.com/atatus/atatus-coding-harness-tracing.git"
INSTALL_BRANCH="${ATATUS_INSTALL_BRANCH:-main}"
set_branch_urls() {
    TARBALL_URL="https://github.com/atatus/atatus-coding-harness-tracing/archive/refs/heads/${INSTALL_BRANCH}.tar.gz"
    INSTALL_SH_URL="https://raw.githubusercontent.com/atatus/atatus-coding-harness-tracing/${INSTALL_BRANCH}/install.sh"
}
set_branch_urls
INSTALL_DIR="${HOME}/.atatus/harness"
VENV_DIR="${INSTALL_DIR}/venv"
WHEEL_DIR="${ATATUS_WHEEL_DIR:-}"

# -- Terminal helpers --------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
[[ -n "${NO_COLOR:-}" ]] || [[ ! -t 1 ]] && { RED=""; GREEN=""; YELLOW=""; BLUE=""; BOLD=""; NC=""; }

info()   { echo -e "${GREEN}[atatus]${NC} $*"; }
warn()   { echo -e "${YELLOW}[atatus]${NC} $*"; }
err()    { echo -e "${RED}[atatus]${NC} $*" >&2; }
header() { echo -e "\n${BOLD}${BLUE}$*${NC}\n"; }
command_exists() { command -v "$1" &>/dev/null; }

# Package-manager-appropriate way to get Python 3, for the machine actually running this.
python_install_hint() {
    if command_exists brew;      then echo "brew install python@3.12"
    elif command_exists apt-get; then echo "sudo apt-get install -y python3 python3-venv"
    elif command_exists dnf;     then echo "sudo dnf install -y python3"
    elif command_exists yum;     then echo "sudo yum install -y python3"
    elif command_exists pacman;  then echo "sudo pacman -S python"
    elif command_exists zypper;  then echo "sudo zypper install -y python3"
    elif command_exists apk;     then echo "sudo apk add python3"
    elif [[ "$(uname -s 2>/dev/null)" == "Darwin" ]]; then echo "brew install python@3.12"
    else echo "install Python 3.9 or newer with your package manager"
    fi
}

# TTY input for curl|bash scenarios
_tty_in=""
if [[ -t 0 ]]; then _tty_in="/dev/stdin"
elif (exec 3< /dev/tty) 2>/dev/null; then exec 3<&-; _tty_in="/dev/tty"; fi

# Run a command with stdin wired to the user's TTY when possible.
# Under `curl | bash`, our own stdin is the pipe — not a terminal — so any
# subprocess that calls input() (e.g. tracing/<harness>/install.py) would hit
# EOFError on the very first prompt. Redirecting from _tty_in lets Python read
# from the actual terminal. No-op in non-interactive environments without a TTY.
run_with_tty() {
    if [[ -n "$_tty_in" ]]; then
        "$@" < "$_tty_in"
    else
        "$@"
    fi
}

# -- Python discovery --------------------------------------------------------
find_python() {
    local candidates=(python3 python /usr/bin/python3 /usr/local/bin/python3 "$HOME/.local/bin/python3")
    [[ -d "$HOME/.pyenv/shims" ]] && candidates+=("$HOME/.pyenv/shims/python3")
    [[ -x "/opt/homebrew/bin/python3" ]] && candidates+=("/opt/homebrew/bin/python3")
    local conda_base
    conda_base=$(conda info --base 2>/dev/null) && [[ -n "$conda_base" ]] && candidates+=("${conda_base}/bin/python3")
    for p in "${candidates[@]}"; do
        local resolved
        if [[ "$p" == /* ]]; then resolved="$p"
        else resolved=$(command -v "$p" 2>/dev/null || true); fi
        [[ -z "$resolved" || ! -f "$resolved" ]] && continue
        "$resolved" -c "import sys; assert sys.version_info >= (3, 9)" 2>/dev/null && { echo "$resolved"; return 0; }
    done
    return 1
}

# Debian/Ubuntu ship python3 without python3-venv (no ensurepip). Probe before any
# download or write: failing inside `python -m venv` leaves a venv with bin/python
# but no pip, and every retry then trips over that instead of the real cause.
venv_install_hint() {
    local pyver; pyver=$("$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 3)
    if command_exists apt-get; then echo "sudo apt-get install -y python${pyver}-venv"
    else echo "install the venv/ensurepip modules for $1 with your package manager"; fi
}
check_python_can_venv() {
    "$1" -c "import venv, ensurepip" 2>/dev/null && return 0
    err "$1 cannot create a virtual environment (venv/ensurepip missing). Install them and run again:"
    err "    $(venv_install_hint "$1")"
    return 1
}

# -- Venv helpers ------------------------------------------------------------
venv_python() {
    [[ -x "${VENV_DIR}/bin/python" ]] && { echo "${VENV_DIR}/bin/python"; return; }
    [[ -x "${VENV_DIR}/Scripts/python.exe" ]] && { echo "${VENV_DIR}/Scripts/python.exe"; return; }
    return 1
}
venv_pip() {
    [[ -x "${VENV_DIR}/bin/pip" ]] && { echo "${VENV_DIR}/bin/pip"; return; }
    [[ -x "${VENV_DIR}/Scripts/pip.exe" ]] && { echo "${VENV_DIR}/Scripts/pip.exe"; return; }
    return 1
}

# -- Repository download ----------------------------------------------------
git_sync_harness_repo() {
    local branch="$1"
    [[ -d "${INSTALL_DIR}/.git" ]] || return 1
    info "Syncing with origin/${branch}..."
    git -C "$INSTALL_DIR" fetch --depth 1 origin "$branch" 2>/dev/null \
        && git -C "$INSTALL_DIR" checkout -B "$branch" FETCH_HEAD 2>/dev/null && return 0
    git -C "$INSTALL_DIR" fetch origin "$branch" 2>/dev/null \
        && git -C "$INSTALL_DIR" checkout -B "$branch" FETCH_HEAD 2>/dev/null && return 0
    warn "git fetch/checkout failed — trying pull --ff-only"
    git -C "$INSTALL_DIR" pull --ff-only origin "$branch" 2>/dev/null && return 0
    git -C "$INSTALL_DIR" pull --ff-only 2>/dev/null && return 0
    return 1
}

# True only when this script is the copy the installer placed in INSTALL_DIR,
# which is the one an update overwrites underneath itself. A pipe has no file.
running_from_install_dir() {
    [[ -f "${BASH_SOURCE[0]}" ]] || return 1
    local self; self="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || return 1
    local dir;  dir="$(cd "$INSTALL_DIR" 2>/dev/null && pwd)" || return 1
    [[ "$self" == "$dir" ]]
}

# max_time bounds the whole transfer: a hung network must not leave the user
# staring at an update that never starts.
download_file() {
    local url="$1" dest="$2" max_time="${3:-120}"
    if command_exists curl; then curl -sSfL --connect-timeout 5 --max-time "$max_time" "$url" -o "$dest"
    elif command_exists wget; then wget -q --tries=1 --timeout="$max_time" -O "$dest" "$url"
    else err "Neither curl nor wget found — cannot download"; return 1; fi
}

install_repo_tarball() {
    local tarball_url="${1:-$TARBALL_URL}"
    info "Downloading coding-harness-tracing tarball..."
    local tmp_tar; tmp_tar="$(mktemp)"
    download_file "$tarball_url" "$tmp_tar" || { rm -f "$tmp_tar"; exit 1; }
    mkdir -p "$INSTALL_DIR"
    tar xzf "$tmp_tar" --strip-components=1 -C "$INSTALL_DIR"
    rm -f "$tmp_tar"
    info "Extracted to ${INSTALL_DIR}"
}

install_repo() {
    # Wheel mode fetches nothing. The wheel carries every module the harness
    # needs, so there is no source tree to place — but install.sh itself has to
    # land in INSTALL_DIR, because `status`, `update` and `uninstall` are all
    # documented as running from there and repo mode gets it via the extract.
    if [[ -n "$WHEEL_DIR" ]]; then
        mkdir -p "$INSTALL_DIR"
        if [[ -f "${BASH_SOURCE[0]}" ]] && ! cmp -s "${BASH_SOURCE[0]}" "${INSTALL_DIR}/install.sh"; then
            cp "${BASH_SOURCE[0]}" "${INSTALL_DIR}/install.sh" && chmod +x "${INSTALL_DIR}/install.sh"
        fi
        return 0
    fi
    git_sync_harness_repo "$INSTALL_BRANCH" && return 0
    install_repo_tarball
}

# Invoke a harness's install.py. Repo mode runs the file from the source tree;
# wheel mode has no source tree, so it runs the same code as a module. Both
# resolve `core.*` from site-packages either way — the package is pip-installed,
# never on sys.path by accident — so these are equivalent, not a fallback.
run_harness_py() {
    local key="$1" vp="$2"; shift 2
    local dir; dir=$(harness_dir "$key") || return 1
    if [[ -f "${INSTALL_DIR}/${dir}/install.py" ]]; then
        run_with_tty "$vp" "${INSTALL_DIR}/${dir}/install.py" "$@"
    else
        run_with_tty "$vp" -m "${dir//\//.}.install" "$@"
    fi
}

# -- Venv setup --------------------------------------------------------------

# Fix SSL certificate verification on macOS.
#
# Python.org installers ship their own OpenSSL that doesn't trust the macOS
# system keychain, so urllib (used by every atatus-hook-*) fails with
# "CERTIFICATE_VERIFY_FAILED" against the Atatus collector.
#
# Fix: install certifi into the venv and write a sitecustomize.py that sets
# SSL_CERT_FILE before any hook code runs. Idempotent — safe to call repeatedly.
_fix_macos_ssl_certs() {
    local pip="$1"
    local vp
    vp=$(venv_python 2>/dev/null) || return 0

    local offline=()
    [[ -n "$WHEEL_DIR" ]] && offline=(--no-index --find-links "$WHEEL_DIR")
    if ! "$pip" install --quiet "${offline[@]+"${offline[@]}"}" certifi 2>/dev/null; then
        warn "Could not install certifi — SSL verification may fail on macOS"
        [[ -n "$WHEEL_DIR" ]] && warn "Bundle a certifi wheel in ${WHEEL_DIR} to fix this offline."
        return 0
    fi

    local certifi_where site_dir sc
    certifi_where=$("$vp" -c "import certifi; print(certifi.where())" 2>/dev/null) || return 0
    [[ -z "$certifi_where" ]] && return 0

    site_dir=$("$vp" -c "import site; print(site.getsitepackages()[0])" 2>/dev/null) || return 0
    sc="${site_dir}/sitecustomize.py"

    cat > "$sc" <<'PYEOF'
# Atatus Coding Harness Tracing: point Python's SSL stack at certifi's CA bundle on macOS.
# This runs automatically at interpreter startup, before any hook code.
import os as _os
try:
    import certifi as _certifi
    _bundle = _certifi.where()
    _os.environ.setdefault("SSL_CERT_FILE", _bundle)
    _os.environ.setdefault("REQUESTS_CA_BUNDLE", _bundle)
except ImportError:
    pass
PYEOF
    info "SSL certificates configured via certifi"
}

# Install the package into the venv. Extra args go to pip (`-U` for update).
# Shared so install and update cannot drift: they were the same wheel/repo branch
# written twice, differing only by -U, and a flag added to one would miss the other.
pip_install_harness() {
    local pip="$1"; shift
    if [[ -n "$WHEEL_DIR" ]]; then
        # --no-index so a missing wheel fails loudly instead of quietly reaching
        # PyPI, which would defeat the point of installing offline.
        "$pip" install --quiet "$@" --no-index --find-links "$WHEEL_DIR" atatus-coding-harness-tracing \
            || { err "Failed to install atatus-coding-harness-tracing from ${WHEEL_DIR}"; return 1; }
    else
        "$pip" install --quiet "$@" "$INSTALL_DIR" 2>/dev/null \
            || { err "Failed to install atatus-coding-harness-tracing package"; return 1; }
    fi
}

setup_venv() {
    local python_cmd="$1"
    # A venv with a python but no pip is debris from an earlier failed run.
    if venv_python &>/dev/null && ! venv_pip &>/dev/null; then
        warn "Existing venv at ${VENV_DIR} has no pip; rebuilding it"
        rm -rf "$VENV_DIR"
    fi
    if ! venv_python &>/dev/null; then
        info "Creating venv..."
        "$python_cmd" -m venv "$VENV_DIR" 2>/dev/null || {
            rm -rf "$VENV_DIR"
            err "Failed to create venv with $python_cmd"
            err "Install the venv module and run again: $(venv_install_hint "$python_cmd")"
            return 1
        }
    fi
    local pip; pip=$(venv_pip) || { err "pip not found in venv at ${VENV_DIR}"; return 1; }
    info "Installing atatus-coding-harness-tracing into venv..."
    pip_install_harness "$pip" || return 1

    [[ "$(uname)" == "Darwin" ]] && _fix_macos_ssl_certs "$pip"

    info "Venv ready at ${VENV_DIR}"
}

# -- Harness name mapping ----------------------------------------------------
#
# Accepts both the CLI name and the config key. They are the same for every
# harness except Claude Code, which writes HARNESS_NAME "claude-code" while its
# CLI name is "claude". `update` and full `uninstall` discover harnesses via
# list_installed_harnesses(), which yields *config keys*, so without the alias
# both skipped Claude Code entirely — a full uninstall wiped the venv and left
# all 16 hooks in ~/.claude/settings.json pointing at the deleted path, after
# printing "Uninstall complete." install.bat's resolve_dir has accepted both
# spellings all along, so this is parity, not new behaviour.
#
# The install dispatch below is deliberately left alone: `install.sh claude-code`
# stays an unknown command, because a second way to install the same harness
# would imply there are two harnesses.
harness_dir() {
    case "$1" in
        claude|claude-code)  echo "tracing/claude_code" ;;
        codex)   echo "tracing/codex" ;;
        copilot) echo "tracing/copilot" ;;
        cursor)  echo "tracing/cursor" ;;
        gemini)  echo "tracing/gemini" ;;
        kiro)    echo "tracing/kiro" ;;
        opencode) echo "tracing/opencode" ;;
        omp)     echo "tracing/omp" ;;
        devin)   echo "tracing/devin" ;;
        antigravity) echo "tracing/antigravity" ;;
        *)       return 1 ;;
    esac
}

install_harness() {
    local cmd="$1" skills="$2"
    harness_dir "$cmd" >/dev/null || { err "Unknown harness: ${cmd}"; usage; exit 1; }
    header "Installing ${cmd} tracing"
    # Name the fix, not just the problem: "No Python 3.9+ found" leaves the reader to work out
    # which package to install on their distro, which is the same dead end a bare
    # "jq required" produced.
    local python_cmd
    python_cmd=$(find_python) || {
        err "No Python 3.9+ found. Install it with: $(python_install_hint)"
        exit 1
    }
    info "Found Python: ${python_cmd} ($("$python_cmd" --version 2>&1))"
    check_python_can_venv "$python_cmd" || exit 1
    install_repo
    setup_venv "$python_cmd" || exit 1
    local vp; vp=$(venv_python) || { err "Venv python not found after setup"; exit 1; }
    if [[ "$skills" == true ]]; then
        run_harness_py "$cmd" "$vp" install --with-skills
    else
        run_harness_py "$cmd" "$vp" install
    fi
    info "Setup complete!"
}

usage() {
    cat <<'EOF'

Atatus Coding Harness Tracing Installer

Usage: install.sh <command> [flags]

Commands:
  claude      Install and configure tracing for Claude Code / Agent SDK
  codex       Install and configure tracing for OpenAI Codex CLI
  copilot     Install and configure tracing for GitHub Copilot (VS Code + CLI)
  cursor      Install and configure tracing for Cursor IDE
  gemini      Install and configure tracing for Gemini CLI
  kiro        Install and configure tracing for Kiro CLI
  opencode    Install and configure tracing for opencode
  omp         Install and configure tracing for Oh My Pi (omp)
  devin       Install and configure tracing for Devin CLI
  antigravity Install and configure tracing for Google Antigravity
  status      Report configured harnesses and whether their hooks are wired up
  update      Fetch the latest installer, update atatus-coding-harness-tracing and
              re-register all harnesses
  uninstall <harness>   Tear down one harness
  uninstall             Full wipe: venv + repo + shared config

Flags:
  --with-skills         Symlink harness skills into .agents/skills/
  --branch NAME         Install from a specific git branch (default: main)
  --wheel-dir DIR       Install from local wheels in DIR instead of downloading.
                        Fetches nothing and runs no remote code. Also settable
                        as ATATUS_WHEEL_DIR. Bundle a certifi wheel too if you
                        need the macOS SSL fix.
  --json                With `status`: emit machine-readable JSON. Exit code is
                        0 wired up, 1 nothing configured, 2 hooks missing.
  --non-interactive, -y Ask nothing; read every value from the environment or
                        the file named by ATATUS_ENV_FILE. On a FRESH install a
                        missing required value is an error rather than a prompt;
                        over an already-configured harness the stored values are
                        reused instead.

Re-install:
    Over an already-configured harness the installer shows what is stored and
    asks "Use this existing configuration? [Y/n]". Enter keeps it and just
    re-registers the hooks; 'n' re-asks each question with the stored value as
    the default, which is how a key is rotated or a project renamed.

Non-interactive install:
    Put the licence key in a file so it never reaches argv or shell history:

    printf 'ATATUS_API_KEY=...\nATATUS_PROJECT_NAME=my-team\n' > ~/.atatus/onboarding.env
    ATATUS_ENV_FILE=~/.atatus/onboarding.env ./install.sh claude --non-interactive

    Content capture is OFF unless ATATUS_LOG_PROMPTS / ATATUS_LOG_TOOL_DETAILS /
    ATATUS_LOG_TOOL_CONTENT say otherwise — nobody is watching to consent.

    Over an already-configured harness only ATATUS_ENV_FILE values override what
    is stored; ambient ATATUS_* vars are ignored, since every installed harness
    exports them into the sessions it spawns.

EOF
}

# -- Main dispatch -----------------------------------------------------------
main() {
    local cmd="${1:-}"; shift || true
    local subcmd="" with_skills=false status_args=""
    local args=("$@") i=0
    while [[ $i -lt ${#args[@]} ]]; do
        case "${args[$i]}" in
            --with-skills) with_skills=true ;;
            --non-interactive|-y) export ATATUS_NONINTERACTIVE=1 ;;
            --json) status_args="--json" ;;
            --branch)
                i=$((i + 1))
                INSTALL_BRANCH="${args[$i]:-main}"
                set_branch_urls
                ;;
            --wheel-dir)
                i=$((i + 1))
                WHEEL_DIR="${args[$i]:-}"
                [[ -d "$WHEEL_DIR" ]] || { err "--wheel-dir needs a directory; got '${WHEEL_DIR}'"; exit 1; }
                WHEEL_DIR="$(cd "$WHEEL_DIR" && pwd)"
                compgen -G "${WHEEL_DIR}/atatus_coding_harness_tracing-*.whl" >/dev/null \
                    || { err "No atatus_coding_harness_tracing-*.whl in ${WHEEL_DIR}"; exit 1; }
                ;;
            *) [[ -z "$subcmd" ]] && subcmd="${args[$i]}" ;;
        esac
        i=$((i + 1))
    done

    case "$cmd" in
        claude|codex|copilot|cursor|gemini|kiro|opencode|omp|devin|antigravity)
            install_harness "$cmd" "$with_skills"
            ;;
        uninstall)
            if [[ -n "$subcmd" ]]; then
                harness_dir "$subcmd" >/dev/null || { err "Unknown harness: ${subcmd}"; usage; exit 1; }
                local vp; vp=$(venv_python) || { err "Venv not found — nothing to uninstall"; exit 1; }
                header "Uninstalling ${subcmd} tracing"
                run_harness_py "$subcmd" "$vp" uninstall
            else
                local vp; vp=$(venv_python) || {
                    warn "Venv not found — removing install directory"; rm -rf "$INSTALL_DIR"
                    info "Uninstall complete."; return 0; }
                header "Full uninstall"
                # Run each installed harness's uninstall first so external
                # registrations (settings.json hooks, config.toml notify,
                # cursor hooks.json, .github/hooks/*) are cleaned before the
                # shared runtime is wiped. wipe.py deliberately does not
                # touch those files.
                local harnesses
                harnesses=$("$vp" -c 'from core.setup import list_installed_harnesses as L; print("\n".join(L()))' 2>/dev/null) || true
                if [[ -n "$harnesses" ]]; then
                    # fd 3, not stdin: run_harness_py reads the user's TTY via
                    # /dev/stdin, which inside a `done <<< "$list"` loop is the
                    # list itself — the harness would eat the remaining names as
                    # prompt answers and the loop would end after the first one.
                    while IFS= read -r key <&3; do
                        harness_dir "$key" >/dev/null || { warn "Unknown harness: ${key} (skipping)"; continue; }
                        info "Uninstalling ${key} tracing..."
                        run_harness_py "$key" "$vp" uninstall || warn "${key} uninstall failed (continuing)"
                    done 3<<< "$harnesses"
                fi
                "$vp" -m core.setup.wipe
            fi
            ;;
        status)
            local vp; vp=$(venv_python) || { err "Venv not found — nothing installed"; exit 1; }
            "$vp" -m core.setup.status $status_args
            ;;
        update)
            if [[ -z "${ATATUS_UPDATE_REEXEC:-}" && -z "$WHEEL_DIR" ]] && running_from_install_dir; then
                local fresh="${TMPDIR:-/tmp}/atatus-install-update.sh"
                if download_file "$INSTALL_SH_URL" "$fresh" 8; then
                    info "Fetched the latest installer"
                    export ATATUS_UPDATE_REEXEC=1
                    exec bash "$fresh" update ${args[@]+"${args[@]}"}
                fi
                rm -f "$fresh"
                warn "Could not fetch the latest installer — continuing with the local copy"
            fi
            header "Updating atatus-coding-harness-tracing"
            # Re-registering runs each harness's installer, which prompts for the
            # project name. An update re-registers what is already configured, so
            # it always reuses the stored values instead of re-asking per harness.
            export ATATUS_NONINTERACTIVE=1
            # A wheel install has no repo to pull and no newer wheel to hand us.
            # Silently converting it to a network install would change how it was
            # installed behind the user's back, so refuse and say who can update.
            if [[ -z "$WHEEL_DIR" && ! -d "${INSTALL_DIR}/.git" && ! -f "${INSTALL_DIR}/pyproject.toml" ]]; then
                err "This looks like an offline install with no source tree to update."
                err "Re-run the installer that created it, or pass --wheel-dir <dir> with a newer wheel."
                exit 1
            fi
            if [[ -n "$WHEEL_DIR" ]]; then
                info "Updating from local wheels in ${WHEEL_DIR}..."
            elif [[ -d "${INSTALL_DIR}/.git" ]]; then
                info "Pulling latest changes..."
                git -C "$INSTALL_DIR" pull --ff-only 2>/dev/null || {
                    warn "git pull failed — falling back to tarball re-extract"; install_repo_tarball; }
            else install_repo_tarball; fi
            local pip; pip=$(venv_pip) || { err "Venv not found — run install first"; exit 1; }
            info "Reinstalling atatus-coding-harness-tracing..."
            pip_install_harness "$pip" -U || exit 1
            local vp; vp=$(venv_python) || { err "venv python not found"; exit 1; }
            local harnesses
            harnesses=$("$vp" -c 'from core.setup import list_installed_harnesses as L; print("\n".join(L()))' 2>/dev/null) || true
            if [[ -n "$harnesses" ]]; then
                # fd 3: see the uninstall loop — a plain `<<<` here feeds the
                # harness list to the first harness's prompts as stdin.
                while IFS= read -r key <&3; do
                    harness_dir "$key" >/dev/null || { warn "Unknown harness: ${key} (skipping)"; continue; }
                    # Keep going, as the uninstall loop does: one harness whose
                    # registration fails should not abandon the rest half-updated.
                    info "Re-registering ${key}..."
                    run_harness_py "$key" "$vp" install || warn "${key} re-registration failed (continuing)"
                done 3<<< "$harnesses"
            else info "No installed harnesses found to re-register"; fi
            info "Update complete."
            ;;
        -h|--help|help) usage ;;
        "") usage; exit 1 ;;
        *) err "Unknown command: ${cmd}"; usage; exit 1 ;;
    esac
}

main "$@"
