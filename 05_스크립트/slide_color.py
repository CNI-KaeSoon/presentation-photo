#!/usr/bin/env python3
"""슬라이드툴 색보정 코어 — 브라우저 미리보기와 픽셀 단위로 동일한 파이썬 구현.

이 파일의 `apply_manual_color()` 는 `02_작업장/slide_tool/index.html` 의
`// ===COLOR_CORE_START===` ~ `// ===COLOR_CORE_END===` 블록과 **같은 순서·같은 수식**이다.
둘 중 하나만 고치면 미리보기와 최종 PDF 가 소리 없이 어긋난다.

  ⚠️ 색연산을 수정할 때는 반드시 index.html 과 이 파일을 **함께** 고치고
     `python3 05_스크립트/test_color_parity.py` 로 픽셀 대조를 통과시켜라.

검증된 구현에서 발췌한 색연산 코어다.
"""
from __future__ import annotations

import sys

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# PhotoScape식 수동 색보정 — 02_작업장/slide_tool/index.html 의 colorPipeline 과 동일 순서·수식.
# (JS↔Python 동일 결과: 채널 순서 RGB, 각 단계 float clamp, 마지막에 round(half-up).
#  파리티 테스트: 05_스크립트/test_color_parity.py 가 index.html 의 색연산 블록을
#  node 로 추출·실행해 이 함수와 픽셀 대조한다.)
# ---------------------------------------------------------------------------
DEFAULT_COLOR = dict(brightness=0, contrast=1, saturation=1, temp=0, tint=0,
                     gamma=1, whitePct=100, blackPct=0, sharpen=0,
                     y_nlm=0, darkUnify=0,
                     # 자동 레벨(6b, 채널별 독립 스트레치) — index.html COLOR_DEFS 의 hidden 6개와 1:1.
                     # 기본값 0/255 는 항등이라 구 11키 백업 JSON 도 무수정 호환.
                     rLo=0, rHi=255, gLo=0, gHi=255, bLo=0, bHi=255)


def _pct_from_hist(hist, N, pct):
    """JS percentileFromHist 와 동일: nearest-rank(ceil) 정수 퍼센타일(0..255)."""
    if N <= 0:
        return 0
    rank = int(np.ceil(pct / 100.0 * N))
    rank = max(1, min(rank, N))
    cum = 0
    for v in range(256):
        cum += int(hist[v])
        if cum >= rank:
            return v
    return 255


def _sep_blur(ch):
    """분리형 가우시안 근사 [1,4,6,4,1]/16, 경계 복제(replicate). JS sepBlur1D 와 동일."""
    k = np.array([1, 4, 6, 4, 1], np.float32) / 16.0
    p = np.pad(ch, ((0, 0), (2, 2)), mode="edge")
    hb = np.zeros_like(ch)
    for t in range(5):
        hb += k[t] * p[:, t:t + ch.shape[1]]
    p2 = np.pad(hb, ((2, 2), (0, 0)), mode="edge")
    vb = np.zeros_like(ch)
    for t in range(5):
        vb += k[t] * p2[t:t + ch.shape[0], :]
    return vb


