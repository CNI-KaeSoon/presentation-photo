#!/usr/bin/env python3
"""슬라이드툴 백업 JSON → 폴더별 고해상도 PDF.

브라우저 툴은 **작은 이미지**로 빠르게 코너·색보정을 잡고 백업 JSON 을 내보낸다.
이 스크립트는 그 백업의 정규화 좌표·색보정값을 **원본 고해상도 사진**에 그대로 적용해
폴더(=그룹)별 PDF 를 만든다. 색보정은 브라우저 미리보기와 픽셀 단위로 동일하다
(`slide_color.apply_manual_color`).

백업 키 형식(툴이 만든 것): `../<폴더>/img/<파일명>`
코너 순서: [TL, TR, BL, BR], 정규화 [0,1]

사용 예:
  # 툴이 쓰던 이미지 그대로 PDF (원본이 따로 없을 때)
  python3 05_스크립트/export_pdf.py --backup backup.json \
      --root 02_작업장 --out 03_결과물

  # 원본 고해상도(HEIC/JPG…)에 적용 — 권장
  python3 05_스크립트/export_pdf.py --backup backup.json \
      --root 02_작업장 --src-dir 01_원본사진 \
      --out 03_결과물 --merge 전체.pdf
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from photo_io import (HEIC_EXTS as SIPS_EXTS, RASTER_EXTS, backend_report,  # noqa: E402
                      heic_supported, nfc, read_bgr)
from parallel_map import default_workers, ordered_parallel_map  # noqa: E402
from slide_color import apply_manual_color  # noqa: E402

# Windows 콘솔 기본 인코딩(cp949)에는 '—'·'·'·'⚠' 같은 문자가 없어, 그대로 print 하면
# UnicodeEncodeError 로 스크립트가 죽는다(실측: init_worktree 가 U+2014 에서 중단).
# 출력 스트림을 UTF-8 로 고정해 어떤 콘솔에서도 깨지거나 죽지 않게 한다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 파이프·구버전 등 재설정 불가 시 무시
        pass


MAX_BACKUP_BYTES = 64 * 1024 * 1024
MAX_SLIDES = 20_000
MAX_WIDTH = 20_000
MAX_OUTPUT_PIXELS = 100_000_000
MIN_RATIO = 0.05
MAX_RATIO = 20.0
BACKUP_FIELDS = (
    "slideCorners_v1",
    "slideStatus_v1",
    "slideRatios_v1",
    "slideMoves_v1",
    "slideColor_v1",
)


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def is_single_basename(value) -> bool:
    """외부 입력이 경로가 아닌 단일 파일·폴더 이름인지 판정한다."""
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    if value in {".", ".."} or os.path.isabs(value):
        return False
    if "/" in value or "\\" in value or re.match(r"^[A-Za-z]:", value):
        return False
    return Path(value).name == value


def is_within(path: Path, root: Path) -> bool:
    """Python 3.9에서도 동작하는 resolve 경로 containment 검사."""
    try:
        common = os.path.commonpath((str(path), str(root)))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(str(root))


def output_path(out_root: Path, name: str) -> Path | None:
    candidate = (out_root / name).resolve()
    return candidate if is_within(candidate, out_root) else None


def validate_corners(value):
    """정확한 4쌍의 유한 좌표만 float 목록으로 정규화한다."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    normalized = []
    for pair in value:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return None
        coords = []
        for raw in pair:
            if isinstance(raw, bool):
                return None
            try:
                number = float(raw)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(number) or not -1.0 <= number <= 2.0:
                return None
            coords.append(number)
        normalized.append(coords)
    return normalized


