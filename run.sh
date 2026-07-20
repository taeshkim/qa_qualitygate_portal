#!/bin/bash
# Quality Gate Portal - 실행 스크립트 (Mac / Linux)
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR=".venv"
MIN_MAJOR=3
MIN_MINOR=10

echo "=========================================="
echo "  Quality Gate Portal - Starting..."
echo "=========================================="

# 0. 최신 master 코드 동기화 (로컬 db.sqlite3 는 절대 건드리지 않음 — 수동 명령어로만 변경)
echo "[1/6] 최신 master 동기화..."
if git rev-parse --is-inside-work-tree &> /dev/null; then
    BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
    if [ "$BRANCH" = "master" ]; then
        if git fetch --quiet origin master 2>/dev/null; then
            # ff-only 라 db.sqlite3 를 write/checkout/reset 하지 않음.
            # 원격이 db.sqlite3 를 바꾼 커밋이면 로컬 변경과 충돌해 ff 가 막히고 → 아래 경고 후 그대로 진행.
            if git merge --ff-only --quiet origin/master 2>/dev/null; then
                echo "        → origin/master 로 갱신 완료"
            else
                echo "        [!] fast-forward 불가 (로컬 변경/커밋/분기) — 현재 코드로 계속"
            fi
        else
            echo "        [!] git fetch 실패 (네트워크/원격?) — 현재 코드로 계속"
        fi
    else
        echo "        [!] 현재 브랜치 '$BRANCH' (master 아님) — 동기화 건너뜀"
    fi
else
    echo "        [!] git 저장소 아님 — 동기화 건너뜀"
fi

# 1. Python 3.10+ 탐색 (python3.13 > 3.12 > 3.11 > 3.10 > python3)
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" &> /dev/null; then
        ver=$("$candidate" -c 'import sys;print(f"{sys.version_info[0]} {sys.version_info[1]}")' 2>/dev/null || echo "")
        if [ -n "$ver" ]; then
            major=$(echo "$ver" | awk '{print $1}')
            minor=$(echo "$ver" | awk '{print $2}')
            if [ "$major" -gt "$MIN_MAJOR" ] || { [ "$major" -eq "$MIN_MAJOR" ] && [ "$minor" -ge "$MIN_MINOR" ]; }; then
                PYTHON="$candidate"
                break
            fi
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "[ERROR] Python ${MIN_MAJOR}.${MIN_MINOR}+ 을 찾을 수 없습니다. 설치 후 다시 시도해주세요."
    exit 1
fi

echo "[2/6] Python: $($PYTHON --version) ($(command -v $PYTHON))"

# 2. 가상환경 생성 (기존 venv가 3.10 미만이면 재생성)
RECREATE=0
if [ -d "$VENV_DIR" ]; then
    venv_ver=$("$VENV_DIR/bin/python" -c 'import sys;print(f"{sys.version_info[0]} {sys.version_info[1]}")' 2>/dev/null || echo "0 0")
    v_major=$(echo "$venv_ver" | awk '{print $1}')
    v_minor=$(echo "$venv_ver" | awk '{print $2}')
    if [ "$v_major" -lt "$MIN_MAJOR" ] || { [ "$v_major" -eq "$MIN_MAJOR" ] && [ "$v_minor" -lt "$MIN_MINOR" ]; }; then
        echo "[3/6] 기존 가상환경 Python ${v_major}.${v_minor} → ${MIN_MAJOR}.${MIN_MINOR}+ 로 재생성"
        rm -rf "$VENV_DIR"
        RECREATE=1
    fi
fi

if [ ! -d "$VENV_DIR" ]; then
    [ "$RECREATE" -eq 0 ] && echo "[3/6] 가상환경 생성..."
    $PYTHON -m venv "$VENV_DIR"
else
    echo "[3/6] 가상환경 존재 확인"
fi

# 3. 가상환경 활성화
echo "[4/6] 가상환경 활성화..."
source "$VENV_DIR/bin/activate"

# 4. 패키지 설치
echo "[5/6] 패키지 설치..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

# 5. .env 확인
if [ ! -f ".env" ]; then
    echo "[!] .env 파일이 없습니다. .env.example을 복사합니다."
    cp .env.example .env
    echo "[!] .env 파일을 편집하여 Jenkins 정보를 입력해주세요."
fi

# 6. 마이그레이션
echo "[6/6] 데이터베이스 마이그레이션..."
python manage.py migrate --run-syncdb 2>&1 | tail -1

# 7. 서버 실행
echo ""
echo "=========================================="
echo "  Server: http://localhost:8601"
echo "  Admin:  http://localhost:8601/admin/"
echo "  Suites: http://localhost:8601/suites/"
echo "=========================================="
echo ""
python manage.py runserver 0.0.0.0:8601
