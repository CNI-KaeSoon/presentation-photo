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


def load_plan_order(out_root: Path) -> dict[str, dict[str, int]]:
    """worktree.json 의 그룹별 파일 순서를 {폴더: {파일stem: 순번}} 으로 돌려준다.

    파일명 정렬로는 촬영순을 복원할 수 없다 — 카메라 카운터가 9999 다음 0000 으로
    돌아가면 IMG_0001 이 IMG_9999 보다 앞으로 가서 **한 세션의 순서가 통째로 뒤집힌다**
    (실측: 키사이트 세션 IMG_9974~9999 + IMG_0001~0014). worktree.json 은
    init_worktree 가 EXIF 촬영시각으로 만든 것이라 이 순서가 정본이다.
    발표자료(DECK…)도 계획에 페이지 순으로 들어 있어 함께 해결된다.
    """
    path = out_root / "worktree.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: could not read {path}: {exc}")
        return {}
    order: dict[str, dict[str, int]] = {}
    for folder, files in (raw.get("groups") or {}).items():
        if isinstance(files, list):
            order[folder] = {Path(str(f)).stem: i for i, f in enumerate(files)}
    return order


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
    plan_order = load_plan_order(out_root)
    data: dict[str, list[dict[str, object]]] = {}
    unplanned_total = 0

    for folder in sorted((p for p in out_root.iterdir() if p.is_dir()), key=lambda p: natural_key(p.name)):
        if folder.name == here.name:
            continue
        img_dir = folder / "img"
        if not img_dir.is_dir():
            continue
        images = [p for p in img_dir.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")]
        if not images:
            continue
        # 계획(=EXIF 촬영순) 을 1순위로, 계획에 없는 파일만 이름순으로 뒤에 붙인다.
        seq_of = plan_order.get(folder.name, {})
        unplanned = [p for p in images if p.stem not in seq_of]
        unplanned_total += len(unplanned)
        if unplanned:
            print(f"  note: {folder.name} — 계획에 없는 파일 {len(unplanned)}개는 이름순으로 뒤에 둡니다")
        images.sort(key=lambda p: (seq_of.get(p.stem, len(seq_of) + 1), natural_key(p.name)))

        capture_meta = load_capture_manifest(img_dir)
        slides: list[dict[str, object]] = []
        for image in images:
            item: dict[str, object] = {
                "file": f"../{folder.name}/img/{image.name}",
                "name": image.name,
                # 화면 정렬의 정본. 파일명 정렬은 카운터 롤오버에 뒤집힌다.
                "seq": len(slides),
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
