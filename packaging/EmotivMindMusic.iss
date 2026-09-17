; Inno Setup script for EMOTIV Mind Music (Windows).
;
; Wraps the PyInstaller onedir output into a single setup.exe. Onedir rather
; than onefile on purpose: a onefile build unpacks itself to %TEMP% on every
; launch, which costs ten seconds or more of cold start.
;
; Built by .github/workflows/build.yml after PyInstaller runs. To build by hand
; from the repository root:
;     pyinstaller packaging/EmotivMindMusic.spec --noconfirm
;     iscc packaging/EmotivMindMusic.iss

#define AppName "EMOTIV Mind Music"
#define AppPublisher "EMOTIV"
#define AppExe "EMOTIV Mind Music.exe"
#define AppURL "https://github.com/giovaniemotiv/emotiv-mind-music"

; Overridden by the workflow with /DAppVersion=<tag>; 0.0.0 marks a local build.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

; Built from assets/logo.png by packaging/make_icon.py, which the build runs
; before PyInstaller. Kept optional so compiling by hand from a checkout that
; has not generated it still works.
#define IconFile "app_icon.ico"

[Setup]
AppId={{4F7C2A91-3B6E-4D58-9E21-7A0C5D8B6F14}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Per-user install by default, so no UAC prompt and no admin rights needed.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..
OutputBaseFilename=EMOTIV-Mind-Music-windows-x64-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
#if FileExists(AddBackslash(SourcePath) + IconFile)
SetupIconFile={#IconFile}
#endif

[Languages]
; The installer wizard is English only. The application itself is translated --
; language is chosen on first run and has nothing to do with this.
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The whole PyInstaller output folder: the exe plus _internal and its DLLs.
Source: "..\dist\{#AppName}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; \
    Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Settings, the Cortex keys, the Spotify session and every command binding
; live in ~\.emotiv_mind_music\ and are deliberately left behind on uninstall,
; so a reinstall does not send the user back through setup. Only what the
; installer itself created is removed.
Type: filesandordirs; Name: "{app}\_internal"

[Code]
{ The UI is a WebView2 control. The Evergreen runtime ships with Edge on
  current Windows 10 and 11, so this is nearly always already true -- but on a
  machine where it is missing the app opens a blank window with no explanation,
  which is a miserable way to find out. Warn and continue rather than block:
  detection can be wrong, and being wrong should not stop an install. }

const
  WEBVIEW2_GUID = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WEBVIEW2_URL = 'https://developer.microsoft.com/microsoft-edge/webview2/';

function WebView2Version(RootKey: Integer; SubKey: String): String;
begin
  Result := '';
  if not RegQueryStringValue(RootKey, SubKey, 'pv', Result) then
    Result := '';
  { An entry with 0.0.0.0 means the runtime was registered and then removed. }
  if Result = '0.0.0.0' then
    Result := '';
end;

function WebView2Installed(): Boolean;
begin
  Result :=
    (WebView2Version(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2_GUID) <> '') or
    (WebView2Version(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2_GUID) <> '') or
    (WebView2Version(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2_GUID) <> '');
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not WebView2Installed() then
    MsgBox('The Microsoft Edge WebView2 Runtime does not appear to be installed.'
      + #13#10#13#10
      + 'EMOTIV Mind Music draws its window with WebView2. Without it the app'
      + ' opens blank. It normally arrives with Microsoft Edge; if this machine'
      + ' does not have it, install the Evergreen Runtime from:'
      + #13#10#13#10 + WEBVIEW2_URL
      + #13#10#13#10
      + 'Setup will continue.', mbInformation, MB_OK);
end;
