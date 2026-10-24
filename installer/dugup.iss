; DugUp for Windows: a per-user installer (no administrator rights asked for or used).
; Build the contents first (python installer/build.py), then: ISCC installer\dugup.iss
; Everything installs into one folder: the program, its own Python, and (made on first run) its data.

#include "build\defines.iss"

[Setup]
AppId={{8B3F1C2E-5D7A-4E2B-9C61-2F4A7D9E0B13}
AppName=DugUp
AppVersion={#AppVersion}
AppVerName=DugUp {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=https://github.com/benwade/DugUp
AppSupportURL=https://github.com/benwade/DugUp/issues
DefaultDirName={autopf}\DugUp
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
; The welcome page carries the notes on encryption and warranty: Inno Setup 6 hides it unless told to show it.
DisableWelcomePage=no
LicenseFile=..\LICENSE
OutputDir=build\out
OutputBaseFilename=DugUp-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=DugUp
UninstallDisplayIcon={app}\python\DugUp.exe
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Messages]
WelcomeLabel2=This installs DugUp {#AppVersion}: find your files by what is in them.%n%nEverything goes into one folder for your user account, with no administrator rights needed. Nothing you have leaves this computer unless you turn on cloud search for Claude (it is off until you do).%n%nPlease note: DugUp's index is not yet encrypted (encryption is planned, not implemented). It holds the text of the files you index, so keep it on an encrypted drive (BitLocker or Device encryption). The README explains more.%n%nDugUp is provided "as is", without warranty of any kind, express or implied, including any warranty that it is fit for a particular purpose, that it works as described, or that it is free of errors. You use it at your own risk, and the author is not liable for any damage or loss arising from its use, including lost or changed files. The license on the next page, which you must accept to install DugUp, sets out these terms in full: please read its "No Liability" section.

[Tasks]
Name: "desktopicon"; Description: "Put DugUp on the desktop"; Flags: unchecked
Name: "trayatlogin"; Description: "Start DugUp in the tray when I log in (one key then searches from anywhere)"; Flags: unchecked

[InstallDelete]
; An upgrade replaces the program's own Python whole, so nothing from an older version lingers.
; The data folder is never listed here: it is never touched by installing.
Type: filesandordirs; Name: "{app}\python"

[UninstallDelete]
; Python writes cache files beside its own while it runs, which the uninstaller did not install and would
; leave behind. The program's own Python goes whole. The data folder is never named here.
Type: filesandordirs; Name: "{app}\python"

[Files]
Source: "build\DugUp\*"; DestDir: "{app}"; Excludes: "dugup-data-folder.txt"; Flags: ignoreversion recursesubdirs createallsubdirs
; The marker names where the index lives: written from the choice on "Where DugUp keeps its index" (or, on an
; upgrade, the choice already made), so it is copied and then rewritten.
Source: "build\DugUp\dugup-data-folder.txt"; DestDir: "{app}"; Flags: ignoreversion; AfterInstall: WriteMarker

[Icons]
; Every shortcut starts DugUp.exe (installer/launcher): Windows then shows DugUp, not Python, and the app ID
; matches the one DugUp claims when it runs (identity.py), so pinned and running DugUp are one taskbar button.
Name: "{autoprograms}\DugUp"; Filename: "{app}\python\DugUp.exe"; Parameters: "-m dugup window"; WorkingDir: "{app}"; AppUserModelID: "DugUp.DugUp"; Comment: "Find your files by what is in them"
Name: "{autoprograms}\DugUp Remote"; Filename: "{app}\python\DugUp.exe"; Parameters: "-m dugup remote"; WorkingDir: "{app}"; AppUserModelID: "DugUp.DugUp"; Comment: "Start or quit DugUp in the tray, bring up search, free the graphics card"
Name: "{autoprograms}\Report a problem with DugUp"; Filename: "{app}\python\DugUp.exe"; Parameters: "-m dugup report"; WorkingDir: "{app}"; AppUserModelID: "DugUp.DugUp"; Comment: "Gather what helps find a problem, read it, and choose how to send it (works even when DugUp is stuck)"
Name: "{autodesktop}\DugUp"; Filename: "{app}\python\DugUp.exe"; Parameters: "-m dugup window"; WorkingDir: "{app}"; AppUserModelID: "DugUp.DugUp"; Comment: "Find your files by what is in them"; Tasks: desktopicon

[Run]
; An upgrade remakes a start-at-login entry that is there, so an older script (which started Python) becomes
; a "DugUp tray" shortcut to DugUp.exe. Nothing is made where there was none.
Filename: "{app}\python\DugUp.exe"; Parameters: "-m dugup tray --startup refresh"; WorkingDir: "{app}"; Flags: runhidden waituntilterminated
Filename: "{app}\python\DugUp.exe"; Parameters: "-m dugup tray --startup add"; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; Tasks: trayatlogin
Filename: "{app}\python\DugUp.exe"; Parameters: "-m dugup download --window"; WorkingDir: "{app}"; Flags: waituntilterminated; StatusMsg: "Downloading DugUp's optional parts ..."; Check: WantDownloads
Filename: "{app}\python\DugUp.exe"; Parameters: "-m dugup window"; WorkingDir: "{app}"; Description: "Open DugUp now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{app}\python\DugUp.exe"; Parameters: "-m dugup tray --quit"; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; RunOnceId: "QuitTray"
Filename: "{app}\python\DugUp.exe"; Parameters: "-m dugup tray --startup remove"; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveStartup"

[Code]
var
  IndexPage: TInputDirWizardPage;
  IndexAuto: Boolean;        { the index folder is still the default, so it follows the install folder }
  KeptData: String;          { on an upgrade: the marker's data line, kept as it was }
  UninstallData: String;     { the data folder the uninstaller asks about }
  DownloadPage: TWizardPage;
  DownloadBox: TNewCheckBox;
  RoomLabel: TNewStaticText;

const
  KeepFreeMB = 200;   { DugUp's own default margin (min_free_mb) }

function FreeMBFor(const Folder: String): Int64;
var
  Free, Total: Int64;
begin
  Result := -1;
  if GetSpaceOnDisk64(ExtractFileDrive(Folder), Free, Total) then
    Result := Free div (1024 * 1024);
end;

const
  MarkerName = 'dugup-data-folder.txt';
  SyncCaveat = 'DugUp checks this when it starts, for the sync apps it knows (OneDrive, Google Drive, Dropbox, ' +
    'and folders Windows lists as synced), and keeps the index out of a synced folder it finds. That check is not a ' +
    'guarantee: it may not catch a synced folder every time, and it does not detect other sync or backup apps at all.';

function DefaultIndexFolder: String;
begin
  Result := AddBackslash(WizardDirValue) + 'data';
end;

function IsAbsolute(const Path: String): Boolean;
begin
  Result := ((Length(Path) >= 3) and (Path[2] = ':') and ((Path[3] = '\') or (Path[3] = '/'))) or (Copy(Path, 1, 2) = '\\');
end;

function MarkerData(const Marker: String): String;
{ The data line of a marker ('data' when it has none), as written: relative to the marker's folder, or absolute. }
var
  Lines: TArrayOfString;
  I, Eq: Integer;
  Line: String;
begin
  Result := 'data';
  if not LoadStringsFromFile(Marker, Lines) then
    Exit;
  for I := 0 to GetArrayLength(Lines) - 1 do
  begin
    Line := Trim(Lines[I]);
    Eq := Pos('=', Line);
    if (Eq > 1) and (Copy(Line, 1, 1) <> '#') and (CompareText(Trim(Copy(Line, 1, Eq - 1)), 'data') = 0) then
    begin
      Line := Trim(Copy(Line, Eq + 1, Length(Line)));
      StringChangeEx(Line, '"', '', True);
      if Line <> '' then
        Result := Line;
    end;
  end;
end;

function DataFolderOf(const AppDir, Data: String): String;
begin
  if IsAbsolute(Data) then
    Result := Data
  else
    Result := AddBackslash(AppDir) + Data;
end;

function IsUpgrade: Boolean;
begin
  Result := FileExists(AddBackslash(WizardDirValue) + MarkerName);
end;

function ChosenIndexFolder: String;
{ The data line to write: 'data' for the folder inside the DugUp folder, or the folder chosen. }
begin
  if WizardSilent then
    Result := Trim(ExpandConstant('{param:INDEXDIR|}'))
  else
    Result := Trim(IndexPage.Values[0]);
  Result := RemoveBackslashUnlessRoot(Result);
  if (Result = '') or (CompareText(Result, DefaultIndexFolder) = 0) then
    Result := 'data';
end;

procedure WriteMarker;
var
  Lines: TArrayOfString;
  Data: String;
begin
  if KeptData <> '' then
    Data := KeptData
  else
    Data := ChosenIndexFolder;
  SetArrayLength(Lines, 5);
  Lines[0] := '# DugUp keeps its index, settings, logs and records in the folder named on the "data" line (beside this file';
  Lines[1] := '# unless it is a full path). "skip" lines name the program''s own folders, which are never indexed.';
  Lines[2] := 'data = ' + Data;
  Lines[3] := 'skip = python';
  Lines[4] := '';
  SaveStringsToUTF8File(ExpandConstant('{app}\') + MarkerName, Lines, False);
end;

procedure ShowRoom;
var
  FreeMB: Int64;
  Needed: Int64;
begin
  FreeMB := FreeMBFor(DataFolderOf(WizardDirValue, ChosenIndexFolder));
  Needed := StrToInt64Def('{#BytesEverythingMB}', 0);
  if FreeMB < 0 then
    RoomLabel.Caption := 'The free space on that drive could not be read.'
  else if FreeMB - Needed < KeepFreeMB then
  begin
    RoomLabel.Caption := Format('Free on drive %s: %d MB. That is not enough room for everything ' +
      '(about {#SizeEverything}, and DugUp keeps at least %d MB free), so nothing will be downloaded now. ' +
      'Each part can still be fetched later, when it is first used.', [ExtractFileDrive(DataFolderOf(WizardDirValue, ChosenIndexFolder)), FreeMB, KeepFreeMB]);
    DownloadBox.Checked := False;
    DownloadBox.Enabled := False;
  end
  else
  begin
    RoomLabel.Caption := Format('Free on drive %s: %.1f GB. Enough room.', [ExtractFileDrive(DataFolderOf(WizardDirValue, ChosenIndexFolder)), FreeMB / 1024.0]);
    DownloadBox.Enabled := True;
  end;
end;

procedure InitializeWizard;
var
  Intro, Parts, Card: TNewStaticText;
begin
  IndexPage := CreateInputDirPage(wpSelectDir, 'Where DugUp keeps its index',
    'The index, your settings, the downloaded parts and DugUp''s records all live in one folder.',
    'The index holds the text of the files you index, and it is not yet encrypted. Keep it out of any folder that a ' +
    'cloud-sync or backup app uploads (OneDrive, Google Drive, Dropbox and the like). The usual place is the data ' +
    'folder inside the DugUp folder; if you installed DugUp itself in a synced folder, choose another folder here.' + #13#10#13#10 +
    SyncCaveat, False, 'DugUp data');
  IndexPage.Add('');
  IndexPage.Values[0] := '';
  IndexAuto := True;

  DownloadPage := CreateCustomPage(IndexPage.ID, 'Optional downloads',
    'Most people download these now, so setting up is done in one go.');

  Intro := TNewStaticText.Create(DownloadPage);
  Intro.Parent := DownloadPage.Surface;
  Intro.WordWrap := True;
  Intro.Width := DownloadPage.SurfaceWidth;
  Intro.Caption := 'DugUp works as soon as it is installed: searching names and the words inside documents, ' +
    'and reading words in pictures and scans, need nothing more. These parts add more, and each is otherwise ' +
    'downloaded the first time you use it:';

  Parts := TNewStaticText.Create(DownloadPage);
  Parts.Parent := DownloadPage.Surface;
  Parts.WordWrap := True;
  Parts.Width := DownloadPage.SurfaceWidth;
  Parts.Top := Intro.Top + Intro.Height + ScaleY(8);
  Parts.Caption :=
    '   - Search by meaning: {#SizeMeaning} (or {#SizeMeaningSmall} on a smaller computer; DugUp picks the one that suits this one)' + #13#10 +
    '   - Pictures by likeness: {#SizeLooks}' + #13#10 +
    '   - Naming pictures from what they show: {#SizeCaptions}' + #13#10 +
    '   Together about {#SizeEverything}, kept in the folder chosen for the index.';

  Card := TNewStaticText.Create(DownloadPage);
  Card.Parent := DownloadPage.Surface;
  Card.WordWrap := True;
  Card.Width := DownloadPage.SurfaceWidth;
  Card.Top := Parts.Top + Parts.Height + ScaleY(10);
  Card.Caption := 'Graphics card: DugUp includes a runtime that can use NVIDIA, AMD and Intel cards. When it first ' +
    'opens, it measures whether your card is actually faster than the processor, and uses the card only if it is. ' +
    'Each file is downloaded from one fixed address and checked against its known checksum before use.';

  DownloadBox := TNewCheckBox.Create(DownloadPage);
  DownloadBox.Parent := DownloadPage.Surface;
  DownloadBox.Top := Card.Top + Card.Height + ScaleY(14);
  DownloadBox.Width := DownloadPage.SurfaceWidth;
  DownloadBox.Height := ScaleY(20);
  DownloadBox.Font.Style := [fsBold];
  DownloadBox.Caption := 'Download everything now (about {#SizeEverything}; recommended)';
  DownloadBox.Checked := True;

  RoomLabel := TNewStaticText.Create(DownloadPage);
  RoomLabel.Parent := DownloadPage.Surface;
  RoomLabel.WordWrap := True;
  RoomLabel.Width := DownloadPage.SurfaceWidth;
  RoomLabel.Top := DownloadBox.Top + DownloadBox.Height + ScaleY(6);
  RoomLabel.Caption := '';
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = IndexPage.ID) and (IndexAuto or (IndexPage.Values[0] = '')) then
    IndexPage.Values[0] := DefaultIndexFolder;      { follows the install folder until changed by hand }
  if CurPageID = DownloadPage.ID then
    ShowRoom;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { An upgrade keeps the index where it already is; DugUp itself can move it later (dugup data move). }
  Result := (PageID = IndexPage.ID) and IsUpgrade;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Folder: String;
begin
  Result := True;
  if CurPageID = IndexPage.ID then
  begin
    Folder := Trim(IndexPage.Values[0]);
    Folder := RemoveBackslashUnlessRoot(Folder);
    IndexAuto := CompareText(Folder, DefaultIndexFolder) = 0;
    if not IsAbsolute(Folder) then
    begin
      MsgBox('Please choose a full folder path, such as C:\DugUp data.', mbError, MB_OK);
      Result := False;
    end
    else if Pos(LowerCase(AddBackslash(WizardDirValue) + 'python'), LowerCase(AddBackslash(Folder))) = 1 then
    begin
      MsgBox('That folder is part of DugUp''s own program files. Please choose another.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssInstall) and IsUpgrade then
    KeptData := MarkerData(AddBackslash(WizardDirValue) + MarkerName);
end;

function WantDownloads: Boolean;
begin
  { Silent installs download only when asked: /DOWNLOADS=yes }
  if WizardSilent then
    Result := CompareText(ExpandConstant('{param:DOWNLOADS|no}'), 'yes') = 0
  else
    Result := DownloadBox.Checked and DownloadBox.Enabled;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
  Existing: String;
begin
  { An upgrade: ask a running tray to quit first, so its files are free to be replaced. }
  Existing := ExpandConstant('{app}\python\pythonw.exe');
  if FileExists(Existing) then
    Exec(Existing, '-m dugup tray --quit', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep = usUninstall then      { before the files go: the marker says where the data is }
    UninstallData := DataFolderOf(ExpandConstant('{app}'), MarkerData(ExpandConstant('{app}\') + MarkerName));
  if CurUninstallStep <> usPostUninstall then
    Exit;
  Data := UninstallData;
  if Data = '' then
    Data := ExpandConstant('{app}\data');
  if DirExists(Data) and not UninstallSilent then
  begin
    if MsgBox('Delete DugUp''s data as well?' + #13#10#13#10 +
              'It is in ' + Data + ' and holds your index (which can be built again), your settings, ' +
              'the downloaded models, and the records that let DugUp undo renames and bring back cleared-out copies. ' +
              'Those records cannot be made again.' + #13#10#13#10 +
              'Choose No to keep it: installing DugUp again and choosing the same folder for the index picks it all up.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(Data, True, True, True);
  end;
  RemoveDir(ExpandConstant('{app}'));    { only when nothing is left in it }
end;
