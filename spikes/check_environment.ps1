[CmdletBinding()]
param(
    [string]$EngineRoot,
    [string]$Project,
    [switch]$RequireReady
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Get-EngineInfo([string]$Root) {
    $buildVersion = Join-Path $Root 'Engine/Build/Build.version'
    if (-not (Test-Path -LiteralPath $buildVersion -PathType Leaf)) { return }

    $build = Get-Content -LiteralPath $buildVersion -Raw | ConvertFrom-Json
    $files = [ordered]@{
        editor = 'Engine/Binaries/Win64/UnrealEditor.exe'
        ubt = 'Engine/Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.dll'
        uat = 'Engine/Build/BatchFiles/RunUAT.bat'
        viewport_input = 'Engine/Source/Runtime/Engine/Classes/Engine/GameViewportClient.h'
        input_event = 'Engine/Source/Runtime/Engine/Public/InputKeyEventArgs.h'
        enhanced_input = 'Engine/Plugins/EnhancedInput/Source/EnhancedInput/Public/EnhancedPlayerInput.h'
        mapping_context = 'Engine/Plugins/EnhancedInput/Source/EnhancedInput/Public/InputMappingContext.h'
        http_server = 'Engine/Source/Runtime/Online/HTTPServer/Public/HttpServerModule.h'
        http_router = 'Engine/Source/Runtime/Online/HTTPServer/Public/IHttpRouter.h'
    }
    foreach ($name in @($files.Keys)) {
        $path = Join-Path $Root $files[$name]
        $files[$name] = if (Test-Path -LiteralPath $path -PathType Leaf) { $path }
    }
    [pscustomobject]@{
        root = $Root
        version = "$($build.MajorVersion).$($build.MinorVersion).$($build.PatchVersion)"
        changelist = $build.Changelist
        branch = $build.BranchName
        files = $files
    }
}

$roots = @()
if ($EngineRoot) {
    $roots += $EngineRoot
} else {
    $launcher = Join-Path $env:ProgramData 'Epic/UnrealEngineLauncher/LauncherInstalled.dat'
    if (Test-Path -LiteralPath $launcher -PathType Leaf) {
        $entries = (Get-Content -LiteralPath $launcher -Raw | ConvertFrom-Json).InstallationList
        $roots += $entries | Where-Object AppName -Like 'UE_*' | ForEach-Object InstallLocation
    }
    $builds = Get-Item 'HKCU:\Software\Epic Games\Unreal Engine\Builds' -ErrorAction SilentlyContinue
    if ($builds) { $roots += $builds.Property | ForEach-Object { $builds.GetValue($_) } }
    $roots += Get-ItemProperty -Path 'HKLM:\SOFTWARE\EpicGames\Unreal Engine\*','HKLM:\SOFTWARE\WOW6432Node\EpicGames\Unreal Engine\*' -ErrorAction SilentlyContinue |
        ForEach-Object InstalledDirectory
    $roots += Get-ChildItem (Join-Path $env:ProgramFiles 'Epic Games') -Filter 'UE_*' -Directory -ErrorAction SilentlyContinue |
        ForEach-Object FullName
}
$engines = @($roots | Where-Object { $_ } | Sort-Object -Unique | ForEach-Object { Get-EngineInfo $_ })
$target = $engines | Where-Object { $_.version -eq '5.9.0' -and $_.files.uat -and $_.files.ubt -and $_.files.editor } | Select-Object -First 1

$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
$compilers = @()
if (Test-Path -LiteralPath $vswhere -PathType Leaf) {
    $installations = & $vswhere -all -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    $compilers = @($installations | ForEach-Object {
        Get-ChildItem (Join-Path $_ 'VC/Tools/MSVC') -Directory -ErrorAction SilentlyContinue
    } | ForEach-Object {
        $compiler = Join-Path $_.FullName 'bin/Hostx64/x64/cl.exe'
        if (Test-Path -LiteralPath $compiler -PathType Leaf) {
            [pscustomobject]@{ version = $_.Name; path = $compiler }
        }
    })
}

$kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits/10'
$sdks = @(Get-ChildItem (Join-Path $kits 'Include') -Directory -ErrorAction SilentlyContinue |
    Where-Object {
        (Test-Path -LiteralPath (Join-Path $_.FullName 'um/Windows.h') -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $kits "Lib/$($_.Name)/um/x64/kernel32.lib") -PathType Leaf)
    } | Select-Object @{Name='version'; Expression={$_.Name}}, @{Name='include_path'; Expression={$_.FullName}})

$projectFile = if ($Project -and [IO.Path]::GetExtension($Project) -ieq '.uproject') {
    Get-Item -LiteralPath $Project -ErrorAction SilentlyContinue | Where-Object { -not $_.PSIsContainer }
}
$missing = @()
if (-not $target) { $missing += 'UE_5_9_0_BUILD_TOOLS' }
if (-not $compilers) { $missing += 'MSVC_X64' }
if (-not $sdks) { $missing += 'WINDOWS_SDK' }
if (-not $projectFile) { $missing += 'UPROJECT' }

[ordered]@{
    windows = [Environment]::OSVersion.Version.ToString()
    engines = $engines
    msvc = $compilers
    sdk = $sdks
    project = $projectFile.FullName
    missing = $missing
} | ConvertTo-Json -Depth 8

if ($RequireReady -and $missing.Count) { exit 2 }
