<#
.SYNOPSIS
    Platform Atlas - Installer for Windows

.DESCRIPTION
    Installs the Platform Atlas CLI and optional WebUI into a dedicated Python
    virtual environment, then adds them to your user PATH.

    The script auto-discovers wheel files in its own folder and the current
    directory. Run it from the folder containing your .whl files and it will
    find them automatically.

.PARAMETER Wheel
    Path to the platform_atlas-*.whl file. Auto-discovered if omitted.

.PARAMETER Venv
    Where to create the virtual environment. Default: %USERPROFILE%\atlas_venv

.PARAMETER WebUI
    Path to the platform_atlas_webui-*.whl file. Auto-discovered if omitted.

.EXAMPLE
    .\install.ps1

.EXAMPLE
    .\install.ps1 -Wheel "C:\Downloads\platform_atlas-1.8.0-py3-none-any.whl"

.EXAMPLE
    .\install.ps1 -Wheel .\platform_atlas-1.8.0-py3-none-any.whl -Venv C:\tools\atlas_venv
#>
[CmdletBinding()]
param(
    [string]$Wheel = "",
    [string]$Venv  = "",
    [string]$WebUI = ""
)

$ErrorActionPreference = "Stop"

# -----------------------------------------------------------------------------
# Colour helpers
# -----------------------------------------------------------------------------
function Write-Banner {
    Write-Host ""
    Write-Host "  +========================================================+" -ForegroundColor Cyan
    Write-Host "  |                                                        |" -ForegroundColor Cyan
    Write-Host "  |        Platform Atlas  --  Windows Installer           |" -ForegroundColor Cyan
    Write-Host "  |                                                        |" -ForegroundColor Cyan
    Write-Host "  +========================================================+" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  You're a few minutes away from enterprise-grade IAP auditing." -ForegroundColor White
    Write-Host "  Everything installs inside a self-contained virtual environment --" -ForegroundColor DarkGray
    Write-Host "  nothing system-wide, easy to remove, and simple to upgrade later." -ForegroundColor DarkGray
    Write-Host ""
}

# Running step counter -- auto-increments so numbers stay correct
# regardless of which steps run for a given install mode.
$script:_stepNum = 0
function Write-Step([string]$label) {
    $script:_stepNum++
    Write-Host ""
    Write-Host "-- Step $($script:_stepNum): $label" -ForegroundColor Cyan
    Write-Host "   ----------------------------------------------------" -ForegroundColor DarkGray
}

function Write-Ok([string]$msg)   { Write-Host "   [OK] $msg" -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "   [!!] $msg" -ForegroundColor Yellow }
function Write-Fail([string]$msg) { Write-Host "   [XX] $msg" -ForegroundColor Red }
function Write-Info([string]$msg) { Write-Host "   $msg" }
function Write-Dim([string]$msg)  { Write-Host "   $msg" -ForegroundColor DarkGray }

# -----------------------------------------------------------------------------
# Python detection  (compatible with all PowerShell versions)
# -----------------------------------------------------------------------------
function Find-Python311 {
    $candidates = @("py", "python3.13", "python3.12", "python3.11", "python3", "python")
    foreach ($cmd in $candidates) {
        $cmdObj = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($cmdObj -eq $null) { continue }
        $exe = $cmdObj.Source
        if (-not $exe) { continue }

        if ($cmd -eq "py") {
            $versionArgs = @("-3", "-c", "import sys; print(str(sys.version_info[0])+'.'+str(sys.version_info[1])+'.'+str(sys.version_info[2]))")
        } else {
            $versionArgs = @("-c", "import sys; print(str(sys.version_info[0])+'.'+str(sys.version_info[1])+'.'+str(sys.version_info[2]))")
        }
        try {
            $ver = & $exe @versionArgs 2>$null
            if (-not $ver) { continue }
            $parts = $ver.Trim().Split(".")
            if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11) {
                return [PSCustomObject]@{ Exe = $exe; Cmd = $cmd; Version = $ver.Trim() }
            }
        } catch { continue }
    }
    return $null
}

function Write-PythonHelp {
    Write-Host ""
    Write-Warn "Python 3.11 or newer is required but wasn't found on your system."
    Write-Host ""
    Write-Info "The easiest way to install Python on Windows:"
    Write-Host ""
    Write-Info "  Option A - Microsoft Store (easiest, no admin rights needed):"
    Write-Info "    1. Open the Microsoft Store"
    Write-Info "    2. Search for 'Python 3.11' and click Install"
    Write-Host ""
    Write-Info "  Option B - python.org installer:"
    Write-Info "    1. Go to https://www.python.org/downloads/"
    Write-Info "    2. Download the latest Python 3.11 (or 3.12) Windows installer"
    Write-Info "    3. Run it - make sure to tick 'Add Python to PATH'"
    Write-Host ""
    Write-Info "  After installing, close this window, open a new one,"
    Write-Info "  and run this script again."
    Write-Host ""
    Write-Fail "Please install Python 3.11+ and run this script again."
    Write-Host ""
    exit 1
}

