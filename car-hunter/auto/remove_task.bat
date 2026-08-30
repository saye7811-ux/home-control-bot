@echo off
REM ===========================================================================
REM  car-hunter 자동 실행 끄기 - 이 파일을 더블클릭하세요.
REM
REM  등록만 지웁니다. 프로그램과 수집한 데이터는 그대로 남으므로,
REM  나중에 setup_task.bat 로 다시 켤 수 있고 run_weekly.bat 를 직접
REM  더블클릭해서 수동으로 돌리는 것도 그대로 됩니다.
REM ===========================================================================
setlocal
set TASKNAME=car-hunter

echo.
echo  ==========================================================
echo   car-hunter 자동 실행 끄기
echo  ==========================================================
echo.

schtasks /Query /TN "%TASKNAME%" >nul 2>&1
if errorlevel 1 (
  echo   등록된 자동 실행이 없습니다. 이미 꺼져 있습니다.
  echo.
  pause
  exit /b 0
)

schtasks /Delete /TN "%TASKNAME%" /F
if errorlevel 1 (
  echo.
  echo   [!] 끄는 데 실패했습니다. 위 메시지를 알려주세요.
  pause
  exit /b 1
)

echo.
echo   자동 실행을 껐습니다.
echo   프로그램과 데이터는 그대로 있습니다.
echo   - 수동 실행: run_weekly.bat 더블클릭
echo   - 다시 켜기: auto\setup_task.bat 더블클릭
echo.
pause
exit /b 0
