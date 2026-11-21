#requires -Version 7
<# Ephemeral CI OpenGL provider; never copies a DLL into a runtime or package. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Readiness','Package','PostPackage')][string]$Task,
    [string]$RuntimeDir,
    [string]$WorkDir,
    [ValidateSet('all','rotation-playback','preferences-owner-close')][string]$TestCase = 'all',
    [ValidateSet('both','en','zh_CN')][string]$Language = 'both'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -or $env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or $env:RUNNER_OS -ne 'Windows') {
    throw 'The scoped software GL provider is permitted only on an ephemeral GitHub-hosted Windows runner.'
}
$nativeSource = @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
namespace OpenRv {
    public static class CiGlNative {
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        public static extern bool SetDllDirectoryW(string path);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        private static extern uint GetDllDirectoryW(uint capacity, StringBuilder path);
        public static string GetDirectory() {
            var value = new StringBuilder(32768);
            var length = GetDllDirectoryW((uint)value.Capacity,value);
            if (length >= value.Capacity) throw new InvalidOperationException("DLL directory exceeds the supported buffer.");
            return value.ToString();
        }
    }
}
'@
Add-Type -TypeDefinition $nativeSource

function Invoke-OpenRvScopedGlProvider([string]$Provider,[string]$Sha256,[Collections.IDictionary]$Report,[scriptblock]$Action) {
    $previousDirectory = [OpenRv.CiGlNative]::GetDirectory()
    $previousEnvironment = @{}
    $settings = @{QT_OPENGL='desktop';QT_OPENGL_DLL=$Provider;OPENRV_CI_GL_PROVIDER=$Provider;OPENRV_CI_GL_SHA256=$Sha256}
    foreach ($key in $settings.Keys) { $previousEnvironment[$key] = [Environment]::GetEnvironmentVariable($key,'Process') }
    try {
        if (-not [OpenRv.CiGlNative]::SetDllDirectoryW((Split-Path -Parent $Provider))) {
            throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error())
        }
        foreach ($key in $settings.Keys) { [Environment]::SetEnvironmentVariable($key,$settings[$key],'Process') }
        & $Action
    } finally {
        $restorePath = if ($previousDirectory) { $previousDirectory } else { [NullString]::Value }
        $restored = [OpenRv.CiGlNative]::SetDllDirectoryW($restorePath)
        foreach ($key in $settings.Keys) {
            if ($null -eq $previousEnvironment[$key]) { Remove-Item -LiteralPath ('Env:' + $key) -ErrorAction SilentlyContinue }
            else { [Environment]::SetEnvironmentVariable($key,$previousEnvironment[$key],'Process') }
        }
        $Report.parentDllDirectoryRestored = $restored -and [OpenRv.CiGlNative]::GetDirectory() -eq $previousDirectory
        $Report.parentEnvironmentRestored = $true
        foreach ($key in $settings.Keys) {
            if ([Environment]::GetEnvironmentVariable($key,'Process') -cne $previousEnvironment[$key]) { $Report.parentEnvironmentRestored = $false }
        }
        if (-not $Report.parentDllDirectoryRestored -or -not $Report.parentEnvironmentRestored) { throw 'The scoped GL provider could not restore parent process settings.' }
    }
}

$expectedSha256 = 'b04de4541863bc7d8879040a78889c4849c1b1da2784c4630f734c146c2998ce'
$runnerTemp = (Resolve-Path -LiteralPath $env:RUNNER_TEMP).Path.TrimEnd('\')
if ($Task -eq 'PostPackage') {
    if (-not $RuntimeDir -or -not $WorkDir) { throw 'PostPackage requires the extracted runtime and a new diagnostics directory.' }
    $RuntimeDir = (Resolve-Path -LiteralPath $RuntimeDir).Path
    $WorkDir = [IO.Path]::GetFullPath($WorkDir)
    if (-not $RuntimeDir.StartsWith($runnerTemp + '\', [StringComparison]::OrdinalIgnoreCase) -or
        -not $WorkDir.StartsWith($runnerTemp + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $WorkDir -eq $RuntimeDir -or $WorkDir.StartsWith($RuntimeDir.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -or
        (Test-Path -LiteralPath $WorkDir)) { throw 'PostPackage paths must be isolated below RUNNER_TEMP with a new work directory.' }
}
$qt = (Resolve-Path -LiteralPath $env:QT_ROOT_DIR).Path.TrimEnd('\')
$source = Join-Path $qt 'bin\opengl32sw.dll'
$sourceItem = Get-Item -LiteralPath $source
if ($sourceItem.Length -ne 20639888 -or (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -cne $expectedSha256) {
    throw 'Unexpected software GL DLL: require the verified Qt 6.5.3 Windows x64 SDK provider.'
}
$providerDir = Join-Path $runnerTemp ('openrv-ci-gl-' + [Guid]::NewGuid().ToString('N'))
if (Test-Path -LiteralPath $providerDir) { throw 'The private provider directory must be new.' }
[void](New-Item -ItemType Directory -Path $providerDir)
$provider = Join-Path $providerDir 'opengl32.dll'
Copy-Item -LiteralPath $source -Destination $provider
if ((Get-FileHash -LiteralPath $provider -Algorithm SHA256).Hash.ToLowerInvariant() -cne $expectedSha256) { throw 'The private GL provider copy differs from the verified SDK DLL.' }
$script = Join-Path $PSScriptRoot $(if ($Task -eq 'Readiness') { 'test-windows-gl-readiness.ps1' } else { 'package-windows.ps1' })
$report = [ordered]@{status='failed';task=$Task;mode='hosted-software';provider=$provider;providerSha256=$expectedSha256;source=$source;providerCopiedIntoRuntime=$false}
function Assert-NoRuntimeProviderAlias {
    if ($Task -eq 'PostPackage') { $runtimeRoots = @($RuntimeDir) }
    elseif ($Task -eq 'Package') {
        $repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
        $runtimeRoots = @((Join-Path $repo '_install_release'),(Join-Path $env:OPENRV_BUILD_DIR 'stage\app'))
    } else { return }
    foreach ($runtimeRoot in $runtimeRoots) {
        if (Get-ChildItem -LiteralPath $runtimeRoot -Recurse -File -Filter 'opengl32.dll') {
            throw 'A CI-only OpenGL alias must never be present in a runtime or stage tree.'
        }
    }
}
try {
    Assert-NoRuntimeProviderAlias
    Invoke-OpenRvScopedGlProvider $provider $expectedSha256 $report {
        if ($Task -eq 'PostPackage') {
            & python -B (Join-Path $PSScriptRoot 'post_package_smoke.py') --runtime $RuntimeDir --work $WorkDir --case $TestCase --language $Language
            if ($LASTEXITCODE -ne 0) { throw 'Independent post-package functional tests failed; see their diagnostics.' }
        } else { & $script }
    }
    Assert-NoRuntimeProviderAlias
    $report.status = 'passed'
} catch {
    $report.error = $_.Exception.Message
    throw
} finally {
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $providerDir 'report.json') -Encoding utf8
    if ($Task -eq 'PostPackage') {
        if (-not (Test-Path -LiteralPath $WorkDir)) { [void](New-Item -ItemType Directory -Path $WorkDir) }
        $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $WorkDir 'gl-provider.json') -Encoding utf8
    }
    Write-Host ('Scoped GL provider report: ' + (Join-Path $providerDir 'report.json'))
}