def apply_manual_color(img_bgr, params):
    """수동 색보정 적용(BGR uint8 in/out). slide_tool 미리보기와 동일 결과."""
    p = {**DEFAULT_COLOR}
    # ⚠️ 아래 필터는 미지 키를 조용히 버린다 — JS 쪽에만 파라미터가 추가되면 PDF 가 소리 없이
    # 미리보기와 어긋난다. 그 구조적 함정을 소리나게: 미지 키는 무시하되 stderr 로 경고한다.
    unknown = sorted(k for k in (params or {}) if k not in p)
    if unknown:
        print(f"    WARN apply_manual_color: 미지 색보정 파라미터 무시됨 {unknown} — "
              f"index.html COLOR_DEFS 와 불일치(미리보기≠최종 PDF 위험)", file=sys.stderr)
    for k, v in (params or {}).items():
        if k in p and v is not None:
            try:
                p[k] = float(v)
            except (TypeError, ValueError):
                pass

    def cl(a):
        return np.clip(a, 0.0, 255.0)

    R = img_bgr[:, :, 2].astype(np.float32)
    G = img_bgr[:, :, 1].astype(np.float32)
    B = img_bgr[:, :, 0].astype(np.float32)
    b, c, sat = p["brightness"], p["contrast"], p["saturation"]
    temp, tint, g = p["temp"], p["tint"], p["gamma"]
    wp, bp, sh = p["whitePct"], p["blackPct"], p["sharpen"]
    ynlm, du = p["y_nlm"], p["darkUnify"]
    rLo, rHi = p["rLo"], p["rHi"]
    gLo, gHi = p["gLo"], p["gHi"]
    bLo, bHi = p["bLo"], p["bHi"]

    # 1 밝기
    R = cl(R + b); G = cl(G + b); B = cl(B + b)
    # 2 대비
    R = cl((R - 128) * c + 128); G = cl((G - 128) * c + 128); B = cl((B - 128) * c + 128)
    # 3 색온도/색조
    R = cl(R + temp * 0.5); G = cl(G + tint * 0.5); B = cl(B - temp * 0.5)
    # 4 채도 (HSV S*, V=max 유지)
    if sat != 1:
        V = np.maximum(np.maximum(R, G), B)
        m = np.minimum(np.minimum(R, G), B)
        C = V - m
        big = C > 1e-6
        safeC = np.where(big, C, 1.0)
        scale = np.where(big, np.minimum(sat, V / safeC), 1.0)
        R = V - (V - R) * scale; G = V - (V - G) * scale; B = V - (V - B) * scale
    # 5 감마
    if g != 1:
        invg = 1.0 / g
        R = 255.0 * np.power(cl(R) / 255.0, invg)
        G = 255.0 * np.power(cl(G) / 255.0, invg)
        B = 255.0 * np.power(cl(B) / 255.0, invg)
    R = cl(R); G = cl(G); B = cl(B)
    # 6 레벨(퍼센타일 스트레치)
    if bp > 0 or wp < 100:
        gray = np.clip(np.floor(0.299 * R + 0.587 * G + 0.114 * B + 0.5), 0, 255).astype(np.int64)
        hist = np.bincount(gray.ravel(), minlength=256)
        Npx = gray.size
        lo = _pct_from_hist(hist, Npx, bp)
        hi = _pct_from_hist(hist, Npx, wp)
        if hi - lo > 1:
            scl = 255.0 / (hi - lo)
            R = cl((R - lo) * scl); G = cl((G - lo) * scl); B = cl((B - lo) * scl)
    # 6b 자동레벨(채널별 독립 스트레치) — 공통 레벨(6) 직후. 순서 고정: JS colorPipeline 과 동일.
    # hi<=lo 인 채널은 무동작(0 나눗셈 가드). 기본값 0/255 는 scale=1·offset=0 이라 정확히 항등.
    #
    # ⚠️ float64 로 올려 계산한 뒤 마지막에 한 번만 float32 로 되돌린다 — JS 와 비트 단위로 맞추기 위함.
    # numpy 는 float32배열 * 파이썬float 에서 스칼라를 float32 로 낮춰 곱하지만(NEP 50), JS 는
    # Float32Array 원소를 double 로 올려 double 스케일과 곱한 뒤 저장 시 1회만 반올림한다.
    # 스케일 255/(hi-lo) 가 float32 로 정확히 표현되지 않으면(예: 255/252, 상대오차 4.5e-8 —
    # 반면 255/240=1.0625 는 정확) 그 미세차가 최종 round(x+0.5) 경계를 넘나들며 실사 이미지에서
    # 픽셀 0.1% 가 ±1 어긋난다. 아래 float64 경로는 그 이중 반올림을 제거해 maxdiff=0 을 만든다.
    # (기존 6단계는 같은 구조지만 손대지 않는다 — 이미 보정된 결과물의 재현성을 깨지 않기 위해.)
    def _stretch(ch, lo, hi):
        return np.clip((ch.astype(np.float64) - lo) * (255.0 / (hi - lo)),
                       0.0, 255.0).astype(np.float32)

    if rHi > rLo:
        R = _stretch(R, rLo, rHi)
    if gHi > gLo:
        G = _stretch(G, gLo, gHi)
    if bHi > bLo:
        B = _stretch(B, bLo, bHi)
    # 7 선명도(언샤프)
    if sh > 0:
        R = cl(R + sh * (R - _sep_blur(R)))
        G = cl(G + sh * (G - _sep_blur(G)))
        B = cl(B + sh * (B - _sep_blur(B)))
    # 8a 무아레제거: 최종 PDF 는 실제 NLM(luma), 미리보기는 약식 근사
    if ynlm > 0:
        Yf = 0.299 * R + 0.587 * G + 0.114 * B
        Yd = cv2.fastNlMeansDenoising(cl(Yf).astype(np.uint8), None, float(ynlm), 7, 21).astype(np.float32)
        d = Yd - Yf
        R = cl(R + d); G = cl(G + d); B = cl(B + d)
    # 8b 다크영역통일(chroma→중립; JS 와 동일 수식)
    if du > 0:
        Y = 0.299 * R + 0.587 * G + 0.114 * B
        wgt = np.clip((48.0 - Y) / 48.0, 0.0, 1.0) * du
        R = R * (1 - wgt) + Y * wgt; G = G * (1 - wgt) + Y * wgt; B = B * (1 - wgt) + Y * wgt

    out = np.empty_like(img_bgr)
    out[:, :, 2] = np.clip(np.floor(R + 0.5), 0, 255).astype(np.uint8)
    out[:, :, 1] = np.clip(np.floor(G + 0.5), 0, 255).astype(np.uint8)
    out[:, :, 0] = np.clip(np.floor(B + 0.5), 0, 255).astype(np.uint8)
    return out