# -----------------------------------------------------------------------------
# Wheel discovery
# Searches the script's own folder and the current working directory
# (no recursion -- depth 1 only).  Returns a deduplicated array.
# -----------------------------------------------------------------------------
function Find-Wheels([string]$pattern) {
    $found = @()
    $seen  = @{}

    $searchDirs = @()
    if ((-not [string]::IsNullOrEmpty($PSScriptRoot)) -and (Test-Path $PSScriptRoot)) {
        $searchDirs += $PSScriptRoot
    }
    $cwd = (Get-Location).Path
    if ($cwd -ne $PSScriptRoot) {
        $searchDirs += $cwd
    }

    foreach ($dir in $searchDirs) {
        $items = Get-ChildItem -Path $dir -Filter $pattern -ErrorAction SilentlyContinue |
                 Sort-Object Name -Descending
        foreach ($item in $items) {
            if (-not $seen.ContainsKey($item.FullName)) {
                $found += $item.FullName
                $seen[$item.FullName] = $true
            }
        }
    }
    return $found
}

# -----------------------------------------------------------------------------
# Check whether platform-atlas CLI is installed in a given venv
# -----------------------------------------------------------------------------
function Test-AtlasInstalled([string]$venvPath) {
    if ([string]::IsNullOrEmpty($venvPath)) { return $false }
    return (Test-Path (Join-Path $venvPath "Scripts\platform-atlas.exe"))
}

function Get-AtlasVersion([string]$venvPath) {
    $exe = Join-Path $venvPath "Scripts\platform-atlas.exe"
    if (-not (Test-Path $exe)) { return "" }
    $ver = (& $exe --version 2>$null) | Select-Object -First 1
    if ($ver -eq $null) { return "" }
    return $ver.Trim()
}

# =============================================================================
# Main
# =============================================================================
Write-Banner

# -- Step 1: Python -----------------------------------------------------------
Write-Step "Checking Python version"

$py = Find-Python311
if (-not $py) {
    Write-PythonHelp
}
Write-Ok "Found Python $($py.Version) at $($py.Exe)"

$pyExe = $py.Exe
if ($py.Cmd -eq "py") { $pyArgs = @("-3") } else { $pyArgs = @() }

# -----------------------------------------------------------------------------
# Discovery  (gathers info before Step 2)
# -----------------------------------------------------------------------------
$defaultVenv = Join-Path $env:USERPROFILE "atlas_venv"

# Collect CLI wheels
$cliWheels = @()
if ($Wheel -ne "") {
    if (-not (Test-Path $Wheel)) { Write-Fail "CLI wheel not found: $Wheel"; exit 1 }
    $cliWheels = @($Wheel)
} else {
    $cliWheels = @(Find-Wheels "platform_atlas-*.whl")
}

# Collect WebUI wheels
$webuiWheels = @()
if ($WebUI -ne "") {
    if (-not (Test-Path $WebUI)) { Write-Fail "WebUI wheel not found: $WebUI"; exit 1 }
    $webuiWheels = @($WebUI)
} else {
    $webuiWheels = @(Find-Wheels "platform_atlas_webui-*.whl")
}

# Check for an existing CLI installation at the default venv or on PATH
$existingCLIVer  = ""
$existingCLIVenv = ""

if (Test-AtlasInstalled $defaultVenv) {
    $existingCLIVer  = Get-AtlasVersion $defaultVenv
    $existingCLIVenv = $defaultVenv
} else {
    $atlasCmd = Get-Command "platform-atlas" -ErrorAction SilentlyContinue
    if ($atlasCmd -ne $null) {
        $existingCLIVer  = (& $atlasCmd.Source --version 2>$null) | Select-Object -First 1
        if ($existingCLIVer -eq $null) { $existingCLIVer = "installed" }
        $existingCLIVenv = Split-Path (Split-Path $atlasCmd.Source -Parent) -Parent
    }
}

# -- Step 2: Review packages --------------------------------------------------
Write-Step "Reviewing packages"
Write-Host ""

