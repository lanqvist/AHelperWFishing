; AHelperW Fishing installer (Inno Setup 6.6+ / 7). Built by tools/build.py:
;   ISCC /DAppVersion=0.4.0 /DAppDir=<frozen app> /DReleaseDir=<staging> /O<out> AHelperWFishing.iss
; Optional code signing: add /DSign /Ssigntool="signtool.exe sign /fd sha256 ... $f"
;
; Per-user install (no administrator rights) into %LOCALAPPDATA%\Programs\AHelperWFishing.
; The game addon is installed by the app itself ("AHelperWFishing.exe --install-addon"),
; elevated only if the game folder is write-protected.
; Silent install: AHelperWFishing-Setup-x.y.z.exe /VERYSILENT /GAME="D:\Games\World of Warcraft\_forever_"

#ifndef AppVersion
  #error Pass /DAppVersion=x.y.z (tools/build.py does)
#endif
#ifndef AppDir
  #error Pass /DAppDir=<folder with AHelperWFishing.exe>
#endif
#ifndef ReleaseDir
  #error Pass /DReleaseDir=<release staging folder>
#endif

#define AppName "AHelperW Fishing"
#define AppExe "AHelperWFishing.exe"
#define AppPublisher "lanqvist"
#define AppURL "https://github.com/lanqvist/AHelperWFishing"
; Held by the running app (and by the combat helper); setup waits until it is closed.
#define AppMutexName "Local\AHelperWForever.Reader.Installed"

