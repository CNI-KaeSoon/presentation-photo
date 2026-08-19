#!/usr/bin/env python3
"""90° 회전(코너 순환) 회귀 테스트 — JS(index.html) ↔ Python(export_pdf.warp) 파리티.

회전은 코너 배열 [TL, TR, BL, BR] 의 **순서만 순환**시켜 구현한다. JS 미리보기와 Python 최종
PDF 가 같은 코너 규약을 쓰기 때문에 이 순환 하나로 양쪽이 함께 돌아간다. 그래서 검증해야 할
것은 "순환식이 정말 90° 회전인가"와 "JS 와 Python 이 같은 순환을 보는가" 두 가지다.

- R1 JS 순환식 = Python 기대 순열: index.html 의 rotateCorners 를 node 로 그대로 추출·실행해
       임의 사각형 200개에 대해 완전 일치를 확인한다(부동소수 오차 없음 — 순수 재배열).
- R2 4회 항등 · CW↔CCW 왕복: 값이 한 비트도 변하지 않아야 한다(반대 버튼 = 정확한 되돌리기).
- R3 사영변환 항등: 회전 코너의 사영변환이 "원본 사영변환 ∘ 결과면 90° 회전"과 같은지 연속
       수학으로 확인한다. 래스터화 이전 단계이므로 리샘플링 오차가 섞이지 않는다.
- R4 실렌더 4분면 배치: export_pdf.warp 로 실제 이미지를 굽고, 사분면 색이 90° 돌아간 자리로
       갔는지 본다. 16:9(고정 비율)·자유 비율 모두. 회전 방향이 뒤바뀌면 여기서 잡힌다.
- R5 픽셀 완전 일치: 정사각 출력에서 warp(회전코너) == rot90(warp(원본코너)) 가 성립하는지.
       export_pdf 의 dst 규약(W-1 이 아니라 W)에서 오는 기존 반픽셀 어긋남을 배제하기 위해
       대칭 dst 로 같은 변환을 재구성해 비교한다 — 순환식 자체의 정확도만 본다.

Usage: python3 05_스크립트/test_rotate_parity.py    (node 필요, 없으면 R1 만 건너뜀)
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import tempfile

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from export_pdf import warp  # noqa: E402

# Windows 콘솔 기본 인코딩(cp949)에는 '—'·'·'·'⚠' 같은 문자가 없어, 그대로 print 하면
# UnicodeEncodeError 로 스크립트가 죽는다. 출력 스트림을 UTF-8 로 고정한다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

INDEX = os.path.join(os.path.dirname(HERE), "02_작업장", "slide_tool", "index.html")
FN_RE = re.compile(r"function rotateCorners\(corners,dir\)\{.*?\n\}", re.S)

failures = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"[{'OK ' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def rotate_py(corners, direction):
    """Python 쪽 기대 순열. 코너 순서는 [TL, TR, BL, BR]."""
    c = corners
    return [c[2], c[0], c[3], c[1]] if direction > 0 else [c[1], c[3], c[0], c[2]]


def random_quads(n, seed):
    rng = random.Random(seed)
    return [[[round(rng.random(), 5), round(rng.random(), 5)] for _ in range(4)]
            for _ in range(n)]


# ---- R1. index.html 의 회전 함수를 node 로 실행해 Python 기대 순열과 대조 -------------
def r1_js_matches_python():
    from shutil import which
    if which("node") is None:
        print("[SKIP] node 없음 — R1(JS 순환식 대조)을 건너뜁니다.")
        return
    try:
        source = open(INDEX, encoding="utf-8").read()
    except OSError as exc:
        check(False, "R1 index.html 읽기", str(exc))
        return
    found = FN_RE.search(source)
    if not found:
        check(False, "R1 rotateCorners 추출", "index.html 에서 함수를 찾지 못했습니다")
        return
    quads = random_quads(200, seed=11)
    script = (found.group(0) + "\n"
              + "const quads=JSON.parse(process.argv[2]);\n"
              + "const out=quads.map(q=>[rotateCorners(q,1),rotateCorners(q,-1)]);\n"
              + "process.stdout.write(JSON.stringify(out));\n")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        path = handle.name
    try:
        run = subprocess.run([which("node"), path, json.dumps(quads)],
                             capture_output=True, text=True, encoding="utf-8", check=False)
    finally:
        os.unlink(path)
    if run.returncode != 0:
        check(False, "R1 node 실행", (run.stderr or run.stdout).strip()[:300])
        return
    got = json.loads(run.stdout)
    want = [[rotate_py(q, 1), rotate_py(q, -1)] for q in quads]
    check(got == want, "R1 JS 순환식 = Python 기대 순열",
          f"임의 사각형 {len(quads)}개 · 시계/반시계 모두 완전 일치")


# ---- R2. 4회 회전 항등 · CW↔CCW 왕복 -----------------------------------------------
def r2_identity():
    bad = 0
    for quad in random_quads(200, seed=23):
        spun = quad
        for _ in range(4):
            spun = rotate_py(spun, 1)
        if spun != quad:
            bad += 1
        if rotate_py(rotate_py(quad, 1), -1) != quad:
            bad += 1
        if rotate_py(rotate_py(quad, -1), 1) != quad:
            bad += 1
    check(bad == 0, "R2 4회 회전 항등 · CW↔CCW 왕복", f"불일치 {bad}건 (값 비트 동일 요구)")


# ---- R3. 사영변환 항등 (래스터화 이전) ----------------------------------------------
def r3_projective_identity():
    worst = 0.0
    side = 1.0
    rng = random.Random(37)
    samples = [(rng.random(), rng.random()) for _ in range(20)]

    def project(mat, px, py):
        vec = mat @ np.array([px, py, 1.0])
        return vec[:2] / vec[2]

    def matrix(corners):
        tl, tr, bl, br = [np.array(p, np.float32) for p in corners]
        src = np.array([tl, tr, br, bl], np.float32)
        dst = np.array([[0, 0], [side, 0], [side, side], [0, side]], np.float32)
        return cv2.getPerspectiveTransform(src, dst)

    for quad in random_quads(150, seed=37):
        try:
            inv_plain = np.linalg.inv(matrix(quad))
            inv_spun = np.linalg.inv(matrix(rotate_py(quad, 1)))
        except np.linalg.LinAlgError:
            continue          # 퇴화 사각형 — 도구에서도 degenerate 로 표시되는 경우
        for x, y in samples:
            # 결과를 시계 90° 돌리면 좌표 (x,y) 에는 원본 결과의 (y, side-x) 가 온다
            got = project(inv_spun, x, y)
            want = project(inv_plain, y, side - x)
            worst = max(worst, float(np.abs(got - want).max()))
    check(worst < 1e-5, "R3 사영변환 항등 (연속 수학)", f"최대오차 {worst:.2e} (허용 1e-5)")


# ---- R4. 실렌더 4분면 배치 -----------------------------------------------------------
QUADRANT_IMG = None


def quadrant_image():
    global QUADRANT_IMG
    if QUADRANT_IMG is None:
        img = np.zeros((400, 400, 3), np.uint8)
        img[:200, :200] = (255, 0, 0)      # BGR 파랑 = 좌상
        img[:200, 200:] = (0, 255, 0)      # 초록    = 우상
        img[200:, :200] = (0, 0, 255)      # 빨강    = 좌하
        img[200:, 200:] = (0, 255, 255)    # 노랑    = 우하
        QUADRANT_IMG = img
    return QUADRANT_IMG


def quadrant_colors(rendered):
    height, width = rendered.shape[:2]

    def patch(y, x):
        block = rendered[y - 6:y + 6, x - 6:x + 6].reshape(-1, 3)
        return tuple(int(v) for v in np.round(block.mean(axis=0)))
    return {
        "TL": patch(height // 4, width // 4),
        "TR": patch(height // 4, 3 * width // 4),
        "BL": patch(3 * height // 4, width // 4),
        "BR": patch(3 * height // 4, 3 * width // 4),
    }


def r4_render_layout():
    blue, green, red, yellow = (255, 0, 0), (0, 255, 0), (0, 0, 255), (0, 255, 255)
    full = [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]
    skew = [[0.06, 0.03], [0.94, 0.11], [0.02, 0.92], [0.97, 0.98]]
    for label, corners in (("전체 프레임", full), ("사다리꼴", skew)):
        for ratio_label, ratio in (("자유", None), ("16:9", 16 / 9)):
            plain = warp(quadrant_image(), corners, ratio, 480)
            spun_cw = warp(quadrant_image(), rotate_py(corners, 1), ratio, 480)
            spun_ccw = warp(quadrant_image(), rotate_py(corners, -1), ratio, 480)
            base = quadrant_colors(plain)
            want_cw = {"TL": base["BL"], "TR": base["TL"], "BL": base["BR"], "BR": base["TR"]}
            want_ccw = {"TL": base["TR"], "TR": base["BR"], "BL": base["TL"], "BR": base["BL"]}
            check(quadrant_colors(spun_cw) == want_cw,
                  f"R4 시계 90° 4분면 배치 ({label}·{ratio_label})",
                  f"{quadrant_colors(spun_cw)}")
            check(quadrant_colors(spun_ccw) == want_ccw,
                  f"R4 반시계 90° 4분면 배치 ({label}·{ratio_label})",
                  f"{quadrant_colors(spun_ccw)}")
    # 방향이 실제로 다른지 — 시계와 반시계가 같은 결과면 위 비교가 통과해도 무의미하다
    plain = warp(quadrant_image(), full, None, 480)
    check(not np.array_equal(warp(quadrant_image(), rotate_py(full, 1), None, 480),
                             warp(quadrant_image(), rotate_py(full, -1), None, 480)),
          "R4 시계 ≠ 반시계", "두 방향이 서로 다른 결과인지")
    check(not np.array_equal(warp(quadrant_image(), rotate_py(full, 1), None, 480), plain),
          "R4 회전 ≠ 원본", "회전이 실제로 그림을 바꾸는지")


# ---- R5. 정사각 출력 픽셀 완전 일치 ---------------------------------------------------
def symmetric_warp(img, corners, side):
    """export_pdf.warp 와 같은 변환이되 dst 를 대칭(side-1)으로 잡은 판본.

    export_pdf 는 dst 를 [0, W] 로 잡는데, 이 규약에서는 출력 격자가 회전에 대해 대칭이
    아니라 회전 전후 비교에 최대 1픽셀의 기존 어긋남이 섞인다(회전 로직과 무관한 성질).
    여기서는 순환식 자체의 정확도만 보기 위해 대칭 격자로 재구성한다.
    """
    height, width = img.shape[:2]
    tl, tr, bl, br = [np.array([p[0] * width, p[1] * height], np.float32) for p in corners]
    src = np.array([tl, tr, br, bl], np.float32)
    edge = side - 1
    dst = np.array([[0, 0], [edge, 0], [edge, edge], [0, edge]], np.float32)
    return cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (side, side))


def r5_pixel_exact():
    """warp(회전코너) 와 rot90(warp(원본코너)) 의 래스터 비교.

    변환행렬 수준에서는 정확히 같다(R3 가 1e-10 수준으로, 별도 실측은 상대오차 1e-17). 다만
    cv2.warpPerspective 는 원본 좌표를 1/32 픽셀 고정소수점으로 양자화하므로, 원근이 들어간
    사다리꼴에서는 반올림 경계에 걸린 극소수 픽셀이 1 LSB 어긋난다(실측 270,000 중 1픽셀).
    그래서 판정은 "maxdiff <= 1 이고 어긋난 픽셀이 0.01% 이하" 로 둔다 — 순열이 틀리면
    화면 절반이 어긋나므로 이 기준으로도 즉시 잡힌다.
    """
    yy, xx = np.mgrid[0:400, 0:400].astype(np.float32)
    smooth = np.dstack([xx * 0.6, yy * 0.6, (xx + yy) * 0.3]).astype(np.uint8)
    for label, img in (("계단 4분면", quadrant_image()), ("그라디언트", smooth)):
        for corners_label, corners in (
            ("전체 프레임", [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]),
            ("사다리꼴", [[0.05, 0.04], [0.95, 0.09], [0.03, 0.93], [0.96, 0.99]]),
        ):
            plain = symmetric_warp(img, corners, 300)
            for direction, k, name in ((1, -1, "시계"), (-1, 1, "반시계")):
                got = symmetric_warp(img, rotate_py(corners, direction), 300)
                want = np.rot90(plain, k)
                delta = np.abs(got.astype(int) - want.astype(int))
                worst = int(delta.max())
                off = int((delta > 0).sum())
                ratio = off / delta.size
                # 축정렬(전체 프레임)에서는 양자화 경계가 대칭이라 완전 일치가 나와야 한다
                limit = 0.0 if corners_label == "전체 프레임" else 1e-4
                check(worst <= 1 and ratio <= limit,
                      f"R5 {name} 90° 래스터 일치 ({label}·{corners_label})",
                      f"maxdiff={worst} 어긋난 픽셀={off}/{delta.size} "
                      f"(상한 {int(round(limit * delta.size))}픽셀·1LSB)")


def main() -> int:
    r1_js_matches_python()
    r2_identity()
    r3_projective_identity()
    r4_render_layout()
    r5_pixel_exact()
    print()
    if failures:
        print(f"ROTATE PARITY: FAIL ({len(failures)}건) — " + " / ".join(failures[:5]))
        return 1
    print("ROTATE PARITY: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