# CLI wheel status
$cliWheelPath = ""
if ($cliWheels.Count -eq 1) {
    $cliWheelPath = $cliWheels[0]
    Write-Ok "CLI wheel    : $([System.IO.Path]::GetFileName($cliWheelPath))"
} elseif ($cliWheels.Count -gt 1) {
    Write-Warn "CLI wheel    : multiple found -- you will choose below"
} elseif ($existingCLIVer -ne "") {
    Write-Host "   [--] CLI wheel    : not found (already installed: $existingCLIVer)" -ForegroundColor DarkGray
} else {
    Write-Info "[  ] CLI wheel    : not found"
}

# WebUI wheel status
$webuiWheelPath = ""
if ($webuiWheels.Count -ge 1) {
    $webuiWheelPath = $webuiWheels[0]
    Write-Ok "WebUI wheel  : $([System.IO.Path]::GetFileName($webuiWheelPath))"
} else {
    Write-Host "   [  ] WebUI wheel  : not found  (the WebUI is optional)" -ForegroundColor DarkGray
}

# Existing install status
if ($existingCLIVer -ne "") {
    Write-Ok "Existing CLI : $existingCLIVer at $existingCLIVenv"
} else {
    Write-Host "   [  ] Existing CLI : none detected at $defaultVenv" -ForegroundColor DarkGray
}
Write-Host ""

# Resolve multiple CLI wheels
if ($cliWheels.Count -gt 1) {
    Write-Info "Multiple CLI wheels found -- which one should we install?"
    Write-Host ""
    for ($i = 0; $i -lt $cliWheels.Count; $i++) {
        Write-Info "  $($i+1)) $([System.IO.Path]::GetFileName($cliWheels[$i]))"
        Write-Host "       $($cliWheels[$i])" -ForegroundColor DarkGray
    }
    Write-Host ""
    $choice = Read-Host "   Enter number [1]"
    if ($choice -eq "") { $choice = "1" }
    $idx = [int]$choice - 1
    if ($idx -lt 0 -or $idx -ge $cliWheels.Count) { Write-Fail "Invalid selection."; exit 1 }
    $cliWheelPath = $cliWheels[$idx]
    Write-Ok "Selected: $([System.IO.Path]::GetFileName($cliWheelPath))"
}

# Determine install flags
$installCLI   = ($cliWheelPath  -ne "")
$installWebUI = ($webuiWheelPath -ne "")

# No wheels found at all -- ask for CLI wheel path
if (-not $installCLI -and -not $installWebUI) {
    Write-Host ""
    Write-Warn "No wheel files found."
    Write-Host ""
    Write-Info "Looked in:"
    if (-not [string]::IsNullOrEmpty($PSScriptRoot)) { Write-Info "  $PSScriptRoot" }
    Write-Info "  $((Get-Location).Path)"
    Write-Host ""
    Write-Info "Please enter the full path to the CLI wheel file."
    Write-Info "(Your Itential contact should have provided this .whl file.)"
    Write-Host ""
    $cliWheelPath = Read-Host "   CLI wheel path"
    if ($cliWheelPath -eq "" -or -not (Test-Path $cliWheelPath)) {
        Write-Fail "File not found: $cliWheelPath"
        Write-Host ""
        Write-Info "Tip: drag the .whl file from Explorer into this window to paste its path."
        exit 1
    }
    $installCLI = $true
}

# WebUI wheel found but no CLI to install it into
if ($installWebUI -and -not $installCLI -and $existingCLIVenv -eq "") {
    Write-Host ""
    Write-Fail "Cannot install the WebUI -- Platform Atlas CLI is not installed."
    Write-Host ""
    Write-Info "The WebUI is a companion to the Atlas CLI and must live in the"
    Write-Info "same virtual environment."
    Write-Host ""
    Write-Info "Options:"
    Write-Info "  A) Put the CLI wheel in the same folder and re-run:"
    Write-Host "       .\install.ps1" -ForegroundColor Cyan
    Write-Host ""
    Write-Info "  B) Specify the CLI wheel explicitly:"
    Write-Host "       .\install.ps1 -Wheel \path\to\platform_atlas-*.whl" -ForegroundColor Cyan
    Write-Host ""
    Write-Info "  C) If Atlas is in a custom venv, pass -Venv:"
    Write-Host "       .\install.ps1 -WebUI $webuiWheelPath -Venv \path\to\atlas_venv" -ForegroundColor Cyan
    exit 1
}

# WebUI-only: note which venv we'll use
if ($installWebUI -and -not $installCLI -and $existingCLIVenv -ne "") {
    Write-Info "The WebUI will be installed into your existing Atlas environment."
    Write-Ok "Using: $existingCLIVenv"
}