[Setup]
AppId={{8F3C2A61-5B7E-4D2A-9C1F-6E4B0A7D9F21}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
AppCopyright=© 2026 {#AppPublisher}
AppMutex={#AppMutexName}
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
; Per user, no UAC prompt; "/ALLUSERS" on the command line still allows a machine-wide install.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
DefaultDirName={autopf}\AHelperWFishing
DisableProgramGroupPage=yes
DisableWelcomePage=no
DisableDirPage=auto
UsePreviousAppDir=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputBaseFilename=AHelperWFishing-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
#if Ver >= EncodeVer(6, 6, 0)
WizardStyle=modern dynamic
#else
WizardStyle=modern
#endif
ShowLanguageDialog=auto
CloseApplications=no
RestartApplications=no
SetupLogging=yes
#ifdef Sign
SignTool=signtool
SignedUninstaller=yes
#endif

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
ru.DesktopIcon=Ярлык на рабочем столе
en.DesktopIcon=Desktop shortcut
ru.Readme=Инструкция (ПРОЧИТАЙ)
en.Readme=Instructions (Russian)
ru.GameTitle=Аддон для игры
en.GameTitle=Game addon
ru.GameSubtitle=Куда установить HUD рыбалки (аддон AHelperWFishing)?
en.GameSubtitle=Where should the fishing HUD addon (AHelperWFishing) go?
ru.GameText=Укажи папку игры _forever_ (в ней Wow.exe и Interface). Оставь поле пустым, чтобы не ставить аддон сейчас: его можно поставить позже кнопкой «Установить аддон в игру…» в программе.
en.GameText=Select the game's _forever_ folder (it contains Wow.exe and Interface). Leave it empty to skip; the app can install the addon later.
ru.GameFound=Папка игры найдена автоматически.
en.GameFound=The game folder was found automatically.
ru.GameNotFound=Папка игры не найдена автоматически — укажи её через «Обзор…».
en.GameNotFound=The game folder was not found automatically — use Browse.
ru.GameWrongFolder=Выбери именно папку _forever_ (в ней Wow.exe и Interface).
en.GameWrongFolder=Choose the _forever_ folder itself (it contains Wow.exe and Interface).
ru.AddonElevate=Папка игры защищена от записи. Установить аддон с правами администратора?
en.AddonElevate=The game folder is write-protected. Install the addon as administrator?
ru.AddonFailed=Аддон не установлен
en.AddonFailed=The addon was not installed
ru.AddonSkipped=Аддон не устанавливался: папка игры не указана. Его можно поставить из программы.
en.AddonSkipped=The addon was skipped (no game folder). The app can install it later.
ru.AfterInstall=В игре: /reload, затем /ahwfish. Включи автосбор добычи.
en.AfterInstall=In game: /reload, then /ahwfish. Turn on auto loot.
ru.RemoveData=Удалить также настройки, журнал и записи забросов рыбалки?%n%n%1%n%nАддон в папке игры остаётся.
en.RemoveData=Also delete fishing settings, log and recorded casts?%n%n%1%n%nThe game addon stays.
ru.Launch=Запустить {#AppName}
en.Launch=Launch {#AppName}

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; The frozen runtime is replaced as a whole: no stale DLLs from an older version.
Type: filesandordirs; Name: "{app}\_internal"
; Layout of the 0.3.x self-made setup (app in a subfolder, Uninstall.cmd, Start menu folder).
Type: filesandordirs; Name: "{app}\AHelperWFishing"
Type: filesandordirs; Name: "{app}\Addon"
Type: files; Name: "{app}\Uninstall.cmd"
Type: files; Name: "{app}\install.json"
Type: files; Name: "{app}\Запустить рыбалку.cmd"
Type: files; Name: "{app}\InstallFishingAddon.cmd"
Type: files; Name: "{app}\InstallFishingAddon.ps1"
Type: filesandordirs; Name: "{userprograms}\AHelperW Fishing"

[Files]
Source: "{#AppDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#ReleaseDir}\ПРОЧИТАЙ.txt"; DestDir: "{app}"; Flags: ignoreversion isreadme
Source: "{#ReleaseDir}\Подробно\*"; DestDir: "{app}\Подробно"; Flags: ignoreversion recursesubdirs
Source: "{#ReleaseDir}\LICENSES\*"; DestDir: "{app}\LICENSES"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Comment: "Помощник рыбалки для WoW Forever"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:Launch}"; Flags: nowait postinstall skipifsilent

[Code]
var
  GamePage: TInputDirWizardPage;
  AddonMessage: String;

function IsGameFolder(const Path: String): Boolean;
begin
  Result := (CompareText(ExtractFileName(RemoveBackslashUnlessRoot(Path)), '_forever_') = 0) and
            (DirExists(AddBackslash(Path) + 'Interface') or FileExists(AddBackslash(Path) + 'Wow.exe'));
end;

function TryCandidate(const Path: String; var Found: String): Boolean;
begin
  Result := (Found = '') and (Path <> '') and IsGameFolder(Path);
  if Result then
    Found := Path;
end;

{ Same search order as the app (ahelperw_fishing/addon.py): registry, Program Files, other drives. }
function FindGame(): String;
var
  InstallPath: String;
  Letter: Integer;
  Root: String;
begin
  Result := '';
  if RegQueryStringValue(HKLM32, 'SOFTWARE\Blizzard Entertainment\World of Warcraft', 'InstallPath', InstallPath) or
     RegQueryStringValue(HKLM64, 'SOFTWARE\Blizzard Entertainment\World of Warcraft', 'InstallPath', InstallPath) then
  begin
    InstallPath := RemoveBackslashUnlessRoot(InstallPath);
    TryCandidate(InstallPath + '\_forever_', Result);
    TryCandidate(ExtractFileDir(InstallPath) + '\_forever_', Result);
  end;
  TryCandidate(ExpandConstant('{commonpf32}') + '\World of Warcraft\_forever_', Result);
  TryCandidate(ExpandConstant('{commonpf64}') + '\World of Warcraft\_forever_', Result);
  for Letter := Ord('C') to Ord('J') do
  begin
    Root := Chr(Letter) + ':\';
    if Result <> '' then
      Break;
    if not DirExists(Root) then
      Continue;
    TryCandidate(Root + 'World of Warcraft\_forever_', Result);
    TryCandidate(Root + 'Games\World of Warcraft\_forever_', Result);
    TryCandidate(Root + 'Battle.net\World of Warcraft\_forever_', Result);
    TryCandidate(Root + 'Program Files (x86)\World of Warcraft\_forever_', Result);
  end;
end;

procedure InitializeWizard();
var
  Initial, Note: String;
begin
  GamePage := CreateInputDirPage(wpSelectTasks, CustomMessage('GameTitle'), CustomMessage('GameSubtitle'),
    CustomMessage('GameText'), False, '');
  GamePage.Add('');
  Initial := ExpandConstant('{param:GAME|}');
  if Initial = '' then
    Initial := GetPreviousData('GamePath', '');
  if (Initial = '') or not IsGameFolder(Initial) then
    Initial := FindGame();
  GamePage.Values[0] := Initial;
  if Initial <> '' then
    Note := CustomMessage('GameFound')
  else
    Note := CustomMessage('GameNotFound');
  GamePage.SubCaptionLabel.Caption := GamePage.SubCaptionLabel.Caption + #13#10#13#10 + Note;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Path: String;
begin
  Result := True;
  if (GamePage <> nil) and (CurPageID = GamePage.ID) then
  begin
    Path := Trim(GamePage.Values[0]);
    if (Path <> '') and not IsGameFolder(Path) then
    begin
      MsgBox(CustomMessage('GameWrongFolder'), mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'GamePath', Trim(GamePage.Values[0]));
end;

function ReadResult(const FileName: String): String;
var
  Text: AnsiString;
begin
  Result := '';
  if LoadStringFromFile(FileName, Text) then
    Result := Trim(UTF8Decode(Text));
end;

{ Runs the installed app with --install-addon; 5 = access denied -> offer to retry elevated. }
procedure InstallAddon();
var
  Game, Exe, ResultFile, Params: String;
  Code: Integer;
begin
  Game := Trim(GamePage.Values[0]);
  if Game = '' then
  begin
    AddonMessage := CustomMessage('AddonSkipped');
    Log('Addon skipped: no game folder');
    Exit;
  end;
  Exe := ExpandConstant('{app}\{#AppExe}');
  ResultFile := ExpandConstant('{tmp}\addon-result.txt');
  Params := '--install-addon "' + Game + '" --result-file "' + ResultFile + '"';
  Log('Installing addon: ' + Params);
  if not Exec(Exe, Params, '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Code := -1;
  if (Code = 5) and ((WizardSilent() and not IsAdminInstallMode()) or
     (not WizardSilent() and (MsgBox(CustomMessage('AddonElevate'), mbConfirmation, MB_YESNO) = IDYES))) then
  begin
    if not ShellExec('runas', Exe, Params, '', SW_HIDE, ewWaitUntilTerminated, Code) then
      Code := -1;
  end;
  AddonMessage := ReadResult(ResultFile);
  if (Code <> 0) and (Code <> 2) then
  begin
    if AddonMessage = '' then
      AddonMessage := CustomMessage('AddonFailed') + ' (' + IntToStr(Code) + ')'
    else
      AddonMessage := CustomMessage('AddonFailed') + ': ' + AddonMessage;
  end;
  Log('Addon result ' + IntToStr(Code) + ': ' + AddonMessage);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    { The 0.3.x setup registered its own "Apps & features" entry; this installer replaces it. }
    if RegKeyExists(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\AHelperWFishing') then
    begin
      Log('Removing the 0.3.x uninstall entry');
      RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\AHelperWFishing');
    end;
  end;
  if CurStep = ssPostInstall then
    InstallAddon();
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and (AddonMessage <> '') then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 + AddonMessage + #13#10#13#10 +
      CustomMessage('AfterInstall');
end;

{ Only the fishing files: %LOCALAPPDATA%\AHelperWForever may also hold the combat helper's data. }
procedure RemoveFishingData();
var
  Data: String;
  Find: TFindRec;
begin
  Data := ExpandConstant('{localappdata}\AHelperWForever');
  DeleteFile(Data + '\fishing.json');
  DeleteFile(Data + '\fishing-model.json');
  DelTree(Data + '\fishing-sessions', True, True, True);
  if FindFirst(Data + '\logs\fishing.log*', Find) then
  try
    repeat
      DeleteFile(Data + '\logs\' + Find.Name);
    until not FindNext(Find);
  finally
    FindClose(Find);
  end;
  if FindFirst(Data + '\Backups\AHelperWFishing-*', Find) then
  try
    repeat
      DelTree(Data + '\Backups\' + Find.Name, True, True, True);
    until not FindNext(Find);
  finally
    FindClose(Find);
  end;
  RemoveDir(Data + '\logs');
  RemoveDir(Data + '\Backups');
  RemoveDir(Data);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent() then
  begin
    Data := ExpandConstant('{localappdata}\AHelperWForever');
    if DirExists(Data) and (MsgBox(FmtMessage(CustomMessage('RemoveData'), [Data]), mbConfirmation,
       MB_YESNO or MB_DEFBUTTON2) = IDYES) then
      RemoveFishingData();
  end;
end;
