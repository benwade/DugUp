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
LicenseFile=..\LICENSE
OutputDir=build\out
OutputBaseFilename=DugUp-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=DugUp
UninstallDisplayIcon={app}\python\pythonw.exe
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Messages]
WelcomeLabel2=This installs DugUp {#AppVersion}: find your files by what is in them.%n%nEverything goes into one folder for your user account, with no administrator rights needed. Nothing you have leaves this computer.%n%nPlease note: DugUp's index is not yet encrypted (encryption is planned, not implemented). It holds the text of the files you index, so keep it on an encrypted drive (BitLocker or Device encryption). The README explains more.

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
Source: "build\DugUp\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\DugUp"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m dugup window"; WorkingDir: "{app}"; Comment: "Find your files by what is in them"
Name: "{autoprograms}\DugUp Remote"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m dugup remote"; WorkingDir: "{app}"; Comment: "Start or quit DugUp in the tray, bring up search, free the graphics card"
Name: "{autodesktop}\DugUp"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m dugup window"; WorkingDir: "{app}"; Comment: "Find your files by what is in them"; Tasks: desktopicon

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: "-m dugup tray --startup add"; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; Tasks: trayatlogin
Filename: "{app}\python\pythonw.exe"; Parameters: "-m dugup download --window"; WorkingDir: "{app}"; Flags: waituntilterminated; StatusMsg: "Downloading DugUp's optional parts ..."; Check: WantDownloads
Filename: "{app}\python\pythonw.exe"; Parameters: "-m dugup window"; WorkingDir: "{app}"; Description: "Open DugUp now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{app}\python\pythonw.exe"; Parameters: "-m dugup tray --quit"; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; RunOnceId: "QuitTray"
Filename: "{app}\python\pythonw.exe"; Parameters: "-m dugup tray --startup remove"; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveStartup"

[Code]
var
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

procedure ShowRoom;
var
  FreeMB: Int64;
  Needed: Int64;
begin
  FreeMB := FreeMBFor(WizardDirValue);
  Needed := StrToInt64Def('{#BytesEverythingMB}', 0);
  if FreeMB < 0 then
    RoomLabel.Caption := 'The free space on that drive could not be read.'
  else if FreeMB - Needed < KeepFreeMB then
  begin
    RoomLabel.Caption := Format('Free on drive %s: %d MB. That is not enough room for everything ' +
      '(about {#SizeEverything}, and DugUp keeps at least %d MB free), so nothing will be downloaded now. ' +
      'Each part can still be fetched later, when it is first used.', [ExtractFileDrive(WizardDirValue), FreeMB, KeepFreeMB]);
    DownloadBox.Checked := False;
    DownloadBox.Enabled := False;
  end
  else
  begin
    RoomLabel.Caption := Format('Free on drive %s: %.1f GB. Enough room.', [ExtractFileDrive(WizardDirValue), FreeMB / 1024.0]);
    DownloadBox.Enabled := True;
  end;
end;

procedure InitializeWizard;
var
  Intro, Parts, Card: TNewStaticText;
begin
  DownloadPage := CreateCustomPage(wpSelectDir, 'Optional downloads',
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
    '   Together about {#SizeEverything}, kept in the data folder inside the DugUp folder.';

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
  if CurPageID = DownloadPage.ID then
    ShowRoom;
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
  if CurUninstallStep <> usPostUninstall then
    Exit;
  Data := ExpandConstant('{app}\data');
  if DirExists(Data) and not UninstallSilent then
  begin
    if MsgBox('Delete DugUp''s data as well?' + #13#10#13#10 +
              'It is in ' + Data + ' and holds your index (which can be built again), your settings, ' +
              'the downloaded models, and the records that let DugUp undo renames and bring back cleared-out copies. ' +
              'Those records cannot be made again.' + #13#10#13#10 +
              'Choose No to keep it: installing DugUp into the same folder again picks it all up.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(Data, True, True, True);
  end;
  RemoveDir(ExpandConstant('{app}'));    { only when nothing is left in it }
end;
