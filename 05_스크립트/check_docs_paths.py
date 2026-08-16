#!/usr/bin/env python3
"""README와 설명서의 구경로, 삭제 기능 참조, 깨진 로컬 링크를 검사한다."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


OLD_PATH_PATTERNS = (
    (re.compile(r"(?<![\w가-힣])docs[/\\]"), "구경로 docs/"),
    (re.compile(r"(?<![\w가-힣])scripts[/\\]"), "구경로 scripts/"),
    (re.compile(r"(?<![\w가-힣])tool[/\\]"), "구경로 tool/"),
    (
        re.compile(r"(?<!02_작업장[/\\])(?<![\w가-힣])slide_tool[/\\]"),
        "단독 slide_tool/ 경로",
    ),
)
REMOVED_PATTERNS = (
    (re.compile(r"모드\s*[AB]", re.IGNORECASE), "삭제된 모드 구분"),
    (re.compile("tail" + "scale", re.IGNORECASE), "삭제된 원격 기능"),
    (re.compile("공유" + "서버"), "삭제된 원격 기능"),
    (re.compile("100" + r"\.120\."), "삭제된 기기 주소"),
)
LINK_PATTERN = re.compile(r"\[[^]]+\]\(([^)]+)\)")
URL_PATTERN = re.compile(r"https?://[^\s)]+")


def docs(root: Path):
    yield root / "README.md"
    yield from sorted((root / "04_설명서").glob("*.md"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent),
        help="검사할 배포 폴더(기본: 이 스크립트의 상위 패키지)",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    found = []
    for path in docs(root):
        if not path.is_file():
            found.append((path.relative_to(root).as_posix(), 0, "문서 파일 없음", ""))
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            searchable = URL_PATTERN.sub("", line)
            for pattern, reason in OLD_PATH_PATTERNS + REMOVED_PATTERNS:
                if pattern.search(searchable):
                    found.append((path.relative_to(root).as_posix(), lineno, reason, line.strip()))
            for match in LINK_PATTERN.finditer(line):
                target = match.group(1).strip()
                if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                    continue
                local = target.split("#", 1)[0]
                if local and not (path.parent / local).resolve().is_file():
                    found.append((path.relative_to(root).as_posix(), lineno,
                                  "깨진 문서 링크", target))
    for rel, lineno, reason, content in found:
        print(f"{rel}:{lineno}  {reason}  {content}")
    if found:
        print(f"DOCS PATHS: FAIL ({len(found)}건)")
        return 1
    print("DOCS PATHS: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
