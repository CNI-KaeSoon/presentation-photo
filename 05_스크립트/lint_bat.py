#!/usr/bin/env python3
"""macOS에서도 실행 가능한 Windows 배치 파일 정적 린터."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from typing import Optional


LABEL_RE = re.compile(r"^\s*:([A-Za-z0-9_.-]+)\s*$")
GOTO_RE = re.compile(r"\bgoto\s+:?([A-Za-z0-9_.-]+)", re.IGNORECASE)
CALL_RE = re.compile(r"\bcall\s+:([A-Za-z0-9_.-]+)", re.IGNORECASE)
QUOTED_DP0_RE = re.compile(r'"%~dp0([^"%]+)"', re.IGNORECASE)
PLAIN_DP0_RE = re.compile(r"%~dp0([^\s&|<>\"%]+)", re.IGNORECASE)
EXIT_RE = re.compile(r"\b(?:exit(?:\s+/b)?|goto\s+:eof)\b", re.IGNORECASE)
UNCONDITIONAL_EXIT_RE = re.compile(
    r"^\s*@?(?:exit(?:\s+/b)?|goto\s+:eof)\b",
    re.IGNORECASE,
)


def _line_for_offset(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1


def _significant(lines: list[str], before: Optional[int] = None) -> list[tuple[int, str]]:
    limit = len(lines) if before is None else before
    items = []
    for index, line in enumerate(lines[:limit], 1):
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("rem ") or stripped.startswith("::"):
            continue
        items.append((index, stripped))
    return items


def lint_file(path: Path) -> list[tuple[int, str]]:
    issues: list[tuple[int, str]] = []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [(1, f"파일을 읽을 수 없음: {exc}")]

    non_ascii = next((i for i, byte in enumerate(raw) if byte > 0x7F), None)
    if non_ascii is not None:
        issues.append((_line_for_offset(raw, non_ascii), "L1 ASCII가 아닌 바이트가 있음"))

    raw_lines = raw.splitlines(keepends=True)
    for line_no, raw_line in enumerate(raw_lines, 1):
        if not raw_line.endswith(b"\r\n"):
            issues.append((line_no, "L2 줄끝이 CRLF가 아님"))
    if not raw_lines and raw:
        issues.append((1, "L2 줄끝이 CRLF가 아님"))

    text = raw.decode("ascii", errors="replace")
    lines = text.splitlines()

    if not lines or lines[0].strip().lower() != "@echo off":
        issues.append((1, "L4 첫 줄은 @echo off 여야 함"))
    if len(lines) < 2 or lines[1].strip().lower() != "chcp 65001 >nul":
        issues.append((2, "L4 둘째 줄은 chcp 65001 >nul 이어야 함"))

    labels = {
        match.group(1).lower()
        for line in lines
        if (match := LABEL_RE.match(line))
    }
    for line_no, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.lower().startswith("rem ") or stripped.startswith("::"):
            continue
        refs = [*GOTO_RE.findall(line), *CALL_RE.findall(line)]
        for label in refs:
            if label.lower() == "eof":
                continue
            if label.lower() not in labels:
                issues.append((line_no, f"L3 참조 label :{label} 이 없음"))

    seen_refs: set[tuple[int, str]] = set()
    for line_no, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.lower().startswith("rem ") or stripped.startswith("::"):
            continue
        refs = [*QUOTED_DP0_RE.findall(line), *PLAIN_DP0_RE.findall(line)]
        for referenced in refs:
            referenced = referenced.rstrip("),;")
            key = (line_no, referenced.lower())
            if key in seen_refs:
                continue
            seen_refs.add(key)
            target = path.parent / Path(referenced.replace("\\", "/"))
            if not target.exists():
                issues.append((line_no, f"L5 참조 파일이 없음: %~dp0{referenced}"))

    terminals = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower().startswith("rem ") or stripped.startswith("::"):
            continue
        terminal = EXIT_RE.search(stripped)
        if terminal and not re.match(r"^@?echo\b", stripped, re.IGNORECASE):
            terminals.append(index)
            previous = _significant(lines, before=index)
            same_line_prefix = stripped[:terminal.start()]
            same_line_pause = bool(re.search(r"(?:^|[&|(])\s*pause\s*(?:[&|)])", same_line_prefix,
                                             re.IGNORECASE))
            if not same_line_pause and (not previous or previous[-1][1].lower() != "pause"):
                issues.append((index + 1, "L6 종료 직전에 pause가 없음"))
    if not terminals:
        significant = _significant(lines)
        if not significant or significant[-1][1].lower() != "pause":
            issues.append((len(lines) or 1, "L6 파일 말미 종료 경로에 pause가 없음"))
    else:
        significant = _significant(lines)
        last = significant[-1][1] if significant else ""
        if not UNCONDITIONAL_EXIT_RE.match(last) and last.lower() != "pause":
            issues.append((significant[-1][0], "L6 파일 말미 종료 경로에 pause가 없음"))

    return issues


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path, metavar="파일")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    found = False
    for path in args.files:
        for line_no, reason in lint_file(path):
            found = True
            print(f"{path}:{line_no} {reason}")
    if found:
        return 1
    print("BAT LINT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
