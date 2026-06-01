#!/usr/bin/env bash
# =============================================================================
# Platform Atlas - Installer for macOS and Linux
#
# Installs the Platform Atlas CLI and optional WebUI into a dedicated Python
# virtual environment, then wires them into your shell PATH.
#
# Usage:
#   bash install.sh
#   bash install.sh --wheel /path/to/platform_atlas-*.whl
#   bash install.sh --webui /path/to/platform_atlas_webui-*.whl
#   bash install.sh --venv  /opt/atlas_venv
#
# =============================================================================

set -euo pipefail

# Absolute path to the directory where this script lives
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# -----------------------------------------------------------------------------
# Colour helpers
# -----------------------------------------------------------------------------
bold()   { printf '\033[1m%s\033[0m'  "$*"; }
green()  { printf '\033[32m%s\033[0m' "$*"; }
yellow() { printf '\033[33m%s\033[0m' "$*"; }
red()    { printf '\033[31m%s\033[0m' "$*"; }
cyan()   { printf '\033[36m%s\033[0m' "$*"; }
dim()    { printf '\033[2m%s\033[0m'  "$*"; }
nl()     { echo; }

ok()   { echo "   $(green "[OK]") $*"; }
warn() { echo "   $(yellow "[!!]") $*"; }
fail() { echo "   $(red "[XX]") $*"; }
info() { echo "   $*"; }

# Running step counter — auto-increments so numbers stay correct
# regardless of which steps are skipped for a given install mode.
_STEP=0
step() {
    _STEP=$((_STEP + 1))
    nl
    printf '%s\n' "$(cyan "-- Step ${_STEP}: $*")"
    echo "   $(dim "----------------------------------------------------")"
}

# -----------------------------------------------------------------------------
# Banner
# -----------------------------------------------------------------------------
print_banner() {
    nl
    cyan "  +========================================================+"; nl
    cyan "  |                                                        |"; nl
    echo "  |        $(cyan "$(bold "Platform Atlas  --  Installer")")                    |"
    cyan "  |                                                        |"; nl
    cyan "  +========================================================+"; nl
    nl
    echo "  You're a few minutes away from enterprise-grade IAP auditing."
    echo "  $(dim "Everything installs inside a self-contained virtual environment --")"
    echo "  $(dim "nothing system-wide, easy to remove, and simple to upgrade later.")"
    nl
}

# -----------------------------------------------------------------------------
# OS detection
# -----------------------------------------------------------------------------
detect_os() {
    if   [[ "$OSTYPE" == darwin* ]]; then echo "macos"
    elif [[ -f /etc/redhat-release ]] || [[ -f /etc/rocky-release ]] || [[ -f /etc/centos-release ]]; then echo "rhel"
    elif [[ -f /etc/debian_version ]]; then echo "debian"
    else echo "linux"
    fi
}
OS="$(detect_os)"

# -----------------------------------------------------------------------------
# Python detection
# -----------------------------------------------------------------------------
find_python() {
    for cmd in python3.13 python3.12 python3.11 python3 python; do
        if command -v "$cmd" &>/dev/null; then
            local ver
            ver="$("$cmd" -c 'import sys; print(str(sys.version_info[0])+"."+str(sys.version_info[1]))' 2>/dev/null || true)"
            local major minor
            major="$(echo "$ver" | cut -d. -f1)"
            minor="$(echo "$ver" | cut -d. -f2)"
            if [[ "$major" == "3" ]] && [[ "$minor" -ge 11 ]]; then
                echo "$cmd"; return 0
            fi
        fi
    done
    return 1
}

