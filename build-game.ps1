[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Project,
    [Parameter(Mandatory)][string]$EngineRoot,
    [string]$OutputRoot = (Join-Path $PSScriptRoot 'runs/game-builds'),
    [string]$Map
)

$ErrorActionPreference = 'Stop'
$projectFile = Get-Item -LiteralPath $Project
$engine = (Resolve-Path -LiteralPath $EngineRoot).Path
if ($projectFile.Extension -ine '.uproject' -or !(Test-Path "$($projectFile.DirectoryName)/Source" -PathType Container)) {
    throw 'A C++ Unreal project is required.'
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

function Get-BuildInputFingerprint([string]$Path) {
    $engineHash = (Get-FileHash -LiteralPath $engineVersion).Hash
    $result = Invoke-Controller project-fingerprint $Path --engine-build $engineHash
    if ($LASTEXITCODE -ne 0) { throw 'Project fingerprint failed.' }
    $result | ConvertFrom-Json
}

$before = Get-BuildInputFingerprint $projectFile.FullName
Invoke-Controller project-copy-inputs $projectFile.FullName $isolated | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Project input copy failed.' }
$isolatedProject = Join-Path $isolated $projectFile.Name
if ((Get-BuildInputFingerprint $isolatedProject).fingerprint -ne $before.fingerprint) {
    throw 'BUILD_INPUT_CHANGED_DURING_BUILD'
}
$plugin = Join-Path $isolated 'Plugins/GameQABridge'
if (!(Test-Path -LiteralPath $plugin)) {
    New-Item -ItemType Directory -Path (Split-Path $plugin) -Force | Out-Null
    Copy-Item -LiteralPath $pluginSource -Destination $plugin -Recurse
}
$descriptor = Get-Content -LiteralPath $isolatedProject -Raw | ConvertFrom-Json
if (!$descriptor.PSObject.Properties['Plugins']) {
    $descriptor | Add-Member -NotePropertyName Plugins -NotePropertyValue @()
}
$descriptor.Plugins = @($descriptor.Plugins | Where-Object { $_ -and $_.Name -ne 'GameQABridge' })
$descriptor.Plugins += @{Name='GameQABridge'; Enabled=$true}
[IO.File]::WriteAllText($isolatedProject, ($descriptor | ConvertTo-Json -Depth 20))
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

$projectId = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash(
    [Text.Encoding]::UTF8.GetBytes($projectFile.FullName.ToLowerInvariant()))).Replace('-', '').ToLowerInvariant()
$lastGood = Join-Path $output "$projectId.last-good.json"
$temporary = Join-Path $run 'last-good.tmp'
$manifest = [ordered]@{ project=$projectFile.FullName; fingerprint=$after.fingerprint; executable=$game;
                       executable_sha256=(Get-FileHash -LiteralPath $game).Hash; uat_log="$run/uat-console.log";
                       smoke_log="$run/smoke.log" }
[IO.File]::WriteAllText($temporary, ($manifest | ConvertTo-Json))
if (Test-Path -LiteralPath $lastGood) { [IO.File]::Replace($temporary, $lastGood, $null) }
else { [IO.File]::Move($temporary, $lastGood) }
$manifest | ConvertTo-Json
