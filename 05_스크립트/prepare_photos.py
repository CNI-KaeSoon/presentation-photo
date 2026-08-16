#!/usr/bin/env python3
"""[섹터 2] 사진을 워크트리에 넣는다 — 원본 → 작업용 축소본.

브라우저 도구는 사진을 캔버스에 올려 실시간으로 보정을 미리보기 한다. 원본(수천만 화소·
HEIC)을 그대로 넣으면 브라우저가 기어다니므로, **작업은 축소본으로 하고 최종 PDF 만
원본에 적용**하는 것이 이 도구의 설계다. 이 스크립트가 그 축소본을 만든다.

만들어지는 구조 (`--out` 이 서버 루트가 된다):

    <out>/
      01_그룹이름/img/IMG_0001.jpg   ← 축소본 (도구가 읽음)
      02_다른그룹/img/IMG_0100.jpg
      slide_tool/                    ← 도구 폴더를 여기에 두고 서버를 <out> 에서 실행

⚠️ **파일명(확장자 제외)은 바꾸지 않는다.** 나중에 `export_pdf.py` 가 이 이름으로 원본을
   되찾고, 도구의 저장 키도 이 경로 문자열이다. 이름을 바꾸면 작업물이 전부 어긋난다.
⚠️ 폴더명·파일명에 `#`, `?`, `%` 를 쓰지 마라 — 도구가 URL 인코딩 없이 경로를 넘긴다.

사용 예:
  # [섹터 1] 계획대로 배치 — 권장
  python3 05_스크립트/prepare_photos.py --plan 02_작업장/worktree.json

  # 계획 없이 한 그룹으로 몰아넣기
  python3 05_스크립트/prepare_photos.py \
      --src 01_원본사진 --out 02_작업장 --group 내발표

  # 이미 하위 폴더로 나눠 뒀을 때
  python3 05_스크립트/prepare_photos.py \
      --src 01_원본사진 --out 02_작업장 --by-subfolder
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from photo_io import (ALL_EXTS, HEIC_EXTS, backend_report, heic_supported,  # noqa: E402
                      is_image, nfc, open_pil)
from parallel_map import default_workers, ordered_parallel_map  # noqa: E402

BAD_CHARS = ("#", "?", "%")


def is_single_basename(value) -> bool:
    """경로가 아닌 단일 그룹 이름만 허용한다."""
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    if value in {".", ".."} or os.path.isabs(value):
        return False
    if "/" in value or "\\" in value or re.match(r"^[A-Za-z]:", value):
        return False
    return Path(value).name == value


def collect(src_dir, by_subfolder, group):
    """{그룹명: [원본경로…]} — 계획 파일이 없을 때 쓰는 단순 수집."""
    groups: dict[str, list[str]] = {}
    if by_subfolder:
        for entry in sorted(os.listdir(src_dir)):
            sub = os.path.join(src_dir, entry)
            if not os.path.isdir(sub) or entry.startswith("."):
                continue
            files = [os.path.join(sub, f) for f in sorted(os.listdir(sub))
                     if is_image(os.path.join(sub, f))]
            if files:
                groups[nfc(entry)] = files
    else:
        files = []
        for root, _d, fns in os.walk(src_dir):
            for f in sorted(fns):
                p = os.path.join(root, f)
                if not f.startswith(".") and is_image(p):
                    files.append(p)
        if files:
            groups[nfc(group)] = sorted(files)
    return groups


def _relative_to_plan(value, plan_dir):
    """v2의 POSIX 상대경로를 현재 운영체제 경로로 바꾼다."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"계획 경로 값이 올바르지 않다: {value!r}")
    parts = PurePosixPath(str(value).replace("\\", "/")).parts
    return str(Path(plan_dir, *parts).resolve())


def _v2_path(value, plan_dir, mode):
    """v2 저장 방식에 따라 상대경로 또는 불가피한 절대경로를 해석한다."""
    if mode == "absolute":
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError(f"계획 경로 값이 올바르지 않다: {value!r}")
        return str(Path(os.path.normpath(value.replace("/", os.sep))).expanduser().resolve())
    normalized = str(value).replace("\\", "/")
    if PurePosixPath(normalized).is_absolute() or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"상대경로 모드에 절대경로가 들어 있다: {value!r}")
    return _relative_to_plan(value, plan_dir)


