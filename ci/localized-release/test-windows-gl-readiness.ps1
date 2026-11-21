#requires -Version 7
<# Hosted OpenGL prerequisite only; real RV GUI gates remain mandatory. #>
[CmdletBinding()]
param(
    [string]$RepoDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$WorkDir = ''
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -or $env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or $env:RUNNER_OS -ne 'Windows') {
    throw 'GL readiness is permitted only on an ephemeral GitHub-hosted Windows runner.'
}
$repo = (Resolve-Path -LiteralPath $RepoDir).Path.TrimEnd('\')
$qt = (Resolve-Path -LiteralPath $env:QT_ROOT_DIR).Path.TrimEnd('\')
$workspace = [IO.Path]::GetFullPath($env:GITHUB_WORKSPACE).TrimEnd('\')
$runnerTemp = [IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\')
$provider = ''
$expectedProviderSha256 = 'b04de4541863bc7d8879040a78889c4849c1b1da2784c4630f734c146c2998ce'
if ($env:OPENRV_CI_GL_PROVIDER) {
    if (-not ('OpenRv.CiGlNative' -as [type])) { throw 'The hosted GL provider requires the scoped wrapper.' }
    $provider = (Resolve-Path -LiteralPath $env:OPENRV_CI_GL_PROVIDER).Path
    $providerDir = Split-Path -Parent $provider
    $relativeProvider = [IO.Path]::GetRelativePath($runnerTemp,$provider)
    if ($relativeProvider -notmatch '^openrv-ci-gl-[a-f0-9]{32}[\\/]opengl32\.dll$' -or
        ([IO.File]::GetAttributes($provider) -band [IO.FileAttributes]::ReparsePoint) -or
        ([IO.File]::GetAttributes($providerDir) -band [IO.FileAttributes]::ReparsePoint) -or
        (Get-Item -LiteralPath $provider).Length -ne 20639888 -or
        (Get-FileHash -LiteralPath $provider -Algorithm SHA256).Hash.ToLowerInvariant() -cne $expectedProviderSha256 -or
        $env:OPENRV_CI_GL_SHA256 -cne $expectedProviderSha256 -or
        [OpenRv.CiGlNative]::GetDirectory() -ine $providerDir) {
        throw 'The active hosted GL provider is not the verified private runner-temp DLL.'
    }
}
if (-not $WorkDir) { $WorkDir = Join-Path $repo ('_release_work\gl-readiness-' + [Guid]::NewGuid().ToString('N')) }
$work = [IO.Path]::GetFullPath($WorkDir).TrimEnd('\')
if (-not ($work.StartsWith($workspace + '\',[StringComparison]::OrdinalIgnoreCase) -or $work.StartsWith($runnerTemp + '\',[StringComparison]::OrdinalIgnoreCase))) {
    throw 'Readiness outputs must stay in the hosted workspace or runner temp.'
}
if (Test-Path -LiteralPath $work) { throw 'Readiness WorkDir must be new.' }
$compiler = (Get-Command cl.exe -ErrorAction Stop).Source
if ($compiler -notmatch '[\\/]MSVC[\\/]14\.40\.[^\\/]+[\\/]bin[\\/]Hostx64[\\/]x64[\\/]cl\.exe$' -or $env:VCToolsVersion -notlike '14.40.*') {
    throw 'Readiness requires the initialized MSVC 14.40 x64 toolset.'
}
$cmake = (Get-Command cmake.exe -ErrorAction Stop).Source
[void](New-Item -ItemType Directory -Path $work)
$report = [ordered]@{status='failed';fixture='windows-gl-readiness';realRuntimeVerified=$false;steps=@();
    providerMode=$(if ($provider) { 'hosted-software' } else { 'default' });provider=$provider}
if ($provider) { $report.providerSha256 = $expectedProviderSha256 }

function Invoke-ReadinessTool([string]$Name,[string]$File,[string[]]$Arguments,[int]$TimeoutSeconds=60,[switch]$DefaultProvider) {
    $info = [Diagnostics.ProcessStartInfo]::new($File)
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.WorkingDirectory = $work
    foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
    if ($DefaultProvider) {
        foreach ($key in @($info.Environment.Keys)) {
            if ($key.StartsWith('QT_',[StringComparison]::OrdinalIgnoreCase) -or $key.StartsWith('QML_',[StringComparison]::OrdinalIgnoreCase)) {
                [void]$info.Environment.Remove($key)
            }
        }
        $info.Environment['QT_QPA_PLATFORM'] = 'windows'
        $info.Environment['QT_PLUGIN_PATH'] = Join-Path $qt 'plugins'
        if ($provider) {
            # Reapply only the provider validated above after clearing ambient Qt overrides.
            $info.Environment['QT_OPENGL'] = 'desktop'
            $info.Environment['QT_OPENGL_DLL'] = $provider
        }
        $info.Environment['PATH'] = (Join-Path $qt 'bin') + ';' + $env:PATH
    }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $info
    $started = $false
    try {
        $started = $process.Start()
        if (-not $started) { throw "$Name did not start." }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            if (-not $process.HasExited) { $process.Kill($true) }
            throw "$Name exceeded its readiness timeout."
        }
        if (-not $stdout.Wait(5000) -or -not $stderr.Wait(5000)) { throw "$Name streams did not close." }
        $out = $stdout.GetAwaiter().GetResult()
        $err = $stderr.GetAwaiter().GetResult()
        [IO.File]::WriteAllText((Join-Path $work "$Name.stdout.log"),$out)
        [IO.File]::WriteAllText((Join-Path $work "$Name.stderr.log"),$err)
        $report.steps += @{name=$Name;exitCode=$process.ExitCode}
        if ($DefaultProvider -or $process.ExitCode -ne 0) {
            Write-Host $out.Substring([Math]::Max(0,$out.Length-6000))
            if ($err) { Write-Host $err.Substring([Math]::Max(0,$err.Length-3000)) }
        }
        if ($process.ExitCode -ne 0) { throw "$Name failed with exit $($process.ExitCode); see $work." }
    } finally {
        try { if ($started -and -not $process.HasExited) { $process.Kill($true); [void]$process.WaitForExit(5000) } }
        finally { $process.Dispose() }
    }
}

try {
    $build = Join-Path $work 'build'
    Invoke-ReadinessTool 'configure' $cmake @('-S',(Join-Path $PSScriptRoot 'qt-gl-readiness'),'-B',$build,'-G','Visual Studio 17 2022','-A','x64','-T','v143,version=14.40',"-DCMAKE_PREFIX_PATH=$qt")
    Invoke-ReadinessTool 'build' $cmake @('--build',$build,'--config','Release','--parallel','2')
    Invoke-ReadinessTool 'probe' (Join-Path $build 'Release\openrv-gl-readiness.exe') @() -DefaultProvider
    $details = Get-Content -LiteralPath (Join-Path $work 'probe.stdout.log') -Raw | ConvertFrom-Json -AsHashtable
    if ($details.status -ne 'passed' -or $details.qtVersion -ne '6.5.3' -or -not $details.madeCurrent -or $details.visibleWindows -ne 0 -or $details.glewStatus -ne 0 -or -not $details.glewVersion21) {
        throw 'Expected the hosted Qt/core OpenGL provider to support OpenGL 2.1 and real GLEW initialization.'
    }
    if ($provider -and ($details.providerSelection -ne 'hosted-software' -or
        [IO.Path]::GetFullPath($details.qtFunctionLibrary) -ine $provider -or
        [IO.Path]::GetFullPath($details.linkedCoreLibrary) -ine $provider)) {
        throw 'Qt and native core OpenGL did not load the same verified private provider.'
    }
    $report.details = $details
    $report.status = 'passed'
    Write-Host 'OPENRV_GL_READINESS_OK'
} catch {
    $report.error = $_.Exception.Message
    throw
} finally {
    $nativeReport = Join-Path $work 'probe.stdout.log'
    if (-not $report.Contains('details') -and (Test-Path -LiteralPath $nativeReport)) {
        try { $report.details = Get-Content -LiteralPath $nativeReport -Raw | ConvertFrom-Json -AsHashtable }
        catch { $report.nativeReportError = $_.Exception.Message }
    }
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $work 'report.json') -Encoding utf8
}