# Confirmed plan
Write-Host ""
if ($installCLI -and $installWebUI) {
    Write-Ok "Plan: Install CLI + WebUI -- let's go!"
} elseif ($installCLI) {
    Write-Host "   [OK] Plan: Install CLI" -ForegroundColor Green -NoNewline
    Write-Host "  (no WebUI wheel found -- that's fine, it's optional)" -ForegroundColor DarkGray
} else {
    Write-Ok "Plan: Install WebUI into existing environment"
}

# =============================================================================
# Venv setup (CLI install only -- skipped for WebUI-only mode)
# =============================================================================
$VenvPath = $Venv

if ($installCLI) {

    # -- Step 3: Venv location ------------------------------------------------
    Write-Step "Choosing a virtual environment"
    Write-Host ""

    if ($VenvPath -eq "") {
        Write-Info "Atlas will be installed in a virtual environment -- a self-contained"
        Write-Info "folder that keeps it separate from the rest of your system."
        Write-Host ""
        Write-Info "Suggested location: $defaultVenv"
        Write-Host ""
        $venvInput = Read-Host "   Press Enter to use that path, or type a different one"
        if ($venvInput -eq "") { $VenvPath = $defaultVenv } else { $VenvPath = $venvInput }
    }

    if (Test-Path $VenvPath) {
        Write-Host ""
        Write-Warn "A virtual environment already exists at $VenvPath"
        $curVer = Get-AtlasVersion $VenvPath
        if ($curVer -ne "") {
            Write-Host "   Currently installed: $curVer" -ForegroundColor DarkGray
        }
        $overwrite = Read-Host "   Reinstall / upgrade into it? [Y/n]"
        if ($overwrite -eq "") { $overwrite = "Y" }
        if ($overwrite -notmatch "^[Yy]") {
            Write-Info "No changes made. Goodbye!"; exit 0
        }
        Write-Ok "Will upgrade the existing environment."
    } else {
        Write-Ok "Will create environment at: $VenvPath"
    }

    # -- Step 4: Create venv --------------------------------------------------
    Write-Step "Creating the virtual environment"

    if (-not (Test-Path $VenvPath)) {
        & $pyExe @pyArgs -m venv $VenvPath
        Write-Ok "Virtual environment created at $VenvPath"
    } else {
        Write-Ok "Using existing environment at $VenvPath"
    }

    $pipExe   = Join-Path $VenvPath "Scripts\pip.exe"
    $atlasBin = Join-Path $VenvPath "Scripts\platform-atlas.exe"

    & $pipExe install --quiet --upgrade pip

    # -- Step 5: Install CLI --------------------------------------------------
    Write-Step "Installing Platform Atlas CLI"
    Write-Host ""

    & $pipExe install --force-reinstall --no-compile $cliWheelPath

    if (Test-Path $atlasBin) {
        $installedVer = (& $atlasBin --version 2>$null) | Select-Object -First 1
        Write-Ok "Installed: $installedVer"
    } else {
        Write-Fail "Something went wrong - 'platform-atlas' was not found after install"
        exit 1
    }

} else {
    # WebUI-only: use existing venv (--venv overrides the detected one)
    if ($VenvPath -ne "") {
        # user passed -Venv explicitly
    } else {
        $VenvPath = $existingCLIVenv
    }
    $pipExe   = Join-Path $VenvPath "Scripts\pip.exe"
    $atlasBin = Join-Path $VenvPath "Scripts\platform-atlas.exe"
}

$webuiBin = Join-Path $VenvPath "Scripts\platform-atlas-webui.exe"

# -- Step: Install WebUI (if applicable) -------------------------------------
if ($installWebUI) {
    Write-Step "Installing Platform Atlas WebUI"
    Write-Host ""

    # Build --find-links args so pip can resolve platform-atlas from local WHL (not PyPI)
    $_flArgs = @()
    $_webuiDir = Split-Path ([System.IO.Path]::GetFullPath($webuiWheelPath)) -Parent
    $_flArgs += "--find-links"; $_flArgs += $_webuiDir
    if ($cliWheelPath -ne "") {
        $_cliDir = Split-Path ([System.IO.Path]::GetFullPath($cliWheelPath)) -Parent
        if ($_cliDir -ne $_webuiDir) { $_flArgs += "--find-links"; $_flArgs += $_cliDir }
    }
    & $pipExe install --force-reinstall --no-compile @_flArgs $webuiWheelPath

    if (Test-Path $webuiBin) {
        # Use pip show instead of running the binary to avoid NativeCommandError across PS versions
        $_verLine = (& $pipExe show platform-atlas-webui 2>$null | Select-String "^Version:") | Select-Object -First 1
        if ($_verLine) {
            $wuiVer = "platform-atlas-webui " + (($_verLine.Line -split "\s+")[1])
        } else {
            $wuiVer = "platform-atlas-webui"
        }
        Write-Ok "Installed: $wuiVer"
        Write-Ok "Launch the WebUI with: platform-atlas-webui"
    } else {
        Write-Fail "'platform-atlas-webui' was not found after install - something went wrong"
        exit 1
    }
}