def _resolve_v1_paths(doc, plan_dir):
    """v1 절대경로를 우선 사용하고, 이동된 계획은 새 위치에 재배치한다."""
    raw_root = doc.get("root")
    raw_source = doc.get("source")
    for name, value in (("root", raw_root), ("source", raw_source)):
        if value is not None and (not isinstance(value, str) or "\x00" in value):
            raise ValueError(f"계획 파일의 {name} 경로가 올바르지 않다: {value!r}")

    if raw_root and os.path.isabs(raw_root) and os.path.isdir(raw_root):
        root = str(Path(raw_root).expanduser().resolve())
    else:
        relative_root = _relative_to_plan(raw_root or ".", plan_dir)
        root = relative_root if os.path.isdir(relative_root) else plan_dir

    if raw_source and os.path.isabs(raw_source) and os.path.isdir(raw_source):
        source = str(Path(raw_source).expanduser().resolve())
    else:
        candidates = []
        if raw_source:
            candidates.append(_relative_to_plan(raw_source, plan_dir))
        if raw_root and raw_source:
            try:
                old_relative = os.path.relpath(raw_source, raw_root)
            except ValueError:
                old_relative = None
            if old_relative:
                candidates.append(str(Path(root, old_relative).resolve()))
        source = next((path for path in candidates if os.path.isdir(path)), None)
    return root, source


