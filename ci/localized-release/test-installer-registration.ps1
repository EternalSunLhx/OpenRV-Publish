#requires -Version 7
<# CI-only installer registration fixture; it does not verify the real RV runtime. #>
[CmdletBinding()]
param(
    [string]$RepoDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$WorkDir = '',
    [string]$IsccPath = $env:OPENRV_ISCC
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -or $env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or $env:RUNNER_OS -ne 'Windows') {
    throw 'Registration fixture installation is permitted only on an ephemeral GitHub-hosted Windows runner.'
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'The hosted fixture must already have an administrator token; no UAC request is made.'
}
$repo = (Resolve-Path -LiteralPath $RepoDir).Path.TrimEnd('\')
$runnerTemp = [IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\')
$workspace = [IO.Path]::GetFullPath($env:GITHUB_WORKSPACE).TrimEnd('\')
if (-not $WorkDir) { $WorkDir = Join-Path $repo ('_release_work\installer-registration-' + [Guid]::NewGuid().ToString('N')) }
$work = [IO.Path]::GetFullPath($WorkDir).TrimEnd('\')
if (-not ($work.StartsWith($workspace + '\',[StringComparison]::OrdinalIgnoreCase) -or $work.StartsWith($runnerTemp + '\',[StringComparison]::OrdinalIgnoreCase))) {
    throw 'Fixture files must stay in the hosted workspace or runner temp.'
}
if (Test-Path -LiteralPath $work) { throw 'Registration fixture WorkDir must be new.' }
$compiler = (Get-Command cl.exe -ErrorAction Stop).Source
if ($compiler -notmatch '[\\/]MSVC[\\/]14\.40\.[^\\/]+[\\/]bin[\\/]Hostx64[\\/]x64[\\/]cl\.exe$' -or $env:VCToolsVersion -notlike '14.40.*') {
    throw 'Registration fixture requires the initialized MSVC 14.40 x64 toolset.'
}
if (-not $IsccPath -or -not (Test-Path -LiteralPath $IsccPath -PathType Leaf)) { throw 'The pinned Inno Setup compiler is required.' }
$template = Join-Path $repo 'ci\localized-release\OpenRV.iss'
$lifecycle = Join-Path $repo 'ci\localized-release\test-installer.ps1'
foreach ($path in $template,$lifecycle,(Join-Path $repo 'src\bin\apps\rv\RV.ico'),(Join-Path $repo 'LICENSE')) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing trusted fixture input: $path" }
}
[void](New-Item -ItemType Directory -Path $work)
$runtime = Join-Path $work 'runtime'
$binaryDir = Join-Path $runtime 'bin'
$output = Join-Path $work 'setup'
foreach ($path in $binaryDir,$output) { [void](New-Item -ItemType Directory -Path $path) }
$report = [ordered]@{status='failed'; fixture='installer-registration'; realRuntimeVerified=$false;
    version='1.2.3'; compilerToolset='14.40'; staticCrt=$true; steps=@()}

function Invoke-FixtureTool([string]$Name,[string]$File,[string[]]$Arguments) {
    $info = [Diagnostics.ProcessStartInfo]::new($File)
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.WorkingDirectory = $work
    foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $info
    $started = $false
    try {
        $started = $process.Start()
        if (-not $started) { throw "$Name did not start." }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(60000)) {
            if (-not $process.HasExited) { $process.Kill($true) }
            throw "$Name exceeded its 60-second fixture timeout."
        }
        if (-not $stdout.Wait(5000) -or -not $stderr.Wait(5000)) { throw "$Name streams did not close." }
        [IO.File]::WriteAllText((Join-Path $work "$Name.stdout.log"),$stdout.GetAwaiter().GetResult())
        [IO.File]::WriteAllText((Join-Path $work "$Name.stderr.log"),$stderr.GetAwaiter().GetResult())
        $report.steps += @{name=$Name;exitCode=$process.ExitCode}
        if ($process.ExitCode -ne 0) { throw "$Name failed with exit $($process.ExitCode); see fixture logs in $work." }
    } finally {
        try { if ($started -and -not $process.HasExited) { $process.Kill($true); [void]$process.WaitForExit(5000) } }
        finally { $process.Dispose() }
    }
}

try {
    # /MT removes runtime-DLL prerequisites. These executables only print a
    # semantic version, so no media/GUI success can be inferred from this test.
    $stubSource = Join-Path $work 'version-stub.c'
    [IO.File]::WriteAllText($stubSource,"#include <stdio.h>`nint main(void) { puts(`"1.2.3`"); return 0; }`n",[Text.Encoding]::ASCII)
    $stub = Join-Path $binaryDir 'rv.exe'
    Invoke-FixtureTool 'compile-stub' $compiler @('/nologo','/MT','/O2','/W4','/WX',$stubSource,('/Fo' + (Join-Path $work 'version-stub.obj')),('/Fe' + $stub),'/link','/MACHINE:X64')
    $stream = [IO.File]::OpenRead($stub)
    try {
        $reader = [IO.BinaryReader]::new($stream)
        $stream.Position = 0x3c
        $stream.Position = $reader.ReadInt32()
        if ($reader.ReadUInt32() -ne 0x4550 -or $reader.ReadUInt16() -ne 0x8664) { throw 'Fixture compiler did not produce an x64 PE.' }
    } finally { $stream.Dispose() }
    Copy-Item -LiteralPath $stub -Destination (Join-Path $binaryDir 'rvio.exe')
    $name = 'OpenRV-Registration-Fixture-DoNotDistribute'
    Invoke-FixtureTool 'compile-installer' $IsccPath @('/Qp',"/DSourceDir=$runtime","/DRepoDir=$repo","/DOutputDir=$output","/DOutputName=$name",'/DAppVersion=1.2.3','/DReleaseVersion=1.2.3-registration-fixture','/DCommit=registration-fixture','/DRepositoryUrl=https://github.com/EternalSunLhx/OpenRV-Publish',$template)
    $lifecycleWork = Join-Path $work 'lifecycle'
    & $lifecycle -InstallerPath (Join-Path $output "$name.exe") -RuntimeDir $runtime -WorkDir $lifecycleWork -TimeoutSeconds 60
    $result = Get-Content -LiteralPath (Join-Path $lifecycleWork 'report.json') -Raw | ConvertFrom-Json
    if ($result.status -ne 'passed' -or -not $result.removed -or $result.fileAssociations.status -ne 'passed' -or -not $result.fileAssociations.removed -or -not $result.fileAssociations.defaultsPreserved -or -not $result.fileAssociations.otherApplicationsPreserved -or @($result.fileAssociations.shellOpenWithVerified).Count -ne 2) {
        throw 'Actual hosted install/Shell-enumeration/uninstall registration fixture failed.'
    }
    $report.fileAssociations = $result.fileAssociations
    $report.status = 'passed'
    Write-Host 'Installer registration fixture passed: real MP4/EXR Open With enumeration, existing defaults preserved, own registration removed. Real RV runtime validation remains required.'
} catch {
    $report.error = $_.Exception.Message
    throw
} finally {
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $work 'report.json') -Encoding utf8
}
