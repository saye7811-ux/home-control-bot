@echo off
REM ===========================================================================
REM  car-hunter 실행 — 이 파일을 더블클릭하면 됩니다.
REM
REM  하는 일
REM    1) 엔카에서 대상 차종을 수집합니다 (증분이라 보통 몇 분이면 끝납니다)
REM    2) 적정가와 저평가 판정을 다시 계산하고 report.html 을 만듭니다
REM    3) 수집 결과를 GitHub 에 자동으로 올립니다 (커밋 메시지는 알아서 씁니다)
REM    4) GitHub 이 리포트를 폰에서 볼 수 있는 주소로 배포합니다
REM
REM  수집을 PC 에서 하는 이유: 엔카가 클라우드(데이터센터) IP 를 막습니다.
REM  GitHub 서버에서 돌리면 HTTP 407 로 거절당합니다. 그래서 수집은 여기서,
REM  배포는 GitHub 이 맡습니다.
REM
REM  창을 닫지 말고 두세요. 끝나면 리포트가 자동으로 열립니다.
REM ===========================================================================
setlocal
cd /d "%~dp0"

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
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
echo   car-hunter   %date% %time%
echo  ==========================================================
echo.

echo  [1/4] 엔카 수집 중... (증분이라 보통 몇 분, 처음이면 30분 이상)
"%PY%" collect.py
if errorlevel 1 goto :failed

echo.
echo  [2/4] 적정가 계산 + 리포트 생성...
"%PY%" score.py --top 20
if errorlevel 1 goto :failed

echo.
echo  [3/4] GitHub 에 올리는 중...
REM  git 저장소 최상위는 이 폴더의 부모입니다.
pushd ..
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
  echo   [i] git 저장소가 아니라 업로드를 건너뜁니다. 리포트는 만들어졌습니다.
  popd
  goto :done
)

REM  자동 실행이라 사람이 커밋 메시지를 쓰지 않아도 되게 날짜로 만듭니다.
for /f "tokens=1-3 delims=-/. " %%a in ("%date%") do set TODAY=%%a-%%b-%%c
git add car-hunter/data
git diff --cached --quiet
if not errorlevel 1 (
  echo   바뀐 내용이 없어 올릴 것이 없습니다.
  popd
  goto :done
)

git -c user.name="car-hunter" -c user.email="car-hunter@local" commit -q -m "car-hunter: %TODAY% 수집"
if errorlevel 1 (
  echo   [!] 커밋에 실패했습니다.
  popd
  goto :failed
)

REM  현재 브랜치를 main 으로 올립니다. 그 사이 다른 데서 push 했을 수 있으니
REM  rebase 로 맞춘 뒤 올립니다.
git pull --rebase --autostash origin main
if errorlevel 1 (
  echo   [!] 원격과 맞추는 데 실패했습니다. 충돌이 났을 수 있습니다.
  popd
  goto :failed
)
git push origin HEAD:main
if errorlevel 1 (
  echo   [!] push 에 실패했습니다. GitHub 로그인 상태를 확인하세요.
  popd
  goto :failed
)
popd

echo.
echo  [4/4] GitHub 이 폰용 리포트를 배포합니다 (2~3분 걸립니다)
echo        주소: https://saye7811-ux.github.io/home-control-bot/

:done
echo.
echo  ==========================================================
echo   완료. PC 용 리포트를 엽니다.
echo  ==========================================================
if exist report.html start "" "report.html"
echo.
echo   폰에서 보실 주소 (즐겨찾기 해두세요):
echo     https://saye7811-ux.github.io/home-control-bot/
echo.
echo   다음 할 일: 리포트에서 "VIN 확인 필요" 로 표시된 매물의 차대번호를
echo   BMW 는 bimmer.work, 벤츠는 mb.vin 에 넣어 에어서스를 확인하고
echo   data\vin_verified.json 에 적으면 점수에 반영됩니다.
echo.
pause
exit /b 0

:failed
echo.
echo   [!] 실행 중 오류가 났습니다. 위 메시지를 그대로 복사해서 알려주세요.
echo.
pause
exit /b 1
