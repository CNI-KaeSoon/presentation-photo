#!/usr/bin/env python3
"""사진 읽기·촬영시각 추출 — Windows · macOS · Linux 공통.

HEIC 를 여는 방법이 OS 마다 달라서 이 모듈이 차이를 흡수한다.

  1) pillow-heif 가 설치돼 있으면  → 모든 OS 에서 HEIC 지원 (권장)
  2) macOS 라면                   → 내장 `sips` 로 폴백
  3) 둘 다 없으면                  → HEIC 미지원 (JPG/PNG 는 정상 동작)

    pip install pillow-heif        # HEIC 를 쓸 때만 필요

JPG·PNG 만 다룬다면 아무것도 추가로 설치할 필요 없다.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# HEIC 지원 감지
# ---------------------------------------------------------------------------
HEIF_OK = False
try:                                    # 1) pillow-heif (크로스 플랫폼)
    import pillow_heif                  # type: ignore
    pillow_heif.register_heif_opener()
    HEIF_OK = True
except ImportError:
    pass

IS_MAC = platform.system() == "Darwin"
SIPS_OK = IS_MAC and shutil.which("sips") is not None

RASTER_EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp")
HEIC_EXTS = (".heic", ".heif")
ALL_EXTS = RASTER_EXTS + HEIC_EXTS


def heic_supported() -> bool:
    return HEIF_OK or SIPS_OK


def backend_report() -> str:
    """지금 이 환경이 무엇으로 사진을 읽는지 한 줄 설명."""
    if HEIF_OK:
        return "HEIC: pillow-heif (모든 OS)"
    if SIPS_OK:
        return "HEIC: macOS sips (pillow-heif 를 설치하면 더 빠릅니다)"
    return ("HEIC: ✗ 미지원 — JPG/PNG 만 처리합니다. "
            "HEIC 를 쓰려면: pip install pillow-heif")


def nfc(s: str) -> str:
    """macOS 는 한글 파일명을 자모 분리(NFD)로 저장한다. 비교 전 완성형으로 통일."""
    return unicodedata.normalize("NFC", s)


def is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in ALL_EXTS


# ---------------------------------------------------------------------------
# 이미지 열기
# ---------------------------------------------------------------------------
def open_pil(path: str) -> Image.Image | None:
    """PIL Image 로 연다(RGB). 실패하면 None."""
    ext = os.path.splitext(path)[1].lower()
    if ext in HEIC_EXTS and not HEIF_OK:
        if not SIPS_OK:
            return None
        with tempfile.TemporaryDirectory() as td:                # sips 폴백
            out = os.path.join(td, "conv.png")
            subprocess.run(["sips", "-s", "format", "png", path, "--out", out],
                           capture_output=True)
            if not os.path.exists(out):
                return None
            with Image.open(out) as im:
                return im.convert("RGB")
    try:
        with Image.open(path) as im:
            return im.convert("RGB")
    except Exception:
        return None


def read_bgr(path: str) -> np.ndarray | None:
    """OpenCV 가 쓰는 BGR ndarray 로 읽는다.

    cv2.imread 를 쓰지 않는 이유: 한글·비ASCII 경로에서 빌드에 따라 조용히 실패한다.
    """
    im = open_pil(path)
    if im is None:
        return None
    return np.asarray(im)[:, :, ::-1].copy()          # RGB → BGR


# ---------------------------------------------------------------------------
# 촬영시각
# ---------------------------------------------------------------------------
_EXIF_DATETIME_ORIGINAL = 36867
_EXIF_DATETIME = 306


def _from_exif(path: str) -> datetime | None:
    """EXIF DateTimeOriginal. 카메라가 기록한 **현지 시각**(시간대 정보 없음)."""
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            if not exif:
                return None
            raw = exif.get(_EXIF_DATETIME_ORIGINAL) or exif.get(_EXIF_DATETIME)
            # DateTimeOriginal 은 보통 IFD 하위에 있다
            if not raw:
                try:
                    sub = exif.get_ifd(0x8769)
                    raw = sub.get(_EXIF_DATETIME_ORIGINAL) if sub else None
                except Exception:
                    raw = None
            if not raw:
                return None
            return datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None


def _from_sips(path: str) -> datetime | None:
    """macOS `sips -g creation`. HEIC 에서 pillow-heif 없이 촬영시각을 얻는 경로.

    ⚠️ `mdls kMDItemContentCreationDate` 를 쓰면 안 된다. 그건 **파일** 생성시각이라
       사진을 복사하는 순간 복사 시점으로 덮여 촬영시각을 잃는다(배포받은 사람은
       반드시 복사한다 → 그룹 배정이 통째로 무너진다). `sips -g creation` 은 이미지
       내부 메타데이터를 읽어 복사본에서도 정확하고, 카메라 현지시각이라 시간대 보정도
       필요 없다.
    """
    if not SIPS_OK:
        return None
    try:
        out = subprocess.run(["sips", "-g", "creation", path],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("creation:"):
                return datetime.strptime(line.split("creation:", 1)[1].strip(),
                                         "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None
    return None


def capture_time(path: str) -> tuple[datetime | None, str]:
    """(촬영시각, 출처) 를 돌려준다. 출처 = exif | sips | mtime | none.

    시간대 없는 naive datetime 이다 — 사진끼리의 **상대 순서·간격**만 쓰므로 충분하다.
    """
    dt = _from_exif(path)
    if dt:
        return dt, "exif"
    dt = _from_sips(path)
    if dt:
        return dt, "sips"
    try:                                                  # 최후: 파일 수정시각
        return datetime.fromtimestamp(os.path.getmtime(path)), "mtime"
    except OSError:
        return None, "none"


def scan_photos(src_dir: str) -> list[dict]:
    """폴더를 재귀 스캔해 [{path, name, stem, ext, when, when_src}] 를 촬영순으로."""
    rows = []
    for root, _d, files in os.walk(src_dir):
        for fn in sorted(files):
            if fn.startswith("."):
                continue
            p = os.path.join(root, fn)
            if not is_image(p):
                continue
            stem, ext = os.path.splitext(fn)
            when, src = capture_time(p)
            rows.append(dict(path=p, name=fn, stem=nfc(stem), ext=ext.lower(),
                             when=when, when_src=src))
    rows.sort(key=lambda r: (r["when"] or datetime.max, r["stem"]))
    return rows


if __name__ == "__main__":                                # 진단용
    print(f"OS: {platform.system()}  ·  {backend_report()}")
    if len(sys.argv) > 1:
        for r in scan_photos(sys.argv[1])[:20]:
            w = r["when"].strftime("%Y-%m-%d %H:%M:%S") if r["when"] else "—"
            print(f"  {r['name']:28s} {w}  ({r['when_src']})")
