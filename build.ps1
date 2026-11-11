param([string]$Python = 'python', [string]$Destination = (Join-Path $PSScriptRoot 'dist'))
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    & $Python -m PyInstaller --noconfirm --clean --windowed --onedir --name 'ABI Sound Scout' --icon (Join-Path $PSScriptRoot 'scout.ico') --collect-all soundfile --collect-all pyaudiowpatch --exclude-module scipy --exclude-module matplotlib --exclude-module pytest --distpath $Destination --workpath (Join-Path $Destination '.build') --specpath $Destination app.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
    $release = Join-Path $Destination 'ABI Sound Scout'
    Copy-Item -LiteralPath 'data' -Destination $release -Recurse -Force
    $noticeDir = Join-Path $release 'licenses'
    New-Item -ItemType Directory -Path $noticeDir -Force | Out-Null
    Get-ChildItem -LiteralPath 'licenses' | Where-Object Name -NE 'scipy' | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $noticeDir -Recurse -Force
    }
    Copy-Item -LiteralPath 'README_RU.txt', 'THIRD_PARTY_NOTICES.txt', 'LOCAL_GAME_INSPECTION.txt', 'scout.ico' -Destination $release -Force
    if (Test-Path -LiteralPath 'README.md') { Copy-Item -LiteralPath 'README.md' -Destination $release -Force }
    Write-Output "Ready: $release"
} finally {
    Pop-Location
}
