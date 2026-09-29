#!/usr/bin/env python3
"""슬라이드 사각형 자동 검출기를 사람이 손본 정답과 비교해 점수를 낸다.

정답: slide_tool 백업 JSON 의 data.slideCorners_v1 (정규화 [TL,TR,BL,BR]).
오차: 모서리 픽셀 거리 / 이미지 대각선 길이 (가로세로비 보정).
기본값 사각형과 같은(손대지 않은) 정답은 제외한다.

Usage:
  python 05_스크립트/eval_auto_detect.py --backup <백업.json> \
      --workspace <02_작업장> [--csv out.csv] [--jobs N] \
      [--detector auto_detect_screen:detect_screen]
"""
import argparse
import csv
import importlib
import json
import math
import os
import statistics
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DEFAULT = [[0.05, 0.08], [0.95, 0.08], [0.05, 0.92], [0.95, 0.92]]
IMG_EXT = (".jpg", ".jpeg", ".png")


def load_detector(spec):
    mod, _, fn = spec.partition(":")
    return getattr(importlib.import_module(mod), fn or "detect_screen")


def is_default(q):
    return all(abs(a - b) < 1e-6 for p, d in zip(q, DEFAULT) for a, b in zip(p, d))


def load_truth(backup):
    with open(backup, encoding="utf-8") as f:
        data = json.load(f)
    raw = data["data"]["slideCorners_v1"]
    return json.loads(raw) if isinstance(raw, str) else raw


def index_workspace(ws):
    """basename -> [경로...] (workspace 전체)."""
    idx = defaultdict(list)
    for root, _, files in os.walk(ws):
        for n in files:
            if n.lower().endswith(IMG_EXT):
                idx[n].append(os.path.join(root, n))
    return idx


def resolve(key, ws, idx):
    """('exact'|'basename'|None, path)."""
    parts = key.split("/")
    if parts[:1] == [".."]:
        parts = parts[1:]
    p = os.path.join(ws, *parts)
    if os.path.isfile(p):
        return "exact", p
    c = idx.get(parts[-1], [])
    if len(c) == 1:
        return "basename", c[0]
    return None, None


def match_truth(backup, workspace):
    """백업의 정답을 작업장 사진과 짝짓는다(평가·학습이 같은 규칙을 쓴다).

    반환: ([(key, 사진 경로, 정답 4점)...], 정답 전체 수, 기본값이라 뺀 수, 파일 없음 수, 파일명 대체 매칭 수).
    """
    truth = load_truth(backup)
    idx = index_workspace(workspace)
    matched, skipped_default, missing, by_base = [], 0, 0, 0
    for key, q in truth.items():
        if not (isinstance(q, list) and len(q) == 4):
            continue
        if is_default(q):
            skipped_default += 1
            continue
        how, path = resolve(key, workspace, idx)
        if not path:
            missing += 1
            continue
        by_base += how == "basename"
        matched.append((key, path, q))
    return matched, len(truth), skipped_default, missing, by_base


def corner_errors(pred, truth, w, h):
    diag = math.hypot(w, h)
    return [math.hypot((p[0] - t[0]) * w, (p[1] - t[1]) * h) / diag
            for p, t in zip(pred, truth)]


