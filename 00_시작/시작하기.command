#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3가 없습니다. https://www.python.org/downloads/ 에서 설치 후 다시 실행하세요."
  read -r -p "엔터를 누르면 닫힙니다."
  exit 1
fi

python3 "$SCRIPT_DIR/launch.py" --auto
run_rc=$?
read -r -p "엔터를 누르면 닫힙니다."
exit "$run_rc"
