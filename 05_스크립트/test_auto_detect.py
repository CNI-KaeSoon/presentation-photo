#!/usr/bin/env python3
"""자동 검출기(auto_detect_screen.detect_screen)의 입출력 계약 회귀 테스트.

정확도는 사람이 손본 정답이 있어야 잴 수 있으므로 eval_auto_detect.py 가 맡는다. 여기서는
합성 사진으로 (1) 반환 형식·범위, (2) 뚜렷한 밝은 사각형을 대체로 찾는지, (3) 이상한 입력에서도
죽지 않는지만 확인한다.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import cv2
import numpy as np

# Windows 콘솔 기본 인코딩(cp949)에는 '—'·'·'·'⚠' 같은 문자가 없어, 그대로 print 하면
# UnicodeEncodeError 로 스크립트가 죽는다(실측: init_worktree 가 U+2014 에서 중단).
# 출력 스트림을 UTF-8 로 고정해 어떤 콘솔에서도 깨지거나 죽지 않게 한다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 파이프·구버전 등 재설정 불가 시 무시
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from auto_detect_screen import detect_screen  # noqa: E402


def synthetic(w=960, h=720):
    """어두운 무대 위의 밝은 원근 사각형(글자 모양 잡음 포함)과 그 정답(TL,TR,BL,BR 정규좌표)."""
    img = np.full((h, w, 3), 25, np.uint8)
    quad = np.array([[0.15 * w, 0.18 * h], [0.85 * w, 0.14 * h], [0.12 * w, 0.74 * h], [0.88 * w, 0.70 * h]], np.float32)
    poly = quad[[0, 1, 3, 2]].astype(np.int32)
    cv2.fillConvexPoly(img, poly, (235, 235, 235))
    for i in range(6):  # 슬라이드 내용처럼 안쪽에 어두운 줄
        y = int((0.28 + 0.07 * i) * h)
        cv2.line(img, (int(0.22 * w), y), (int(0.7 * w), y), (40, 40, 40), 6)
    truth = (quad / np.array([w, h], np.float32)).tolist()
    return img, truth


def check_contract(quad, conf):
    assert isinstance(conf, float) and math.isfinite(conf) and 0.0 <= conf <= 1.0, conf
    if quad is None:
        return
    assert len(quad) == 4
    for x, y in quad:
        assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0, quad


def main() -> int:
    img, truth = synthetic()
    quad, conf = detect_screen(img)
    check_contract(quad, conf)
    assert quad is not None, "뚜렷한 밝은 사각형을 못 찾음"
    diag = math.hypot(*img.shape[1::-1])
    worst = max(math.hypot((p[0] - t[0]) * img.shape[1], (p[1] - t[1]) * img.shape[0])
                for p, t in zip(quad, truth)) / diag
    assert worst < 0.05, f"합성 사각형 오차 {worst:.3f}"
    print(f"synthetic ok: max_err={worst:.4f} conf={conf:.2f}")

    # 흑백·알파 채널·세로 사진·아주 작은 사진·빈 화면에서도 예외 없이 형식을 지킨다.
    variants = {
        "gray": cv2.cvtColor(img, cv2.COLOR_BGR2GRAY),
        "bgra": cv2.cvtColor(img, cv2.COLOR_BGR2BGRA),
        "portrait": np.ascontiguousarray(np.rot90(img)),
        "tiny": cv2.resize(img, (64, 48)),
        "blank": np.full((480, 640, 3), 128, np.uint8),
        "noise": np.random.default_rng(0).integers(0, 255, (480, 640, 3), dtype=np.uint8),
    }
    for name, v in variants.items():
        q, c = detect_screen(v)
        check_contract(q, c)
        print(f"{name}: {'none' if q is None else 'quad'} conf={c:.2f}")
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
