; English / Simplified Chinese all-users installer with Open With candidates. No PATH/default changes.
#ifndef SourceDir
  #error "SourceDir is required"
#endif
#ifndef RepositoryUrl
  #error "RepositoryUrl is required"
#endif
[Setup]
AppId={{E89AA412-7B72-4AE8-9E25-F85F14E50CFB}
AppName=OpenRV
AppVersion={#ReleaseVersion}
AppVerName=OpenRV {#ReleaseVersion}
AppComments=English / Simplified Chinese community build. Source commit: {#Commit}
AppPublisher=EternalSunLhx
AppPublisherURL={#RepositoryUrl}
AppSupportURL={#RepositoryUrl}/issues
AppUpdatesURL={#RepositoryUrl}/releases
DefaultDirName={autopf}\OpenRV
DefaultGroupName=OpenRV
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
SetupIconFile={#RepoDir}\src\bin\apps\rv\RV.ico
UninstallDisplayName=OpenRV {#ReleaseVersion}
UninstallDisplayIcon={app}\bin\rv.exe
UninstallFilesDir={app}\uninstall
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany=EternalSunLhx
VersionInfoDescription=OpenRV bilingual Release installer
VersionInfoTextVersion={#ReleaseVersion}
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
ChangesAssociations=yes
ChangesEnvironment=no
LicenseFile={#RepoDir}\LICENSE
LanguageDetectionMethod=uilanguage
ShowLanguageDialog=yes
UsePreviousLanguage=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\OpenRV\OpenRV"; Filename: "{app}\bin\rv.exe"; WorkingDir: "{app}\bin"
Name: "{autoprograms}\OpenRV\{cm:UninstallProgram,OpenRV}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\OpenRV"; Filename: "{app}\bin\rv.exe"; WorkingDir: "{app}\bin"; Tasks: desktopicon


[CustomMessages]
english.MediaType=OpenRV media
english.MediaDescription=Play and review video, image sequences and RV sessions in OpenRV.
chinesesimplified.MediaType=OpenRV 媒体
chinesesimplified.MediaDescription=使用 OpenRV 播放和审阅视频、图像序列及 RV 会话。

[Registry]
; A private ProgID avoids overwriting ShotGrid RV's identically named rv.exe registration.
; Shared extension keys receive only our OpenWithProgids value, never a default/UserChoice.
Root: HKLM64; Subkey: "Software\Classes\OpenRV.Publish.Media"; ValueType: string; ValueName: ""; ValueData: "{cm:MediaType}"; Flags: uninsdeletekey
Root: HKLM64; Subkey: "Software\Classes\OpenRV.Publish.Media\Application"; ValueType: string; ValueName: "ApplicationName"; ValueData: "OpenRV"
Root: HKLM64; Subkey: "Software\Classes\OpenRV.Publish.Media\Application"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "{cm:MediaDescription}"
Root: HKLM64; Subkey: "Software\Classes\OpenRV.Publish.Media\Application"; ValueType: string; ValueName: "ApplicationIcon"; ValueData: """{app}\bin\rv.exe"",0"
Root: HKLM64; Subkey: "Software\Classes\OpenRV.Publish.Media\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: """{app}\bin\rv.exe"",0"
Root: HKLM64; Subkey: "Software\Classes\OpenRV.Publish.Media\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\bin\rv.exe"" ""%1"""
Root: HKLM64; Subkey: "Software\OpenRV-Publish"; Flags: uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "OpenRV-Publish"; Flags: uninsdeletekey
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "{cm:MediaDescription}"
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities"; ValueType: string; ValueName: "ApplicationIcon"; ValueData: """{app}\bin\rv.exe"",0"
Root: HKLM64; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "OpenRV-Publish"; ValueData: "Software\OpenRV-Publish\Capabilities"; Flags: uninsdeletevalue
Root: HKLM64; Subkey: "Software\Classes\.mp4\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mp4"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.mov\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mov"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.mkv\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mkv"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.avi\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".avi"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.m4v\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".m4v"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.webm\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".webm"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.mxf\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mxf"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.mpg\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mpg"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.mpeg\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mpeg"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.wmv\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".wmv"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.ts\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".ts"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.m2ts\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".m2ts"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.exr\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".exr"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.dpx\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".dpx"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.cin\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".cin"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.tif\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".tif"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.tiff\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".tiff"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.png\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".png"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.jpg\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".jpg"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.jpeg\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".jpeg"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.bmp\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".bmp"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.tga\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".tga"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.hdr\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".hdr"; ValueData: "OpenRV.Publish.Media"
Root: HKLM64; Subkey: "Software\Classes\.rv\OpenWithProgids"; ValueType: string; ValueName: "OpenRV.Publish.Media"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKLM64; Subkey: "Software\OpenRV-Publish\Capabilities\FileAssociations"; ValueType: string; ValueName: ".rv"; ValueData: "OpenRV.Publish.Media"

[Run]
Filename: "{app}\bin\rv.exe"; Description: "{cm:LaunchProgram,OpenRV}"; WorkingDir: "{app}\bin"; Flags: nowait postinstall skipifsilent unchecked
