@echo off
REM ===========================================================================
REM  car-hunter 주간 실행 — 이 파일을 더블클릭하면 됩니다.
REM
REM  하는 일
REM    1) 엔카에서 대상 차종을 새로 수집합니다 (data/history/ 에 날짜별 보관)
REM    2) 지난 실행과 비교해 신규 / 가격인하 / 사라진 매물을 뽑습니다
REM    3) 적정가와 저평가 판정을 다시 계산하고 report.html 을 엽니다
REM
REM  걸리는 시간: 20~40분. 엔카에 부담을 주지 않으려고 요청 사이를 3초씩
REM  띄우기 때문입니다. 이 값을 줄이지 마세요 (차단됩니다).
REM
REM  창을 닫지 말고 두세요. 끝나면 리포트가 자동으로 열립니다.
REM ===========================================================================
setlocal
cd /d "%~dp0"

set PYTHONIOENCODING=utf-8
set PY=.venv\Scripts\python.exe

if not exist "%PY%" (
  echo.
  echo   [!] 파이썬 가상환경을 찾지 못했습니다: %PY%
  echo       처음 한 번만 아래를 실행하세요:
  echo.
  echo         python -m venv .venv
  echo         .venv\Scripts\pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

echo.
echo  ==========================================================
echo   car-hunter 주간 실행   %date% %time%
echo  ==========================================================
echo.

echo  [1/2] 엔카 수집 중... (20~40분, 창을 닫지 마세요)
"%PY%" collect.py
if errorlevel 1 goto :failed

echo.
echo  [2/2] 적정가 계산 + 리포트 생성...
"%PY%" score.py --top 15
if errorlevel 1 goto :failed

echo.
echo  ==========================================================
echo   완료. 리포트를 엽니다.
echo  ==========================================================
if exist report.html start "" "report.html"
echo.
echo   다음 할 일: 리포트의 "설명되지 않는 저평가" 매물의 차량번호를
echo   헤이딜러 앱 '숨은이력찾기' 에서 조회하고, 결과 스크린샷을
echo   hidden\ 폴더에 차량번호 이름으로 저장하세요. (예: hidden\354주4191.png)
echo.
pause
exit /b 0

:failed
echo.
echo   [!] 실행 중 오류가 났습니다. 위 메시지를 그대로 복사해서 알려주세요.
echo.
pause
exit /b 1
