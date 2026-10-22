# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
#
# Installs the plug-in into the per-user GIMP 3 profile on Windows and writes a
# default config if none exists. Run from the repository root:
#
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-GimpVersion 3.2] [-ProfileDir <dir>]
#              [-ComfyDir <folder of an existing ComfyUI>] [-Link]
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall [-RemoveConfig] [-ProfileDir <dir>]
#   powershell -ExecutionPolicy Bypass -File install.ps1 -ListModels | -Model <id> [-ComfyDir <dir>]
#   powershell -ExecutionPolicy Bypass -File install.ps1 -DetectComfy
#
# Most people just double-click "Install OpenFill.cmd". If no ComfyUI is found on this PC, the
# plug-in offers to download one the first time you run Generative Fill.
#
# -Link         creates a directory junction to the repo instead of copying (handy for development).
# -ProfileDir   the GIMP profile folder itself (default: $env:GIMP3_DIRECTORY, else the newest
#               %APPDATA%\GIMP\3.x). Use it to install into an isolated test profile.
# -Uninstall    removes the plug-in folder or junction. The settings folder (<profile>\openfill,
#               which holds config.json and the Hugging Face token) is kept unless -RemoveConfig.
# -Model        downloads a catalog model with curl and verifies its sha256. A gated model needs a
#               Hugging Face token: set $env:HF_TOKEN (preferred) or pass -HfToken.

