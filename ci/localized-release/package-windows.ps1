#requires -Version 7
[CmdletBinding()]
param(
    [string]$RepoDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$RuntimeDir = '',
    [string]$OutputDir = '',
    [string]$IsccPath = $env:OPENRV_ISCC,
    [string]$BuildDir = $env:OPENRV_BUILD_DIR
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath($RepoDir).TrimEnd('\')
if (-not $RuntimeDir) { $RuntimeDir = Join-Path $repo '_install_release' }
if (-not $BuildDir) { $BuildDir = Join-Path $repo '_build_release' }
$build = (Resolve-Path -LiteralPath $BuildDir).Path
if (-not $OutputDir) { $OutputDir = Join-Path $repo 'dist\localized-release' }
$runtime = (Resolve-Path -LiteralPath $RuntimeDir).Path
$output = [IO.Path]::GetFullPath($OutputDir)
if ($output.StartsWith($runtime + '\',[StringComparison]::OrdinalIgnoreCase) -or $output -eq $runtime) { throw 'Output must be outside the runtime.' }
[void](New-Item -ItemType Directory -Path $output -Force)
$python = (Get-Command python).Source
$metadataText = & $python (Join-Path $PSScriptRoot 'release_common.py') --repo $repo
if ($LASTEXITCODE -ne 0) { throw 'Could not read source metadata.' }
$metadata = $metadataText | ConvertFrom-Json -AsHashtable
$baseName = "OpenRV-$($metadata.releaseVersion)-Windows-x64"
foreach ($binary in 'rv.exe','rvio.exe','py-interp.exe') {
    $installed = Join-Path $runtime "bin\$binary"
    $stage = Join-Path $build "stage\app\bin\$binary"
    if (-not (Test-Path -LiteralPath $installed) -or -not (Test-Path -LiteralPath $stage)) { throw "Missing runtime/stage $binary" }
    if ((Get-FileHash -LiteralPath $installed).Hash -ne (Get-FileHash -LiteralPath $stage).Hash) { throw "Stage mismatch for $binary" }
    $stream = [IO.File]::OpenRead($installed)
    try {
        $reader = [IO.BinaryReader]::new($stream)
        $stream.Position = 0x3c
        $stream.Position = $reader.ReadInt32()
        if ($reader.ReadUInt32() -ne 0x4550 -or $reader.ReadUInt16() -ne 0x8664) { throw "Not an AMD64 PE: $binary" }
    } finally { $stream.Dispose() }
}
$development = Get-ChildItem -LiteralPath $runtime -Recurse -File | Where-Object { $_.Extension -in '.pdb','.lib','.exp' }
if ($development) { throw "Development file in runtime: $($development[0].FullName)" }
if (-not $IsccPath -or -not (Test-Path -LiteralPath $IsccPath)) { throw 'Supply the pinned Inno Setup 7.1.1 compiler.' }
foreach ($language in 'Default.isl','Languages\ChineseSimplified.isl') {
    if (-not (Test-Path -LiteralPath (Join-Path (Split-Path $IsccPath) $language))) { throw "Missing installer language: $language" }
}
# Deploy only Microsoft's canonical redistributable CRT directory, never System32.
$redistRoot = Join-Path $env:VSINSTALLDIR 'VC\Redist\MSVC'
$crt = $null
if ($env:VCToolsRedistDir) {
    $preferredRedist = [IO.Path]::GetFullPath($env:VCToolsRedistDir).TrimEnd('\')
    $canonicalRedistRoot = [IO.Path]::GetFullPath($redistRoot).TrimEnd('\')
    if (-not $preferredRedist.StartsWith($canonicalRedistRoot + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'The selected MSVC redistributable must be inside the official Visual Studio redist tree.' }
    $preferredCrt = Join-Path $preferredRedist 'x64\Microsoft.VC143.CRT'
    if (Test-Path -LiteralPath $preferredCrt -PathType Container) { $crt = $preferredCrt }
}
if (-not $crt) {
    $crt = Get-ChildItem -LiteralPath $redistRoot -Directory | Where-Object {
        $redistVersion = $null
        [version]::TryParse($_.Name,[ref]$redistVersion)
    } | Sort-Object { [version]$_.Name } -Descending | ForEach-Object {
        $candidate = Join-Path $_.FullName 'x64\Microsoft.VC143.CRT'
        if (Test-Path -LiteralPath $candidate -PathType Container) { $candidate }
    } | Select-Object -First 1
}
if (-not $crt) { throw 'No official app-local MSVC x64 redistributable directory was found.' }
$crtFiles = @(Get-ChildItem -LiteralPath $crt -File -Filter '*.dll')
if (-not ($crtFiles.Name -contains 'vcruntime140.dll') -or -not ($crtFiles.Name -contains 'msvcp140.dll')) { throw 'Incomplete canonical CRT directory.' }
if ([version](Get-Item -LiteralPath (Join-Path $crt 'vcruntime140.dll')).VersionInfo.FileVersion -lt [version]'14.40') { throw 'CRT is older than the compiler.' }
$crtManifest = @()
foreach ($file in $crtFiles) {
    Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $runtime 'bin') -Force
    $crtManifest += @{name=$file.Name;sha256=(Get-FileHash -LiteralPath $file.FullName).Hash.ToLowerInvariant();version=$file.VersionInfo.FileVersion}
}
$env:ORV_PACKAGE_RUNTIME = $runtime
$env:ORV_PACKAGE_REPO = $repo
$env:ORV_PACKAGE_BUILD = $build
$licenseCode = @'
import os, pathlib, sys
sys.path.insert(0, os.environ['ORV_CI_SCRIPT'])
from release_common import copy_licenses, copy_dependency_licenses
repo=pathlib.Path(os.environ['ORV_PACKAGE_REPO']); runtime=pathlib.Path(os.environ['ORV_PACKAGE_RUNTIME'])
copy_licenses(repo, runtime/'licenses'/'OpenRV')
copy_dependency_licenses(pathlib.Path(os.environ['ORV_PACKAGE_BUILD']), pathlib.Path(os.environ['QT_ROOT_DIR']), runtime/'licenses'/'dependencies')
'@
$env:ORV_CI_SCRIPT = $PSScriptRoot
& $python -c $licenseCode
if ($LASTEXITCODE -ne 0) { throw 'License staging failed.' }
$qtProfileCode = @'
import json, os, pathlib, sys
sys.path.insert(0, os.environ['ORV_CI_SCRIPT'])
from qt_runtime_profile import apply_windows_qt_runtime_profile
records = apply_windows_qt_runtime_profile(pathlib.Path(os.environ['ORV_PACKAGE_RUNTIME']))
print(json.dumps(records))
'@
$qtProfileText = & $python -c $qtProfileCode
if ($LASTEXITCODE -ne 0) { throw 'Qt runtime profile staging failed.' }
$metadata.excludedQtPlugins = @($qtProfileText | ConvertFrom-Json -AsHashtable)
$smokeDir = Join-Path $repo ('_release_work\smoke-' + [Guid]::NewGuid().ToString('N'))
$smokeArguments = @((Join-Path $PSScriptRoot 'runtime_smoke.py'),'--runtime',$runtime,'--work',$smokeDir)
if ($env:ORV_REQUIRE_GUI_SMOKE -eq 'true') { $smokeArguments += '--require-gui' }
& $python @smokeArguments
if ($LASTEXITCODE -ne 0) { throw "Native runtime checks failed. See $smokeDir\report.json" }
$smoke = Get-Content -LiteralPath (Join-Path $smokeDir 'report.json') -Raw | ConvertFrom-Json
$persistenceProof = @{status='skipped';reason='Artifacts-only diagnostic run did not require visible GUI tests.'}
if ($env:ORV_REQUIRE_GUI_SMOKE -eq 'true') {
    $persistenceDir = Join-Path $repo ('_release_work\persistence-' + [Guid]::NewGuid().ToString('N'))
    & $python (Join-Path $repo 'tests\localization\language_persistence.py') --runtime $runtime --work $persistenceDir
    if ($LASTEXITCODE -ne 0) { throw "Native language persistence checks failed. See $persistenceDir\report.json" }
    $persistence = Get-Content -LiteralPath (Join-Path $persistenceDir 'report.json') -Raw | ConvertFrom-Json
    if ($persistence.status -ne 'passed' -or $persistence.launches.Count -ne 3) { throw 'Expected three successful isolated language-persistence launches.' }
    $persistenceProof = @{status=$persistence.status;launches=@($persistence.launches | ForEach-Object {
        @{stage=$_.stage;status=$_.status;exitCode=$_.exitCode;actualLanguage=$_.details.actualLanguage;
          nativeMenuRebuilds=$_.details.nativeMenuRebuilds;settingsIsolationVerifiedBeforeAction=$_.details.settingsIsolationVerifiedBeforeAction;
          savedPreference=$_.details.savedPreference}
    })}
}
$installerName = "$baseName-Setup"
$repositoryUrl = if ($env:GITHUB_REPOSITORY) { "https://github.com/$env:GITHUB_REPOSITORY" } else { 'https://github.com/AcademySoftwareFoundation/OpenRV' }
& $IsccPath '/Qp' "/DSourceDir=$runtime" "/DRepoDir=$repo" "/DOutputDir=$output" "/DOutputName=$installerName" "/DAppVersion=$($metadata.version)" "/DReleaseVersion=$($metadata.releaseVersion)" "/DCommit=$($metadata.shortCommit)" "/DRepositoryUrl=$repositoryUrl" (Join-Path $PSScriptRoot 'OpenRV.iss')
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
$installerSmokeDir = Join-Path $repo ('_release_work\installer-' + [Guid]::NewGuid().ToString('N'))
& (Join-Path $PSScriptRoot 'test-installer.ps1') -InstallerPath (Join-Path $output "$installerName.exe") -RuntimeDir $runtime -WorkDir $installerSmokeDir
$installerSmoke = Get-Content -LiteralPath (Join-Path $installerSmokeDir 'report.json') -Raw | ConvertFrom-Json
if ($installerSmoke.status -ne 'passed' -or -not $installerSmoke.removed -or $installerSmoke.fileAssociations.status -ne 'passed' -or -not $installerSmoke.fileAssociations.removed -or -not $installerSmoke.fileAssociations.defaultsPreserved -or -not $installerSmoke.fileAssociations.otherApplicationsPreserved -or @($installerSmoke.fileAssociations.shellOpenWithVerified).Count -ne 2) { throw 'Installer lifecycle validation failed.' }
$env:ORV_ZIP_PATH = Join-Path $output "$baseName-Portable.zip"
$env:ORV_ZIP_ROOT = $baseName
$zipCode = @'
import os, pathlib, zipfile
root=pathlib.Path(os.environ['ORV_PACKAGE_RUNTIME'])
with zipfile.ZipFile(os.environ['ORV_ZIP_PATH'], 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
    for source in sorted(root.rglob('*')):
        if source.is_file(): archive.write(source, pathlib.PurePosixPath(os.environ['ORV_ZIP_ROOT'])/source.relative_to(root).as_posix())
'@
& $python -c $zipCode
if ($LASTEXITCODE -ne 0) { throw 'Portable ZIP failed.' }
$metadata.platform = 'Windows'
$metadata.architecture = 'x64'
$metadata.compiler = (& cl 2>&1 | Out-String).Trim()
$metadata.installerCompilerRelease = '7.1.1'
$metadata.installationScope = 'all-users'
$metadata.crt = $crtManifest
$metadata.binarySha256 = @{}
foreach ($name in 'rv.exe','rvio.exe') { $metadata.binarySha256[$name] = (Get-FileHash -LiteralPath (Join-Path $runtime "bin\$name")).Hash.ToLowerInvariant() }
$metadata.smoke = @($smoke.steps | Select-Object name,status,exitCode,timedOut)
$metadata.guiPassed = $smoke.guiPassed
$metadata.languagePersistence = $persistenceProof
$metadata.installerSmoke = @{status=$installerSmoke.status; removed=$installerSmoke.removed; steps=$installerSmoke.steps; fileAssociations=$installerSmoke.fileAssociations}
$metadata.files = @("$installerName.exe","$baseName-Portable.zip")
$manifestName = "$baseName.build.json"
$metadata | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $output $manifestName) -Encoding utf8
$hashLines = foreach ($name in @($metadata.files) + @($manifestName)) { '{0}  {1}' -f (Get-FileHash -LiteralPath (Join-Path $output $name)).Hash.ToLowerInvariant(),$name }
$hashLines | Set-Content -LiteralPath (Join-Path $output "$baseName.sha256.txt") -Encoding ascii
Write-Host "Created installer, portable ZIP, manifest and checksums in $output"
