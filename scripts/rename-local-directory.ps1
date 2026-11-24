# Rename D:\endd to D:\MarketScope after closing the processes that use the
# directory (Claude Code, terminals, Python workers).
#
# Run from any PowerShell window that is NOT inside D:\endd:
#   powershell -ExecutionPolicy Bypass -File D:\endd\scripts\rename-local-directory.ps1

param(
    [string]$OldPath = 'D:\endd',
    [string]$NewPath = 'D:\MarketScope'
)

$ErrorActionPreference = 'Stop'
$oldFull = [System.IO.Path]::GetFullPath($OldPath).TrimEnd('\')
$newFull = [System.IO.Path]::GetFullPath($NewPath).TrimEnd('\')
$newName = Split-Path $newFull -Leaf

if (-not (Test-Path -LiteralPath $oldFull)) {
    if (Test-Path -LiteralPath $newFull) {
        Write-Host "Already renamed: $newFull"
        exit 0
    }
    throw "Source directory does not exist: $oldFull"
}
if (Test-Path -LiteralPath $newFull) {
    throw "Target already exists: $newFull"
}

# Leave the source directory before trying the rename.
Set-Location -LiteralPath (Split-Path $oldFull -Parent)

$renamed = $false
for ($attempt = 1; $attempt -le 30; $attempt++) {
    try {
        Rename-Item -LiteralPath $oldFull -NewName $newName -ErrorAction Stop
        $renamed = $true
        break
    } catch {
        Write-Host "Attempt $attempt/30 failed; close Claude Code/terminals/Python workers in $oldFull and wait..."
        Start-Sleep -Seconds 2
    }
}

if (-not $renamed) {
    throw @"
Could not rename $oldFull because a process still uses it.
Close Claude Code, this terminal, uvicorn/worker processes, and any Explorer window
inside $oldFull, then run this script again from a PowerShell window outside it.
"@
}

Set-Location -LiteralPath $newFull
Write-Host "Renamed $oldFull -> $newFull"

$venvPython = Join-Path $newFull '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython) {
    Write-Host 'Repairing editable package metadata...'
    & $venvPython -m pip uninstall -y market-intel-backend 2>$null | Out-Null
    & $venvPython -m pip install -e $newFull --no-deps
    if ($LASTEXITCODE -ne 0) {
        Write-Warning 'Editable reinstall failed; run from the new directory:'
        Write-Host "  $venvPython -m pip install -e $newFull --no-deps"
    }
}

Write-Host ''
Write-Host 'Next steps:'
Write-Host "  1. Open a new terminal in $newFull"
Write-Host '  2. Create/update your new database or keep the old DB values in .env'
Write-Host '  3. Run: pytest -q'
Write-Host '  4. Rename the GitHub repository separately if desired.'
