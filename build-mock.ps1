[CmdletBinding()]
param(
    [string]$Python = (Join-Path $env:LOCALAPPDATA 'Python/bin/python.exe'),
    [switch]$Product
)

$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    $build = '.qa-build'
    $name = if ($Product) { 'UnrealAutoQA' } else { 'UnrealAutoQA-Mock' }
    $tools = Join-Path $build 'tools'
    if (-not (Test-Path -LiteralPath (Join-Path $tools 'PyInstaller/__main__.py'))) {
        & $Python -B -m pip --disable-pip-version-check --isolated install --index-url https://pypi.org/simple --only-binary=:all: --target $tools 'PyInstaller==6.22.3'
        if ($LASTEXITCODE -ne 0) { throw 'PyInstaller installation failed.' }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $tools 'yaml/__init__.py'))) {
        & $Python -B -m pip --disable-pip-version-check --isolated install --index-url https://pypi.org/simple --only-binary=:all: --target $tools 'PyYAML==6.0.3'
        if ($LASTEXITCODE -ne 0) { throw 'PyYAML installation failed.' }
    }
    $previousPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = (Resolve-Path -LiteralPath $tools).Path
        $data = @('--add-data', "${PSScriptRoot}/qa_panel.html:.",
                  '--add-data', "${PSScriptRoot}/spikes/check_environment.ps1:spikes")
        if ($Product) { $data += @('--add-data', "${PSScriptRoot}/plugins/GameQABridge:plugins/GameQABridge") }
        & $Python -B -m PyInstaller --onedir --noconfirm --name $name --specpath $build --workpath "$build/work" --distpath "$build/dist" @data qa.py
        if ($LASTEXITCODE -ne 0) { throw 'QA bundle build failed.' }
    } finally {
        $env:PYTHONPATH = $previousPythonPath
    }
    $bundle = Join-Path $build "dist/$name"
    Copy-Item -LiteralPath 'README.md' -Destination $bundle
    @(
        '@echo off'
        ('"%~dp0{0}.exe" register-key' -f $name)
        'pause'
    ) | Set-Content -LiteralPath (Join-Path $bundle 'Register-OpenAI-Key.cmd') -Encoding Ascii
    if ($Product) {
        Copy-Item -LiteralPath 'docs/g2-bridge.md' -Destination $bundle
        Copy-Item -LiteralPath 'build-game.ps1' -Destination $bundle
        Compress-Archive -LiteralPath $bundle -DestinationPath 'UnrealAutoQA-win-x64.zip' -Force
    } else {
        Copy-Item -LiteralPath 'examples' -Destination $bundle -Recurse
        Compress-Archive -LiteralPath $bundle -DestinationPath 'UnrealAutoQA-Mock-win-x64.zip' -Force
    }
} finally {
    Pop-Location
}
