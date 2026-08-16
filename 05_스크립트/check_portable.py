#!/usr/bin/env python3
"""배포 폴더의 하드코딩 경로와 이동 불가 참조를 검사한다."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys

# Windows 콘솔 기본 인코딩(cp949)에는 '—'·'·'·'⚠' 같은 문자가 없어, 그대로 print 하면
# UnicodeEncodeError 로 스크립트가 죽는다(실측: init_worktree 가 U+2014 에서 중단).
# 출력 스트림을 UTF-8 로 고정해 어떤 콘솔에서도 깨지거나 죽지 않게 한다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 파이프·구버전 등 재설정 불가 시 무시
        pass


# 생성물·외부 환경·에이전트 런타임 상태는 배포 소스가 아니므로 검사하지 않는다.
EXCLUDED_DIRS = {".git", ".venv", "__pycache__", ".omc", ".omx", ".codex", ".claude"}
# 이 파일은 탐지 문자열 자체를 정의하므로 자기 자신은 검사할 수 없다.
EXCLUDED_FILES = {
    "05_스크립트/check_portable.py": "검사 규칙 정의",
    # 아래는 실행하면 생기는 런타임 산출물이다. 사용자 기기의 절대경로가 들어가는 것이
    # 정상이고 .gitignore 로 배포에서 제외되므로 검사 대상이 아니다.
    # (이걸 검사하면 환경 점검을 한 사용자는 누구나 FAIL 을 보게 된다.)
    "00_시작/_env_status.json": "런타임 생성물 · gitignore 대상",
    "02_작업장/worktree.json": "런타임 생성물 · gitignore 대상",
    "02_작업장/slide_tool/data.js": "런타임 생성물 · gitignore 대상",
}
# 공개 라이선스의 승인된 저작권자 표기만 예외로 둔다.
ALLOWED_MARKERS = {
    ("LICENSE", "kaesoon"): "승인된 MIT 저작권자 표기",
}

# 이 도구의 공개 저장소 주소. 문서의 설치 안내에 반드시 들어간다.
PUBLIC_REPO_URL = re.compile(
    r"https://github\.com/CNI-KaeSoon/presentation-photo[^\s)\"']*", re.IGNORECASE)

CONFIG_EXTENSIONS = {".py", ".json", ".toml", ".ini", ".cfg", ".yaml", ".yml"}
HOME_PATTERNS = (
    re.compile(r"/" + r"Users/"),
    re.compile(r"/" + r"home/"),
    re.compile(r"[A-Za-z]:\\" + r"Users\\", re.IGNORECASE),
)
DEV_MARKERS = (
    "kae" + "soon",
    "i" + "Cloud",
    "Mobile" + " Documents",
    "pw" + "-venv",
    "99" + "_tmp",
    "APS" + " 2026",
    "02" + "_scripts",
    "03" + "_out",
    "04" + "_data",
)
REMOVED_MARKERS = (
    "tail" + "scale",
    "100" + ".120.",
    "공유" + "서버",
)
GETCWD_PATTERN = re.compile(r"\bos\.getcwd\s*\(")


def iter_text_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        rel = path.relative_to(root).as_posix()
        if rel in EXCLUDED_FILES:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data:
            continue
        try:
            yield path, rel, data.decode("utf-8")
        except UnicodeDecodeError:
            continue


def violations(root: Path):
    found = []
    for path, rel, text in iter_text_files(root):
        for lineno, line in enumerate(text.splitlines(), 1):
            checks = []
            # 공개 저장소 주소는 문서에 반드시 들어가야 하는 정보다. 계정명이 개발자명과
            # 겹치더라도 위반이 아니므로, 마커 검사 전에 이 URL 부분만 지운 사본으로 본다.
            line = PUBLIC_REPO_URL.sub("", line)
            if path.suffix.lower() in CONFIG_EXTENSIONS:
                if any(pattern.search(line) for pattern in HOME_PATTERNS):
                    checks.append("사용자 홈 절대경로")
            for marker in DEV_MARKERS:
                if marker.lower() in line.lower() and (rel, marker) not in ALLOWED_MARKERS:
                    checks.append(f"개발 환경 고유 문자열: {marker}")
            for marker in REMOVED_MARKERS:
                if marker.lower() in line.lower():
                    checks.append(f"제거 대상 잔존: {marker}")
            if path.suffix.lower() == ".py" and GETCWD_PATTERN.search(line):
                checks.append("os.getcwd() 기반 패키지 경로")
            for reason in checks:
                found.append((rel, lineno, reason, line.strip()))
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent),
        help="검사할 배포 폴더(기본: 이 스크립트의 상위 패키지)",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"검사할 폴더가 없습니다: {root}", file=sys.stderr)
        return 2
    found = violations(root)
    for rel, lineno, reason, line in found:
        print(f"{rel}:{lineno}  {reason}  {line}")
    if found:
        print(f"PORTABLE: FAIL ({len(found)}건)")
        return 1
    print("PORTABLE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