print_python_help() {
    nl
    warn "Python 3.11 or newer is required but wasn't found on your system."
    nl
    case "$OS" in
        macos)
            info "On macOS, the easiest way is Homebrew:"
            nl
            info "  $(cyan "brew install python@3.11")"
            nl
            info "No Homebrew? Install it first:"
            info "  $(cyan '/bin/bash -c "$(curl -fsSL https://brew.sh/install.sh)"')"
            nl
            info "Or download from $(dim "https://www.python.org/downloads/")"
            ;;
        rhel)
            info "On RHEL / Rocky Linux / CentOS:"
            nl
            info "  $(cyan "sudo dnf install python3.11 python3.11-pip")  $(dim "# RHEL 9 / Rocky 9")"
            nl
            info "  RHEL 8 / Rocky 8 -- enable the AppStream module first:"
            info "  $(cyan "sudo dnf module enable python311 && sudo dnf install python311")"
            ;;
        debian)
            info "On Ubuntu / Debian:"
            nl
            info "  $(cyan "sudo apt update && sudo apt install python3.11 python3.11-venv")"
            nl
            info "  Not in package list? Add the deadsnakes PPA:"
            info "  $(cyan "sudo add-apt-repository ppa:deadsnakes/ppa")"
            info "  $(cyan "sudo apt update && sudo apt install python3.11 python3.11-venv")"
            ;;
        *)
            info "Download Python 3.11+ from $(dim "https://www.python.org/downloads/")"
            ;;
    esac
    nl
    fail "Please install Python 3.11+ and run this script again."
    nl
    exit 1
}

# -----------------------------------------------------------------------------
# Wheel discovery
# Searches the script's own directory and the current working directory
# (depth 1 only).  Prints one absolute path per line, sorted newest-first.
# -----------------------------------------------------------------------------
find_wheels() {
    local pattern="$1"
    local sdir cwd
    sdir="$(cd "$SCRIPT_DIR" && pwd -P 2>/dev/null || echo "$SCRIPT_DIR")"
    cwd="$(pwd -P)"
    if [[ "$cwd" == "$sdir" ]]; then
        find "$sdir" -maxdepth 1 -name "$pattern" 2>/dev/null
    else
        find "$sdir" "$cwd" -maxdepth 1 -name "$pattern" 2>/dev/null
    fi | sort -ru
}

# -----------------------------------------------------------------------------
# Argument parsing
# -----------------------------------------------------------------------------
ARG_WHEEL=""
ARG_VENV=""
ARG_WEBUI=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --wheel|-w)  ARG_WHEEL="$2";  shift 2 ;;
        --venv|-v)   ARG_VENV="$2";   shift 2 ;;
        --webui)     ARG_WEBUI="$2";  shift 2 ;;
        --help|-h)
            echo "Usage: bash install.sh [OPTIONS]"
            echo
            echo "  --wheel PATH   Path to the platform_atlas-*.whl file"
            echo "  --webui PATH   Path to the platform_atlas_webui-*.whl file (optional)"
            echo "  --venv  PATH   Where to create the virtual environment (default: ~/atlas_venv)"
            echo
            echo "With no arguments the script auto-discovers wheel files in the current"
            echo "directory and the folder where install.sh lives."
            exit 0
            ;;
        *) fail "Unknown option: $1  (run with --help for usage)"; exit 1 ;;
    esac
done

# =============================================================================
# Main
# =============================================================================
print_banner

# -- Step 1: Python -----------------------------------------------------------
step "Checking Python version"

PYTHON_CMD=""
if PYTHON_CMD="$(find_python)"; then
    PYTHON_VER="$("$PYTHON_CMD" -c 'import sys; print(str(sys.version_info[0])+"."+str(sys.version_info[1])+"."+str(sys.version_info[2]))')"
    ok "Found $(bold "Python $PYTHON_VER") at $(command -v "$PYTHON_CMD")"
else
    print_python_help
fi

# -----------------------------------------------------------------------------
# Discovery  (runs before Step 2, no step number of its own)
# -----------------------------------------------------------------------------
DEFAULT_VENV="$HOME/atlas_venv"