# =============================================================================
# PATH setup (skip for WebUI-only -- CLI already added the venv to PATH)
# =============================================================================
if ($installCLI) {

    Write-Step "Adding platform-atlas to your PATH"
    Write-Host ""
    Write-Info "To use platform-atlas without activating the virtual environment"
    Write-Info "every time, we can add it to your user PATH."
    Write-Host ""

    $scriptsDir = Join-Path $VenvPath "Scripts"
    $userPath   = [Environment]::GetEnvironmentVariable("PATH", "User")
    if ($userPath -eq $null) { $userPath = "" }

    if ($userPath -like "*$scriptsDir*") {
        Write-Ok "$scriptsDir is already in your user PATH."
    } else {
        Write-Info "This will add the following to your user PATH (your account only):"
        Write-Host ""
        Write-Host "    $scriptsDir" -ForegroundColor Cyan
        Write-Host ""
        $pathChoice = Read-Host "   Add it now? [Y/n]"
        if ($pathChoice -eq "") { $pathChoice = "Y" }
        if ($pathChoice -match "^[Yy]") {
            if ($userPath -eq "") { $newPath = $scriptsDir } else { $newPath = "$userPath;$scriptsDir" }
            [Environment]::SetEnvironmentVariable("PATH", $newPath, "User")
            Write-Ok "Added to your user PATH."
            Write-Warn "Open a new terminal window for the change to take effect."
        } else {
            Write-Host ""
            Write-Info "No problem. Activate the environment manually when you need it:"
            Write-Host ""
            Write-Host "    $VenvPath\Scripts\Activate.ps1" -ForegroundColor Cyan
            Write-Host ""
            Write-Info "(then platform-atlas will be available for that terminal session)"
        }
    }
}

# =============================================================================
# Done!
# =============================================================================
Write-Host ""
Write-Host ""
Write-Host "  +========================================================+" -ForegroundColor Green
Write-Host "  |                                                        |" -ForegroundColor Green
if ($installCLI -and $installWebUI) {
    Write-Host "  |    Platform Atlas  --  CLI + WebUI installed!          |" -ForegroundColor Green
} elseif ($installCLI) {
    Write-Host "  |       Platform Atlas CLI  --  installed!               |" -ForegroundColor Green
} else {
    Write-Host "  |      Platform Atlas WebUI  --  installed!              |" -ForegroundColor Green
}
Write-Host "  |                                                        |" -ForegroundColor Green
Write-Host "  +========================================================+" -ForegroundColor Green
Write-Host ""

if ($installCLI) {
    Write-Host "  Next steps:"
    Write-Host ""
    Write-Host "  1. " -NoNewline
    Write-Host "Open a new terminal" -ForegroundColor White -NoNewline
    Write-Host " (so the PATH change takes effect)"
    Write-Host "     Verify with: " -NoNewline
    Write-Host "platform-atlas --version" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  2. " -NoNewline
    Write-Host "Run the setup wizard" -ForegroundColor White -NoNewline
    Write-Host " to configure your first environment:"
    Write-Host "     " -NoNewline
    Write-Host "platform-atlas config init" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  3. Create a session and run your first audit:"
    Write-Host "     " -NoNewline
    Write-Host "platform-atlas session create my-audit" -ForegroundColor Cyan
    Write-Host "     " -NoNewline
    Write-Host "platform-atlas session run all" -ForegroundColor Cyan
    if ($installWebUI) {
        Write-Host ""
        Write-Host "  4. Launch the WebUI for a visual view of your results:"
        Write-Host "     " -NoNewline
        Write-Host "platform-atlas-webui" -ForegroundColor Cyan
    }
} else {
    Write-Host ""
    Write-Host "  The WebUI is ready. Launch it with:"
    Write-Host "     " -NoNewline
    Write-Host "platform-atlas-webui" -ForegroundColor Cyan
}

Write-Host ""
Write-Host "  Questions? Reach out to your Itential Customer Success contact." -ForegroundColor DarkGray
Write-Host ""