[CmdletBinding()]
param(
    [string]$GimpVersion = "",
    [string]$ProfileDir = "",
    [string]$ComfyDir = "",
    [switch]$Link,
    [switch]$Uninstall,
    [switch]$RemoveConfig,
    [switch]$ListModels,
    [switch]$DetectComfy,
    [string]$Model = "",
    [string]$HfToken = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $repo "gimp-openfill"
$name = "gimp-openfill"
$catalogPath = Join-Path $repo "models\catalog.json"
$setupPath = Join-Path $repo "models\comfy_setup.json"
function Get-ConfiguredComfy {
    # comfy_dir from the OpenFill config of the profile this script would install into, if any.
    $dirs = @()
    if ($ProfileDir) { $dirs += $ProfileDir } elseif ($env:GIMP3_DIRECTORY) { $dirs += $env:GIMP3_DIRECTORY }
    else {
        $dirs += @(Get-ChildItem -LiteralPath (Join-Path $env:APPDATA "GIMP") -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^3\.\d+$' } | Sort-Object { [version]$_.Name } -Descending | ForEach-Object { $_.FullName })
    }
    foreach ($d in $dirs) {
        $cfg = Read-JsonQuiet (Join-Path $d "openfill\config.json")
        if ($cfg -and $cfg.comfy_dir -and (Test-ComfyFolder $cfg.comfy_dir)) { return [string]$cfg.comfy_dir }
    }
    return $null
}

function Fail([string]$Message) {
    [Console]::Error.WriteLine("error: $Message")
    exit 1
}

function Get-Catalog {
    (Get-Content -Raw -Encoding UTF8 $catalogPath | ConvertFrom-Json).models
}

function Test-ComfyFolder([string]$Dir) {
    # The layouts the plug-in can start: the portable build, or a checkout with a venv.
    if (-not $Dir -or -not (Test-Path -LiteralPath $Dir -PathType Container)) { return $false }
    if ((Test-Path -LiteralPath (Join-Path $Dir "python_embeded\python.exe")) -and
        (Test-Path -LiteralPath (Join-Path $Dir "ComfyUI\main.py"))) { return $true }
    if (Test-Path -LiteralPath (Join-Path $Dir "main.py")) {
        foreach ($v in "venv", ".venv", "env") {
            if (Test-Path -LiteralPath (Join-Path $Dir "$v\Scripts\python.exe")) { return $true }
        }
    }
    return $false
}

function Read-JsonQuiet([string]$Path) {
    try { return (Get-Content -Raw -Encoding UTF8 -LiteralPath $Path | ConvertFrom-Json) } catch { return $null }
}

function Find-Comfy {
    # Mirrors comfy_setup.detect_comfy in the plug-in: returns objects with Path and Source, best first.
    $homeDir = $env:USERPROFILE
    $appData = if ($env:APPDATA) { $env:APPDATA } else { Join-Path $homeDir "AppData\Roaming" }
    $local = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $homeDir "AppData\Local" }
    $list = New-Object System.Collections.ArrayList
    function Add-Cand($p, $src) { [void]$list.Add([pscustomobject]@{ Path = $p; Source = $src }) }
    Add-Cand (Join-Path $local "OpenFill\ComfyUI\ComfyUI_windows_portable") "installed by OpenFill"
    $fixed = if ($env:OPENFILL_NO_FIXED_ROOTS) { @() } else { @("C:\AI\ComfyUI", "C:\", "C:\ComfyUI") }
    foreach ($r in ($fixed + @($homeDir, (Join-Path $homeDir "ComfyUI")))) { Add-Cand (Join-Path $r "ComfyUI_windows_portable") "portable folder" }
    foreach ($f in "Downloads", "Desktop", "Documents") {
        Add-Cand (Join-Path $homeDir "$f\ComfyUI_windows_portable") "portable folder"
        Add-Cand (Join-Path $homeDir "$f\ComfyUI") "portable folder"
    }
    Add-Cand (Join-Path $homeDir "ComfyUI") "ComfyUI folder"
    $desk = Read-JsonQuiet (Join-Path $appData "ComfyUI\config.json")
    if ($desk -and $desk.basePath) { Add-Cand ([string]$desk.basePath) "ComfyUI Desktop" }
    $libs = @((Join-Path $appData "StabilityMatrix"), (Join-Path $local "StabilityMatrix"))
    foreach ($sf in "library.json", "settings.json") {
        $sm = Read-JsonQuiet (Join-Path $appData "StabilityMatrix\$sf")
        if ($sm) {
            $lp = if ($sm.LibraryPath) { $sm.LibraryPath } else { $sm.libraryPath }
            if ($lp) { $libs = @([string]$lp) + $libs }
        }
    }
    foreach ($l in $libs) {
        Add-Cand (Join-Path $l "Data\Packages\ComfyUI") "Stability Matrix"
        Add-Cand (Join-Path $l "Packages\ComfyUI") "Stability Matrix"
    }
    $pino = @((Join-Path $homeDir "pinokio"))
    if (-not $env:OPENFILL_NO_FIXED_ROOTS) { $pino += "C:\pinokio" }
    foreach ($r in $pino) { Add-Cand (Join-Path $r "api\comfy.git\app") "Pinokio" }
    $seen = @{}
    foreach ($c in $list) {
        foreach ($p in @($c.Path, (Join-Path $c.Path "ComfyUI_windows_portable"))) {
            $key = $p.ToLowerInvariant()
            if ($seen.ContainsKey($key)) { continue }
            if (Test-ComfyFolder $p) { $seen[$key] = $true; [pscustomobject]@{ Path = $p; Source = $c.Source } }
        }
    }
}

function Get-ZipComment([string]$Path) {
    # The archive GitHub builds for a commit carries that commit id as its zip comment.
    $fs = [IO.File]::OpenRead($Path)
    try {
        $len = [math]::Min($fs.Length, 70000)
        $buf = New-Object byte[] $len
        [void]$fs.Seek(-$len, [IO.SeekOrigin]::End)
        [void]$fs.Read($buf, 0, $len)
    } finally { $fs.Dispose() }
    for ($i = $len - 22; $i -ge 0; $i--) {
        if ($buf[$i] -eq 0x50 -and $buf[$i + 1] -eq 0x4b -and $buf[$i + 2] -eq 5 -and $buf[$i + 3] -eq 6) {
            $n = $buf[$i + 20] + 256 * $buf[$i + 21]
            return [Text.Encoding]::ASCII.GetString($buf, $i + 22, $n)
        }
    }
    return ""
}

function Install-NodeZip([string]$Repo, [string]$Commit, [string]$Folder, [string]$NodesDir) {
    # No git needed: the GitHub zip of the pinned commit, checked against the pin, unpacked and moved into place.
    $zip = Join-Path $NodesDir ".$Folder.zip"
    $work = Join-Path $NodesDir ".$Folder.unpack"
    New-Item -ItemType Directory -Force $NodesDir | Out-Null
    if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
    Write-Host "   download $Repo/archive/$Commit.zip"
    & curl.exe -sS -L --retry 5 --retry-delay 5 --fail -o $zip "$Repo/archive/$Commit.zip"
    if ($LASTEXITCODE -ne 0) { Fail "download failed for $Repo (curl exit $LASTEXITCODE)" }
    try {
        $comment = (Get-ZipComment $zip).Trim()
        if ($comment -ne $Commit) { Fail "the archive for $Folder is for commit '$comment', expected $Commit" }
        Expand-Archive -LiteralPath $zip -DestinationPath $work -Force
        $inner = @(Get-ChildItem -LiteralPath $work -Directory)
        if ($inner.Count -ne 1) { Fail "the archive for $Folder does not have a single top folder" }
        Move-Item -LiteralPath $inner[0].FullName -Destination (Join-Path $NodesDir $Folder)
        [IO.File]::WriteAllText((Join-Path (Join-Path $NodesDir $Folder) ".openfill-commit"), "$Commit`n")
    } finally {
        Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

function Get-ModelsRoot([string]$Dir) {
    if (-not $Dir) { $Dir = Get-ConfiguredComfy }
    if (-not $Dir) { $Dir = (Find-Comfy | Select-Object -First 1).Path }
    if (-not $Dir) { Fail "No ComfyUI folder given or found. Pass -ComfyDir <folder>, or run Generative Fill in GIMP and press Download ComfyUI." }
    if (Test-Path -LiteralPath (Join-Path $Dir "ComfyUI\models")) { return (Join-Path $Dir "ComfyUI\models") }
    return (Join-Path $Dir "models")
}

function Assert-SafeDest([string]$Dest) {
    # Catalog destinations are "<subfolder>/<file>" made of plain names only.
    foreach ($segment in ($Dest -split '[\\/]')) {
        if ($segment -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.+-]*$' -or $segment -eq '..') {
            Fail "unsafe destination in the catalog: '$Dest'"
        }
    }
}

function Test-ModelInstalled($Entry, [string]$Root) {
    foreach ($f in $Entry.files) {
        $p = Join-Path $Root $f.dest
        if (-not (Test-Path -LiteralPath $p)) { return $false }
        if ($f.size -and (Get-Item -LiteralPath $p).Length -ne $f.size) { return $false }
    }
    return $true
}

function Remove-PluginTarget([string]$Path) {
    # A junction is removed as a link only; Remove-Item -Recurse on a junction can
    # delete the files of the repository it points to on Windows PowerShell 5.1.
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        $item.Delete()
        return "junction"
    }
    Remove-Item -LiteralPath $Path -Recurse -Force
    return "folder"
}

function Find-GimpExe {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\GIMP 3\bin"),
        (Join-Path $env:ProgramFiles "GIMP 3\bin")
    )
    foreach ($dir in $candidates) {
        if (Test-Path -LiteralPath $dir) {
            $exe = Get-ChildItem -LiteralPath $dir -Filter "gimp-3*.exe" -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -notmatch 'console' } | Select-Object -First 1
            if ($exe) { return $exe.FullName }
        }
    }
    return $null
}

# ---- headless model management (same catalog as the Models tab) ----
if ($DetectComfy) {
    $found = @(Find-Comfy)
    if ($found.Count -eq 0) { Write-Host "No ComfyUI found on this PC." }
    foreach ($c in $found) { "{0}  [{1}]" -f $c.Path, $c.Source }
    exit 0
}

if ($ListModels) {
    $root = Get-ModelsRoot $ComfyDir
    foreach ($e in Get-Catalog) {
        $state = if (Test-ModelInstalled $e $root) { "Installed" } elseif ($e.gated) { "Needs token" } else { "Available" }
        $gb = [math]::Round((($e.files | Measure-Object -Property size -Sum).Sum / 1GB), 2)
        "{0,-30} {1,-9} {2,-12} {3,7} GB  {4}  [{5}]" -f $e.id, $e.tier, $state, $gb, $e.license.name, $e.source_type
    }
    exit 0
}

if ($Model) {
    if (-not $HfToken) { $HfToken = $env:HF_TOKEN }
    $root = Get-ModelsRoot $ComfyDir
    $catalog = Get-Catalog
    $entry = $catalog | Where-Object { $_.id -eq $Model }
    if (-not $entry) { Fail "Unknown model id '$Model'. Use -ListModels." }
    $queue = @()
    foreach ($dep in $entry.requires_models) { $queue += ($catalog | Where-Object { $_.id -eq $dep }) }
    $queue += $entry
    foreach ($m in $queue) {
        Write-Host "== $($m.id): $($m.name)"
        Write-Host "   License: $($m.license.name) - $($m.license.url)"
        foreach ($f in $m.files) {
            Assert-SafeDest $f.dest
            if ($f.url -notmatch '^https://') { Fail "catalog URL is not https: $($f.url)" }
            $dest = Join-Path $root $f.dest
            New-Item -ItemType Directory -Force (Split-Path -Parent $dest) | Out-Null
            if ((Test-Path -LiteralPath $dest) -and (-not $f.size -or (Get-Item -LiteralPath $dest).Length -eq $f.size)) {
                Write-Host "   present  $($f.dest)"; continue
            }
            if ($f.optional -and (Test-Path -LiteralPath $dest)) { continue }
            $useToken = $false
            if ($f.gated -or $m.gated) {
                if (-not $HfToken) { Fail "$($f.dest) is gated. Accept the license at $($m.page) and set `$env:HF_TOKEN (huggingface.co/settings/tokens)." }
                $useToken = ([Uri]$f.url).Host -match '(^|\.)(huggingface\.co|hf\.co)$'
            }
            $part = "$dest.part"
            Write-Host "   download $($f.url)"
            if ($useToken) {
                # The header goes to curl on stdin, so the token never appears in the process list.
                # curl does not resend a custom Authorization header to another host after a redirect.
                "Authorization: Bearer $HfToken" | & curl.exe -sS -L -C - --retry 5 --retry-delay 5 --fail -H "@-" -o $part $f.url
            } else {
                & curl.exe -sS -L -C - --retry 5 --retry-delay 5 --fail -o $part $f.url
            }
            if ($LASTEXITCODE -ne 0) { Fail "download failed for $($f.url) (curl exit $LASTEXITCODE); the .part file is kept for resume" }
            if ($f.size -and (Get-Item -LiteralPath $part).Length -ne $f.size) {
                Remove-Item -LiteralPath $part -Force
                Fail "size mismatch for $($f.dest); the partial file was deleted, re-run to download again"
            }
            if ($f.sha256) {
                $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $part).Hash.ToLower()
                if ($hash -ne $f.sha256) { Remove-Item -LiteralPath $part -Force; Fail "sha256 mismatch for $($f.dest): $hash" }
                Write-Host "   sha256 ok"
            }
            Move-Item -Force -LiteralPath $part -Destination $dest
        }
        foreach ($n in $m.requires_nodes) {
            if ($n.repo -notmatch '^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' -or
                $n.commit -notmatch '^[0-9a-f]{40}$' -or $n.folder -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.+-]*$') {
                Fail "catalog node entry is not a pinned GitHub repository: $($n.repo) $($n.folder) $($n.commit)"
            }
            $nodes = Join-Path (Split-Path -Parent $root) "custom_nodes"
            $target = Join-Path $nodes $n.folder
            if (-not (Test-Path -LiteralPath $target)) {
                Install-NodeZip $n.repo $n.commit $n.folder $nodes
                $py = Join-Path (Split-Path -Parent (Split-Path -Parent $root)) "python_embeded\python.exe"
                if ((Test-Path -LiteralPath (Join-Path $target "requirements.txt")) -and (Test-Path -LiteralPath $py)) {
                    & $py -s -m pip install --no-warn-script-location -r (Join-Path $target "requirements.txt")
                    if ($LASTEXITCODE -ne 0) { Fail "pip install failed for $($n.folder)" }
                }
            }
        }
    }
    Write-Host "Done. Select the model in Filters > OpenFill > Generative Fill Settings > Models (Set active)."
    exit 0
}

