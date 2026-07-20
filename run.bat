@echo off
REM Quality Gate Portal - 실행 스크립트 (Windows CMD)
setlocal enabledelayedexpansion

cd /d "%~dp0"

set VENV_DIR=.venv
set MIN_MAJOR=3
set MIN_MINOR=10

echo ==========================================
echo   Quality Gate Portal - Starting...
echo ==========================================

REM 0. 최신 master 코드 동기화 (로컬 db.sqlite3 는 절대 건드리지 않음 - 수동 명령어로만 변경)
echo [1/6] 최신 master 동기화...
where git >nul 2>&1
if %errorlevel% neq 0 (
    echo         [!] git 미설치 - 동기화 건너뜀
    goto :sync_done
)
set GIT_BRANCH=
for /f "tokens=*" %%b in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set GIT_BRANCH=%%b
if not "%GIT_BRANCH%"=="master" (
    echo         [!] 현재 브랜치 "%GIT_BRANCH%" master 아님 - 동기화 건너뜀
    goto :sync_done
)
git fetch --quiet origin master >nul 2>&1
if %errorlevel% neq 0 (
    echo         [!] git fetch 실패 ^(네트워크/원격?^) - 현재 코드로 계속
    goto :sync_done
)
REM ff-only 라 db.sqlite3 를 write/checkout/reset 하지 않음. ff 막히면 경고 후 그대로 진행.
git merge --ff-only --quiet origin/master >nul 2>&1
if %errorlevel% neq 0 (
    echo         [!] fast-forward 불가 ^(로컬 변경/커밋/분기^) - 현재 코드로 계속
    goto :sync_done
)
echo         origin/master 로 갱신 완료
:sync_done

REM 1. Python 3.10+ 탐색 (py launcher 우선, 없으면 python)
set PYTHON=
set PYCMD=

where py >nul 2>&1
if %errorlevel% equ 0 (
    for %%V in (3.13 3.12 3.11 3.10) do (
        py -%%V -c "import sys" >nul 2>&1
        if !errorlevel! equ 0 (
            set PYTHON=py -%%V
            set PYCMD=py
            goto :python_found
        )
    )
)

REM fallback: python 명령이 3.10+ 인지 검사
where python >nul 2>&1
if %errorlevel% equ 0 (
    for /f "tokens=1,2 delims=." %%a in ('python -c "import sys;print(f\"{sys.version_info[0]}.{sys.version_info[1]}\")"') do (
        set PY_MAJOR=%%a
        set PY_MINOR=%%b
    )
    if !PY_MAJOR! gtr %MIN_MAJOR% (
        set PYTHON=python
        set PYCMD=python
    ) else if !PY_MAJOR! equ %MIN_MAJOR% (
        if !PY_MINOR! geq %MIN_MINOR% (
            set PYTHON=python
            set PYCMD=python
        )
    )
)

:python_found
if "%PYTHON%"=="" (
    echo [ERROR] Python %MIN_MAJOR%.%MIN_MINOR%+ 을 찾을 수 없습니다. 설치 후 다시 시도해주세요.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('%PYTHON% --version 2^>^&1') do echo [2/6] %%i

REM 2. 기존 venv 버전 확인, 미달이면 재생성
set RECREATE=0
if exist "%VENV_DIR%\Scripts\python.exe" (
    for /f "tokens=1,2 delims=." %%a in ('"%VENV_DIR%\Scripts\python.exe" -c "import sys;print(f\"{sys.version_info[0]}.{sys.version_info[1]}\")"') do (
        set V_MAJOR=%%a
        set V_MINOR=%%b
    )
    set OLD=1
    if !V_MAJOR! gtr %MIN_MAJOR% set OLD=0
    if !V_MAJOR! equ %MIN_MAJOR% if !V_MINOR! geq %MIN_MINOR% set OLD=0
    if !OLD! equ 1 (
        echo [3/6] 기존 가상환경 Python !V_MAJOR!.!V_MINOR! 재생성
        rmdir /s /q "%VENV_DIR%"
        set RECREATE=1
    )
)

if not exist "%VENV_DIR%" (
    if !RECREATE! equ 0 echo [3/6] 가상환경 생성...
    %PYTHON% -m venv %VENV_DIR%
) else (
    echo [3/6] 가상환경 존재 확인
)

REM 3. 가상환경 활성화
echo [4/6] 가상환경 활성화...
call %VENV_DIR%\Scripts\activate.bat

REM 4. 패키지 설치
echo [5/6] 패키지 설치...
pip install -q --upgrade pip
pip install -q -r requirements.txt

REM 5. .env 확인
if not exist ".env" (
    echo [!] .env 파일이 없습니다. .env.example을 복사합니다.
    copy .env.example .env
    echo [!] .env 파일을 편집하여 Jenkins 정보를 입력해주세요.
)

REM 6. 마이그레이션
echo [6/6] 데이터베이스 마이그레이션...
python manage.py migrate --run-syncdb

REM 7. 서버 실행
echo.
echo ==========================================
echo   Server: http://localhost:8601
echo   Admin:  http://localhost:8601/admin/
echo   Suites: http://localhost:8601/suites/
echo ==========================================
echo.
python manage.py runserver 0.0.0.0:8601

endlocal