def load_backup(path):
    """백업 JSON → (corners, status, ratios, moves, colors).

    툴은 각 값을 JSON 문자열로 저장하므로 이중 파싱한다.
    """
    backup_path = Path(path).expanduser()
    size = backup_path.stat().st_size
    if size > MAX_BACKUP_BYTES:
        raise ValueError(
            f"백업 파일이 {MAX_BACKUP_BYTES // (1024 * 1024)}MB 상한을 넘는다: {size}바이트"
        )
    with backup_path.open(encoding="utf-8") as fh:
        b = json.load(fh)
    if not isinstance(b, dict) or b.get("_type") != "slide_tool_backup":
        raise ValueError("백업 _type 이 slide_tool_backup 이 아니다")
    d = b.get("data")
    if not isinstance(d, dict):
        raise ValueError("백업 data 가 객체가 아니다")

    def _j(k):
        v = d.get(k)
        if v is None:
            return {}
        try:
            parsed = json.loads(v) if isinstance(v, str) else v
        except json.JSONDecodeError as exc:
            raise ValueError(f"백업 {k} 값이 올바른 JSON이 아니다") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"백업 {k} 값이 객체가 아니다")
        if len(parsed) > MAX_SLIDES:
            raise ValueError(f"백업 {k} 항목 수가 {MAX_SLIDES}개 상한을 넘는다")
        return parsed

    corners, status, ratios, moves, colors = (_j(k) for k in BACKUP_FIELDS)
    slide_keys = set().union(corners, status, ratios, moves, colors)
    if len(slide_keys) > MAX_SLIDES:
        raise ValueError(f"백업 슬라이드 항목 수가 {MAX_SLIDES}개 상한을 넘는다")

    clean_corners = {}
    for key, value in corners.items():
        normalized = validate_corners(value)
        if normalized is None:
            print(f"⚠️ 코너 값 거부 — 항목 건너뜀: {key!r}", file=sys.stderr)
            continue
        clean_corners[key] = normalized

    clean_ratios = {}
    for key, value in ratios.items():
        ratio = parse_ratio(value)
        if ratio is None:
            print(f"⚠️ 비율 값 거부 — 자동계산 사용: {key!r}", file=sys.stderr)
            continue
        clean_ratios[key] = ratio
    return clean_corners, status, clean_ratios, moves, colors


def is_excluded(status_val) -> bool:
    """`제외` 판정. 신형 dict({'excluded':True}) · 구형 문자열 'excluded' 둘 다 지원."""
    if isinstance(status_val, dict):
        return status_val.get("excluded") is True
    return status_val == "excluded"


def is_done(status_val) -> bool:
    if isinstance(status_val, dict):
        return status_val.get("done") is True
    return status_val == "done"


def parse_ratio(r):
    """'16:9' 문자열 또는 숫자 → w/h float. 없으면 None."""
    if r is None or isinstance(r, bool):
        return None
    if isinstance(r, (int, float)):
        value = float(r)
        return value if math.isfinite(value) and MIN_RATIO <= value <= MAX_RATIO else None
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*$", str(r))
    if m:
        w, h = float(m.group(1)), float(m.group(2))
        value = w / h if h > 0 else None
        return value if value is not None and MIN_RATIO <= value <= MAX_RATIO else None
    try:
        v = float(r)
        return v if math.isfinite(v) and MIN_RATIO <= v <= MAX_RATIO else None
    except (TypeError, ValueError):
        return None


def warp(img, corners_norm, ratio_wh, target_w):
    """정규화 코너[TL,TR,BL,BR]로 원근 보정. 출력 폭은 target_w 로 직접 warp
    (전해상도 → 목표 캔버스 1회 변환: 메모리·속도 절감). ratio_wh 있으면 그 종횡비로 conform."""
    h, w = img.shape[:2]
    tl, tr, bl, br = [np.array([c[0] * w, c[1] * h], np.float32) for c in corners_norm]
    if ratio_wh and ratio_wh > 0:
        W = int(target_w)
        H = int(round(W / ratio_wh))
    else:
        wA = np.linalg.norm(br - bl); wB = np.linalg.norm(tr - tl)
        hA = np.linalg.norm(tr - br); hB = np.linalg.norm(tl - bl)
        Wf = max(wA, wB); Hf = max(hA, hB)
        if Wf < 10 or Hf < 10:
            return None
        W = int(target_w)
        H = int(round(W * Hf / Wf))
    if W < 10 or H < 10:
        return None
    if W * H > MAX_OUTPUT_PIXELS:
        raise ValueError(f"출력 크기 {W}x{H}가 {MAX_OUTPUT_PIXELS:,}픽셀 상한을 넘음")
    src = np.array([tl, tr, br, bl], np.float32)
    dst = np.array([[0, 0], [W, 0], [W, H], [0, H]], np.float32)
    M = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(img, M, (W, H))


def inset_crop(img, frac):
    """각 변을 frac 비율만큼 균일 인셋 크롭(종횡비 보존). 베젤·주변부 누수 제거용.
    기본 0 = 무크롭 — 가장자리 텍스트가 잘리는 사고를 막기 위한 기본값이다."""
    if frac <= 0:
        return img
    h, w = img.shape[:2]
    dy = int(round(h * frac)); dx = int(round(w * frac))
    if h - 2 * dy < 10 or w - 2 * dx < 10:
        return img
    return img[dy:h - dy, dx:w - dx]