def eval_one(job):
    key, path, truth, spec = job
    import cv2
    row = {"key": key, "folder": key.split("/")[1] if key.count("/") >= 2 else "",
           "path": path, "w": 0, "h": 0, "conf": "", "max_err": None, "mean_err": None,
           "truth_area": "", "truth_aspect": "", "status": "ok"}
    img = cv2.imread(path)
    if img is None:
        row["status"] = "unreadable"
        return row
    h, w = img.shape[:2]
    row["w"], row["h"] = w, h
    tl, tr, bl, br = truth
    area = 0.5 * abs(sum(a[0] * b[1] - b[0] * a[1]
                         for a, b in ((tl, tr), (tr, br), (br, bl), (bl, tl))))
    row["truth_area"] = round(area, 4)
    top = math.hypot((tr[0] - tl[0]) * w, (tr[1] - tl[1]) * h)
    side = math.hypot((bl[0] - tl[0]) * w, (bl[1] - tl[1]) * h)
    row["truth_aspect"] = round(top / side, 3) if side else ""
    try:
        quad, conf = load_detector(spec)(img)
    except Exception as e:  # 검출기 예외도 실패로 센다
        row["status"] = f"error:{type(e).__name__}"
        return row
    row["conf"] = round(float(conf), 4)
    if quad is None:
        row["status"] = "fail"
        return row
    errs = corner_errors([[float(a), float(b)] for a, b in quad], truth, w, h)
    row["max_err"], row["mean_err"] = max(errs), sum(errs) / 4
    return row


def fmt(x):
    return "n/a" if x is None else f"{x:.4f}"


def summarize(rows):
    n = len(rows)
    ok = [r for r in rows if r["max_err"] is not None]
    fail = n - len(ok)
    mx = [r["max_err"] for r in ok]
    mn = [r["mean_err"] for r in ok]
    # 실패는 hit 아님(분모는 전체 n)
    return {
        "n": n, "fail": fail / n if n else 0.0,
        "hit2": sum(v < 0.02 for v in mx) / n if n else 0.0,
        "hit5": sum(v < 0.05 for v in mx) / n if n else 0.0,
        "mean_max": statistics.mean(mx) if mx else None,
        "mean_mean": statistics.mean(mn) if mn else None,
        "median_max": statistics.median(mx) if mx else None,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backup", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--detector", default="auto_detect_screen:detect_screen")
    ap.add_argument("--jobs", type=int, default=None)
    ap.add_argument("--csv")
    args = ap.parse_args()

    matched, n_truth, skipped_default, missing, by_base = match_truth(args.backup, args.workspace)
    jobs = [(key, path, q, args.detector) for key, path, q in matched]

    print(f"정답 {n_truth}건 | 기본값(손대지 않음) 제외 {skipped_default} | "
          f"파일 없음 {missing} | 파일명 대체 매칭 {by_base} | 평가 {len(jobs)}")
    if not jobs:
        print("EVAL: n=0 hit@2%=n/a hit@5%=n/a fail=n/a median_max_err=n/a")
        return 1

    from parallel_map import ordered_parallel_map
    rows = list(ordered_parallel_map(eval_one, jobs, workers=args.jobs))

    unread = [r for r in rows if r["status"] == "unreadable"]
    if unread:
        print(f"읽기 실패 {len(unread)}건(검출 실패로 계산)")
    s = summarize(rows)
    print(f"전체 평균 max_err={fmt(s['mean_max'])} mean_err={fmt(s['mean_mean'])} "
          f"중앙 max_err={fmt(s['median_max'])}")
    print("폴더별 요약")
    print(f"  {'폴더':<40} {'n':>4} {'hit@2%':>7} {'hit@5%':>7} {'fail':>6} {'med_max':>8}")
    byf = defaultdict(list)
    for r in rows:
        byf[r["folder"]].append(r)
    for f in sorted(byf):
        t = summarize(byf[f])
        print(f"  {f:<40} {t['n']:>4} {t['hit2']:>7.3f} {t['hit5']:>7.3f} "
              f"{t['fail']:>6.3f} {fmt(t['median_max']):>8}")

    if args.csv:
        cols = ["key", "folder", "w", "h", "status", "conf", "max_err", "mean_err",
                "truth_area", "truth_aspect", "path"]
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            wr.writeheader()
            wr.writerows(rows)
        print(f"CSV: {args.csv}")

    print(f"EVAL: n={s['n']} hit@2%={s['hit2']:.3f} hit@5%={s['hit5']:.3f} "
          f"fail={s['fail']:.3f} median_max_err={fmt(s['median_max'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