def from_plan(plan_path):
    """[섹터 1] 계획 파일 → (out_root, {그룹: [원본경로…]})."""
    plan_path = str(Path(plan_path).expanduser().resolve())
    plan_dir = str(Path(plan_path).parent)
    with open(plan_path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("_type") != "slide_tool_worktree":
        raise ValueError(f"{plan_path} 는 워크트리 계획 파일이 아니다")
    if int(doc.get("_version", 1)) >= 2:
        path_mode = doc.get("_path_mode") if isinstance(doc.get("_path_mode"), dict) else {}
        out_root = _v2_path(doc.get("root", "."), plan_dir, path_mode.get("root"))
        raw_source = doc.get("source")
        src = (_v2_path(raw_source, plan_dir, path_mode.get("source"))
               if raw_source else None)
    else:
        out_root, src = _resolve_v1_paths(doc, plan_dir)
    out_root = str(Path(out_root).resolve())
    src = str(Path(src).resolve()) if src else None
    plan_groups = doc.get("groups")
    if not isinstance(plan_groups, dict):
        raise ValueError("계획 파일의 groups 가 객체가 아니다")
    for label, names in plan_groups.items():
        if not is_single_basename(label):
            raise ValueError(f"그룹명 거부 — 단일 폴더명이 아님: {label!r}")
        if not isinstance(names, list):
            raise ValueError(f"그룹 사진 목록이 배열이 아님: {label!r}")
    if not src or not os.path.isdir(src):
        raise ValueError(f"계획 파일의 원본 폴더를 찾을 수 없다: {doc.get('source')}\n"
                         f"  사진을 옮겼다면 worktree.json 의 \"source\" 를 고쳐라.")
    # 원본 폴더를 재귀 스캔해 파일명 → 실제 경로
    index = {}
    for source_root, _d, fns in os.walk(src):
        for f in fns:
            if is_image(os.path.join(source_root, f)):
                index.setdefault(nfc(f), os.path.join(source_root, f))
    groups, missing = {}, []
    for label, names in plan_groups.items():
        paths = []
        for n in names:
            p = index.get(nfc(n))
            if p:
                paths.append(p)
            else:
                missing.append(n)
        groups[label] = paths
    if missing:
        print(f"⚠️ 계획에 있으나 원본에서 못 찾은 사진 {len(missing)}장: "
              f"{', '.join(missing[:5])}{' …' if len(missing) > 5 else ''}",
              file=sys.stderr)
    return out_root, groups


def convert(src, dst, max_px, quality):
    """축소 + JPEG 변환. HEIC·JPG·PNG 모두 같은 경로로 처리(OS 무관)."""
    im = open_pil(src)
    if im is None:
        return False
    im.thumbnail((max_px, max_px), Image.LANCZOS)     # 종횡비 유지, 확대는 안 함
    try:
        im.save(dst, "JPEG", quality=quality, optimize=True)
    except OSError:
        return False
    return True


def _convert_task(t: dict) -> tuple[int, bool, str | None]:
    """워커용 변환. 결과와 경고문은 부모 프로세스에 돌려준다."""
    ok = convert(t["src"], t["dst"], t["max_px"], t["quality"])
    warning = None if ok else f"    ⚠️ 변환 실패: {t['src']}"
    return t["idx"], ok, warning


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", help="[섹터 1] 이 만든 worktree.json (권장)")
    ap.add_argument("--src", help="원본 사진 폴더 (읽기만 함 — 변형하지 않음)")
    ap.add_argument("--out", help="작업 폴더 = 서버 루트 (예: tool)")
    ap.add_argument("--group", default="slides", help="단일 그룹 이름 (기본 slides)")
    ap.add_argument("--by-subfolder", action="store_true",
                    help="--src 의 하위 폴더 하나를 그룹 하나로")
    ap.add_argument("--max-px", type=int, default=1920,
                    help="긴 변 최대 픽셀 (기본 1920). 작을수록 도구가 빠름")
    ap.add_argument("--quality", type=int, default=85, help="JPEG 품질 (기본 85)")
    ap.add_argument("--force", action="store_true", help="이미 있는 축소본도 다시 생성")
    ap.add_argument("--workers", type=int, default=0,
                    help="병렬 워커 수 (0=자동, 1=직렬; 기본 0)")
    a = ap.parse_args()
    if a.workers < 0:
        ap.error("--workers 는 0 이상의 정수여야 한다")
    workers = default_workers() if a.workers == 0 else a.workers

    print(backend_report())

    # ---- 무엇을 어디에 ----
    if a.plan:
        try:
            out_root, groups = from_plan(a.plan)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"‼️ {exc}", file=sys.stderr)
            return 1
        if a.out:
            out_root = a.out
    else:
        if not (a.src and a.out):
            print("‼️ --plan 을 주거나, --src 와 --out 을 함께 줘야 한다.", file=sys.stderr)
            return 1
        if not os.path.isdir(a.src):
            print(f"‼️ --src 폴더가 없다: {a.src}", file=sys.stderr)
            return 1
        out_root = a.out
        groups = collect(a.src, a.by_subfolder, a.group)

    if not groups or not any(groups.values()):
        print(f"‼️ 처리할 이미지가 없다.\n   지원 확장자: {', '.join(ALL_EXTS)}",
              file=sys.stderr)
        return 1

    # ---- HEIC 인데 지원이 없으면 미리 알린다 (조용히 전부 실패하는 것 방지) ----
    heic_n = sum(1 for fs in groups.values() for f in fs
                 if os.path.splitext(f)[1].lower() in HEIC_EXTS)
    if heic_n and not heic_supported():
        print(f"‼️ HEIC 사진이 {heic_n}장인데 이 환경은 HEIC 를 못 읽는다.\n"
              f"   해결:  pip install pillow-heif\n"
              f"   (또는 사진을 미리 JPG 로 변환해서 넣어라)", file=sys.stderr)
        return 1

    # ---- 위험한 이름 사전 차단 ----
    problems = []
    for g, files in groups.items():
        if not is_single_basename(g):
            problems.append(f"단일 폴더명이 아닌 그룹 {g!r}")
        elif any(c in g for c in BAD_CHARS):
            problems.append(f"폴더명 {g!r}")
        for f in files:
            base = os.path.basename(f)
            if any(c in base for c in BAD_CHARS):
                problems.append(f"파일명 {base!r}")
    if problems:
        print("‼️ 경로 형태의 그룹명이나 #, ?, % 문자가 든 이름은 사용할 수 없다:",
              file=sys.stderr)
        for p in problems[:20]:
            print(f"   - {p}", file=sys.stderr)
        return 1

    # ---- 변환 ----
    total_ok = total_skip = total_fail = 0
    for g in sorted(groups):
        files = groups[g]
        img_dir = os.path.join(out_root, g, "img")
        os.makedirs(img_dir, exist_ok=True)
        ok = skip = fail = 0
        tasks = []
        for src in files:
            stem = nfc(os.path.splitext(os.path.basename(src))[0])
            dst = os.path.join(img_dir, stem + ".jpg")
            if os.path.exists(dst) and not a.force:
                skip += 1
                continue
            tasks.append(dict(idx=len(tasks), src=src, dst=dst,
                              max_px=a.max_px, quality=a.quality))

        # 서로 다른 원본이 같은 stem 이면 기존 직렬 덮어쓰기 순서를 보존한다.
        destinations = [t["dst"] for t in tasks]
        group_workers = 1 if len(destinations) != len(set(destinations)) else workers
        for _idx, converted, warning in ordered_parallel_map(
                _convert_task, tasks, group_workers):
            if converted:
                ok += 1
            else:
                if warning:
                    print(warning, file=sys.stderr)
                fail += 1
        total_ok += ok; total_skip += skip; total_fail += fail
        print(f"  [{g}] 원본 {len(files)}장 → 생성 {ok}"
              + (f", 기존 유지 {skip}" if skip else "")
              + (f", 실패 {fail}" if fail else ""))

    print(f"\n=== 요약 === 생성 {total_ok} · 유지 {total_skip} · 실패 {total_fail}")
    if total_fail:
        print("⚠️ 실패분은 축소본이 없어 도구에 뜨지 않는다.", file=sys.stderr)

    tool_dir = os.path.join(out_root, "slide_tool")
    print("\n다음 단계 [섹터 3]:")
    if not os.path.isdir(tool_dir):
        print(f"  ⚠️ slide_tool 폴더가 {out_root} 안에 없다 — 복사해 넣어라")
    print(f"  1) python3 {os.path.join(tool_dir, 'gen_manifest.py')}")
    print("  2) python3 00_시작/serve_tool.py")
    print("  3) 브라우저에서 http://localhost:8770/slide_tool/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
