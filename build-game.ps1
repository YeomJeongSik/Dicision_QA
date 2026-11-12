[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Project,
    [Parameter(Mandatory)][string]$EngineRoot,
    [string]$OutputRoot = (Join-Path $PSScriptRoot 'runs/game-builds'),
    [string]$Map,
    [switch]$AutoDiscover,
    [switch]$Reuse
)

$ErrorActionPreference = 'Stop'
$projectFile = Get-Item -LiteralPath $Project
$engine = (Resolve-Path -LiteralPath $EngineRoot).Path
if ($projectFile.Extension -ine '.uproject') {
    throw 'An Unreal .uproject is required.'
}
$engineVersion = Join-Path $engine 'Engine/Build/Build.version'
$version = Get-Content -LiteralPath $engineVersion -Raw | ConvertFrom-Json
if ("$($version.MajorVersion).$($version.MinorVersion).$($version.PatchVersion)" -ne '5.9.0') {
    throw 'UE 5.9.0 is required.'
}
$python = Join-Path $engine 'Engine/Binaries/ThirdParty/Python3/Win64/python.exe'
$controller = Join-Path $PSScriptRoot 'qa.py'
$pluginSource = Join-Path $PSScriptRoot 'plugins/GameQABridge'
if (!(Test-Path -LiteralPath $controller -PathType Leaf)) {
    $controller = Join-Path $PSScriptRoot 'UnrealAutoQA.exe'
    $pluginSource = Join-Path $PSScriptRoot '_internal/plugins/GameQABridge'
}
$uat = Join-Path $engine 'Engine/Build/BatchFiles/RunUAT.bat'
$output = [IO.Path]::GetFullPath($OutputRoot)
$run = Join-Path $output ([guid]::NewGuid().ToString('N'))
$isolated = Join-Path $run 'Project'
$package = Join-Path $run 'Package'
if ($engine -match '[%!"\r\n]' -or $run -match '[%!"\r\n]') { throw 'UAT paths contain unsupported characters.' }

function Invoke-Controller {
    if ($controller.EndsWith('.exe')) { & $controller @args }
    else { & $python -B $controller @args }
}

function Get-BuildInputFingerprint([string]$Path, [switch]$MetadataOnly) {
    $engineHash = (Get-FileHash -LiteralPath $engineVersion).Hash
    $options = @()
    if ($MetadataOnly) { $options += '--metadata-only' }
    $result = Invoke-Controller project-fingerprint $Path --engine-build $engineHash @options
    if ($LASTEXITCODE -ne 0) { throw 'Project fingerprint failed.' }
    $result | ConvertFrom-Json
}

$projectId = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash(
    [Text.Encoding]::UTF8.GetBytes($projectFile.FullName.ToLowerInvariant()))).Replace('-', '').ToLowerInvariant()
$lastGood = Join-Path $output "$projectId.last-good.json"
$bridgeHashes = @(Get-ChildItem -LiteralPath $pluginSource -File -Recurse | Sort-Object FullName |
    ForEach-Object { (Get-FileHash -LiteralPath $_.FullName).Hash })
