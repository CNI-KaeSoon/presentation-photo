#!/usr/bin/env python3
"""직렬(workers=1) ↔ 병렬(workers=cpu) 픽셀·JPEG·순서 동일 검증(C2 게이트)."""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile

import cv2
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from export_pdf import render_page  # noqa: E402
from parallel_map import default_workers, ordered_parallel_map  # noqa: E402
from prepare_photos import _convert_task  # noqa: E402
from test_color_parity import CASES  # noqa: E402

# Windows 콘솔 기본 인코딩(cp949)에는 '—'·'·'·'⚠' 같은 문자가 없어, 그대로 print 하면
# UnicodeEncodeError 로 스크립트가 죽는다(실측: init_worktree 가 U+2014 에서 중단).
# 출력 스트림을 UTF-8 로 고정해 어떤 콘솔에서도 깨지거나 죽지 않게 한다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 파이프·구버전 등 재설정 불가 시 무시
        pass


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _synth_photo(seed: int) -> np.ndarray:
    """1200×900 RGB 합성 사진. 밝은 가짜 스크린 사각형을 포함한다."""
    rng = np.random.default_rng(seed)
    h, w = 900, 1200
    yy, xx = np.mgrid[0:h, 0:w]
    base = np.empty((h, w, 3), np.float32)
    base[:, :, 0] = 30 + 150 * xx / w + 35 * yy / h
    base[:, :, 1] = 45 + 80 * xx / w + 95 * yy / h
    base[:, :, 2] = 70 + 130 * (1 - xx / w) + 30 * yy / h
    noise = rng.normal(0, 11, size=(h, w, 3))
    image = np.clip(base + noise, 0, 255).astype(np.uint8)

    screen = np.array([[150, 125], [1050, 105], [1090, 760], [120, 790]], np.int32)
    cv2.fillConvexPoly(image, screen, (238, 242, 247))
    for row in range(7):
        y = 205 + row * 72
        color = (35 + row * 18, 75 + seed % 60, 165 - row * 12)
        cv2.rectangle(image, (235, y), (940 - row * 18, y + 24), color, -1)
    cv2.putText(image, f"SLIDE TEST / {seed}", (230, 175), cv2.FONT_HERSHEY_SIMPLEX,
                1.25, (30, 30, 30), 3, cv2.LINE_AA)
    return image


def _report(name: str, ok: bool, detail: str = "") -> bool:
    suffix = f" · {detail}" if detail else ""
    print(f"[{'OK' if ok else 'FAIL'}] {name}{suffix}")
    return ok


def main() -> int:
    all_ok = True
    workers = default_workers()

    with tempfile.TemporaryDirectory(prefix="parallel-parity-") as tmp:
        sources = []
        for idx in range(6):
            path = os.path.join(tmp, f"IMG_{idx + 1:04d}.jpg")
            Image.fromarray(_synth_photo(20260716 + idx)).save(
                path, "JPEG", quality=93, optimize=True)
            sources.append(path)

        borrowed = [CASES[i][1] for i in (0, 1, 2, 6, 9, 12)]
        color_sets = [dict(params) for params in borrowed]
        color_sets[4]["y_nlm"] = 9
        corners = [
            [[0.125, 0.139], [0.875, 0.117], [0.100, 0.878], [0.908, 0.844]],
            [[0.130, 0.145], [0.870, 0.120], [0.105, 0.872], [0.900, 0.840]],
            [[0.120, 0.135], [0.880, 0.115], [0.098, 0.880], [0.910, 0.850]],
            [[0.128, 0.140], [0.872, 0.118], [0.102, 0.875], [0.905, 0.846]],
            [[0.122, 0.142], [0.878, 0.116], [0.100, 0.876], [0.907, 0.848]],
            [[0.126, 0.137], [0.874, 0.119], [0.103, 0.879], [0.903, 0.843]],
        ]
        ratios = [16 / 9, 4 / 3, 16 / 10, 3 / 2, 1.85, 16 / 9]
        tasks = [
            dict(idx=idx, path=path, corners=corners[idx], ratio=ratios[idx],
                 width=720, inset=0.006 * idx, color=color_sets[idx], enhance=False,
                 fname=os.path.basename(path))
            for idx, path in enumerate(sources)
        ]

        try:
            serial = [render_page(task) for task in tasks]
            parallel = list(ordered_parallel_map(render_page, tasks, workers=workers))
            serial_order = [row[0] for row in serial]
            parallel_order = [row[0] for row in parallel]
            all_ok &= _report(
                "render_page 반환 순서 0..5",
                serial_order == list(range(6)) and parallel_order == list(range(6)),
                f"workers={workers}",
            )
            for idx, (serial_row, parallel_row) in enumerate(zip(serial, parallel)):
                serial_arr, parallel_arr = serial_row[1], parallel_row[1]
                ok = (
                    serial_arr is not None
                    and parallel_arr is not None
                    and serial_row[2] is None
                    and parallel_row[2] is None
                    and _sha_bytes(serial_arr.tobytes())
                    == _sha_bytes(parallel_arr.tobytes())
                )
                detail = "y_nlm=9" if color_sets[idx].get("y_nlm", 0) > 0 else "sha256 동일"
                all_ok &= _report(f"render_page {idx} 픽셀 패리티", ok, detail)
        except Exception as exc:
            all_ok &= _report("render_page 직렬/병렬 실행", False, repr(exc))

        serial_dir = os.path.join(tmp, "serial")
        parallel_dir = os.path.join(tmp, "parallel")
        os.makedirs(serial_dir)
        os.makedirs(parallel_dir)
        serial_tasks = [
            dict(idx=idx, src=src, dst=os.path.join(serial_dir, f"IMG_{idx + 1:04d}.jpg"),
                 max_px=640, quality=85)
            for idx, src in enumerate(sources)
        ]
        parallel_tasks = [
            dict(idx=idx, src=src, dst=os.path.join(parallel_dir, f"IMG_{idx + 1:04d}.jpg"),
                 max_px=640, quality=85)
            for idx, src in enumerate(sources)
        ]
        try:
            serial_converted = list(ordered_parallel_map(
                _convert_task, serial_tasks, workers=1))
            parallel_converted = list(ordered_parallel_map(
                _convert_task, parallel_tasks, workers=workers))
            for idx in range(6):
                rows_ok = serial_converted[idx][1:] == (True, None)
                rows_ok &= parallel_converted[idx][1:] == (True, None)
                hashes_ok = (
                    _sha_file(serial_tasks[idx]["dst"])
                    == _sha_file(parallel_tasks[idx]["dst"])
                )
                all_ok &= _report(
                    f"prepare_photos {idx} JPEG 파일 패리티",
                    rows_ok and hashes_ok,
                    "sha256 동일",
                )
        except Exception as exc:
            all_ok &= _report("prepare_photos 직렬/병렬 실행", False, repr(exc))

    print("PARALLEL PARITY: ALL PASS" if all_ok else "PARALLEL PARITY: FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
