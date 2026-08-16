#!/usr/bin/env python3
"""Auto-detect the projection-screen quadrilateral in each slide photo and emit
a slide_tool backup JSON that pre-fills the 4-corner perspective handles.

Corner format matches the tool: normalized [x,y] in [0,1], order TL,TR,BL,BR.
Backup JSON: {_type:'slide_tool_backup',_version:1,data:{slideCorners_v1:"<json string>"}}
Image keys match data.js: ../<folder>/img/<name>

만들어진 JSON 은 브라우저 도구의 `백업 불러오기` 로 넣는다. 검출은 완벽하지 않으므로
**사람이 훑으며 손보는 것을 전제**로 쓴다(어두운 슬라이드·복잡한 배경에서 자주 빗나감).

Usage: python3 05_스크립트/auto_detect_screen.py \
    --root 02_작업장 --out 02_작업장/auto_corners.json
"""
import argparse, glob, json, os, re
import cv2
import numpy as np


def natural_key(s):
    """IMG_9 < IMG_10 순으로 정렬(사전순이면 IMG_10 이 앞선다)."""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", os.path.basename(s))]


def order_quad(pts):
    """Return [TL, TR, BL, BR] from 4 points."""
    pts = np.array(pts, dtype=np.float32)
    s = pts.sum(axis=1)
    d = (pts[:, 0] - pts[:, 1])
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmax(d)]
    bl = pts[np.argmin(d)]
    return [tl, tr, bl, br]


def detect_screen(img):
    """Detect the largest rectangular region (projection screen), tolerant of
    dark slides. Returns normalized [TL,TR,BL,BR] + confidence, or (None,0)."""
    h, w = img.shape[:2]
    scale = 1000.0 / max(h, w)
    small = cv2.resize(img, (int(w * scale), int(h * scale)))
    sh, sw = small.shape[:2]
    area_full = sh * sw
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)

    candidates = []
    # multi-strategy edge maps: Canny (several thresholds) + adaptive threshold
    edge_maps = [
        cv2.Canny(gray, 30, 100),
        cv2.Canny(gray, 50, 150),
        cv2.Canny(gray, 15, 60),
    ]
    for em in edge_maps:
        em = cv2.dilate(em, np.ones((3, 3), np.uint8), iterations=2)
        cnts, _ = cv2.findContours(em, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            a = cv2.contourArea(c)
            if a < area_full * 0.06:
                continue
            peri = cv2.arcLength(c, True)
            for eps in (0.02, 0.03, 0.05):
                approx = cv2.approxPolyDP(c, eps * peri, True)
                if len(approx) == 4 and cv2.isContourConvex(approx):
                    pts = approx.reshape(4, 2).astype(np.float32)
                    quad = order_quad(pts)
                    wq = (np.linalg.norm(quad[1] - quad[0]) + np.linalg.norm(quad[3] - quad[2])) / 2
                    hq = (np.linalg.norm(quad[2] - quad[0]) + np.linalg.norm(quad[3] - quad[1])) / 2
                    if hq < 1 or wq < 1:
                        continue
                    ar = wq / hq
                    if not (1.05 < ar < 2.6):   # screen aspect ~4:3..16:9
                        continue
                    candidates.append((a, quad))
                    break
    if not candidates:
        return None, 0.0
    a, quad = max(candidates, key=lambda x: x[0])
    norm = [[float(p[0] / sw), float(p[1] / sh)] for p in quad]
    return norm, a / area_full


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    DEFAULT = [[0.05, 0.08], [0.95, 0.08], [0.05, 0.92], [0.95, 0.92]]
    corners_store = {}
    stats = {"detected": 0, "folder_median": 0, "default": 0, "total": 0}
    per_folder_detects = {}   # folder -> list of quads (normalized, flattened)
    per_photo = {}            # key -> (folder, own_quad_or_None)

    for folder in sorted(os.listdir(args.root)):
        img_dir = os.path.join(args.root, folder, "img")
        if not os.path.isdir(img_dir):
            continue
        per_folder_detects.setdefault(folder, [])
        # gen_manifest.py 와 같은 확장자 집합을 스캔해야 한다. png 만 훑으면
        # prepare_photos.py 가 만든 jpg 축소본을 통째로 놓쳐 빈 결과를 조용히 낸다.
        files = sorted(
            (p for ext in ("*.jpg", "*.jpeg", "*.png")
             for p in glob.glob(os.path.join(img_dir, ext))),
            key=natural_key)
        for png in files:
            stats["total"] += 1
            key = f"../{folder}/img/{os.path.basename(png)}"
            img = cv2.imread(png)
            quad = None
            if img is not None:
                quad, conf = detect_screen(img)
            per_photo[key] = (folder, quad)
            if quad is not None:
                per_folder_detects[folder].append(np.array(quad, dtype=np.float32))

    # per-folder median quad (camera is fixed per speaker)
    folder_median = {}
    for folder, quads in per_folder_detects.items():
        if quads:
            arr = np.stack(quads)               # (n,4,2)
            folder_median[folder] = np.median(arr, axis=0)

    # assign: own detection if present, else folder-median, else default
    for key, (folder, quad) in per_photo.items():
        if quad is not None:
            corners_store[key] = [[round(float(x), 5), round(float(y), 5)] for x, y in quad]
            stats["detected"] += 1
        elif folder in folder_median:
            m = folder_median[folder]
            corners_store[key] = [[round(float(x), 5), round(float(y), 5)] for x, y in m]
            stats["folder_median"] += 1
        else:
            corners_store[key] = DEFAULT
            stats["default"] += 1
    report = [(k, "detected" if v is not None else "filled", 0.0) for k, (f, v) in per_photo.items()]

    backup = {
        "_type": "slide_tool_backup", "_version": 1,
        "_savedAt": "auto-detected",
        "data": {"slideCorners_v1": json.dumps(corners_store, ensure_ascii=False)},
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(backup, f, ensure_ascii=False, indent=2)

    print(f"총 {stats['total']}장 | 직접검출 {stats['detected']} | 폴더대표 채움 {stats['folder_median']} | "
          f"기본값 {stats['default']}")
    print(f"백업 JSON: {args.out}")
    from collections import Counter
    byf, okf = Counter(), Counter()
    for key, kind, conf in report:
        fld = key.split("/")[1]
        byf[fld] += 1
        if kind == "detected":
            okf[fld] += 1
    for fld in sorted(byf):
        note = "" if fld in folder_median else "  ⚠ 폴더 검출 0 → 기본값(수동)"
        print(f"  {fld}: 직접검출 {okf[fld]}/{byf[fld]}{note}")


if __name__ == "__main__":
    main()
