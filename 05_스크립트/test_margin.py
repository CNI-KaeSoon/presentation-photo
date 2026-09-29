#!/usr/bin/env python3
"""경계 여백(안쪽·바깥쪽 이동) 계산 회귀 테스트 — index.html 의 순수 함수를 node 로 그대로 실행한다.

여백은 자동 찾기로 얻은 사각형을 **보정된 슬라이드 좌표계**에서 가로·세로 pct% 만큼 넓히거나 좁힌 결과다.
사각형 4점의 호모그래피 H(단위 정사각형 → 사진)로 단위 정사각형을 (-m,-m)~(1+m,1+m) 로 바꿔 되사영하므로,
원근이 있어도 네 변이 균일하게 움직인다. 이 테스트는 그 수식을 OpenCV(getPerspectiveTransform)로 독립 검증한다.

- M1 pct=0 이면 경계가 한 비트도 변하지 않는다(5자리 반올림 범위 안).
- M2 정면 직사각형: 여백 +5% 가 슬라이드 폭·높이의 5% 만큼 정확히 바깥으로 나간다(닫힌 식과 일치).
- M3 원근 사다리꼴: 결과 4점을 OpenCV 역변환(사진 → 단위 정사각형)하면 정확히 (-m,-m)/(1+m,-m)/(-m,1+m)/(1+m,1+m).
- M4 중심 확대·축소와 다르다: 원근이 큰 사각형에서 단순 중심 스케일과 위치가 확연히 어긋나야 한다(같으면 원근 무시).
- M5 회전(90° 순환) 교환: 순환한 배열에 여백을 넣은 것 == 여백을 넣고 순환한 것. 순환 순서가 유지된다.
- M6 자동 찾기 기록 추적: 기록 {raw,pct} 결과 그대로면 회전 횟수(0~3), 한 점이라도 손대면 -1(수동). 여백을 여러 번 바꿔도
       원시 경계에서 다시 계산하므로 누적되지 않는다.
- M7 클램프: 사진 가장자리를 넘는 여백은 [0,1] 로 잘리고 잘린 모서리 수를 알려 준다. 안 넘으면 0.
- M8 비정상 사각형(납작·오목·꼬임): ok=false 로 입력 그대로 돌려준다(여백 0% 는 ok=true).
- M9 단조성: 여백이 클수록 사각형 넓이가 커진다. 값 정리(clampMarginPct)는 0.5% 단계·±3% 범위.

Usage: python3 05_스크립트/test_margin.py    (node 필요, 없으면 건너뜀)
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import tempfile
from shutil import which

import cv2
import numpy as np

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(os.path.dirname(HERE), "02_작업장", "slide_tool", "index.html")
CORE_RE = re.compile(r"// ===MARGIN_CORE_START===.*?// ===MARGIN_CORE_END===", re.S)
ROT_RE = re.compile(r"function rotateCorners\(corners,dir\)\{.*?\n\}", re.S)

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"[{'OK ' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def unit_corners(m: float):
    return [[-m, -m], [1 + m, -m], [-m, 1 + m], [1 + m, 1 + m]]


def random_persp_quads(n: int, seed: int):
    """정면 직사각형을 원근·회전으로 비틀어 만든 볼록 사각형([TL,TR,BL,BR], 사진 안쪽 여유 있음)."""
    rng = random.Random(seed)
    out = []
    while len(out) < n:
        cx, cy = rng.uniform(0.4, 0.6), rng.uniform(0.4, 0.6)
        w, h = rng.uniform(0.30, 0.5), rng.uniform(0.2, 0.35)
        base = [[cx - w / 2, cy - h / 2], [cx + w / 2, cy - h / 2], [cx - w / 2, cy + h / 2], [cx + w / 2, cy + h / 2]]
        quad = [[x + rng.uniform(-0.05, 0.05), y + rng.uniform(-0.05, 0.05)] for x, y in base]
        quad = [[round(min(0.98, max(0.02, x)), 5), round(min(0.98, max(0.02, y)), 5)] for x, y in quad]
        pts = np.array([quad[0], quad[1], quad[3], quad[2]], dtype=np.float64)
        area = 0.5 * abs(sum(pts[i][0] * pts[(i + 1) % 4][1] - pts[(i + 1) % 4][0] * pts[i][1] for i in range(4)))
        if area > 0.06:
            out.append(quad)
    return out


def run_node(payload: dict) -> dict | None:
    if which("node") is None:
        print("[SKIP] node 없음 — 여백 테스트를 건너뜁니다.")
        return None
    source = open(INDEX, encoding="utf-8").read()
    core, rot = CORE_RE.search(source), ROT_RE.search(source)
    if not core or not rot:
        check(False, "여백 코어 추출", "MARGIN_CORE 블록 또는 rotateCorners 를 찾지 못했습니다")
        return None
    # rotateCorners 를 먼저(코어가 부른다). 코어는 함수 선언과 const 뿐이라 그대로 실행된다.
    script = (rot.group(0) + "\n" + core.group(0) + "\n"
              + "const P=JSON.parse(process.argv[2]);\n"
              + "const R={};\n"
              + "R.margin=P.cases.map(c=>marginCorners(c.q,c.pct));\n"
              + "R.rotIn=P.rot.map(c=>{let q=c.q;for(let i=0;i<c.k;i++)q=rotateCorners(q,1);return marginCorners(q,c.pct);});\n"
              + "R.rotOut=P.rot.map(c=>{const r=marginCorners(c.q,c.pct);let q=r.corners;for(let i=0;i<c.k;i++)q=rotateCorners(q,1);return q;});\n"
              + "R.track=P.track.map(c=>{\n"
              + "  const rec={raw:c.raw,pct:c.pct};\n"
              + "  const cur=marginFromRecord(rec,c.pct,c.k).corners;\n"
              + "  const hand=cur.map(p=>[p[0],p[1]]);hand[c.moveIdx][0]=+(hand[c.moveIdx][0]+0.01).toFixed(5);\n"
              + "  const seq=c.seq.map(v=>marginFromRecord(rec,v,c.k).corners);\n"
              + "  return {k:autoRecordRotation(cur,rec),kHand:autoRecordRotation(hand,rec),kNone:autoRecordRotation(cur,null),\n"
              + "    seqLast:seq[seq.length-1],direct:marginFromRecord(rec,c.seq[c.seq.length-1],c.k).corners};\n"
              + "});\n"
              + "R.clampPct=P.clampIn.map(v=>clampMarginPct(v));\n"
              + "process.stdout.write(JSON.stringify(R));\n")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        path = handle.name
    try:
        run = subprocess.run([which("node"), path, json.dumps(payload)],
                             capture_output=True, text=True, encoding="utf-8", check=False)
    finally:
        os.unlink(path)
    if run.returncode != 0:
        check(False, "node 실행", (run.stderr or run.stdout).strip()[:400])
        return None
    return json.loads(run.stdout)


def area(quad) -> float:
    pts = [quad[0], quad[1], quad[3], quad[2]]
    return 0.5 * abs(sum(pts[i][0] * pts[(i + 1) % 4][1] - pts[(i + 1) % 4][0] * pts[i][1] for i in range(4)))


def main() -> int:
    quads = random_persp_quads(60, seed=5)
    pcts = [-3.0, -1.5, 1.0, 3.0]

    # 페이로드 준비 --------------------------------------------------------------
    cases = []
    for q in quads:
        cases.append({"q": q, "pct": 0})
        for p in pcts:
            cases.append({"q": q, "pct": p})
    rect = [[0.2, 0.2], [0.8, 0.2], [0.2, 0.6], [0.8, 0.6]]
    cases.append({"q": rect, "pct": 5})             # M2 (범위 밖 5% 도 코어 자체는 계산한다)
    strong = [[0.30, 0.20], [0.70, 0.20], [0.10, 0.80], [0.90, 0.80]]   # 강한 사다리꼴(원근 큼) — M4
    cases.append({"q": strong, "pct": 3})
    edge = [[0.01, 0.01], [0.99, 0.02], [0.02, 0.99], [0.98, 0.98]]     # 가장자리에 붙은 사각형 — M7
    cases.append({"q": edge, "pct": 3})
    bad = {
        "flat": [[0.1, 0.1], [0.5, 0.1], [0.3, 0.1], [0.7, 0.1]],
        "bowtie": [[0.1, 0.1], [0.9, 0.9], [0.1, 0.9], [0.9, 0.1]],   # TR·BR 가 꼬인 나비매듭
        "concave": [[0.1, 0.1], [0.9, 0.1], [0.1, 0.9], [0.4, 0.4]],
    }
    for q in bad.values():
        cases.append({"q": q, "pct": 2})
        cases.append({"q": q, "pct": 0})
    rot_cases = [{"q": q, "pct": p, "k": k} for q in quads[:20] for p in (-2.5, 2.0) for k in (1, 2, 3)]
    rng = random.Random(9)
    track_cases = [{"raw": q, "pct": p, "k": k, "moveIdx": rng.randrange(4), "seq": [-3, 1.5, 0.5, -1.0, 2.5, p]}
                   for q in quads[:20] for p in (-1.0, 0.0, 2.0) for k in (0, 1, 2, 3)]
    clamp_in = [-9, -3.4, -3, -2.74, -0.26, 0, 0.24, 0.26, 1.2, 2.9, 3, 9, "1.5", None, "x", float("nan")]
    payload = {"cases": cases, "rot": rot_cases, "track": track_cases,
               "clampIn": [None if isinstance(v, float) and v != v else v for v in clamp_in]}

    res = run_node(payload)
    if res is None:
        return 1 if failures else 0
    out = res["margin"]
    idx = 0

    # M1 / M3 (임의 원근 사각형) ---------------------------------------------------
    m1_bad = m3_worst = 0.0
    m3_fail = 0
    for q in quads:
        r0 = out[idx]; idx += 1
        if not (r0["ok"] and r0["clamped"] == 0 and r0["corners"] == q):
            m1_bad += 1
        src = np.float32([q[0], q[1], q[2], q[3]])
        dst = np.float32([[0, 0], [1, 0], [0, 1], [1, 1]])
        h_inv = cv2.getPerspectiveTransform(src, dst)   # 사진 → 슬라이드(단위 정사각형) 좌표
        for p in pcts:
            r = out[idx]; idx += 1
            m = p / 100
            if not r["ok"]:
                m3_fail += 1
                continue
            got = cv2.perspectiveTransform(np.float32([r["corners"]]), h_inv)[0]
            want = np.array(unit_corners(m))
            err = float(np.abs(got - want).max())
            m3_worst = max(m3_worst, err)
            if err > 2e-3 or r["clamped"]:
                m3_fail += 1
    check(m1_bad == 0, "M1 pct=0 경계 불변", f"임의 원근 사각형 {len(quads)}개, 불일치 {int(m1_bad)}건")
    check(m3_fail == 0, "M3 원근 사다리꼴 — OpenCV 역투영이 (-m,-m)~(1+m,1+m)",
          f"{len(quads)}개 × 여백 {pcts}, 최대 오차 {m3_worst:.5f} (5자리 반올림 한도 2e-3)")

    # M2 정면 직사각형 -------------------------------------------------------------
    r = out[idx]; idx += 1
    w, h, m = 0.6, 0.4, 0.05
    want = [[0.2 - m * w, 0.2 - m * h], [0.8 + m * w, 0.2 - m * h], [0.2 - m * w, 0.6 + m * h], [0.8 + m * w, 0.6 + m * h]]
    err = max(abs(a - b) for pa, pb in zip(r["corners"], want) for a, b in zip(pa, pb))
    check(r["ok"] and err < 1e-5, "M2 정면 직사각형 +5% — 폭·높이의 5% 만큼 바깥", f"최대 오차 {err:.2e}")

    # M4 원근 — 중심 스케일과 다름 ---------------------------------------------------
    r = out[idx]; idx += 1
    cx = sum(p[0] for p in strong) / 4
    cy = sum(p[1] for p in strong) / 4
    grow = 1.06  # 폭 기준 +3%*2 = 6% 넓힘에 해당하는 단순 중심 확대
    naive = [[cx + (p[0] - cx) * grow, cy + (p[1] - cy) * grow] for p in strong]
    dev = max(abs(a - b) for pa, pb in zip(r["corners"], naive) for a, b in zip(pa, pb))
    check(r["ok"] and dev > 0.003, "M4 단순 중심 확대와 다름(원근 반영)", f"최대 위치 차 {dev:.4f}")

    # M7 클램프 -------------------------------------------------------------------
    r = out[idx]; idx += 1
    inside = all(0 <= v <= 1 for p in r["corners"] for v in p)
    check(r["ok"] and r["clamped"] >= 1 and inside, "M7 가장자리 넘는 여백은 [0,1] 로 잘리고 개수를 알려 줌",
          f"잘린 모서리 {r['clamped']}개")

    # M8 비정상 사각형 -------------------------------------------------------------
    for name, q in bad.items():
        r2, r0 = out[idx], out[idx + 1]
        idx += 2
        same = r2["corners"] == [[round(a, 5), round(b, 5)] for a, b in q]
        check((not r2["ok"]) and same and r0["ok"], f"M8 {name} — 여백 못 넣고 입력 그대로, 0% 는 ok")

    # M9 단조성 --------------------------------------------------------------------
    mono_bad = 0
    for q in quads:
        rs = [out[quads.index(q) * 5 + 1 + i]["corners"] for i in range(4)]   # -3, -1.5, +1, +3
        a = [area(x) for x in rs]
        a0 = area(q)
        if not (a[0] < a[1] < a0 < a[2] < a[3]):
            mono_bad += 1
    check(mono_bad == 0, "M9 여백이 클수록 넓이가 커짐", f"위반 {mono_bad}건")
    clamp_expect = [-3, -3, -3, -2.5, -0.5, 0, 0, 0.5, 1, 3, 3, 3, 1.5, 0, 0, 0]
    check(res["clampPct"] == clamp_expect, "M9 clampMarginPct — 0.5% 단계 · ±3% · 잘못된 값은 0",
          f"{res['clampPct']}")

    # M5 회전 교환 -----------------------------------------------------------------
    bad5 = 0
    for a, b in zip(res["rotIn"], res["rotOut"]):
        if not (a["ok"] and a["corners"] == b):
            bad5 += 1
    check(bad5 == 0, "M5 회전한 배열의 여백 == 여백 뒤 회전(순환 순서 유지)", f"{len(rot_cases)}건, 불일치 {bad5}")

    # M6 자동 찾기 기록 추적 ---------------------------------------------------------
    bad6 = 0
    for c, t in zip(track_cases, res["track"]):
        if t["k"] != c["k"] or t["kHand"] != -1 or t["kNone"] != -1 or t["seqLast"] != t["direct"]:
            bad6 += 1
    check(bad6 == 0, "M6 기록 그대로면 회전 횟수 · 손대면 -1 · 여백 여러 번 바꿔도 누적 없음", f"{len(track_cases)}건, 불일치 {bad6}")

    print(f"\nMARGIN: {'PASS' if not failures else 'FAIL'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
