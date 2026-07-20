# Quality Gate Portal - 실행 스크립트 (Windows PowerShell)
$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

$VENV_DIR = ".venv"
$MinMajor = 3
$MinMinor = 10

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Quality Gate Portal - Starting..."       -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# 0. 최신 master 코드 동기화 (로컬 db.sqlite3 는 절대 건드리지 않음 — 수동 명령어로만 변경)
Write-Host "[1/6] 최신 master 동기화..."
if (Get-Command git -ErrorAction SilentlyContinue) {
    try {
        $branch = (git rev-parse --abbrev-ref HEAD 2>$null)
        if ($branch -eq "master") {
            git fetch --quiet origin master 2>$null
            if ($LASTEXITCODE -eq 0) {
                # ff-only 라 db.sqlite3 를 write/checkout/reset 하지 않음. ff 막히면 경고 후 그대로 진행.
                git merge --ff-only --quiet origin/master 2>$null
                if ($LASTEXITCODE -eq 0) {
                    Write-Host "        origin/master 로 갱신 완료"
                } else {
                    Write-Host "        [!] fast-forward 불가 (로컬 변경/커밋/분기) - 현재 코드로 계속" -ForegroundColor Yellow
                }
            } else {
                Write-Host "        [!] git fetch 실패 (네트워크/원격?) - 현재 코드로 계속" -ForegroundColor Yellow
            }
        } else {
            Write-Host "        [!] 현재 브랜치 '$branch' (master 아님) - 동기화 건너뜀" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "        [!] 동기화 중 예외 - 현재 코드로 계속" -ForegroundColor Yellow
    }
} else {
    Write-Host "        [!] git 미설치 - 동기화 건너뜀" -ForegroundColor Yellow
}

# 1. Python 3.10+ 탐색 (py launcher 우선, 없으면 python)
function Test-PythonVersion($cmd, $args) {
    try {
        $out = & $cmd @args -c "import sys;print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
        $parts = $out.Trim().Split(".")
        return [int]$parts[0] * 100 + [int]$parts[1]
    } catch { return $null }
}

$PythonCmd = $null
$PythonArgs = @()

$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($pyLauncher) {
    foreach ($v in @("3.13", "3.12", "3.11", "3.10")) {
        $score = Test-PythonVersion "py" @("-$v")
        if ($score -and $score -ge ($MinMajor * 100 + $MinMinor)) {
            $PythonCmd = "py"
            $PythonArgs = @("-$v")
            break
        }
    }
}

if (-not $PythonCmd) {
    $pythonExe = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonExe) {
        $score = Test-PythonVersion "python" @()
        if ($score -and $score -ge ($MinMajor * 100 + $MinMinor)) {
            $PythonCmd = "python"
            $PythonArgs = @()
        }
    }
}

if (-not $PythonCmd) {
    Write-Host "[ERROR] Python $MinMajor.$MinMinor+ 을 찾을 수 없습니다. 설치 후 다시 시도해주세요." -ForegroundColor Red
    exit 1
}

$pyVersion = & $PythonCmd @PythonArgs --version 2>&1
Write-Host "[2/6] $pyVersion ($PythonCmd $($PythonArgs -join ' '))"

# 2. 기존 venv 버전 확인, 미달이면 재생성
$recreate = $false
$venvPython = Join-Path $VENV_DIR "Scripts\python.exe"
if (Test-Path $venvPython) {
    $venvScore = Test-PythonVersion $venvPython @()
    if (-not $venvScore -or $venvScore -lt ($MinMajor * 100 + $MinMinor)) {
        Write-Host "[3/6] 기존 가상환경이 Python $MinMajor.$MinMinor 미만 → 재생성"
        Remove-Item -Recurse -Force $VENV_DIR
        $recreate = $true
    }
}

if (-not (Test-Path $VENV_DIR)) {
    if (-not $recreate) { Write-Host "[3/6] 가상환경 생성..." }
    & $PythonCmd @PythonArgs -m venv $VENV_DIR
} else {
    Write-Host "[3/6] 가상환경 존재 확인"
}

# 3. 가상환경 활성화
Write-Host "[4/6] 가상환경 활성화..."
& "$VENV_DIR\Scripts\Activate.ps1"

# 4. 패키지 설치
Write-Host "[5/6] 패키지 설치..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

# 5. .env 확인
if (-not (Test-Path ".env")) {
    Write-Host "[!] .env 파일이 없습니다. .env.example을 복사합니다." -ForegroundColor Yellow
    Copy-Item .env.example .env
    Write-Host "[!] .env 파일을 편집하여 Jenkins 정보를 입력해주세요." -ForegroundColor Yellow
}

# 6. 마이그레이션
Write-Host "[6/6] 데이터베이스 마이그레이션..."
& python manage.py migrate --run-syncdb

# 7. 서버 실행
Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host "  Server: http://localhost:8601"           -ForegroundColor Green
Write-Host "  Admin:  http://localhost:8601/admin/"    -ForegroundColor Green
Write-Host "  Suites: http://localhost:8601/suites/"   -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""
& python manage.py runserver 0.0.0.0:8601
