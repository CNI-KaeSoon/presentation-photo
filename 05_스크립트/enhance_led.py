#!/usr/bin/env python3
"""Edge-preserving cleanup for photos of LED screens: removes pixel-grid/moire
noise and evens out the dirty/uneven white background WITHOUT blurring text or
graphic edges.

Pipeline:
  1) edge-preserving denoise (bilateral) — kills LED pixel/moire noise, keeps edges
  2) illumination flattening (divide by large-blur background) — evens uneven brightness
  3) white-point / gentle levels — background toward clean white
  4) mild unsharp mask — restores edge crispness lost in step 1

Tunable via args. `enhance(img, **params)` is importable by `05_스크립트/export_pdf.py`.
"""
import argparse
import cv2
import numpy as np
import sys

# Windows 콘솔 기본 인코딩(cp949)에는 '—'·'·'·'⚠' 같은 문자가 없어, 그대로 print 하면
# UnicodeEncodeError 로 스크립트가 죽는다(실측: init_worktree 가 U+2014 에서 중단).
# 출력 스트림을 UTF-8 로 고정해 어떤 콘솔에서도 깨지거나 죽지 않게 한다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 파이프·구버전 등 재설정 불가 시 무시
        pass


def enhance(img, y_nlm=11, chroma_med=5, chroma_nlm=11,
            flatten=1, bg_blur=121, flatten_amt=0.5,
            white=1, white_pct=82, black_pct=2.0, unsharp_amt=0.25,
            adaptive=True):
    """LED-screen cleanup. If adaptive and the slide is NOT a bright/white-
    background one (median luminance low), the aggressive white-clip and flatten
    are softened so dark/colored slides are not blown out."""
    if adaptive:
        med = float(np.median(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)))
        if med < 140:                       # dark / colored background slide
            white_pct = max(white_pct, 97.0)
            flatten_amt = min(flatten_amt, 0.25)
    """LED-screen cleanup in YCrCb: remove pixel-grid on luminance (edge-
    preserving NLM), kill color fringing on chroma, gently flatten + whiten the
    background, keep text/graphic edges crisp."""
    ycc = cv2.cvtColor(img, cv2.COLOR_BGR2YCrCb)
    Y, Cr, Cb = cv2.split(ycc)

    # 1) luminance: NLM denoise removes the LED grid/moire while keeping edges
    if y_nlm:
        Y = cv2.fastNlMeansDenoising(Y, None, float(y_nlm), 7, 21)

    # 2) chroma: suppress color fringing/rainbow (median + NLM on Cr,Cb only —
    #    does not soften perceived edges, which live in luminance)
    if chroma_med:
        Cr = cv2.medianBlur(Cr, chroma_med | 1)
        Cb = cv2.medianBlur(Cb, chroma_med | 1)
    if chroma_nlm:
        Cr = cv2.fastNlMeansDenoising(Cr, None, float(chroma_nlm), 7, 21)
        Cb = cv2.fastNlMeansDenoising(Cb, None, float(chroma_nlm), 7, 21)

    Yf = Y.astype(np.float32)
    # 3) gentle illumination flattening on luminance only (blend to avoid halos)
    if flatten:
        k = bg_blur | 1
        bg = cv2.GaussianBlur(Yf, (k, k), 0)
        bg = np.clip(bg, 1, None)
        target = np.percentile(bg, 92)
        flat = np.clip(Yf / bg * target, 0, 255)
        Yf = (1 - flatten_amt) * Yf + flatten_amt * flat   # partial to limit halos

    # 4) white-point / black-point on luminance (gentle; high pct keeps text dark)
    if white:
        hi = np.percentile(Yf, white_pct)
        lo = np.percentile(Yf, black_pct)
        if hi - lo > 1:
            Yf = (Yf - lo) * (255.0 / (hi - lo))
        Yf = np.clip(Yf, 0, 255)

    # 5) very mild unsharp on luminance to keep edges crisp (small -> no halo)
    if unsharp_amt:
        blur = cv2.GaussianBlur(Yf, (0, 0), 1.0)
        Yf = np.clip(cv2.addWeighted(Yf, 1 + unsharp_amt, blur, -unsharp_amt, 0), 0, 255)

    merged = cv2.merge([Yf.astype(np.uint8), Cr, Cb])
    return cv2.cvtColor(merged, cv2.COLOR_YCrCb2BGR)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    # expose a few knobs
    ap.add_argument("--bilat-color", type=int, default=45)
    ap.add_argument("--bg-blur", type=int, default=61)
    ap.add_argument("--white-pct", type=float, default=98.5)
    ap.add_argument("--unsharp-amt", type=float, default=0.6)
    ap.add_argument("--no-flatten", action="store_true")
    a = ap.parse_args()
    img = cv2.imread(a.inp)
    if img is None:
        raise SystemExit(f"cannot read {a.inp}")
    out = enhance(img, bilat_color=a.bilat_color, bg_blur=a.bg_blur,
                  flatten=0 if a.no_flatten else 1, white_pct=a.white_pct,
                  unsharp_amt=a.unsharp_amt)
    cv2.imwrite(a.out, out)
    print("wrote", a.out)


if __name__ == "__main__":
    main()
