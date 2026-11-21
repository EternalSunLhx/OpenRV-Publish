#requires -Version 7
[CmdletBinding()]
param([ValidateSet('MSVC','Helpers','Inno')][string]$Step)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $env:GITHUB_ACTIONS -or $env:RUNNER_OS -ne 'Windows') { throw 'This setup script is for an ephemeral Windows Actions runner.' }
function Invoke-Checked([string]$File, [string[]]$Arguments) {
    & $File @Arguments
    if ($LASTEXITCODE -notin 0,3010) { throw "$File failed: $LASTEXITCODE" }
}
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if ($Step -eq 'MSVC') {
    $vsPath = & $vswhere -latest -products '*' -version '[17.0,18.0)' -property installationPath
    if (-not $vsPath) { throw 'Visual Studio 2022 is missing from the runner image.' }
    $compiler = Join-Path $vsPath 'VC\Tools\MSVC\14.40.33807\bin\Hostx64\x64\cl.exe'
    if (-not (Test-Path -LiteralPath $compiler)) {
        $installer = Join-Path (Split-Path $vswhere) 'vs_installer.exe'
        $startInfo = [Diagnostics.ProcessStartInfo]::new($installer)
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        foreach ($argument in @('modify','--installPath',$vsPath,'--add','Microsoft.VisualStudio.Component.VC.14.40.17.10.x86.x64','--quiet','--norestart','--nocache')) { $startInfo.ArgumentList.Add($argument) }
        $process = [Diagnostics.Process]::Start($startInfo)
        if (-not $process.WaitForExit(1800000)) { $process.Kill($true); throw 'Toolset installation exceeded 30 minutes.' }
        if ($process.ExitCode -notin 0,3010) { throw "VS installer failed: $($process.ExitCode)" }
    }
    if (-not (Test-Path -LiteralPath $compiler)) { throw 'MSVC 14.40.33807 did not install.' }
    Get-PSDrive -PSProvider FileSystem | Select-Object Name,Used,Free
    return
}
if ($Step -eq 'Helpers') {
Invoke-Checked python @('-m','pip','install','-r','requirements.txt')
Invoke-Checked choco @('install','jom','--yes','--no-progress')
Invoke-Checked rustup @('toolchain','install','1.100.0','--profile','minimal')
# A process/job toolchain override avoids writing a user-wide rustup default.
"RUSTUP_TOOLCHAIN=1.100.0" | Add-Content -LiteralPath $env:GITHUB_ENV
}
$download = Join-Path $env:RUNNER_TEMP 'innosetup-7.1.1-x64.exe'
Invoke-WebRequest 'https://github.com/jrsoftware/issrc/releases/download/is-7_1_1/innosetup-7.1.1-x64.exe' -OutFile $download
$expectedHash = '8a5a6711c3766a6e5fb74f280ba0e3551e719ce94538d901fdbd1f8f9920b3f0'
if ((Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) { throw 'Inno Setup download checksum mismatch.' }
$signature = Get-AuthenticodeSignature -LiteralPath $download
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Pyrsys B.V.') { throw 'Inno Setup signature is unexpected.' }
$innoRoot = Join-Path $env:RUNNER_TEMP 'openrv-inno-7.1.1'
$info = [Diagnostics.ProcessStartInfo]::new($download)
$info.UseShellExecute = $false
$info.CreateNoWindow = $true
foreach ($argument in @('/PORTABLE=1','/CURRENTUSER','/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',"/DIR=$innoRoot")) { $info.ArgumentList.Add($argument) }
$installerProcess = [Diagnostics.Process]::Start($info)
if (-not $installerProcess.WaitForExit(180000)) { $installerProcess.Kill($true); throw 'Inno compiler extraction timed out.' }
if ($installerProcess.ExitCode -ne 0) { throw "Inno compiler extraction failed: $($installerProcess.ExitCode)" }
if (-not (Test-Path -LiteralPath (Join-Path $innoRoot 'ISCC.exe'))) { throw 'Inno Setup compiler is absent.' }
"OPENRV_ISCC=$(Join-Path $innoRoot 'ISCC.exe')" | Add-Content -LiteralPath $env:GITHUB_ENV
if ($Step -eq 'Helpers') {
    "OPENRV_BUILD_PYTHON=$((Get-Command python).Source)" | Add-Content -LiteralPath $env:GITHUB_ENV
    "OPENRV_CMAKE=$((Get-Command cmake).Source)" | Add-Content -LiteralPath $env:GITHUB_ENV
}
