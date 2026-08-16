#!/usr/bin/env python3
"""브라우저 도구의 색 코어와 스크립트 삽입 위치를 검증한다."""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
import sys

# Windows 콘솔 기본 인코딩(cp949)에는 '—'·'·'·'⚠' 같은 문자가 없어, 그대로 print 하면
# UnicodeEncodeError 로 스크립트가 죽는다(실측: init_worktree 가 U+2014 에서 중단).
# 출력 스트림을 UTF-8 로 고정해 어떤 콘솔에서도 깨지거나 죽지 않게 한다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 파이프·구버전 등 재설정 불가 시 무시
        pass


COLOR_CORE_SHA256 = "d06f4a4ba88c58aa976983999f153f5ffd9986b78ff73787456d0005cbd50434"
START = "// ===COLOR_CORE_START==="
END = "// ===COLOR_CORE_END==="
DATA_TAG = '<script src="data.js"></script>'
HEARTBEAT_TAG = '<script src="heartbeat.js" defer></script>'
WORKFLOW_TAG = '<script src="workflow.js" defer></script>'


def fail(message: str) -> None:
    print(f"[FAIL] {message}")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    tool = root / "02_작업장" / "slide_tool"
    index_path = tool / "index.html"
    workflow_path = tool / "workflow.js"
    failures = 0

    try:
        source = index_path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"index.html을 읽지 못했습니다: {exc}")
        print("INDEX: FAIL")
        return 1

    try:
        start = source.index(START)
        end = source.index(END)
    except ValueError:
        fail("COLOR_CORE 시작·끝 마커를 찾지 못했습니다.")
        failures += 1
    else:
        digest = hashlib.sha256(source[start:end].encode("utf-8")).hexdigest()
        if digest != COLOR_CORE_SHA256:
            fail(f"COLOR_CORE sha256 불일치: {digest}")
            failures += 1
        else:
            print("[OK] COLOR_CORE sha256")

    for tag, label in (
        (DATA_TAG, "data.js"),
        (HEARTBEAT_TAG, "heartbeat.js"),
        (WORKFLOW_TAG, "workflow.js"),
    ):
        count = source.count(tag)
        if count != 1:
            fail(f"{label} 태그가 {count}회입니다.")
            failures += 1
        else:
            print(f"[OK] {label} 태그 1회")

    if all(source.count(tag) == 1 for tag in (DATA_TAG, HEARTBEAT_TAG, WORKFLOW_TAG)):
        data_at = source.index(DATA_TAG)
        heartbeat_at = source.index(HEARTBEAT_TAG)
        workflow_at = source.index(WORKFLOW_TAG)
        head_end = source.index("</head>") if "</head>" in source else -1
        adjacent = HEARTBEAT_TAG + "\n" + WORKFLOW_TAG
        if not (data_at < heartbeat_at < workflow_at < head_end):
            fail("스크립트 태그 순서 또는 head 내부 위치가 올바르지 않습니다.")
            failures += 1
        elif source.count(adjacent) != 1:
            fail("workflow.js가 heartbeat.js 바로 다음 줄에 있지 않습니다.")
            failures += 1
        else:
            print("[OK] 스크립트 앵커 순서")

    for closing in ("</body>", "</html>"):
        count = source.count(closing)
        if count != 1:
            fail(f"{closing} 문자열이 {count}회입니다.")
            failures += 1
        else:
            print(f"[OK] {closing} 1회")

    if not workflow_path.is_file():
        fail("workflow.js 파일이 없습니다.")
        failures += 1
    else:
        node = shutil.which("node")
        if node is None:
            print("[SKIP] node 없음 — workflow.js 구문 검사를 생략합니다.")
        else:
            check = subprocess.run(
                [node, "--check", str(workflow_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if check.returncode != 0:
                detail = (check.stderr or check.stdout).strip()
                fail(f"workflow.js 구문 오류: {detail}")
                failures += 1
            else:
                print("[OK] workflow.js 구문")

    if failures:
        print("INDEX: FAIL")
        return 1
    print("INDEX: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
