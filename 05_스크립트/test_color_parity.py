#!/usr/bin/env python3
"""JS(index.html) ↔ Python(slide_color.apply_manual_color) 색연산 파리티 테스트.

02_작업장/slide_tool/index.html 의 `// ===COLOR_CORE_START===` ~ `// ===COLOR_CORE_END===`
블록(브라우저 미리보기가 실제로 쓰는 코드)을 그대로 node 로 추출·실행하고, 동일 입력·
파라미터에 대해 Python apply_manual_color 결과와 픽셀 단위로 대조한다.

- 대수 단계(밝기·대비·색온도/색조·채도·감마·레벨·다크통일): 완전 일치(max diff 0)를 기대.
- 선명도(언샤프, 부동소수 분리 컨볼루션): 반올림 오차로 <=1 허용.
- 무아레제거(y_nlm)는 파이썬 전용 NLM 이므로 파리티 대상에서 제외(=0 으로 테스트).

색연산을 고쳤다면 **반드시** 이 테스트를 통과시켜라. 실패 = 미리보기와 최종 PDF 가 어긋난 상태.

Usage: python3 05_스크립트/test_color_parity.py    (node 필요)
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import slide_color  # noqa: E402

PKG = os.path.dirname(HERE)
INDEX_HTML = os.path.join(PKG, "02_작업장", "slide_tool", "index.html")
START = "// ===COLOR_CORE_START==="
END = "// ===COLOR_CORE_END==="

RUNNER_TAIL = r"""
// ---- parity runner (test-only) ----
const fs = require('fs');
const inp = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const {width, height, pixels, params} = inp;
const n = width * height;
const data = new Uint8ClampedArray(n * 4);
for (let i = 0; i < n; i++) {
  data[i*4] = pixels[i*3]; data[i*4+1] = pixels[i*3+1];
  data[i*4+2] = pixels[i*3+2]; data[i*4+3] = 255;
}
const imgData = {data, width, height};
applyColorToImageData(imgData, params);
const out = new Array(n * 3);
for (let i = 0; i < n; i++) { out[i*3]=data[i*4]; out[i*3+1]=data[i*4+1]; out[i*3+2]=data[i*4+2]; }
fs.writeFileSync(process.argv[3], JSON.stringify(out));
"""


def extract_core():
    src = open(INDEX_HTML, encoding="utf-8").read()
    a = src.index(START)
    b = src.index(END)
    if a < 0 or b < 0 or b < a:
        raise SystemExit("COLOR_CORE 마커를 index.html 에서 찾지 못함")
    return src[a:b]


def synth_image(w=40, h=30):
    """다양한 밝기/채도/색상 + 어두운·거의흰 영역을 포함한 합성 RGB 이미지."""
    rng = np.random.default_rng(20260716)
    img = np.zeros((h, w, 3), np.uint8)
    xs = np.linspace(0, 255, w)
    for y in range(h):
        base = np.linspace(y * 3, 255 - y * 2, w)
        img[y, :, 0] = np.clip(base + rng.integers(-15, 15, w), 0, 255)
        img[y, :, 1] = np.clip(xs * 0.7 + y * 2 + rng.integers(-15, 15, w), 0, 255)
        img[y, :, 2] = np.clip(255 - xs + rng.integers(-15, 15, w), 0, 255)
    img[0:3, 0:3] = 4       # near-black patch (dark/levels/darkUnify 자극)
    img[-3:, -3:] = 251     # near-white patch (white-point 자극)
    return img


def run_js(core, img_rgb, params):
    h, w = img_rgb.shape[:2]
    pixels = img_rgb.reshape(-1).tolist()
    with tempfile.TemporaryDirectory() as td:
        runner = os.path.join(td, "runner.js")
        infile = os.path.join(td, "in.json")
        outfile = os.path.join(td, "out.json")
        open(runner, "w").write(core + "\n" + RUNNER_TAIL)
        json.dump(dict(width=w, height=h, pixels=pixels, params=params), open(infile, "w"))
        subprocess.run(["node", runner, infile, outfile], check=True,
                       capture_output=True, text=True)
        flat = json.load(open(outfile))
    return np.array(flat, np.int32).reshape(h, w, 3)


CASES = [
    ("identity", {}, 0),
    ("bright/contrast/temp/tint/sat/gamma",
     dict(brightness=30, contrast=1.3, temp=40, tint=-20, saturation=1.4, gamma=0.8), 0),
    ("levels only", dict(blackPct=2, whitePct=92), 0),
    ("gamma+levels+desat", dict(gamma=1.5, whitePct=90, blackPct=1, saturation=0.6), 0),
    ("darkUnify (same formula)", dict(darkUnify=0.7), 0),
    ("saturation boost", dict(saturation=1.8), 0),
    ("sharpen (float conv, tol=1)", dict(sharpen=1.2), 1),
    ("combined no-nlm (tol=1)",
     dict(brightness=-10, contrast=1.2, temp=-30, saturation=1.3, gamma=1.2,
          blackPct=1.5, whitePct=93, sharpen=0.8, darkUnify=0.4), 1),
    # --- 6b 자동레벨(채널별 독립 스트레치) ---
    # 자동레벨만: 채널마다 다른 lo/hi → 색캐스트 제거. 공통 레벨(6)은 무동작.
    ("autoLevels only (per-channel)",
     dict(rLo=12, rHi=240, gLo=5, gHi=250, bLo=30, bHi=200), 0),
    # 공통 레벨(6) + 채널별(6b) 직렬 병존 — 순서(6 → 6b)가 JS/PY 동일해야 통과.
    ("autoLevels + common levels (serial 6→6b)",
     dict(blackPct=1, whitePct=95, rLo=20, rHi=230, gLo=10, gHi=245, bLo=8, bHi=210,
          brightness=8, contrast=1.15, gamma=0.9), 0),
    # 극단값: hi<=lo(무동작 가드) · 초광폭 · 초협폭(강한 클리핑) 혼합 + 기본값(항등) 채널.
    ("autoLevels extremes (hi<=lo guard / narrow / identity)",
     dict(rLo=200, rHi=200, gLo=120, gHi=130, bLo=0, bHi=255, saturation=1.2), 0),
    # ⚠️ 이중 반올림(double-rounding) 회귀 가드 — 이 케이스는 반드시 유지할 것.
    # 스케일 255/(hi-lo) 가 float32 로 정확히 표현되지 않는 값이라야 JS(double 연산)와
    # Python(NEP 50: float32 스칼라 강등)의 차이가 드러난다. 255/240=1.0625 는 float32 정확값이라
    # 이 버그를 못 잡지만, 255/252(상대오차 4.5e-8)·255/249 는 잡는다. 실제 슬라이드에 '자동 레벨'
    # 을 눌렀을 때 나온 실측값이며, color_render 의 6b 가 float64 경로를 잃으면 이 40x30 이미지
    # 에서도 7개 채널값이 ±1 어긋나 tol=0 에서 즉시 FAIL 한다(1280x720 실사에선 ~7,150개).
    ("autoLevels inexact-scale (255/252·255/249 — double-rounding guard)",
     dict(rLo=0, rHi=252, gLo=0, gHi=249, bLo=0, bHi=255), 0),
    # 오프셋(lo>0) + 앞 단계(밝기·대비)와의 조합. 이 조합도 float64 경로가 없으면 5개 채널값이
    # 어긋나 FAIL 한다 — 앞 단계가 입력 분포를 옮겨 반올림 경계에 걸리는 픽셀이 달라지기 때문.
    ("autoLevels with offsets + brightness/contrast",
     dict(rLo=3, rHi=240, gLo=7, gHi=251, bLo=1, bHi=254, brightness=5, contrast=1.05), 0),
]


def main():
    if subprocess.run(["node", "--version"], capture_output=True).returncode != 0:
        raise SystemExit("node 가 필요합니다")
    core = extract_core()
    img = synth_image()
    img_bgr = img[:, :, ::-1].copy()  # RGB->BGR for python
    print(f"index.html core: {len(core)} chars · test image {img.shape}")
    all_ok = True
    for name, params, tol in CASES:
        js_rgb = run_js(core, img, params)
        py_bgr = slide_color.apply_manual_color(img_bgr, params)
        py_rgb = py_bgr[:, :, ::-1].astype(np.int32)
        diff = np.abs(js_rgb - py_rgb)
        mx = int(diff.max())
        nbad = int((diff > tol).sum())
        ok = mx <= tol
        all_ok &= ok
        print(f"  [{'OK ' if ok else 'FAIL'}] {name:38s} maxdiff={mx:3d} (tol={tol}) "
              f"over-tol px={nbad}")
    print("\nRESULT:", "ALL PASS ✅" if all_ok else "PARITY FAILURE ❌")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