def index_sources(src_dir):
    """원본 폴더를 재귀 스캔해 {NFC 정규화 stem: 경로} 인덱스를 만든다.

    macOS 파일시스템은 한글을 NFD 로 저장하고 백업 JSON 은 NFC 라 그냥 비교하면 못 찾는다.
    확장자가 다른 동명 파일은 raster 를 HEIC 보다 우선(이미 디코딩된 것이 빠름)하지 않고,
    **먼저 발견된 것**을 쓰되 HEIC 는 raster 가 없을 때만 채택한다.
    """
    idx = {}
    if not src_dir:
        return idx, None
    src_root = Path(src_dir).expanduser().resolve()
    for root, _dirs, files in os.walk(src_root):
        for fn in files:
            if not is_single_basename(fn):
                print(f"⚠️ 원본 파일명 거부 — 인덱스 제외: {fn!r}", file=sys.stderr)
                continue
            stem, ext = os.path.splitext(fn)
            ext = ext.lower()
            if ext not in RASTER_EXTS + SIPS_EXTS:
                continue
            if not stem or not is_single_basename(stem):
                print(f"⚠️ 원본 stem 거부 — 인덱스 제외: {fn!r}", file=sys.stderr)
                continue
            candidate = (Path(root) / fn).resolve()
            if not is_within(candidate, src_root):
                print(f"⚠️ 원본 경로 거부 — --src-dir 밖 파일: {fn!r}", file=sys.stderr)
                continue
            key = nfc(stem)
            prev = idx.get(key)
            if prev is None:
                idx[key] = str(candidate)
            elif os.path.splitext(prev)[1].lower() in SIPS_EXTS and ext in RASTER_EXTS:
                idx[key] = str(candidate)     # raster 우선 교체
    return idx, src_root


def read_image(path, tmpdir=None):
    """이미지를 BGR ndarray 로 읽는다 (OS 무관 — photo_io 가 HEIC 차이를 흡수)."""
    return read_bgr(path)


def resolve_source(fname, src_index, src_root):
    """백업의 이미지 파일명 → 실제 파일 경로.
    1순위: --src-dir 인덱스(원본 고해상도), 2순위: 툴이 쓰던 <root>/<폴더>/img/<파일명>."""
    if not is_single_basename(fname):
        print(f"⚠️ 이미지 파일명 거부: {fname!r}", file=sys.stderr)
        return None, None
    raw_stem = os.path.splitext(fname)[0]
    if not raw_stem or not is_single_basename(raw_stem):
        print(f"⚠️ 이미지 stem 거부: {fname!r}", file=sys.stderr)
        return None, None
    stem = nfc(raw_stem)
    hit = src_index.get(stem)
    if hit:
        resolved = Path(hit).resolve()
        if src_root is not None and is_within(resolved, src_root):
            return str(resolved), "src"
        print(f"⚠️ 원본 경로 거부 — --src-dir 밖 파일: {fname!r}", file=sys.stderr)
    return None, None


def resolve_fallback(root: Path, folder: str, fname: str):
    """작업용 이미지 폴백도 서버 루트와 그룹 img 폴더 안으로 제한한다."""
    allowed = (root / folder / "img").resolve()
    if not is_within(allowed, root):
        print(f"⚠️ 폴백 경로 거부 — 서버 루트 밖 폴더: {folder!r}", file=sys.stderr)
        return None
    candidate = (allowed / fname).resolve()
    if not is_within(candidate, allowed):
        print(f"⚠️ 폴백 경로 거부 — img 폴더 밖 파일: {fname!r}", file=sys.stderr)
        return None
    return str(candidate) if candidate.is_file() else None


