@echo off
REM ===========================================================================
REM  car-hunter 자동 실행 켜기 - 이 파일을 더블클릭하세요.
REM
REM  등록하면 이렇게 됩니다:
REM    - 매일 오전 9시에 자동 실행
REM    - PC 가 꺼져 있어 놓친 날은, 다음에 켜면 곧바로 따라잡아 실행
REM    - 검은 창 없이 조용히 (기록은 logs\auto_날짜.log)
REM    - 하루에 한 번만 (하루에 여러 번 켜도 중복 실행 안 함)
REM
REM  [관리자 권한으로 실행]하면 "로그인 5분 뒤" 트리거도 함께 넣습니다.
REM  윈도우는 로그온 트리거 등록에 관리자 권한을 요구하기 때문입니다.
REM  일반 실행이어도 위 기능은 모두 그대로 동작합니다.
REM
REM  끄려면: 같은 폴더의 remove_task.bat 를 더블클릭하세요.
REM ===========================================================================
setlocal
cd /d "%~dp0"

set TASKNAME=car-hunter
set ROOT=%~dp0..
for %%I in ("%ROOT%") do set ROOT=%%~fI
set VBS=%ROOT%\auto\run_hidden.vbs
set TMPXML=%TEMP%\car-hunter-task.xml

echo.
echo  ==========================================================
echo   car-hunter 자동 실행 등록
echo  ==========================================================
echo   대상 폴더 : %ROOT%
echo.

if not exist "%VBS%" (
  echo   [!] 실행 스크립트를 찾지 못했습니다: %VBS%
  pause
  exit /b 1
)
if not exist "%ROOT%\.venv\Scripts\python.exe" (
  echo   [!] 파이썬 가상환경이 없습니다: %ROOT%\.venv
  echo       먼저 아래를 한 번 실행하세요:
  echo         python -m venv .venv
  echo         .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)

REM  이미 있으면 지우고 새로 등록합니다 (경로가 바뀌었을 수 있으므로).
schtasks /Query /TN "%TASKNAME%" >nul 2>&1
if not errorlevel 1 schtasks /Delete /TN "%TASKNAME%" /F >nul 2>&1

REM  1차: 로그온 트리거 포함으로 시도합니다 (관리자 권한이 있으면 성공).
call :make car-hunter-task.xml
schtasks /Create /TN "%TASKNAME%" /XML "%TMPXML%" >nul 2>&1
if not errorlevel 1 (
  set "MODE=로그인 5분 뒤 + 매일 오전 9시"
  goto :ok
)

REM  2차: 로그온 트리거를 빼고 다시 시도합니다 (권한 불필요).
call :make car-hunter-task-nologon.xml
schtasks /Create /TN "%TASKNAME%" /XML "%TMPXML%"
if errorlevel 1 (
  echo.
  echo   [!] 등록에 실패했습니다. 위 메시지를 알려주세요.
  del "%TMPXML%" >nul 2>&1
  pause
  exit /b 1
)
set "MODE=매일 오전 9시 (놓치면 PC 켤 때 따라잡기)"

:ok
del "%TMPXML%" >nul 2>&1
echo.
echo  ==========================================================
echo   등록 완료
echo  ==========================================================
echo.
echo   실행 시점 : %MODE%
echo   실행 기록 : %ROOT%\logs\auto_날짜.log
echo   폰 리포트 : https://saye7811-ux.github.io/home-control-bot/
echo.
echo   지금 바로 한 번 돌려 보려면:
echo      schtasks /Run /TN "car-hunter"
echo.
echo   끄려면 이 폴더의 remove_task.bat 를 더블클릭하세요.
echo.
pause
exit /b 0

REM --- XML 의 자리표시자를 실제 값으로 바꿔 임시 파일을 만듭니다 -------------
REM     작업 스케줄러는 XML 을 UTF-16 으로 요구합니다.
:make
powershell -NoProfile -ExecutionPolicy Bypass -Command "$u = $env:USERDOMAIN + '\' + $env:USERNAME; $x = Get-Content -Raw -Encoding UTF8 '%~1'; $x = $x.Replace('__VBS__', $env:VBS).Replace('__ROOT__', $env:ROOT).Replace('__USER__', $u); Set-Content -Path $env:TMPXML -Value $x -Encoding Unicode"
exit /b 0
