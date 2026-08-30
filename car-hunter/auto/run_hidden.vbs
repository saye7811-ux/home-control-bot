' ---------------------------------------------------------------------------
'  car-hunter 를 창 없이 조용히 실행합니다.
' ---------------------------------------------------------------------------
'  작업 스케줄러가 .bat 을 직접 부르면 검은 창이 뜹니다. 이 스크립트가
'  대신 받아서 창을 숨긴 채(0) 실행합니다.
'
'  실행 기록은 logs\auto_YYYY-MM-DD.log 에 남습니다. 창이 안 뜨므로
'  무슨 일이 있었는지는 그 파일을 보면 됩니다.
' ---------------------------------------------------------------------------
Option Explicit

Dim fso, shell, here, root, logDir, logFile, cmd, d, stamp

Set fso   = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

' 이 스크립트가 있는 auto\ 의 부모가 car-hunter 폴더입니다.
here = fso.GetParentFolderName(WScript.ScriptFullName)
root = fso.GetParentFolderName(here)

logDir = fso.BuildPath(root, "logs")
If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)

d = Now
stamp = Year(d) & "-" & Right("0" & Month(d), 2) & "-" & Right("0" & Day(d), 2)
logFile = fso.BuildPath(logDir, "auto_" & stamp & ".log")

' cmd /c 로 배치를 부르고 출력을 로그로 넘깁니다.
' 마지막 0 = 창 숨김, True = 끝날 때까지 기다림.
cmd = "cmd /c """"" & fso.BuildPath(root, "run_weekly.bat") & """ /auto >> """ & logFile & """ 2>&1"""

shell.Run cmd, 0, True