# Collect CLI wheels
cli_wheels=()
if [[ -n "$ARG_WHEEL" ]]; then
    if [[ ! -f "$ARG_WHEEL" ]]; then
        nl; fail "CLI wheel not found: $ARG_WHEEL"; exit 1
    fi
    cli_wheels=("$ARG_WHEEL")
else
    while IFS= read -r _f; do
        [[ -n "$_f" ]] && cli_wheels+=("$_f")
    done < <(find_wheels "platform_atlas-*.whl")
fi

# Collect WebUI wheels
webui_wheels=()
if [[ -n "$ARG_WEBUI" ]]; then
    if [[ ! -f "$ARG_WEBUI" ]]; then
        nl; fail "WebUI wheel not found: $ARG_WEBUI"; exit 1
    fi
    webui_wheels=("$ARG_WEBUI")
else
    while IFS= read -r _f; do
        [[ -n "$_f" ]] && webui_wheels+=("$_f")
    done < <(find_wheels "platform_atlas_webui-*.whl")
fi

# Check for an existing CLI installation at the default venv or on PATH
EXISTING_CLI_VER=""
EXISTING_CLI_VENV=""
if [[ -x "$DEFAULT_VENV/bin/platform-atlas" ]]; then
    EXISTING_CLI_VER="$("$DEFAULT_VENV/bin/platform-atlas" --version 2>/dev/null | head -1 || echo "installed")"
    EXISTING_CLI_VENV="$DEFAULT_VENV"
elif command -v platform-atlas &>/dev/null; then
    EXISTING_CLI_VER="$(platform-atlas --version 2>/dev/null | head -1 || echo "installed")"
    EXISTING_CLI_VENV="$(dirname "$(dirname "$(command -v platform-atlas)")")"
fi

# -- Step 2: Review packages --------------------------------------------------
step "Reviewing packages"
nl