def render_page(task: dict) -> tuple[int, np.ndarray | None, str | None]:
    """사진 한 장을 직렬 렌더 순서 그대로 처리한다. 워커에서는 출력하지 않는다."""
    idx = task["idx"]
    path = task.get("path")
    fname = task["fname"]
    if path is None:
        return idx, None, f"    ⚠️ 원본 없음: {fname}"
    img = read_image(path)
    if img is None:
        return idx, None, f"    ⚠️ 읽기 실패: {path}"
    try:
        out = warp(img, task["corners"], task.get("ratio"), task["width"])
    except ValueError as exc:
        return idx, None, f"    ⚠️ 출력 크기 거부: {fname} ({exc})"
    if out is None:
        return idx, None, f"    ⚠️ 코너 이상(warp 실패): {fname}"
    out = inset_crop(out, task["inset"])

    warning = None
    color = task.get("color")
    if color:
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            out = apply_manual_color(out, color)
        warning = captured.getvalue().rstrip() or None
    if task.get("enhance"):
        from enhance_led import enhance

        out = enhance(out)
    return idx, out, warning


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backup", required=True, help="툴에서 '백업 내보내기'로 받은 JSON")
    ap.add_argument("--root", required=True,
                    help="서버 루트(=툴 폴더의 부모). 백업의 ../<폴더>/img/ 를 여기서 해석")
    ap.add_argument("--src-dir", default=None,
                    help="원본 고해상도 사진 폴더(재귀 검색). 없으면 --root 의 작업용 이미지 사용")
    ap.add_argument("--out", required=True, help="PDF 출력 폴더")
    ap.add_argument("--width", type=int, default=1600,
                    help="warp 출력 폭 px (기본 1600 ≈ 150dpi A4 가로). 크게 하면 용량↑")
    ap.add_argument("--dpi", type=float, default=150.0, help="PDF 해상도 메타 (기본 150)")
    ap.add_argument("--inset", type=float, default=0.0,
                    help="각 변 인셋 크롭 비율 (기본 0=무크롭). 0.025 면 2.5%%씩 잘라냄")
    ap.add_argument("--only-done", action="store_true", help="`완료` 표시한 것만 내보내기")
    ap.add_argument("--enhance", action="store_true",
                    help="LED 화면 클린업(무아레·조명불균일 제거)을 색보정 뒤에 추가 적용")
    ap.add_argument("--only", nargs="*", help="특정 폴더만 (디버그)")
    ap.add_argument("--merge", default=None,
                    help="폴더별 PDF 를 이 이름의 통합 PDF 로도 병합 (poppler pdfunite 필요)")
    ap.add_argument("--workers", type=int, default=0,
                    help="병렬 워커 수 (0=자동, 1=직렬; 기본 0)")
    a = ap.parse_args()
    if a.workers < 0:
        ap.error("--workers 는 0 이상의 정수여야 한다")
    if not 10 <= a.width <= MAX_WIDTH:
        print(f"‼️ --width 는 10~{MAX_WIDTH} 범위여야 한다: {a.width}", file=sys.stderr)
        return 1
    workers = default_workers() if a.workers == 0 else a.workers

    try:
        corners, status, ratios, moves, colors = load_backup(a.backup)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"‼️ 백업 거부: {exc}", file=sys.stderr)
        return 1
    print(f"백업: corners={len(corners)} status={len(status)} ratios={len(ratios)} "
          f"moves={len(moves)} color={len(colors)}")
    if not corners:
        print("‼️ 코너 정보가 비어 있다 — 툴에서 '백업 내보내기'로 받은 파일이 맞는지 확인하라.",
              file=sys.stderr)
        return 1

    src_index, src_root = index_sources(a.src_dir)
    if a.src_dir:
        print(f"원본 인덱스: {len(src_index)}개 ({a.src_dir})")
        heic_n = sum(1 for p in src_index.values()
                     if os.path.splitext(p)[1].lower() in SIPS_EXTS)
        if heic_n and not heic_supported():
            print(f"‼️ 원본 중 HEIC 가 {heic_n}장인데 이 환경은 HEIC 를 못 읽는다.\n"
                  f"   해결:  pip install pillow-heif\n"
                  f"   그냥 진행하면 그 사진들은 저해상도 축소본으로 대체된다.",
                  file=sys.stderr)

    # ---- 백업 키를 폴더별로 묶는다 (화자/그룹 이동 override 반영) ----
    by_folder = {}
    excluded = skipped_notdone = 0
    for key in corners:
        if not isinstance(key, str):
            print(f"⚠️ 백업 키 거부 — 문자열이 아님: {key!r}", file=sys.stderr)
            continue
        m = re.fullmatch(r"\.\./([^/]+)/img/([^/]+)", key)
        if not m:
            print(f"⚠️ 백업 키 거부 — 형식 위반: {key!r}", file=sys.stderr)
            continue
        raw_src_folder, raw_fname = m.group(1), m.group(2)
        if not is_single_basename(raw_src_folder) or not is_single_basename(raw_fname):
            print(f"⚠️ 백업 키 거부 — 단일 폴더명·파일명이 아님: {key!r}", file=sys.stderr)
            continue
        st = status.get(key, {})
        if is_excluded(st):
            excluded += 1
            continue
        if a.only_done and not is_done(st):
            skipped_notdone += 1
            continue
        src_folder = nfc(raw_src_folder)
        fname = nfc(raw_fname)
        move = moves.get(key)
        if move is None:
            folder = src_folder
        elif not is_single_basename(move):
            print(f"⚠️ 이동 폴더명 거부 — 원래 폴더 사용: {move!r}", file=sys.stderr)
            folder = src_folder
        else:
            folder = nfc(move)
        by_folder.setdefault(folder, []).append(
            dict(key=key, fname=fname, src_folder=src_folder))
    for f in by_folder:
        by_folder[f].sort(key=lambda it: natural_key(it["fname"]))
    print(f"폴더 {len(by_folder)}개 (제외 {excluded}"
          + (f", 미완료 건너뜀 {skipped_notdone}" if a.only_done else "") + ")")

    out_root = Path(a.out).expanduser()
    try:
        out_root.mkdir(parents=True, exist_ok=True)
        out_root = out_root.resolve()
    except OSError as exc:
        print(f"‼️ 출력 폴더를 준비할 수 없다: {exc}", file=sys.stderr)
        return 1
    root = Path(a.root).expanduser().resolve()
    summary, made = [], {}
    missing_total = 0

    # Apply --only before building tasks so filtered folders are never rendered.
    folders = [f for f in sorted(by_folder, key=natural_key)
               if not a.only or f in a.only]
    pdf_paths = {}
    for folder in folders:
        pdf_path = output_path(out_root, folder + ".pdf")
        if pdf_path is None:
            print(f"‼️ 출력 경로 거부 — --out 밖 경로: {folder!r}", file=sys.stderr)
            return 1
        pdf_paths[folder] = pdf_path
    merge_path = None
    if a.merge:
        merge_path = output_path(out_root, a.merge)
        if merge_path is None:
            print(f"‼️ 통합 PDF 경로 거부 — --out 밖 경로: {a.merge!r}", file=sys.stderr)
            return 1

    tasks = []
    for folder in folders:
        for idx, it in enumerate(by_folder[folder]):
            key, fname = it["key"], it["fname"]
            path, _kind = resolve_source(fname, src_index, src_root)
            if path is None:
                # 원본을 못 찾으면 툴이 쓰던 작업용 이미지로 폴백
                path = resolve_fallback(root, it["src_folder"], fname)
            tasks.append(dict(
                folder=folder,
                idx=idx,
                path=path,
                corners=corners[key],
                ratio=ratios.get(key),
                width=a.width,
                inset=a.inset,
                color=colors.get(key),
                enhance=a.enhance,
                fname=fname,
            ))

    # Use one global pool, then redistribute ordered results to their folders.
    rendered = {folder: [] for folder in folders}
    results = ordered_parallel_map(render_page, tasks, workers)
    for task, result in zip(tasks, results):
        rendered[task["folder"]].append(result)

    for folder in folders:
        pages, n_manual, missing = [], 0, 0
        folder_tasks = by_folder[folder]
        for idx, out, warning in sorted(rendered[folder], key=lambda result: result[0]):
            if warning:
                print(warning, file=sys.stderr)
            if out is None:
                missing += 1
                continue
            key = folder_tasks[idx]["key"]
            if colors.get(key):
                n_manual += 1
            pages.append(Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB)))

        missing_total += missing
        if not pages:
            print(f"  [{folder}] 0쪽 — 건너뜀")
            summary.append((folder, 0, n_manual, missing))
            continue
        pdf = pdf_paths[folder]
        pages[0].save(str(pdf), "PDF", resolution=a.dpi,
                      save_all=True, append_images=pages[1:])
        made[folder] = str(pdf)
        summary.append((folder, len(pages), n_manual, missing))
        print(f"  [{folder}] {len(pages)}쪽 (색보정 {n_manual}"
              + (f", 실패 {missing}" if missing else "") + f") → {pdf}")

    # ---- 통합 PDF ----
    if a.merge and made:
        ordered = [made[f] for f in sorted(made, key=natural_key)]
        unified = str(merge_path)
        try:
            subprocess.run(["pdfunite"] + ordered + [unified], check=True)
            print(f"\n통합 PDF → {unified}")
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            print(f"\n⚠️ 통합 실패({exc}). poppler 설치: brew install poppler", file=sys.stderr)

    print(f"\n사용 워커: {workers}개")
    print("=== 요약 ===")
    total = 0
    for folder, n, nm, miss in summary:
        total += n
        print(f"  {folder:40s} {n:4d}쪽  색보정 {nm:4d}"
              + (f"  ⚠️실패 {miss}" if miss else ""))
    print(f"  {'합계':40s} {total:4d}쪽")
    if missing_total:
        print(f"\n⚠️ 원본을 못 찾거나 읽지 못한 사진 {missing_total}장 — "
              f"--src-dir 경로와 파일명(확장자 제외)이 툴에 넣은 것과 같은지 확인하라.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
