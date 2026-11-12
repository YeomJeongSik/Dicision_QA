[CmdletBinding(DefaultParameterSetName = 'Test')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Test')][string]$Objective,
    [Parameter(ParameterSetName = 'Test')][string[]]$Check,
    [Parameter(ParameterSetName = 'Test')][ValidateRange(1, 20)][int]$MaxCalls,
    [Parameter(Mandatory, ParameterSetName = 'List')][switch]$ListActions,
    [Parameter(ParameterSetName = 'List')][switch]$RefreshActions,
    [Parameter(ParameterSetName = 'List')]
    [Parameter(ParameterSetName = 'Test')][switch]$Rebuild,
    [Parameter(Mandatory, ParameterSetName = 'Register')][switch]$RegisterKey
)

$ErrorActionPreference = 'Stop'
$projects = @(Get-ChildItem -LiteralPath (Split-Path -Parent $PSScriptRoot) -Filter '*.uproject' -File)
if ($projects.Count -ne 1) {
    throw 'Place the tool folder directly inside the folder containing exactly one .uproject.'
}
$project = $projects[0]
$association = (Get-Content -LiteralPath $project.FullName -Raw | ConvertFrom-Json).EngineAssociation
$custom = Get-ItemProperty -LiteralPath 'HKCU:\Software\Epic Games\Unreal Engine\Builds' -ErrorAction SilentlyContinue
$installed = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\EpicGames\Unreal Engine\5.9' -ErrorAction SilentlyContinue
$roots = @()
if ($custom -and $association) { $roots += $custom.PSObject.Properties[[string]$association].Value }
$roots += $installed.InstalledDirectory
$roots += Join-Path $env:ProgramFiles 'Epic Games/UE_5.9'
$engine = $roots | Where-Object {
    $_ -and (Test-Path -LiteralPath (Join-Path $_ 'Engine/Build/Build.version') -PathType Leaf)
} | Select-Object -First 1
if (!$engine) { throw 'The installed Unreal Engine 5.9.0 could not be located.' }
$version = Get-Content -LiteralPath (Join-Path $engine 'Engine/Build/Build.version') -Raw | ConvertFrom-Json
if ("$($version.MajorVersion).$($version.MinorVersion).$($version.PatchVersion)" -ne '5.9.0' -or
    ($association -match '^\d+\.\d+$' -and $association -ne '5.9')) {
    throw 'This version of the QA bridge requires an Unreal Engine 5.9.0 project.'
}
$python = Join-Path $engine 'Engine/Binaries/ThirdParty/Python3/Win64/python.exe'
$controller = Join-Path $PSScriptRoot 'qa.py'

if ($RegisterKey) {
    & $python -B $controller register-key
    exit $LASTEXITCODE
}

$installer = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer'
$vswhere = Join-Path $installer 'vswhere.exe'
$component = 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64'
$compiler = if (Test-Path -LiteralPath $vswhere) {
    & $vswhere -latest -products '*' -requires $component -property installationPath
}
$kitRoot = (Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows Kits\Installed Roots' -ErrorAction SilentlyContinue).KitsRoot10
$sdk = if ($kitRoot) {
    Get-ChildItem -Path "$kitRoot/Include/*/um/Windows.h" -File -ErrorAction SilentlyContinue | Select-Object -First 1
}
if (!$compiler -or !$sdk) {
    $setupFolder = Join-Path $PSScriptRoot 'runs/setup'
    New-Item -ItemType Directory -Force -Path $setupFolder | Out-Null
    $existing = if (Test-Path -LiteralPath $vswhere) {
        & $vswhere -latest -products '*' -property installationPath
    }
    if ($existing) {
        $setup = Join-Path $installer 'setup.exe'
        $setupArgs = @('modify', '--installPath', ('"{0}"' -f $existing))
    } else {
        $setup = Join-Path $setupFolder 'vs_BuildTools.exe'
        Invoke-WebRequest -UseBasicParsing 'https://aka.ms/vs/17/release/vs_BuildTools.exe' -OutFile $setup
        $signature = Get-AuthenticodeSignature -LiteralPath $setup
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'CN=Microsoft Corporation(?:,|$)') {
            throw 'The Visual Studio installer signature could not be verified.'
        }
        $setupArgs = @('--wait')
    }
    $setupArgs += @('--quiet', '--norestart', '--add', 'Microsoft.VisualStudio.Workload.VCTools', '--includeRecommended')
    Write-Host 'Preparing MSVC and Windows SDK. Windows may request administrator approval.'
    $setupProcess = Start-Process -FilePath $setup -ArgumentList $setupArgs -WindowStyle Hidden -Wait -PassThru
    if ($setupProcess.ExitCode -eq 3010) { throw 'Windows requires a restart to finish installing the build tools.' }
    if ($setupProcess.ExitCode -ne 0) { throw "Visual Studio setup failed with exit code $($setupProcess.ExitCode)." }
    $compiler = & $vswhere -latest -products '*' -requires $component -property installationPath
    $kitRoot = (Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows Kits\Installed Roots' -ErrorAction SilentlyContinue).KitsRoot10
    if (!$compiler -or !$kitRoot -or
        !(Get-ChildItem -Path "$kitRoot/Include/*/um/Windows.h" -File -ErrorAction SilentlyContinue | Select-Object -First 1)) {
        throw 'MSVC or the Windows SDK could not be located after setup.'
    }
}

# UBT imposes a path length limit on generated C++ files.
$drive = $null
foreach ($letter in 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z') {
    $candidate = "${letter}:"
    if (Get-PSDrive -Name $letter -ErrorAction SilentlyContinue) { continue }
    & subst.exe $candidate $PSScriptRoot 2>$null
    if ($LASTEXITCODE -eq 0) {
        $drive = $candidate
        break
    }
}
if (!$drive) { throw 'No temporary drive is available for the Unreal build.' }
try {
    $manifest = & "$drive\build-game.ps1" -Project $project.FullName -EngineRoot $engine -AutoDiscover -Reuse:(!$Rebuild) |
        ConvertFrom-Json
    if ($ListActions) {
        $mappingOptions = @()
        if (!$RefreshActions -and $manifest.mapping_report -and
            (Test-Path -LiteralPath $manifest.mapping_report -PathType Leaf)) {
            $mappingOptions = @('--from-report', $manifest.mapping_report)
        }
        & $python -B $controller list-actions $manifest.executable --with-keys @mappingOptions
        exit $LASTEXITCODE
    }

    $arguments = @('ai-test', $manifest.executable, '--objective', $Objective)
    foreach ($action in $Check) { $arguments += @('--check', $action) }
    if ($MaxCalls) { $arguments += @('--max-calls', $MaxCalls) }
    & $python -B $controller @arguments
    exit $LASTEXITCODE
} finally {
    & subst.exe $drive /D
}