# CLI wheel status
CLI_WHEEL_PATH=""
if [[ ${#cli_wheels[@]} -eq 1 ]]; then
    CLI_WHEEL_PATH="${cli_wheels[0]}"
    ok "CLI wheel    : $(bold "$(basename "$CLI_WHEEL_PATH")")"
elif [[ ${#cli_wheels[@]} -gt 1 ]]; then
    warn "CLI wheel    : multiple found -- you'll choose below"
elif [[ -n "$EXISTING_CLI_VER" ]]; then
    info "$(dim "[--]") CLI wheel    : $(dim "not found (already installed: $EXISTING_CLI_VER)")"
else
    info "[  ] CLI wheel    : not found"
fi

# WebUI wheel status
WEBUI_WHEEL_PATH=""
if [[ ${#webui_wheels[@]} -ge 1 ]]; then
    WEBUI_WHEEL_PATH="${webui_wheels[0]}"
    ok "WebUI wheel  : $(bold "$(basename "$WEBUI_WHEEL_PATH")")"
else
    info "[  ] WebUI wheel  : $(dim "not found  (the WebUI is optional)")"
fi

# Existing install status
if [[ -n "$EXISTING_CLI_VER" ]]; then
    ok "Existing CLI : $(bold "$EXISTING_CLI_VER") at $EXISTING_CLI_VENV"
else
    info "[  ] Existing CLI : $(dim "none detected at $DEFAULT_VENV")"
fi
nl

# Resolve multiple CLI wheels
if [[ ${#cli_wheels[@]} -gt 1 ]]; then
    info "Multiple CLI wheels found -- which one should we install?"
    nl
    for _i in "${!cli_wheels[@]}"; do
        info "  $((_i+1))) $(bold "$(basename "${cli_wheels[$_i]}")")  $(dim "${cli_wheels[$_i]}")"
    done
    nl
    read -rp "   Enter number [1]: " _choice
    _choice="${_choice:-1}"
    if [[ "$_choice" =~ ^[0-9]+$ ]] && (( _choice >= 1 && _choice <= ${#cli_wheels[@]} )); then
        CLI_WHEEL_PATH="${cli_wheels[$((_choice-1))]}"
        ok "Selected: $(bold "$(basename "$CLI_WHEEL_PATH")")"
    else
        fail "Invalid selection."; exit 1
    fi
fi

# Determine install flags
INSTALL_CLI=false
INSTALL_WEBUI=false
[[ -n "$CLI_WHEEL_PATH" ]]   && INSTALL_CLI=true
[[ -n "$WEBUI_WHEEL_PATH" ]] && INSTALL_WEBUI=true

# No wheels found at all -- ask for CLI wheel path
if ! $INSTALL_CLI && ! $INSTALL_WEBUI; then
    nl
    warn "No wheel files found in:"
    info "  $(bold "$SCRIPT_DIR")"
    info "  $(bold "$(pwd)")"
    nl
    info "Please enter the path to the Platform Atlas CLI wheel file."
    info "$(dim "(Your Itential contact should have provided this .whl file.)")"
    nl
    read -rp "   CLI wheel path: " _user_wheel
    if [[ -z "$_user_wheel" ]] || [[ ! -f "$_user_wheel" ]]; then
        fail "File not found: $_user_wheel"
        nl
        info "Tip: run from the folder containing your .whl files:"
        info "  $(cyan "cd ~/Downloads && bash install.sh")"
        exit 1
    fi
    CLI_WHEEL_PATH="$_user_wheel"
    INSTALL_CLI=true
fi

# WebUI wheel found but no CLI available to install it into
if $INSTALL_WEBUI && ! $INSTALL_CLI && [[ -z "$EXISTING_CLI_VENV" ]]; then
    nl
    fail "Cannot install the WebUI -- Platform Atlas CLI is not installed."
    nl
    info "The WebUI is a companion to the Atlas CLI and must be installed"
    info "into the same virtual environment."
    nl
    info "$(bold "Options:")"
    info "  A) Put the CLI wheel in the same folder and re-run:"
    info "     $(cyan "bash install.sh")"
    nl
    info "  B) Specify the CLI wheel explicitly:"
    info "     $(cyan "bash install.sh --wheel /path/to/platform_atlas-*.whl")"
    nl
    info "  C) If Atlas is installed in a custom location, pass --venv:"
    info "     $(cyan "bash install.sh --webui $WEBUI_WHEEL_PATH --venv /path/to/atlas_venv")"
    exit 1
fi

# WebUI-only into an existing venv
if $INSTALL_WEBUI && ! $INSTALL_CLI && [[ -n "$EXISTING_CLI_VENV" ]]; then
    info "The WebUI will be installed into your existing Atlas environment."
    ok "Using: $(bold "$EXISTING_CLI_VENV")"
fi

# Confirmed plan
nl
if $INSTALL_CLI && $INSTALL_WEBUI; then
    ok "$(bold "Plan:") Install CLI + WebUI -- let's go!"
elif $INSTALL_CLI; then
    ok "$(bold "Plan:") Install CLI  $(dim "(no WebUI wheel found -- that's fine, it's optional)")"
else
    ok "$(bold "Plan:") Install WebUI into existing environment"
fi

# =============================================================================
# Venv setup (CLI install) -- skipped for WebUI-only
# =============================================================================
VENV_PATH="${ARG_VENV:-}"

if $INSTALL_CLI; then

    # -- Step 3: Venv location ------------------------------------------------
    step "Choosing a virtual environment"
    nl

    if [[ -z "$VENV_PATH" ]]; then
        info "Atlas will be installed in a virtual environment -- a self-contained"
        info "folder that keeps it separate from the rest of your system."
        nl
        info "Suggested location: $(bold "$DEFAULT_VENV")"
        nl
        read -rp "   Press Enter to use that path, or type a different one: " _venv_input
        VENV_PATH="${_venv_input:-$DEFAULT_VENV}"
    fi

    # Expand leading ~
    VENV_PATH="${VENV_PATH/#\~/$HOME}"

    if [[ -d "$VENV_PATH" ]]; then
        nl
        warn "A virtual environment already exists at $(bold "$VENV_PATH")"
        if [[ -x "$VENV_PATH/bin/platform-atlas" ]]; then
            _cur_ver="$("$VENV_PATH/bin/platform-atlas" --version 2>/dev/null | head -1 || true)"
            info "$(dim "Currently installed: $_cur_ver")"
        fi
        read -rp "   Reinstall / upgrade into it? [Y/n]: " _ow
        _ow="${_ow:-Y}"
        if [[ ! "$_ow" =~ ^[Yy] ]]; then
            info "No changes made. Goodbye!"; exit 0
        fi
        ok "Will upgrade the existing environment."
    else
        ok "Will create environment at: $(bold "$VENV_PATH")"
    fi

    # -- Step 4: Create venv --------------------------------------------------
    step "Creating the virtual environment"

    if [[ ! -d "$VENV_PATH" ]]; then
        "$PYTHON_CMD" -m venv "$VENV_PATH"
        ok "Virtual environment created at $(bold "$VENV_PATH")"
    else
        ok "Using existing environment at $(bold "$VENV_PATH")"
    fi

    PIP="$VENV_PATH/bin/pip"
    ATLAS_BIN="$VENV_PATH/bin/platform-atlas"

    "$PIP" install --quiet --upgrade pip

    # -- Step 5: Install CLI --------------------------------------------------
    step "Installing Platform Atlas CLI"
    nl

    "$PIP" install --force-reinstall --no-compile "$CLI_WHEEL_PATH"

    if [[ -x "$ATLAS_BIN" ]]; then
        INSTALLED_VER="$("$ATLAS_BIN" --version 2>/dev/null | head -1 || true)"
        ok "$(bold "Installed:") $INSTALLED_VER"
    else
        fail "Something went wrong -- platform-atlas not found at $ATLAS_BIN"
        exit 1
    fi

else
    # WebUI-only: use existing venv (--venv overrides default)
    if [[ -n "$VENV_PATH" ]]; then
        VENV_PATH="${VENV_PATH/#\~/$HOME}"
    else
        VENV_PATH="$EXISTING_CLI_VENV"
    fi
    PIP="$VENV_PATH/bin/pip"
    ATLAS_BIN="$VENV_PATH/bin/platform-atlas"
fi

WEBUI_BIN="$VENV_PATH/bin/platform-atlas-webui"

# -- Step: Install WebUI (if applicable) --------------------------------------
if $INSTALL_WEBUI; then
    step "Installing Platform Atlas WebUI"
    nl

    # Build --find-links args so pip can resolve platform-atlas from local WHL (not PyPI)
    _find_links=()
    _webui_dir="$(cd "$(dirname "$WEBUI_WHEEL_PATH")" && pwd -P)"
    _find_links+=("--find-links" "$_webui_dir")
    if [[ -n "$CLI_WHEEL_PATH" ]]; then
        _cli_dir="$(cd "$(dirname "$CLI_WHEEL_PATH")" && pwd -P)"
        [[ "$_cli_dir" != "$_webui_dir" ]] && _find_links+=("--find-links" "$_cli_dir")
    fi
    "$PIP" install --force-reinstall --no-compile "${_find_links[@]}" "$WEBUI_WHEEL_PATH"

    if [[ -x "$WEBUI_BIN" ]]; then
        _wui_ver="$("$PIP" show platform-atlas-webui 2>/dev/null | grep '^Version:' | awk '{print "platform-atlas-webui "$2}' || echo "platform-atlas-webui")"
        ok "$(bold "Installed:") $_wui_ver"
        ok "Launch the WebUI with: $(bold "platform-atlas-webui")"
    else
        fail "WebUI install may have failed -- binary not found at $WEBUI_BIN"
        exit 1
    fi
fi

# =============================================================================
# PATH setup (skip for WebUI-only -- CLI already added the venv to PATH)
# =============================================================================
if $INSTALL_CLI; then

    step "Adding platform-atlas to your PATH"
    nl
    info "Right now platform-atlas only works when the virtual environment is active."
    info "Adding one line to your shell config makes it available everywhere."
    nl

    SHELL_CFG=""
    if [[ -n "${SHELL:-}" ]]; then
        case "$(basename "$SHELL")" in
            zsh)  SHELL_CFG="$HOME/.zshrc"  ;;
            bash) SHELL_CFG="$HOME/.bashrc" ;;
        esac
    fi
    if [[ -z "$SHELL_CFG" ]]; then
        if   [[ -f "$HOME/.zshrc" ]];  then SHELL_CFG="$HOME/.zshrc"
        elif [[ -f "$HOME/.bashrc" ]]; then SHELL_CFG="$HOME/.bashrc"
        fi
    fi

    PATH_LINE="export PATH=\"$VENV_PATH/bin:\$PATH\""

    if [[ -n "$SHELL_CFG" ]]; then
        info "This will add one line to $(bold "$SHELL_CFG"):"
        nl
        info "  $(cyan "$PATH_LINE")"
        nl
        read -rp "   Add it now? [Y/n]: " _path_choice
        _path_choice="${_path_choice:-Y}"
        if [[ "$_path_choice" =~ ^[Yy] ]]; then
            if grep -qF "$VENV_PATH/bin" "$SHELL_CFG" 2>/dev/null; then
                ok "Already present in $(bold "$SHELL_CFG") -- nothing to add."
            else
                printf '\n# Platform Atlas -- added by installer\n%s\n' "$PATH_LINE" >> "$SHELL_CFG"
                ok "Added to $(bold "$SHELL_CFG")"
                warn "Run $(bold "source $SHELL_CFG") or open a new terminal for the change to take effect."
            fi
        else
            nl
            info "No problem. Activate manually when you need it:"
            info "  $(cyan "source $VENV_PATH/bin/activate")"
            info "  $(dim "(platform-atlas will be available for that terminal session)")"
        fi
    else
        info "Couldn't detect your shell config. Activate the venv manually:"
        info "  $(cyan "source $VENV_PATH/bin/activate")"
    fi

fi

# =============================================================================
# Done!
# =============================================================================
nl
nl
green "  +========================================================+"; nl
green "  |                                                        |"; nl
if $INSTALL_CLI && $INSTALL_WEBUI; then
    echo "  |    $(green "$(bold "Platform Atlas  --  CLI + WebUI installed!")")            |"
elif $INSTALL_CLI; then
    echo "  |       $(green "$(bold "Platform Atlas CLI  --  installed!")")                 |"
else
    echo "  |      $(green "$(bold "Platform Atlas WebUI  --  installed!")")                |"
fi
green "  |                                                        |"; nl
green "  +========================================================+"; nl
nl

if $INSTALL_CLI; then
    echo "  Next steps:"
    nl
    echo "  1. $(bold "Open a new terminal")  (or run $(cyan "source $SHELL_CFG") in this one)"
    echo "     Verify with: $(cyan "platform-atlas --version")"
    nl
    echo "  2. $(bold "Run the setup wizard") to configure your first environment:"
    echo "     $(cyan "platform-atlas config init")"
    nl
    echo "  3. Create a session and run your first audit:"
    echo "     $(cyan "platform-atlas session create my-audit")"
    echo "     $(cyan "platform-atlas session run all")"
    if $INSTALL_WEBUI; then
        nl
        echo "  4. Launch the WebUI for a visual view of your results:"
        echo "     $(cyan "platform-atlas-webui")"
    fi
else
    nl
    echo "  The WebUI is ready. Launch it with:"
    echo "     $(cyan "platform-atlas-webui")"
fi

nl
dim "  Questions? Reach out to your Itential Customer Success contact."; nl
nl
