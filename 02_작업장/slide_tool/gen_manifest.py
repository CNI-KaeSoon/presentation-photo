#!/usr/bin/env python3
"""Generate data.js for the slide perspective review tool."""

from __future__ import annotations

import json
import re
from pathlib import Path
import sys

# Windows 콘솔 기본 인코딩(cp949)에는 '—'·'·'·'⚠' 같은 문자가 없어, 그대로 print 하면
# UnicodeEncodeError 로 스크립트가 죽는다(실측: init_worktree 가 U+2014 에서 중단).
# 출력 스트림을 UTF-8 로 고정해 어떤 콘솔에서도 깨지거나 죽지 않게 한다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 파이프·구버전 등 재설정 불가 시 무시
        pass


def natural_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def load_capture_manifest(img_dir: Path) -> dict[str, dict[str, object]]:
    path = img_dir / "manifest.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: could not read {path}: {exc}")
        return {}

    if isinstance(raw, dict):
        rows = raw.get("slides") or raw.get("images") or raw.get("items") or raw.get("groups") or []
    elif isinstance(raw, list):
        rows = raw
    else:
        rows = []

    by_name: dict[str, dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        file_value = row.get("file") or row.get("name") or row.get("path") or row.get("output_filename")
        if not file_value:
            continue
        by_name[Path(str(file_value)).name] = row
    return by_name


def main() -> int:
    here = Path(__file__).resolve().parent
    out_root = here.parent
    data: dict[str, list[dict[str, object]]] = {}

    for folder in sorted((p for p in out_root.iterdir() if p.is_dir()), key=lambda p: natural_key(p.name)):
        if folder.name == here.name:
            continue
        img_dir = folder / "img"
        if not img_dir.is_dir():
            continue
        images = sorted(
            [p for p in img_dir.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")],
            key=lambda p: natural_key(p.name))
        if not images:
            continue

        capture_meta = load_capture_manifest(img_dir)
        slides: list[dict[str, object]] = []
        for image in images:
            item: dict[str, object] = {
                "file": f"../{folder.name}/img/{image.name}",
                "name": image.name,
            }
            meta = capture_meta.get(image.name)
            if meta:
                for key in ("start_ts", "end_ts", "start_hhmmss", "end_hhmmss"):
                    if key in meta:
                        item[key] = meta[key]
            slides.append(item)
        data[folder.name] = slides

    body = "window.SLIDE_DATA = "
    body += json.dumps(data, ensure_ascii=False, indent=2)
    body += ";\n"
    (here / "data.js").write_text(body, encoding="utf-8")

    if data:
        for folder, slides in data.items():
            print(f"{folder}: {len(slides)} images")
    else:
        print("No slide image folders found. Wrote empty data.js.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
