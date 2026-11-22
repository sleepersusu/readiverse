; Readiverse Windows 安裝程式（Inno Setup 6），跟雅思練習室一樣：
; 預設裝在「使用者\AppData\Local\Programs\Readiverse」：不用管理員權限
; 書庫（書、筆記、收藏）放在「使用者\Readiverse書庫」或你選的 Google 雲端硬碟／OneDrive，更新、重裝、解除安裝都不會動到
#define AppVer GetEnv("APP_VERSION")
#if AppVer == ""
  #define AppVer "0.0.0"
#endif

[Setup]
AppId={{5D2B7C9E-3A41-4F6B-8E0D-7A1C2B3D4E5F}
AppName=Readiverse
AppVersion={#AppVer}
AppVerName=Readiverse {#AppVer}
AppPublisher=SU
DefaultDirName={localappdata}\Programs\Readiverse
DefaultGroupName=Readiverse
DisableProgramGroupPage=yes
DisableDirPage=no
UsePreviousAppDir=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
OutputDir=..\dist
OutputBaseFilename=Readiverse-Setup
SetupIconFile=..\assets\icon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=Readiverse
UninstallDisplayIcon={app}\Readiverse.exe
CloseApplications=yes
RestartApplications=no

[Languages]
#ifexist "ChineseTraditional.isl"
Name: "zh"; MessagesFile: "ChineseTraditional.isl"
#else
Name: "en"; MessagesFile: "compiler:Default.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "在桌面建立捷徑"; GroupDescription: "捷徑："

[InstallDelete]
; 更新時先清掉舊版的程式檔（書庫不在這裡）
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\Readiverse\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Readiverse"; Filename: "{app}\Readiverse.exe"
Name: "{autodesktop}\Readiverse"; Filename: "{app}\Readiverse.exe"; Tasks: desktopicon

[Run]
; 裝完直接打開（自動更新的安靜模式也會打開）
Filename: "{app}\Readiverse.exe"; Description: "打開 Readiverse"; Flags: nowait postinstall

[Code]
function OpenProcess(dwDesiredAccess: Cardinal; bInheritHandle: Boolean; dwProcessId: Cardinal): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(hHandle: THandle; dwMilliseconds: Cardinal): Cardinal;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(hObject: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

{ 自動更新：Readiverse 帶 /WAITPID=程式編號 叫起安裝程式，先等舊的 Readiverse 完全關掉（最多 2 分鐘）再開始裝 }
function InitializeSetup(): Boolean;
var
  Pid: Integer;
  H: THandle;
begin
  Pid := StrToIntDef(ExpandConstant('{param:WAITPID|0}'), 0);
  if Pid > 0 then
  begin
    H := OpenProcess($00100000, False, Pid);
    if H <> 0 then
    begin
      WaitForSingleObject(H, 120000);
      CloseHandle(H);
    end;
    Sleep(1500);
  end;
  Result := True;
end;

{ 選安裝位置：Program Files 之類需要管理員權限的地方不行 }
function NextButtonClick(CurPageID: Integer): Boolean;
var
  D: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    D := Lowercase(AddBackslash(WizardDirValue));
    if (Pos(Lowercase(AddBackslash(ExpandConstant('{commonpf32}'))), D) = 1) or
       (IsWin64 and (Pos(Lowercase(AddBackslash(ExpandConstant('{commonpf64}'))), D) = 1)) or
       (Pos(Lowercase(AddBackslash(ExpandConstant('{win}'))), D) = 1) then
    begin
      MsgBox('這個位置需要系統管理員權限。請改用預設位置，或選你自己的資料夾（例如 D:\Apps\Readiverse）。', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

{ 解除安裝：程式一定會刪掉；書庫不刪（書和筆記留著，重新安裝還能接著用） }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep <> usPostUninstall then Exit;
  if UninstallSilent then Exit;
  MsgBox('Readiverse 已經移除。' + #13#10 + #13#10 +
         '你的書、筆記和收藏都還在（使用者資料夾的「Readiverse書庫」，或你選的雲端硬碟），重新安裝後會接著用。',
         mbInformation, MB_OK);
end;
