#requires -Version 7
<# CI-only installation lifecycle check. Never requests elevation on a user computer. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$InstallerPath,
    [Parameter(Mandatory)][string]$RuntimeDir,
    [Parameter(Mandatory)][string]$WorkDir,
    [ValidateRange(30,600)][int]$TimeoutSeconds = 300
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or $env:RUNNER_OS -ne 'Windows') {
    throw 'Installation smoke is permitted only on an ephemeral GitHub-hosted Windows runner.'
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'The CI test host must already have an administrator token; no UAC request is made.' }
$installer = (Resolve-Path -LiteralPath $InstallerPath).Path
$runtime = (Resolve-Path -LiteralPath $RuntimeDir).Path
$work = [IO.Path]::GetFullPath($WorkDir).TrimEnd('\')
$runnerTemp = [IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\')
$repo = [IO.Path]::GetFullPath($env:GITHUB_WORKSPACE).TrimEnd('\')
if (-not ($work.StartsWith($repo + '\',[StringComparison]::OrdinalIgnoreCase) -or $work.StartsWith($runnerTemp + '\',[StringComparison]::OrdinalIgnoreCase))) { throw 'Test logs must stay in the CI workspace or runner temp.' }
if (Test-Path -LiteralPath $work) { throw 'WorkDir must be a new unique test directory.' }
[void](New-Item -ItemType Directory -Path $work)
$unique = [Guid]::NewGuid().ToString('N')
$programFiles = [Environment]::GetFolderPath('ProgramFiles').TrimEnd('\')
$app = Join-Path $programFiles "OpenRV 安装验证 $unique"
$uninstaller = Join-Path $app 'uninstall\unins000.exe'
$keyName = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{E89AA412-7B72-4AE8-9E25-F85F14E50CFB}_is1'
$report = [ordered]@{status='failed'; appRoot=$app; installerSha256=(Get-FileHash -LiteralPath $installer).Hash.ToLowerInvariant(); steps=@(); removed=$false}
$installationAttempted = $false
$associationBaseline = $null
$associationExtensions = @('.mp4','.mov','.mkv','.avi','.m4v','.webm','.mxf','.mpg','.mpeg','.wmv','.ts','.m2ts','.exr','.dpx','.cin','.tif','.tiff','.png','.jpg','.jpeg','.bmp','.tga','.hdr','.rv')
$associationProgId = 'OpenRV.Publish.Media'
$capabilityPath = 'Software\OpenRV-Publish\Capabilities'
$registeredApplication = 'OpenRV-Publish'
$report.fileAssociations = [ordered]@{status='pending'; scope='all-users'; progId=$associationProgId;
    extensions=$associationExtensions; defaultsPreserved=$false; otherApplicationsPreserved=$false;
    quotedCommandVerified=$false; shellOpenWithVerified=@(); shellFailures=@(); removed=$false}


function Get-PrivateKeys {
    $keys = @()
    foreach ($hive in [Microsoft.Win32.RegistryHive]::CurrentUser,[Microsoft.Win32.RegistryHive]::LocalMachine) {
        foreach ($view in [Microsoft.Win32.RegistryView]::Registry32,[Microsoft.Win32.RegistryView]::Registry64) {
            $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive,$view)
            try {
                $key = $base.OpenSubKey($keyName)
                if ($key) {
                    try { $keys += [pscustomobject]@{hive=[string]$hive; view=[string]$view; location=[string]$key.GetValue('InstallLocation','')} }
                    finally { $key.Dispose() }
                }
            } finally { $base.Dispose() }
        }
    }
    return $keys
}
function Assert-OwnPath([string]$Path) {
    $absolute = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if (-not ($absolute.Equals($app,[StringComparison]::OrdinalIgnoreCase) -or $absolute.StartsWith($app + '\',[StringComparison]::OrdinalIgnoreCase))) { throw 'Path is outside this unique test installation.' }
    $current = if (Test-Path -LiteralPath $absolute -PathType Leaf) { Split-Path $absolute } else { $absolute }
    while ($current -and -not $current.Equals($programFiles,[StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath $current) {
            if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point in test installation: $current" }
        }
        $current = Split-Path $current
    }
    if (-not $current) { throw 'Test installation did not resolve under Program Files.' }
}
function Invoke-Worker([string]$Name,[string]$File,[string[]]$Arguments,[hashtable]$Environment = @{}) {
    $info = [Diagnostics.ProcessStartInfo]::new($File)
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [Text.UTF8Encoding]::new($false)
    $info.StandardErrorEncoding = [Text.UTF8Encoding]::new($false)
    $info.WorkingDirectory = $work
    foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
    foreach ($entry in $Environment.GetEnumerator()) { $info.Environment[$entry.Key] = $entry.Value }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $info
    $started = $false
    try {
        $started = $process.Start()
        if (-not $started) { throw "$Name did not start." }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds*1000)) {
            if (-not $process.HasExited) { $process.Kill($true) }
            [void]$process.WaitForExit(5000)
            throw "$Name exceeded its timeout; only its own process tree was stopped."
        }
        if (-not $stdout.Wait(5000) -or -not $stderr.Wait(5000)) { throw "$Name output streams did not close." }
        $out = $stdout.GetAwaiter().GetResult()
        [IO.File]::WriteAllText((Join-Path $work "$Name.stdout.log"),$out)
        [IO.File]::WriteAllText((Join-Path $work "$Name.stderr.log"),$stderr.GetAwaiter().GetResult())
        $report.steps += @{name=$Name; exitCode=$process.ExitCode}
        if ($process.ExitCode -ne 0) { throw "$Name exited $($process.ExitCode)." }
        return $out
    } finally {
        try { if ($started -and -not $process.HasExited) { $process.Kill($true); [void]$process.WaitForExit(5000) } } finally { $process.Dispose() }
    }
}


# Read only: never set defaults, UserChoice, or another application's registration.
function Read-AssociationTree($Base,[string]$Path,[string]$Prefix,$Entries) {
    $key = $Base.OpenSubKey($Path)
    if (-not $key) { return }
    try {
        foreach ($name in @($key.GetValueNames() | Sort-Object)) {
            if ($name -eq $associationProgId -and $Path.EndsWith('\OpenWithProgids',[StringComparison]::OrdinalIgnoreCase)) { continue }
            if ($Path -eq 'Software\RegisteredApplications' -and $name -eq $registeredApplication) { continue }
            $data = $key.GetValue($name,$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            $encoded = ConvertTo-Json -InputObject $data -Compress -Depth 6
            [void]$Entries.Add("$Prefix|$Path|$name|$($key.GetValueKind($name))|$encoded")
        }
        foreach ($child in @($key.GetSubKeyNames() | Sort-Object)) {
            Read-AssociationTree $Base ($Path + '\' + $child) $Prefix $Entries
        }
    } finally { $key.Dispose() }
}
function Get-AssociationSnapshot {
    $entries = [Collections.Generic.List[string]]::new()
    foreach ($hive in [Microsoft.Win32.RegistryHive]::CurrentUser,[Microsoft.Win32.RegistryHive]::LocalMachine) {
        foreach ($view in [Microsoft.Win32.RegistryView]::Registry32,[Microsoft.Win32.RegistryView]::Registry64) {
            $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive,$view)
            try {
                foreach ($extension in $associationExtensions) {
                    Read-AssociationTree $base "Software\Classes\$extension" "$hive|$view" $entries
                    if ($hive -eq [Microsoft.Win32.RegistryHive]::CurrentUser) {
                        Read-AssociationTree $base "Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\$extension" "$hive|$view" $entries
                    }
                }
                $rvApplication = 'Software\Classes\Applications\rv.exe'
                $rvKey = $base.OpenSubKey($rvApplication)
                [void]$entries.Add("$hive|$view|$rvApplication|exists=$([bool]$rvKey)")
                if ($rvKey) { $rvKey.Dispose() }
                Read-AssociationTree $base $rvApplication "$hive|$view" $entries
                Read-AssociationTree $base 'Software\RegisteredApplications' "$hive|$view" $entries
            } finally { $base.Dispose() }
        }
    }
    return (@($entries | Sort-Object) | ConvertTo-Json -Compress)
}
function Assert-NoOwnAssociations([bool]$AllowMachine = $false) {
    # Classes can be shared between registry views; only HKCU must stay empty
    # while the all-users installation is present.

    foreach ($hive in [Microsoft.Win32.RegistryHive]::CurrentUser,[Microsoft.Win32.RegistryHive]::LocalMachine) {
        if ($AllowMachine -and $hive -eq [Microsoft.Win32.RegistryHive]::LocalMachine) { continue }
        foreach ($view in [Microsoft.Win32.RegistryView]::Registry32,[Microsoft.Win32.RegistryView]::Registry64) {
            $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive,$view)
            try {
                foreach ($path in "Software\Classes\$associationProgId",$capabilityPath) {
                    $key = $base.OpenSubKey($path)
                    if ($key) { $key.Dispose(); throw "Our association key already exists or survived uninstall: $hive/$view/$path" }
                }
                foreach ($extension in $associationExtensions) {
                    $key = $base.OpenSubKey("Software\Classes\$extension\OpenWithProgids")
                    if ($key) {
                        try { if ($key.GetValueNames() -contains $associationProgId) { throw "Our OpenWith value exists: $extension/$hive/$view" } }
                        finally { $key.Dispose() }
                    }
                }
                $key = $base.OpenSubKey('Software\RegisteredApplications')
                if ($key) {
                    try { if ($key.GetValueNames() -contains $registeredApplication) { throw 'Our RegisteredApplications value exists.' } }
                    finally { $key.Dispose() }
                }
            } finally { $base.Dispose() }
        }
    }
}
function Assert-RegisteredAssociations {
    Assert-NoOwnAssociations $true
    $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine,[Microsoft.Win32.RegistryView]::Registry64)
    function Assert-Value([string]$Path,[string]$Name,[string]$Expected) {
        $key = $base.OpenSubKey($Path)
        if (-not $key) { throw "Association key missing: $Path" }
        try {
            if ($key.GetValueNames() -notcontains $Name -or $key.GetValueKind($Name) -ne [Microsoft.Win32.RegistryValueKind]::String -or [string]$key.GetValue($Name) -cne $Expected) { throw "Association value mismatch: $Path/$Name" }
        } finally { $key.Dispose() }
    }
    try {
        $executable = Join-Path $app 'bin\rv.exe'
        Assert-Value "Software\Classes\$associationProgId\shell\open\command" '' ('"' + $executable + '" "%1"')
        Assert-Value "Software\Classes\$associationProgId\DefaultIcon" '' ('"' + $executable + '",0')
        Assert-Value "Software\Classes\$associationProgId\Application" 'ApplicationName' 'OpenRV'
        Assert-Value 'Software\RegisteredApplications' $registeredApplication $capabilityPath
        Assert-Value $capabilityPath 'ApplicationName' $registeredApplication
        foreach ($extension in $associationExtensions) {
            Assert-Value "Software\Classes\$extension\OpenWithProgids" $associationProgId ''
            Assert-Value "$capabilityPath\FileAssociations" $extension $associationProgId
        }
        $report.fileAssociations.quotedCommandVerified = $true
    } finally { $base.Dispose() }
    if ((Get-AssociationSnapshot) -cne $associationBaseline) { throw 'An existing default, UserChoice, OpenWith candidate or rv.exe registration changed.' }
    $report.fileAssociations.defaultsPreserved = $true
    $report.fileAssociations.otherApplicationsPreserved = $true
}

# Each query uses a fresh process, so Shell handler caches cannot make uninstall
# checks pass or fail against an old in-process enumeration. Nothing is invoked.
$associationQuerySource = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
namespace OpenRVInstallerSmoke {
    [ComImport, Guid("F04061AC-1659-4A3F-A954-775AA57FC083"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAssocHandler {
        [PreserveSig] int GetName([MarshalAs(UnmanagedType.LPWStr)] out string name);
        [PreserveSig] int GetUIName([MarshalAs(UnmanagedType.LPWStr)] out string name);
        [PreserveSig] int GetIconLocation([MarshalAs(UnmanagedType.LPWStr)] out string path, out int index);
    }
    [ComImport, Guid("973810AE-9599-4B88-9E4D-6EE98C9552DA"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IEnumAssocHandlers {
        [PreserveSig] int Next(uint count, [MarshalAs(UnmanagedType.Interface)] out IAssocHandler handler, out uint fetched);
    }
    public sealed class Candidate {
        public string Executable, DisplayName, IconPath;
        public int IconIndex;
        public string GetNameHresult, GetUINameHresult, GetIconLocationHresult;
    }
    public sealed class QueryResult {
        public string EnumerationHresult, FinalNextHresult, Error;
        public bool Complete;
        public Candidate[] Candidates = new Candidate[0];
    }
    public static class ShellCandidates {
        [DllImport("shell32.dll", CharSet=CharSet.Unicode)]
        static extern int SHAssocEnumHandlers(string extension, uint filter, out IEnumAssocHandlers handlers);
        static string Hex(int value) { return "0x" + unchecked((uint)value).ToString("X8"); }
        public static QueryResult Read(string extension) {
            var result = new QueryResult();
            var candidates = new List<Candidate>();
            IEnumAssocHandlers enumerator = null;
            try {
                int enumerationStatus = SHAssocEnumHandlers(extension, 0, out enumerator);
                result.EnumerationHresult = Hex(enumerationStatus);
                if (enumerationStatus < 0 || enumerator == null) {
                    result.Error = "SHAssocEnumHandlers did not return an enumeration.";
                    return result;
                }
                for (int index=0; index<512; ++index) {
                    IAssocHandler handler; uint fetched;
                    int nextStatus = enumerator.Next(1, out handler, out fetched);
                    result.FinalNextHresult = Hex(nextStatus);
                    if (nextStatus < 0) {
                        result.Error = "IEnumAssocHandlers.Next failed.";
                        return result;
                    }
                    if (fetched == 0) { result.Complete = true; return result; }
                    if (handler == null) { result.Error = "Next returned a null handler."; return result; }
                    try {
                        var candidate = new Candidate();
                        candidate.GetNameHresult = Hex(handler.GetName(out candidate.Executable));
                        candidate.GetUINameHresult = Hex(handler.GetUIName(out candidate.DisplayName));
                        candidate.GetIconLocationHresult = Hex(handler.GetIconLocation(out candidate.IconPath, out candidate.IconIndex));
                        candidates.Add(candidate);
                    } finally { Marshal.ReleaseComObject(handler); }
                }
                result.Error = "Shell returned too many association handlers.";
            } catch (Exception error) {
                result.Error = error.GetType().Name + ": " + error.Message;
            } finally {
                result.Candidates = candidates.ToArray();
                if (enumerator != null) Marshal.ReleaseComObject(enumerator);
            }
            return result;
        }
    }
}
'@
$associationQuery = Join-Path $work 'query-open-with.ps1'
[IO.File]::WriteAllText((Join-Path $work 'OpenRVInstallerSmoke.cs'),$associationQuerySource)
[IO.File]::WriteAllText($associationQuery,@'
param([string]$Extension)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -Path (Join-Path $PSScriptRoot 'OpenRVInstallerSmoke.cs')
ConvertTo-Json -InputObject ([OpenRVInstallerSmoke.ShellCandidates]::Read($Extension)) -Compress -Depth 6 -EscapeHandling EscapeNonAscii
'@)
function Assert-ShellCandidates([bool]$Installed) {
    $failures = @()
    foreach ($extension in '.mp4','.exr') {
        $phase = if ($Installed) { 'installed' } else { 'uninstalled' }
        $output = Invoke-Worker "open-with-$phase-$($extension.TrimStart('.'))" (Join-Path $PSHOME 'pwsh.exe') @('-NoProfile','-NonInteractive','-STA','-File',$associationQuery,'-Extension',$extension)
        $query = $output | ConvertFrom-Json
        $candidates = @($query.Candidates)
        $expectedExecutable = Join-Path $app 'bin\rv.exe'
        $matching = @($candidates | Where-Object { $_.Executable -and $_.Executable.Equals($expectedExecutable,[StringComparison]::OrdinalIgnoreCase) })
        $diagnostic = [ordered]@{phase=$phase;extension=$extension;expectedExecutable=$expectedExecutable;
            api=$query;matching=$matching}
        # Only Shell API names, executable/icon paths and HRESULTs from this
        # disposable runner are printed. Default/UserChoice snapshots stay private.
        $diagnosticJson = $diagnostic | ConvertTo-Json -Depth 8 -Compress -EscapeHandling EscapeNonAscii
        Write-Host "ShellOpenWithDiagnostics: $diagnosticJson"
        if ($env:GITHUB_STEP_SUMMARY) {
            Add-Content -LiteralPath $env:GITHUB_STEP_SUMMARY -Value "`n### Open With: $phase $extension`n`n``````json`n$diagnosticJson`n```````n" -Encoding utf8
        }
        $failure = $null
        if (-not $query.Complete -or $query.EnumerationHresult -ne '0x00000000') {
            $failure = "Windows Shell enumeration failed for $extension ($($query.EnumerationHresult)): $($query.Error)"
        } elseif ($Installed) {
            if ($matching.Count -lt 1 -or @($matching | Where-Object { $_.DisplayName -ceq 'OpenRV' -and $_.GetNameHresult -eq '0x00000000' -and $_.GetUINameHresult -eq '0x00000000' }).Count -lt 1) {
                $failure = "Windows Shell did not enumerate the installed executable with the OpenRV display name in Open With for $extension."
            } else { $report.fileAssociations.shellOpenWithVerified += $extension }
        } elseif ($matching.Count) { $failure = "Windows Shell still enumerates the removed OpenRV for $extension." }
        if ($failure) {
            $failures += $failure
            $report.fileAssociations.shellFailures += $diagnostic
        }
    }
    if ($failures.Count) { throw ($failures -join ' ') }
}

try {
    if (@(Get-PrivateKeys).Count) { throw 'The private AppId already exists; no existing installation will be touched.' }
    if (Test-Path -LiteralPath $app) { throw 'Unique test installation path already exists.' }
    Assert-OwnPath $app
    Assert-NoOwnAssociations
    $associationBaseline = Get-AssociationSnapshot
    $installationAttempted = $true
    [void](Invoke-Worker 'install' $installer @('/VERYSILENT','/SUPPRESSMSGBOXES','/SP-','/NOICONS','/TASKS=','/NORESTART','/LANG=english',"/DIR=$app","/LOG=$(Join-Path $work 'install.log')"))
    $keys = @(Get-PrivateKeys)
    if ($keys.Count -ne 1 -or $keys[0].hive -ne 'LocalMachine' -or $keys[0].view -ne 'Registry64' -or -not $keys[0].location.TrimEnd('\').Equals($app,[StringComparison]::OrdinalIgnoreCase)) { throw 'Expected only this test installation in HKLM 64-bit uninstall registry.' }
    foreach ($binary in 'rv.exe','rvio.exe') {
        $installed = Join-Path $app "bin\$binary"
        Assert-OwnPath $installed
        if ((Get-FileHash -LiteralPath $installed).Hash -ne (Get-FileHash -LiteralPath (Join-Path $runtime "bin\$binary")).Hash) { throw "Installed $binary differs from the release runtime." }
    }
    Assert-RegisteredAssociations
    Assert-ShellCandidates $true
    $report.fileAssociations.status = 'passed'
    $appData = Join-Path $work 'appdata'; $localData = Join-Path $work 'localappdata'; $support = Join-Path $work 'support'
    foreach ($path in $appData,$localData,$support) { [void](New-Item -ItemType Directory -Path $path) }
    $childEnvironment = @{APPDATA=$appData; LOCALAPPDATA=$localData; RV_SUPPORT_PATH=$support; RV_NO_CONSOLE_REDIRECT='1';
        RV_INIT=(Join-Path $app 'scripts\rv\rvrc.mu'); RV_PYINIT=(Join-Path $app 'scripts\rv\rvrc.py'); RVIO_INIT=(Join-Path $app 'scripts\rv\rviorc.mu');
        RV_CRASH_DUMPS_DIR=(Join-Path $work 'crashes'); PATH=((Join-Path $app 'bin') + ';' + $env:SystemRoot + '\System32;' + $env:SystemRoot)}
    $version = Invoke-Worker 'installed-version' (Join-Path $app 'bin\rv.exe') @('-noPrefs','-prefsPath',(Join-Path $work 'prefs'),'-version') $childEnvironment
    if ($version -notmatch '(?m)^\s*\d+\.\d+\.\d+\s*$') { throw 'Installed rv did not report a semantic version.' }
    $report.status = 'passed'
} catch {
    $report.error = $_.Exception.Message
    throw
} finally {
    $cleanupError = $null
    try {
      if ($installationAttempted -and (Test-Path -LiteralPath $uninstaller -PathType Leaf)) {
        Assert-OwnPath $uninstaller
        $keys = @(Get-PrivateKeys)
        foreach ($key in $keys) { if (-not $key.location.TrimEnd('\').Equals($app,[StringComparison]::OrdinalIgnoreCase)) { throw 'Uninstall key no longer belongs to this test; cleanup refused.' } }
        [void](Invoke-Worker 'uninstall' $uninstaller @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',"/LOG=$(Join-Path $work 'uninstall.log')"))
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        while ((Test-Path -LiteralPath (Join-Path $app 'bin\rv.exe')) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 200 }
      }
      $report.removed = if ($installationAttempted) { @(Get-PrivateKeys).Count -eq 0 -and -not (Test-Path -LiteralPath (Join-Path $app 'bin\rv.exe')) } else { $null }
      if ($installationAttempted -and $report.removed) {
        Assert-NoOwnAssociations
        if ((Get-AssociationSnapshot) -cne $associationBaseline) { throw 'Uninstall changed an existing default, UserChoice or another application registration.' }
        Assert-ShellCandidates $false
        $report.fileAssociations.removed = $true
      }
    } catch { $cleanupError = $_.Exception.Message; $report.cleanupError = $cleanupError; $report.removed = $false }
    if ($installationAttempted -and -not $report.fileAssociations.removed) { $report.status = 'failed' }
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $work 'report.json') -Encoding utf8
    if ($installationAttempted -and (-not $report.removed -or -not $report.fileAssociations.removed)) { throw "This test installation did not cleanly uninstall; no manual registry/filesystem deletion was attempted. $cleanupError" }
}
