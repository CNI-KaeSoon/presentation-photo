#!/usr/bin/env python3
"""외부 백업 JSON의 경로 탈출·거대 할당 방지 회귀 테스트."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image


SCRIPT_DIR = Path(__file__).resolve().parent
EXPORT_PDF = SCRIPT_DIR / "export_pdf.py"
KEY = "../g/img/slide.png"
CORNERS = [[0, 0], [1, 0], [0, 1], [1, 1]]


def write_backup(path: Path, *, key: str = KEY, move=None, ratio=None) -> None:
    maps = {
        "slideCorners_v1": {key: CORNERS},
        "slideStatus_v1": {},
        "slideRatios_v1": {} if ratio is None else {key: ratio},
        "slideMoves_v1": {} if move is None else {key: move},
        "slideColor_v1": {},
    }
    payload = {
        "_type": "slide_tool_backup",
        "_version": 1,
        "data": {name: json.dumps(value, ensure_ascii=False) for name, value in maps.items()},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def run_export(backup: Path, root: Path, out: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(EXPORT_PDF),
            "--backup",
            str(backup),
            "--root",
            str(root),
            "--out",
            str(out),
            "--width",
            "80",
            "--dpi",
            "72",
            "--workers",
            "1",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def require_success(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode != 0:
        raise AssertionError(
            f"export_pdf exit {result.returncode}: {result.stderr.strip()} {result.stdout.strip()}"
        )


def report(label: str, action) -> bool:
    try:
        action()
    except Exception as exc:
        print(f"[FAIL] {label}: {exc}")
        return False
    print(f"[OK] {label}")
    return True


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="slide_security_"))
    results: list[bool] = []
    try:
        root = temp / "server"
        image_dir = root / "g" / "img"
        image_dir.mkdir(parents=True)
        Image.new("RGB", (120, 90), (30, 120, 210)).save(image_dir / "slide.png")
        secret = temp / "secret.png"
        Image.new("RGB", (120, 90), (220, 20, 30)).save(secret)

        def absolute_move() -> None:
            backup = temp / "absolute.json"
            escaped = temp / "outside_absolute"
            out = temp / "out_absolute"
            write_backup(backup, move=str(escaped))
            result = run_export(backup, root, out)
            require_success(result)
            assert not escaped.with_suffix(".pdf").exists(), "--out 밖 PDF가 생성됨"
            assert (out / "g.pdf").is_file(), "원래 폴더 폴백 PDF가 없음"
            assert "이동 폴더명 거부" in result.stderr, "거부 경고가 없음"

        results.append(report("절대경로 이동명 차단", absolute_move))

        def parent_move() -> None:
            backup = temp / "parent.json"
            out = temp / "out_parent"
            escaped = temp / "탈출.pdf"
            write_backup(backup, move="../탈출")
            result = run_export(backup, root, out)
            require_success(result)
            assert not escaped.exists(), "상위 폴더에 PDF가 생성됨"
            assert (out / "g.pdf").is_file(), "원래 폴더 폴백 PDF가 없음"
            assert "이동 폴더명 거부" in result.stderr, "거부 경고가 없음"

        results.append(report("상위경로 이동명 차단", parent_move))

        def traversal_key() -> None:
            backup = temp / "traversal.json"
            out = temp / "out_traversal"
            write_backup(backup, key="../g/img/../../../secret.png")
            result = run_export(backup, root, out)
            require_success(result)
            assert not (out / "g.pdf").exists(), "루트 밖 비밀 이미지가 PDF로 생성됨"
            assert "백업 키 거부" in result.stderr, "경로 키 거부 경고가 없음"

        results.append(report("백업 키 이미지 경로 탈출 차단", traversal_key))

        def tiny_ratio() -> None:
            backup = temp / "tiny_ratio.json"
            out = temp / "out_ratio"
            write_backup(backup, ratio=0.000001)
            result = run_export(backup, root, out)
            require_success(result)
            assert (out / "g.pdf").is_file(), "자동 비율 폴백 PDF가 없음"
            assert "비율 값 거부" in result.stderr, "비율 거부 경고가 없음"

        results.append(report("비정상 비율 안전 폴백", tiny_ratio))

        def normal_backup() -> None:
            backup = temp / "normal.json"
            out = temp / "out_normal"
            write_backup(backup, ratio="4:3")
            result = run_export(backup, root, out)
            require_success(result)
            assert (out / "g.pdf").is_file(), "정상 PDF가 생성되지 않음"

        results.append(report("정상 백업 PDF 생성", normal_backup))
    finally:
        shutil.rmtree(temp, ignore_errors=True)

    if all(results) and len(results) == 5:
        print("SECURITY: ALL PASS")
        return 0
    print("SECURITY: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