$buildOptions = "|$($AutoDiscover.IsPresent)|$Map|$($bridgeHashes -join ',')"
$metadata = (Get-BuildInputFingerprint $projectFile.FullName -MetadataOnly).fingerprint
$before = $null
if ($Reuse -and (Test-Path -LiteralPath $lastGood -PathType Leaf)) {
    $cached = Get-Content -LiteralPath $lastGood -Raw | ConvertFrom-Json
    $cachedGame = $cached.executable
    if ($cached.executable_relative) {
        $cachedGame = [IO.Path]::GetFullPath((Join-Path $output $cached.executable_relative))
    }
    if ($cached.build_key -ceq "$($cached.fingerprint)$buildOptions" -and $cachedGame -and
        $cachedGame.StartsWith("$output$([IO.Path]::DirectorySeparatorChar)", [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $cachedGame -PathType Leaf) -and
        (Get-FileHash -LiteralPath $cachedGame).Hash -ceq $cached.executable_sha256) {
        if ($cached.input_metadata -cne $metadata) { $before = Get-BuildInputFingerprint $projectFile.FullName }
        if ($cached.input_metadata -ceq $metadata -or $cached.fingerprint -ceq $before.fingerprint) {
            $updateCache = $cached.input_metadata -cne $metadata
            $cached.executable = $cachedGame
            $cached | Add-Member -Force -NotePropertyName input_metadata -NotePropertyValue $metadata
            if (!$cached.mapping_report -and $cached.executable_relative) {
                $cachedRun = ($cached.executable_relative -split '[\\/]')[0]
                if ($cachedRun -match '^[0-9a-f]{32}$') {
                    $cachedSmoke = Join-Path $output "$cachedRun/smoke.log"
                    if ((Test-Path -LiteralPath $cachedSmoke -PathType Leaf) -and
                        (Get-Content -LiteralPath $cachedSmoke -TotalCount 1) -match '^UE PASS: (.+)$') {
                        $cached | Add-Member -Force -NotePropertyName mapping_report -NotePropertyValue $Matches[1]
                        $updateCache = $true
                    }
                }
            }
            if ($updateCache) {
                $cacheUpdate = Join-Path $output "$([guid]::NewGuid().ToString('N')).tmp"
                [IO.File]::WriteAllText($cacheUpdate, ($cached | ConvertTo-Json))
                [IO.File]::Replace($cacheUpdate, $lastGood, [NullString]::Value)
            }
            $cached | ConvertTo-Json
            return
        }
    }
}
if (!$before) { $before = Get-BuildInputFingerprint $projectFile.FullName }
$buildKey = "$($before.fingerprint)$buildOptions"
Invoke-Controller project-copy-inputs $projectFile.FullName $isolated | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Project input copy failed.' }
$isolatedProject = Join-Path $isolated $projectFile.Name
if ((Get-BuildInputFingerprint $isolatedProject).fingerprint -ne $before.fingerprint) {
    throw 'BUILD_INPUT_CHANGED_DURING_BUILD'
}
$plugin = Join-Path $isolated 'Plugins/GameQABridge'
if ($AutoDiscover -and (Test-Path -LiteralPath $plugin)) {
    $backup = Join-Path $run 'OriginalGameQABridge'
    Move-Item -LiteralPath $plugin -Destination $backup
}
if (!(Test-Path -LiteralPath $plugin)) {
    New-Item -ItemType Directory -Path (Split-Path $plugin) -Force | Out-Null
    Copy-Item -LiteralPath $pluginSource -Destination $plugin -Recurse
}
$descriptor = Get-Content -LiteralPath $isolatedProject -Raw | ConvertFrom-Json
if (!$descriptor.Modules) {
    $name = $projectFile.BaseName
    if ($name -notmatch '^[A-Za-z][A-Za-z0-9_]*$' -or (Test-Path -LiteralPath "$isolated/Source")) {
        throw 'The Blueprint project needs a valid Unreal module name and no existing Source folder.'
    }
    $module = Join-Path $isolated "Source/$name"
    New-Item -ItemType Directory -Path $module | Out-Null
    @"
using UnrealBuildTool;
public class $name : ModuleRules
{
    public $name(ReadOnlyTargetRules Target) : base(Target)
    {
        PrivateDependencyModuleNames.AddRange(new string[] { "Core", "CoreUObject", "Engine" });
    }
}
"@ | Set-Content -LiteralPath "$module/$name.Build.cs" -Encoding Ascii
    @"
#include "Modules/ModuleManager.h"
IMPLEMENT_PRIMARY_GAME_MODULE(FDefaultGameModuleImpl, $name, "$name");
"@ | Set-Content -LiteralPath "$module/$name.cpp" -Encoding Ascii
    foreach ($type in 'Game', 'Editor') {
        $target = if ($type -eq 'Game') { $name } else { "${name}Editor" }
        @"
using UnrealBuildTool;
public class ${target}Target : TargetRules
{
    public ${target}Target(TargetInfo Target) : base(Target)
    {
        Type = TargetType.$type;
        DefaultBuildSettings = BuildSettingsVersion.V8;
        IncludeOrderVersion = EngineIncludeOrderVersion.Unreal5_9;
        ExtraModuleNames.Add("$name");
    }
}
"@ | Set-Content -LiteralPath "$isolated/Source/$target.Target.cs" -Encoding Ascii
    }
    $descriptor | Add-Member -Force -NotePropertyName Modules -NotePropertyValue @(
        @{ Name=$name; Type='Runtime'; LoadingPhase='Default' })
}
if (!$descriptor.PSObject.Properties['Plugins']) {
    $descriptor | Add-Member -NotePropertyName Plugins -NotePropertyValue @()
}
$descriptor.Plugins = @($descriptor.Plugins | Where-Object { $_ -and $_.Name -ne 'GameQABridge' })
$descriptor.Plugins += @{Name='GameQABridge'; Enabled=$true}
[IO.File]::WriteAllText($isolatedProject, ($descriptor | ConvertTo-Json -Depth 20))
if ($AutoDiscover) {
    Add-Content -LiteralPath (Join-Path $isolated 'Config/DefaultGame.ini') -Value @(
        '', '[GameQABridge]', 'AutoDiscoverKeys=True')
}
if (!$Map) {
    $setting = Select-String -LiteralPath "$isolated/Config/DefaultEngine.ini" -Pattern '^\s*GameDefaultMap\s*=\s*(/[^.\s]+)' |
        Select-Object -First 1
    if (!$setting) { throw 'Pass -Map or define GameDefaultMap.' }
    $Map = $setting.Matches[0].Groups[1].Value
}
if ($Map -notmatch '^/[A-Za-z0-9_/-]+$') { throw 'Invalid Unreal map path.' }
$argsList = @('-WaitForUATMutex', 'BuildCookRun', "-project=$isolatedProject", '-platform=Win64', '-clientconfig=Development',
              '-build','-cook','-stage','-pak','-iostore','-package','-archive',
              "-archivedirectory=$package",'-unattended','-nop4',"-map=$Map")
if ($AutoDiscover) { $argsList += '-ubtargs=-NoHotReloadFromIDE' }
$argsList | Set-Content -LiteralPath "$run/uat-args.txt"
$env:uebp_LogFolder = "$run/uat"
$env:uebp_FinalLogFolder = "$run/uat"
& $uat @argsList *> "$run/uat-console.log"
if ($LASTEXITCODE -ne 0) { throw "UAT failed. See $run/uat-console.log" }
if ((Get-BuildInputFingerprint $projectFile.FullName).fingerprint -ne $before.fingerprint) {
    throw 'BUILD_INPUT_CHANGED_DURING_BUILD'
}

$game = Join-Path $package "Windows/$($projectFile.BaseName)/Binaries/Win64/$($projectFile.BaseName).exe"
if (!(Test-Path -LiteralPath $game -PathType Leaf)) { throw "Packaged game executable not found: $game" }
$smoke = Invoke-Controller _build-smoke $game
$smoke | Set-Content -LiteralPath "$run/smoke.log"
if ($LASTEXITCODE -ne 0) { throw "Input smoke failed. See $run/smoke.log" }
$after = Get-BuildInputFingerprint $projectFile.FullName
if ($before.fingerprint -ne $after.fingerprint) { throw 'BUILD_INPUT_CHANGED_DURING_BUILD' }

$temporary = Join-Path $run 'last-good.tmp'
$manifest = [ordered]@{ project=$projectFile.FullName; fingerprint=$after.fingerprint; build_key=$buildKey; executable=$game;
                       input_metadata=$metadata; mapping_report=($smoke -replace '^UE PASS: ', '');
                       executable_relative=$game.Substring($output.Length + 1);
                       executable_sha256=(Get-FileHash -LiteralPath $game).Hash; uat_log="$run/uat-console.log";
                       smoke_log="$run/smoke.log" }
[IO.File]::WriteAllText($temporary, ($manifest | ConvertTo-Json))
if (Test-Path -LiteralPath $lastGood) { [IO.File]::Replace($temporary, $lastGood, [NullString]::Value) }
else { [IO.File]::Move($temporary, $lastGood) }
$manifest | ConvertTo-Json