# ---- locate the GIMP 3 profile ----
if (-not $ProfileDir -and $env:GIMP3_DIRECTORY) { $ProfileDir = $env:GIMP3_DIRECTORY }
if ($ProfileDir) {
    $profileDir = $ProfileDir
} else {
    $base = Join-Path $env:APPDATA "GIMP"
    if (-not $GimpVersion) {
        $candidates = @(Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^3\.\d+$' } | Sort-Object { [version]$_.Name } -Descending)
        if ($candidates.Count -eq 0) {
            if ($Uninstall) { Write-Host "No GIMP 3.x profile under $base; nothing to uninstall."; exit 0 }
            $exe = Find-GimpExe
            if (-not $exe) {
                Fail "GIMP 3 does not seem to be installed. Install GIMP 3.2 from https://www.gimp.org/downloads/, then double-click Install OpenFill.cmd again."
            }
            # GIMP creates its profile folder the first time it starts; make it now so GIMP need not run first.
            $GimpVersion = if ((Split-Path -Leaf $exe) -match 'gimp-(3\.\d+)') { $Matches[1] } else { "3.2" }
            Write-Host "GIMP $GimpVersion has not been started yet; creating its profile folder."
        } else {
            $GimpVersion = $candidates[0].Name
        }
    }
    $profileDir = Join-Path $base $GimpVersion
}
$pluginsDir = Join-Path $profileDir "plug-ins"
$target = Join-Path $pluginsDir $name
$configDir = Join-Path $profileDir "openfill"
$configFile = Join-Path $configDir "config.json"
Write-Host "GIMP profile: $profileDir"

if (Get-Process -Name "gimp-3*" -ErrorAction SilentlyContinue) {
    Write-Warning "A GIMP process is running. Changes take effect the next time GIMP starts."
}

if ($Uninstall) {
    if (Test-Path -LiteralPath $target) {
        $kind = Remove-PluginTarget $target
        Write-Host "Removed plug-in $kind $target"
    } else {
        Write-Host "No plug-in installed at $target"
    }
    if ($RemoveConfig) {
        if (Test-Path -LiteralPath $configDir) {
            Remove-Item -LiteralPath $configDir -Recurse -Force
            Write-Host "Removed settings folder $configDir (config.json, logs, task queue)"
        }
    } elseif (Test-Path -LiteralPath $configDir) {
        Write-Host "Kept settings folder $configDir (holds config.json and the Hugging Face token; use -RemoveConfig to delete it)"
    }
    Write-Host "Done. Restart GIMP; the Filters > OpenFill entries are gone. Models in the ComfyUI folder are not touched."
    exit 0
}

if (-not (Test-Path -LiteralPath (Join-Path $source "$name.py"))) { Fail "plug-in source not found at $source" }
New-Item -ItemType Directory -Force $pluginsDir | Out-Null
if (Test-Path -LiteralPath $target) {
    $kind = Remove-PluginTarget $target
    Write-Host "Replacing the existing plug-in $kind at $target"
}
if ($Link) {
    New-Item -ItemType Junction -Path $target -Target $source | Out-Null
    Write-Host "Linked $target -> $source"
} else {
    Copy-Item -LiteralPath $source -Destination $target -Recurse -Force
    Get-ChildItem -LiteralPath $target -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
    New-Item -ItemType Directory -Force (Join-Path $target "models") | Out-Null
    Copy-Item -LiteralPath $catalogPath -Destination (Join-Path $target "models\catalog.json") -Force
    Copy-Item -LiteralPath $setupPath -Destination (Join-Path $target "models\comfy_setup.json") -Force
    Write-Host "Copied plug-in (with its model catalog and ComfyUI setup list) to $target"
}

# Default config (only if missing). comfy_dir is written only when a ComfyUI is given or found;
# otherwise the plug-in offers to download one the first time you run Generative Fill.
New-Item -ItemType Directory -Force $configDir | Out-Null
if (-not (Test-Path -LiteralPath $configFile)) {
    $found = $null
    if (-not $ComfyDir) {
        $found = Find-Comfy | Select-Object -First 1
        if ($found) { $ComfyDir = $found.Path; Write-Host "Found ComfyUI ($($found.Source)): $ComfyDir" }
    }
    $config = [ordered]@{
        backend = "comfyui"
        url = "http://127.0.0.1:8188"
        autostart = $true
        idle_timeout_minutes = 20
    }
    if ($ComfyDir) { $config["comfy_dir"] = $ComfyDir }
    # UTF-8 without BOM (Set-Content -Encoding UTF8 adds one on Windows PowerShell).
    [IO.File]::WriteAllText($configFile, (($config | ConvertTo-Json) + "`n"), (New-Object Text.UTF8Encoding $false))
    Write-Host "Wrote default config to $configFile"
} else {
    Write-Host "Keeping existing config at $configFile"
    if (-not $ComfyDir) {
        try { $ComfyDir = (Get-Content -Raw -LiteralPath $configFile | ConvertFrom-Json).comfy_dir } catch { $ComfyDir = "" }
    }
}

if (-not (Find-GimpExe)) {
    Write-Warning "GIMP 3 was not found in the usual install folders. Install it from https://www.gimp.org/downloads/."
}
$haveComfy = $ComfyDir -and (Test-ComfyFolder $ComfyDir)
if ($ComfyDir -and -not $haveComfy) {
    Write-Warning "No ComfyUI was found at $ComfyDir. The plug-in will offer to download one."
}
Write-Host ""
Write-Host "OpenFill is installed. Next steps:"
Write-Host "  1. Start (or restart) GIMP; plug-ins are registered at start-up."
Write-Host "  2. Open an image, select an area and run Filters > OpenFill > Generative Fill..."
if ($haveComfy) {
    Write-Host "  3. The first time, the model manager opens: download LaMa (object removal) and DreamShaper 8 Inpainting"
    Write-Host "     (fill and expand), about 2.3 GB together, then run Generative Fill again."
} else {
    Write-Host "  3. ComfyUI is required and was not found: press Download ComfyUI when asked (about 2 GB, checked against a"
    Write-Host "     published checksum), then download LaMa and DreamShaper 8 Inpainting (about 2.3 GB) in the model manager."
}
Write-Host "To remove the plug-in later: install.ps1 -Uninstall (add -RemoveConfig to delete the settings too)."
