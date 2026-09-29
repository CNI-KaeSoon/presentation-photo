#!/usr/bin/env python3
"""Auto-detect the projection-screen quadrilateral in each slide photo and emit
a slide_tool backup JSON that pre-fills the 4-corner perspective handles.

Corner format matches the tool: normalized [x,y] in [0,1], order TL,TR,BL,BR.
Backup JSON: {_type:'slide_tool_backup',_version:1,data:{slideCorners_v1:"<json string>"}}
Image keys match data.js: ../<folder>/img/<name>

만들어진 JSON 은 브라우저 도구의 `백업 불러오기` 로 넣는다. 검출은 완벽하지 않으므로
**사람이 훑으며 손보는 것을 전제**로 쓴다. detect_screen 이 주는 conf(0~1)가 낮은 사진은
특히 확인이 필요하다(어두운 슬라이드·보조 화면이 붙은 무대에서 빗나가기 쉽다).

Usage: python3 05_스크립트/auto_detect_screen.py \
    --root 02_작업장 --out 02_작업장/auto_corners.json
"""
import argparse, glob, json, math, os, re
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


# ---------------------------------------------------------------------------
# 검출기: "직선 4개 조합 + 학습된 점수" 방식
#  1) 작업 해상도(긴 변 640px)에서 위·아래(±15°)·왼·오른(±22°) 변 후보 직선을 밝기 기울기 지지도로
#     각 방향 최대 24개씩 뽑는다(밝은 화면이 어두운 배경과 만나는 방향을 우선, 반대 방향도 일부 포함).
#     기울기는 화소마다 B·G·R 중 가장 센 채널 값을 써서 어두운 파란 슬라이드 같은 경계도 살린다.
#  2) 위·아래·왼·오른 직선의 모든 조합을 벡터화해 한 번에 점수 매긴다(기하가 안 되는 조합은 먼저 버림).
#     특징은 변별 기울기 지지도(방향별), 변 안팎 밝기 대비, 면적·종횡비·기울기·평행도 등이고, 점수는
#     사람이 손본 정답으로 학습한 작은 신경망(_MODEL, 은닉 12)이다. 화면 안의 큰 그림·표(안쪽 사각형)보다
#     화면 테두리를 고르는 것이 이 점수의 역할이다.
#  3) 1등 사각형의 네 변을 기울기 봉우리에 맞춰 서브픽셀로 다시 맞춘다(해상도 3종의 중앙값).
#  4) conf 는 1등 점수를 "정답과 5% 이내로 맞을 확률"로 환산한 값(0~1)이다. 낮으면 사람이 확인해야 한다.
# ---------------------------------------------------------------------------
_WS = 640.0     # 작업 해상도(긴 변)
_NB = 48        # 직선 한 개를 나눠 지지도를 재는 구간 수
_K_MAIN, _K_ALT = 16, 8


def _ramp(x, lo=1.0, hi=6.0):
    return np.clip((x - lo) / (hi - lo), 0, 1)


def _sample(M, horizontal, params, shift, w, h):
    """직선(각도, 오프셋)을 따라 _NB 곳에서 M 값을 읽는다. 반환 (값, 사진 안 여부)."""
    a, off = params
    tn = math.tan(a)
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    if horizontal:
        t = np.linspace(0, w - 1, _NB)
        pos = off + shift + tn * (t - cx)
        ii = np.clip(np.rint(pos).astype(int), 0, h - 1)
        jj = np.rint(t).astype(int)
        return M[ii, jj], (pos >= 0) & (pos < h)
    t = np.linspace(0, h - 1, _NB)
    pos = off + shift + tn * (t - cy)
    jj = np.clip(np.rint(pos).astype(int), 0, w - 1)
    ii = np.rint(t).astype(int)
    return M[ii, jj], (pos >= 0) & (pos < w)


def _search_lines(E, horizontal, angs, K, w, h):
    """E(지지도 맵) 위에서 (각도, 오프셋) 격자를 훑어 점수 상위 K개 직선을 고른다(주변 억제 포함)."""
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    if horizontal:
        t = np.linspace(0, w - 1, _NB)
        offs = np.arange(-0.3 * h, 1.3 * h, 2.0)
    else:
        t = np.linspace(0, h - 1, _NB)
        offs = np.arange(-0.3 * w, 1.3 * w, 2.0)
    tr_i = np.rint(t).astype(np.int32)
    S = np.zeros((len(angs), len(offs)), np.float32)
    for i, a in enumerate(angs):
        tn = math.tan(a)
        if horizontal:
            pos = offs[:, None] + tn * (t[None, :] - cx)
            ii = np.rint(pos).astype(np.int32)
            valid = (ii >= 0) & (ii < h)
            vals = E[np.clip(ii, 0, h - 1), tr_i[None, :]]
        else:
            pos = offs[:, None] + tn * (t[None, :] - cy)
            jj = np.rint(pos).astype(np.int32)
            valid = (jj >= 0) & (jj < w)
            vals = E[tr_i[None, :], np.clip(jj, 0, w - 1)]
        S[i] = (vals * valid).sum(1) / np.maximum(valid.sum(1), _NB * 0.5)
    Sd = cv2.dilate(S, np.ones((5, 9), np.uint8))
    idx = np.argwhere((S >= Sd) & (S > 0.2))
    if len(idx) == 0:
        return []
    order = np.argsort(-S[idx[:, 0], idx[:, 1]])[:K]
    return [(float(S[i, j]), (float(angs[i]), float(offs[j]))) for i, j in idx[order]]


def _cum(a):
    return np.concatenate([np.zeros((a.shape[0], 1), np.float32), np.cumsum(a, 1, dtype=np.float32)], 1)


def _seg_mean(cs, cv, li, lo_pos, hi_pos, span):
    """직선 li 의 [lo,hi] 구간에서 사진 안에 있는 구간의 평균(누적합 사용)과 사진 안 비율.
    span = 그 축의 최대 좌표."""
    a = np.clip(np.rint(np.minimum(lo_pos, hi_pos) / span * (_NB - 1)), 0, _NB - 1).astype(np.int32)
    b = np.clip(np.rint(np.maximum(lo_pos, hi_pos) / span * (_NB - 1)), 0, _NB - 1).astype(np.int32) + 1
    cnt = cv[li, b] - cv[li, a]
    return (cs[li, b] - cs[li, a]) / np.maximum(cnt, 1), cnt / (b - a)


def _gradients(sm, sigma=1.5):
    """블러한 영상의 밝기 기울기(gx, gy). 화소마다 B·G·R 중 기울기가 가장 센 채널의 부호 있는 값을 쓴다
    (어두운 파란 슬라이드처럼 회색조로는 대비가 약한 경계를 살리려는 것)."""
    b = cv2.GaussianBlur(sm, (0, 0), sigma).astype(np.float32)
    gxs = [cv2.Sobel(b[:, :, c], cv2.CV_32F, 1, 0, ksize=3) / 8.0 for c in range(3)]
    gys = [cv2.Sobel(b[:, :, c], cv2.CV_32F, 0, 1, ksize=3) / 8.0 for c in range(3)]
    mag = np.stack([gxs[c] ** 2 + gys[c] ** 2 for c in range(3)])
    k = np.argmax(mag, 0)
    gx = np.take_along_axis(np.stack(gxs), k[None], 0)[0]
    gy = np.take_along_axis(np.stack(gys), k[None], 0)[0]
    return gx, gy


def _gray_gradients(sm, sigma):
    g = cv2.GaussianBlur(cv2.cvtColor(sm, cv2.COLOR_BGR2GRAY), (0, 0), sigma).astype(np.float32)
    return cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3) / 8.0, cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3) / 8.0


def _candidates(sm):
    """작업 해상도 BGR 이미지 sm → 모든 (위,아래,왼,오른) 직선 조합의 꼭짓점·특징 재료. 없으면 None."""
    h, w = sm.shape[:2]
    gx, gy = _gradients(sm)
    dv = np.ones((5, 1), np.uint8)
    dh = np.ones((1, 5), np.uint8)
    Vp = np.minimum(cv2.dilate(_ramp(np.maximum(gy, 0)), dv), 1.0)     # 아래로 갈수록 밝아짐
    Vn = np.minimum(cv2.dilate(_ramp(np.maximum(-gy, 0)), dv), 1.0)    # 아래로 갈수록 어두워짐
    Hp = np.minimum(cv2.dilate(_ramp(np.maximum(gx, 0)), dh), 1.0)     # 오른쪽으로 갈수록 밝아짐
    Hn = np.minimum(cv2.dilate(_ramp(np.maximum(-gx, 0)), dh), 1.0)
    NEG = 0.35
    ah = np.radians(np.arange(-15, 15.1, 1.0))
    av = np.radians(np.arange(-22, 22.1, 1.0))

    def both(pos, neg, hor, angs):
        main = _search_lines(np.minimum(pos + NEG * neg, 1), hor, angs, _K_MAIN, w, h)
        for l in _search_lines(np.minimum(neg + NEG * pos, 1), hor, angs, _K_ALT, w, h):
            if not any(abs(l[1][0] - m[1][0]) < 0.02 and abs(l[1][1] - m[1][1]) < 4 for m in main):
                main.append(l)
        return main

    T = both(Vp, Vn, True, ah)      # 위 변: 안쪽(아래)이 밝음
    B = both(Vn, Vp, True, ah)      # 아래 변: 안쪽(위)이 밝음
    L = both(Hp, Hn, False, av)     # 왼 변: 안쪽(오른쪽)이 밝음
    R = both(Hn, Hp, False, av)     # 오른 변: 안쪽(왼쪽)이 밝음
    if not (T and B and L and R):
        return None
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    bri = cv2.GaussianBlur(sm.max(axis=2), (0, 0), 2.0).astype(np.float32)

    def bins(lines, M, hor, vm, shift=0.0):
        return np.stack([_sample(M, hor, l[1], shift, w, h)[0] for l in lines]).astype(np.float32) * vm

    # 변마다: 지지도(이 방향 기울기, 반대 방향 기울기), 안팎 밝기 대비(5px·14px), 사진 안 비율
    sides = {}
    for name, lines, hor, pos, neg, sign in (("T", T, True, Vp, Vn, 1), ("B", B, True, Vn, Vp, -1),
                                             ("L", L, False, Hp, Hn, 1), ("R", R, False, Hn, Hp, -1)):
        vm = np.stack([_sample(Vp, hor, l[1], 0.0, w, h)[1] for l in lines]).astype(np.float32)
        e_any = np.minimum(pos + NEG * neg, 1)
        d = {"sp": _cum(bins(lines, pos, hor, vm)), "sn": _cum(bins(lines, neg, hor, vm)),
             "sa": _cum(bins(lines, e_any, hor, vm)), "cv": _cum(vm)}
        for dd in (5, 14):
            d["c%d" % dd] = _cum(bins(lines, bri, hor, vm, sign * dd) - bins(lines, bri, hor, vm, -sign * dd))
        d["par"] = np.array([[math.tan(l[1][0]), l[1][1]] for l in lines])
        d["n"] = len(lines)
        d["sup"] = np.array([l[0] for l in lines], np.float32)
        sides[name] = d
    nT, nB, nL, nR = (sides[k]["n"] for k in "TBLR")

    def inter(hp, vp):
        a = hp[:, 0][:, None]
        b = hp[:, 1][:, None]
        c = vp[:, 0][None]
        d = vp[:, 1][None]
        y = (a * (-c * cy + d - cx) + b) / (1 - a * c)
        return c * (y - cy) + d, y

    TLx, TLy = inter(sides["T"]["par"], sides["L"]["par"])
    TRx, TRy = inter(sides["T"]["par"], sides["R"]["par"])
    BLx, BLy = inter(sides["B"]["par"], sides["L"]["par"])
    BRx, BRy = inter(sides["B"]["par"], sides["R"]["par"])

    def bc(a, ax):
        shp = [1, 1, 1, 1]
        shp[ax[0]], shp[ax[1]] = a.shape
        return a.reshape(shp)

    shape = (nT, nB, nL, nR)
    coords = [bc(TLx, (0, 2)), bc(TLy, (0, 2)), bc(TRx, (0, 3)), bc(TRy, (0, 3)),
              bc(BLx, (1, 2)), bc(BLy, (1, 2)), bc(BRx, (1, 3)), bc(BRy, (1, 3))]
    tlx, tly, trx, try_, blx, bly, brx, bry = [np.broadcast_to(c, shape).reshape(-1) for c in coords]
    X = np.stack([tlx, trx, blx, brx], -1)
    Y = np.stack([tly, try_, bly, bry], -1)
    # 기하가 말이 안 되는 조합(볼록하지 않음·너무 작음·종횡비 이상)은 지지도를 재기 전에 버린다.
    sel = np.flatnonzero(_geom(X, Y, w, h)[0])
    if len(sel) == 0:
        return None
    X, Y = X[sel], Y[sel]
    tlx, tly, trx, try_, blx, bly, brx, bry = (a[sel] for a in (tlx, tly, trx, try_, blx, bly, brx, bry))
    grid = np.unravel_index(sel, shape)
    idx = dict(zip("TBLR", grid))
    ranges = {"T": (tlx, trx, w - 1), "B": (blx, brx, w - 1), "L": (tly, bly, h - 1), "R": (try_, bry, h - 1)}
    feats = {}
    for key in ("sp", "sn", "sa", "c5", "c14"):
        feats[key] = np.stack([_seg_mean(sides[k][key], sides[k]["cv"], idx[k], *ranges[k])[0] for k in "TBLR"], -1)
    feats["ls"] = np.stack([sides[k]["sup"][idx[k]] for k in "TBLR"], -1)
    feats["lr"] = np.stack([(sides[k]["sup"] / max(float(sides[k]["sup"].max()), 1e-3))[idx[k]] for k in "TBLR"], -1)
    feats["cv"] = np.stack([_seg_mean(sides[k]["cv"], sides[k]["cv"], idx[k], *ranges[k])[1] for k in "TBLR"], -1)
    return {"X": X, "Y": Y, "w": w, "h": h, "bri": float(bri.mean()) / 255.0, **feats}


def _geom(X, Y, w, h):
    """꼭짓점(N×4, TL,TR,BL,BR)의 기하 특징. 반환 (유효 마스크, 지표 dict)."""
    P = np.stack([X, Y], -1)
    poly = P[:, [0, 1, 3, 2]]
    e = np.roll(poly, -1, 1) - poly
    e2 = np.roll(e, -1, 1)
    convex = ((e[:, :, 0] * e2[:, :, 1] - e[:, :, 1] * e2[:, :, 0]) > 0).all(1)
    pc = np.stack([np.clip(X, 0, w - 1), np.clip(Y, 0, h - 1)], -1)[:, [0, 1, 3, 2]]
    xs, ys = pc[:, :, 0], pc[:, :, 1]
    area_c = 0.5 * np.abs((xs * np.roll(ys, -1, 1) - ys * np.roll(xs, -1, 1)).sum(1)) / (w * h)

    def dist(a, b):
        return np.hypot(P[:, a, 0] - P[:, b, 0], P[:, a, 1] - P[:, b, 1])

    wt, wb, hl, hr = dist(0, 1), dist(2, 3), dist(0, 2), dist(1, 3)
    asp = (wt + wb) / np.maximum(hl + hr, 1e-6)
    valid = convex & (wt > 0.2 * w) & (wb > 0.2 * w) & (hl > 0.12 * h) & (hr > 0.12 * h) \
        & (area_c > 0.03) & (asp > 0.9) & (asp < 3.0)
    return valid, {"P": P, "poly": poly, "xs": xs, "ys": ys, "area_c": area_c, "asp": asp}


def _features(C):
    """후보 재료 → (특징 행렬 N×F, 유효 마스크, 잘린 면적 비율). 열 순서는 _MODEL 과 같아야 한다."""
    w, h = C["w"], C["h"]
    X, Y = C["X"], C["Y"]
    valid, g = _geom(X, Y, w, h)
    P, poly, xs, ys, area_c, asp = g["P"], g["poly"], g["xs"], g["ys"], g["area_c"], g["asp"]
    pr = np.roll(poly, -1, 1)
    area = 0.5 * np.abs((poly[:, :, 0] * pr[:, :, 1] - poly[:, :, 1] * pr[:, :, 0]).sum(1)) / (w * h)
    at = np.degrees(np.arctan2(P[:, 1, 1] - P[:, 0, 1], P[:, 1, 0] - P[:, 0, 0]))
    ab = np.degrees(np.arctan2(P[:, 3, 1] - P[:, 2, 1], P[:, 3, 0] - P[:, 2, 0]))
    al = np.degrees(np.arctan2(P[:, 2, 0] - P[:, 0, 0], P[:, 2, 1] - P[:, 0, 1]))
    ar = np.degrees(np.arctan2(P[:, 3, 0] - P[:, 1, 0], P[:, 3, 1] - P[:, 1, 1]))
    outside = (np.maximum(0, -X) + np.maximum(0, X - (w - 1))).sum(1) / w \
        + (np.maximum(0, -Y) + np.maximum(0, Y - (h - 1))).sum(1) / h
    cxn, cyn = xs.mean(1) / w, ys.mean(1) / h
    la = np.abs(np.log(np.maximum(asp, 1e-3) / 1.72))
    sp, sn, sa = C["sp"], C["sn"], C["sa"]
    c5, c14, cv_ = C["c5"], C["c14"], C["cv"]
    con = np.concatenate([c5, c14], 1) / 255.0
    cols = [sa, cv_, con, sp, sn,
            sp.mean(1, keepdims=True), sn.mean(1, keepdims=True), sp.min(1, keepdims=True),
            sa.mean(1, keepdims=True), sa.min(1, keepdims=True),
            con[:, :4].mean(1, keepdims=True), con[:, :4].min(1, keepdims=True),
            con[:, 4:].mean(1, keepdims=True), con[:, 4:].min(1, keepdims=True),
            np.stack([area_c, area_c ** 2, area, la, la ** 2, np.minimum(outside, 1),
                      np.abs(at) / 10, np.abs(ab) / 10, np.abs(al) / 10, np.abs(ar) / 10,
                      np.abs(at - ab) / 10, np.abs(al - ar) / 10, cxn, cyn, np.abs(cxn - 0.5),
                      (area_c < 0.08) * 1.0, (area_c > 0.8) * 1.0, (asp < 1.2) * 1.0, (asp > 2.1) * 1.0,
                      (ys.max(1) / h > 0.98) * 1.0], 1)]
    ls, lr = C["ls"], C["lr"]
    rel = con[:, :4] / (C["bri"] + 0.05)
    cols += [ls, lr, lr.mean(1, keepdims=True), lr.min(1, keepdims=True),
             rel.mean(1, keepdims=True), rel.min(1, keepdims=True)]
    F = np.concatenate(cols, 1).astype(np.float32)
    return F, valid, area_c


# BEGIN AUTO-DETECT WEIGHTS
# 이 블록은 05_스크립트/train_auto_detect.py --apply 가 통째로 다시 쓴다. 손으로 고치지 않는다.
# 학습된 점수 가중치(표준화 평균·표준편차·은닉 12개짜리 1층 신경망). 특징 열 순서는 _features 와 같다.
# 학습 자료: 사람이 손본 정답 380장. _MODEL 이 None 이면 변 지지도 평균으로 대신한다.
_MODEL = (
    # 특징 평균, 특징 표준편차
    [0.7727, 0.7776, 0.6272, 0.6762, 0.9782, 0.9882, 0.9451, 0.9531, 0.1419, 0.133, 0.1303, 0.1636, 0.1486, 0.2183, 0.1711, 0.1779, 0.7026, 0.7104, 0.5501, 0.6263, 0.4328, 0.3779, 0.3571, 0.2757, 0.6473, 0.3609, 0.3371, 0.7134, 0.4653, 0.1422, -0.07906, 0.179, -0.09447, 0.3473, 0.1612, 0.3516, 0.2457, 0.08956, 0.01274, 5.3, 5.155, 5.335, 5.38, 5.077, 6.201, 0.486, 0.4833, 0.09261, 0.1044, 0.00909, 0.09802, 0.3814, 0.03527, 0.7659, 0.7849, 0.5662, 0.5617, 0.7721, 0.7904, 0.7263, 0.7205, 0.7523, 0.5691, 0.3016, -0.1659],
    [0.232, 0.231, 0.2747, 0.2684, 0.1001, 0.07728, 0.1621, 0.1477, 0.288, 0.2362, 0.2649, 0.2547, 0.3354, 0.3636, 0.3146, 0.3144, 0.3113, 0.3044, 0.3352, 0.3067, 0.342, 0.3268, 0.3225, 0.2653, 0.2061, 0.1731, 0.2654, 0.1682, 0.2125, 0.1711, 0.1787, 0.2369, 0.2486, 0.2013, 0.156, 0.2058, 0.1709, 0.09725, 0.03214, 7.832, 7.839, 7.556, 7.598, 11.81, 12.28, 0.1344, 0.1059, 0.09849, 0.3065, 0.09493, 0.2976, 0.4856, 0.1844, 0.1672, 0.1814, 0.1681, 0.1647, 0.1665, 0.1813, 0.1986, 0.2007, 0.1105, 0.1419, 0.3907, 0.3902],
    # 은닉층 가중치 W1 (특징수 x 12, 행 우선), 편향 b1
    [-0.07877, -0.2186, -0.2785, -0.07083, -0.01975, 0.07075, -0.03733, -0.1507, -0.03336, -0.1813, 0.2677, 0.04478, -0.01405, 0.04773, -0.1656, 0.005008, -0.03686, -0.1124, -0.04054, -0.0379, -0.09257, 0.02026, 0.1507, 0.01283, -0.05641, -0.0879, -0.05272, 0.06622, -0.2846, -0.1164, -0.06647, -0.08542, 0.04889, -0.02411, 0.1412, 0.05427, -0.2224, -0.1553, -0.1893, 0.2459, -0.1507, -0.06629, -0.2413, 0.009825, -0.14, -0.2113, 0.251, -0.0009177, -0.1593, -0.1466, 0.05213, -0.08602, 0.1482, 0.08897, 0.1193, 0.02516, 0.03953, 0.2257, 0.07345, 0.2252, -0.009593, 0.01044, 0.004157, 0.02001, -0.01866, -0.01136, 0.009849, 0.01029, -0.002425, 0.002979, 0.007512, -0.0123, -0.08212, -0.05792, 0.2436, -0.1744, -0.1788, 0.1831, 0.07338, -0.1678, 0.03625, -0.1626, 0.1673, 0.04595, -0.06249, -0.05465, 0.4758, -0.0693, 0.0937, -0.3537, 0.208, -0.3392, -0.1346, -0.1902, 0.08404, 0.09718, 0.1055, 0.03935, -0.1689, -0.1374, 0.009953, -0.1176, -0.07536, -0.02136, 0.122, -0.01269, 0.1893, 0.006029, 0.06564, -0.04342, 0.08277, 0.1678, -0.007992, 0.1526, -0.0376, -0.1228, 0.09763, 0.08677, -0.0006204, 0.07672, 0.03965, -0.1105, 0.07525, 0.1407, -0.08154, -0.1204, -0.1219, -0.01706, -0.1784, 0.1225, 0.007601, 0.1137, -0.04898, -0.04962, 0.03022, 0.01756, 0.2587, -0.1051, 0.1229, 0.339, 0.1306, -0.1541, -0.3008, 0.05947, 0.1927, -0.1072, 0.04374, -0.2083, 0.1236, 0.1987, -0.06946, -0.1112, 0.1829, 0.2458, -0.05557, 0.6632, -0.03898, 0.8064, -0.3109, 0.2066, -0.1667, 0.04578, 0.05718, -0.222, -0.2268, -0.07533, 0.0849, 0.0385, 0.1319, -0.04469, 0.2624, -0.08998, -0.4279, -0.2896, -0.1904, 0.05118, -0.04519, 0.4588, -0.3536, 0.3883, -0.4233, -0.3499, -0.02447, 0.1993, 0.1839, -0.1388, -0.02376, 0.459, -0.06078, -0.5378, -0.05167, 0.3355, 0.1295, 0.02435, -0.0006217, -0.334, 0.07613, 0.1711, 0.07854, -0.001878, -0.02137, 0.044, -0.01815, 0.003801, 0.1105, -0.008185, 0.04007, -0.04202, 0.009729, -0.0861, 0.1343, 0.141, -0.1251, -0.004802, -0.02724, -0.09648, 0.1126, 0.1097, 0.1008, -0.08081, -0.04428, 0.08524, 0.1622, 0.08024, 0.1199, 0.02049, -0.05635, 0.02706, 0.006899, 0.1826, 0.1468, -0.08335, -0.009206, 0.1012, 0.0551, 0.2068, 0.05687, 0.1777, -0.1415, 0.01131, 0.02945, -0.03103, -0.2281, 0.08865, 0.07013, 0.01874, 0.1312, -0.02871, -0.04904, -0.08368, -0.1009, -0.08222, 0.05398, -0.05943, -0.05384, 0.08906, 0.1771, 0.04566, -0.04967, -0.1418, 0.1696, 0.06319, 0.0584, 0.05543, 0.02346, 0.02, 0.1804, 0.0444, -0.2324, -0.09438, 0.02637, -0.0314, 0.01518, 0.06725, 0.04252, 0.2028, -0.1602, -0.06852, -0.129, -0.05695, -0.1007, 0.01594, -0.1741, 0.4074, 0.05845, -0.06634, 0.02689, 0.008523, 0.1382, 0.1251, 0.1086, -0.2034, 0.01171, 0.1057, 0.166, 0.1612, 0.01519, 0.0895, -0.09254, -0.0178, -0.008789, -0.05628, -0.1015, 0.08059, -0.02986, -0.009498, -0.01146, 0.05741, 0.08205, -0.004553, 0.009863, 0.08092, -0.003185, 0.02613, -0.01002, -0.2553, 0.149, 0.01467, 0.01137, -0.0001048, -0.02657, 0.03227, -0.0234, -0.05672, -0.1431, -0.1555, -0.2494, 0.104, -0.1939, -0.08755, -0.1502, -0.09894, -0.07923, -0.1502, 0.302, 0.04194, -0.05634, 0.02315, -0.07454, -0.2824, -0.04917, -0.0236, -0.1353, -0.0429, -0.01715, 0.02534, 0.1028, -0.003723, 0.0636, -0.06183, -0.003894, 0.05941, 0.06666, -0.0841, -0.04525, 0.06706, 0.06301, 0.01535, -0.02766, 0.09871, -0.04971, -0.09387, 0.1839, -0.2167, 0.05589, -0.219, 0.04295, 0.05802, -0.1843, -0.02858, -0.108, -0.09566, -0.04615, 0.1556, -0.0238, 0.04291, -0.09773, -0.05658, -0.07213, 0.0474, -0.05765, 0.0302, -0.1221, 0.4844, -0.2307, 0.03778, -0.07317, -0.5416, -0.06046, -0.3689, 0.008261, -0.1221, -0.0281, -0.01278, 0.1011, 0.6635, 0.1562, -0.07301, 0.1342, -0.02983, -0.01783, -0.2461, -0.4421, 0.2358, -0.339, 0.0696, -0.03372, -0.2551, 0.09189, 0.02721, 0.02514, -0.1518, 0.001225, -0.1061, -0.3899, 0.2275, -0.07177, 0.2225, -0.01831, -0.2429, 0.1399, -0.05365, 0.1573, -0.03913, -0.0185, -0.2404, -0.4332, 0.2312, -0.3433, 0.06165, -0.04056, -0.2537, 0.5224, 0.5842, 0.1402, 0.04699, -0.3383, -0.2625, -0.5075, 0.7944, -0.1005, 0.212, -0.5995, -0.03177, 0.1093, 0.6139, -0.127, 0.05891, -0.2494, -0.1352, -0.3731, 0.4737, 0.1318, 0.006371, -0.2306, -0.2269, -0.1991, -0.1015, 0.2258, 0.00183, -0.09626, 0.03031, -0.0004369, -0.06011, -0.1339, -0.09833, -0.1127, 0.03481, 0.05306, 0.09239, 0.09271, -0.1519, 0.09591, 0.05825, -0.001553, 0.04257, -0.007924, 0.03137, -0.1478, 0.02649, 0.1137, 0.003696, 0.09835, -0.1434, 0.09863, 0.06976, 0.0768, 0.103, 0.09552, 0.02924, -0.1393, -0.07761, 0.1804, 0.1755, 0.1245, -0.1507, 0.1314, 0.06258, 0.1207, 0.1763, 0.06432, 0.01998, -0.2312, -0.1181, 0.0905, 0.03766, -0.001719, -0.07648, 0.05894, 0.04662, -0.01751, -0.03194, 0.01444, -0.02552, -0.06061, 0.006979, 0.04812, 0.05392, 0.0877, -0.1072, 0.07641, 0.03959, 0.01654, 0.0582, 0.007448, 0.0374, -0.1176, -0.01798, 0.06733, -0.009669, 0.03394, -0.01455, 0.03858, 0.004987, 0.0008158, 0.04226, -0.05253, -0.09275, -0.03928, -0.04317, 0.5718, 0.5363, 0.09673, -0.2955, 0.2411, 0.2718, 0.1101, 0.4039, 0.05963, -0.1366, -0.3428, -0.08369, -0.4511, -0.1126, -0.2231, -0.1003, 0.1975, 0.338, -0.1359, 0.02599, 0.3799, 0.2897, -0.1835, 0.4701, -0.1595, -0.003305, 0.3342, -0.1845, 0.1019, 0.2121, 0.4305, -0.05887, 0.3312, 0.3972, -0.2395, -0.0005022, 0.01546, -0.006036, 0.05408, -0.08976, 0.03207, 0.04345, 0.06043, 0.00475, 0.08374, 0.06434, -0.062, -0.04468, 0.02708, 0.009287, 0.0121, -0.01123, 0.0161, 0.007687, -0.008807, 0.01263, 9.369e-05, 0.0158, -0.0227, 0.006641, 0.01479, -0.01569, -0.02002, -0.03676, 0.02546, 0.01966, -0.02704, -0.03505, 0.008022, -0.01247, -0.0117, 0.03322, -0.2023, -0.5602, -0.1034, 0.1704, -0.41, -0.22, -0.3278, 0.1876, -0.06629, -0.253, -0.2411, -0.0619, 0.02173, -0.007536, 0.0113, -0.03676, 0.03678, 0.02891, -0.0109, 0.002978, 0.01644, 0.0175, -0.02734, 0.01436, 0.4041, 0.03518, 0.03687, 0.01544, -0.09602, 0.0643, 0.1945, 0.04421, 0.2194, -0.4193, 0.3401, 0.07679, -0.1323, 0.1962, -0.178, -0.1726, 0.07758, 0.08141, -0.2051, -0.1641, 0.2663, 0.06856, 0.08934, -0.1305, -0.2063, -0.286, 0.2662, -0.1467, -0.2001, 0.02984, -0.08684, -0.2014, 0.0783, 0.01017, 0.02365, 0.1423, -0.07597, -0.1895, 0.1779, -0.0207, 0.0322, 0.1622, -0.06141, -0.04822, -0.2591, -0.04243, -0.07338, -0.03945, 0.4537, 0.1448, 0.0607, 0.07489, -0.1122, 0.01555, 0.1688, 0.02154, 0.09546, -0.3986, 0.3392, 0.1753, -0.1101, 0.232, -0.2222, -0.09819, 0.05231, 0.05851, -0.2701, -0.2702, 0.1933, 0.09025, 0.1333, -0.02879, 0.1059, 0.08016, 0.07977, -0.004079, -0.2047, -0.1261, 0.09232, 0.04893, -0.03781, 0.1919, -0.04295, -0.08867, -0.01455, -0.02395, -0.2194, 0.05013, -0.01082, 0.1912, -0.07074, 0.1435, -0.2481, -0.091, -0.0001778, 0.07851, 0.1622, 0.1701, -0.1356, 0.006879, -0.1196, 0.0603, -0.03893, -0.01033, -0.01295, -0.0654, 0.1587, 0.04576, 0.09591, 0.02291, 0.0228, -0.09037, -0.08747, 0.1624, -0.1344, 0.06277, -0.1577, 0.2257, 0.2312, -0.09769, -0.166, 0.0845, -0.1063, 0.1392, -0.103, -0.04517, 0.05499, -0.09695, 0.4391, -0.1306, 0.01895, 0.1179, 0.06532, -0.01519, 0.1842, -0.1694, 0.0007046, -0.2353, 0.05033, 0.01202, -0.008927, -0.07954, -0.2548, -0.06929],
    [0.2882, 0.7534, 0.9265, 0.445, 0.812, 0.3833, 1.364, 1.119, 0.6536, 0.6304, -0.2646, -1.097],
    # 출력 가중치 w2, 편향 b2
    [-1.365, -1.584, -1.2, 1.132, -1.074, -1.117, -1.342, -1.49, -1.123, -1.281, 1.259, 1.417],
    -0.8893,
)
# 점수 → 정답(5% 이내) 확률 환산 (a, b): conf = sigmoid(a*z + b)
_CALIB = (1.0281, 1.0745)
# END AUTO-DETECT WEIGHTS


def _score(F):
    if _MODEL is None:
        return F[:, 0:4].mean(1)
    mu, sd, W1, b1, w2 = (np.asarray(v, np.float32) for v in _MODEL[:5])
    Z = (F - mu) / sd
    return np.maximum(Z @ W1.reshape(len(mu), -1) + b1, 0) @ w2 + _MODEL[5]


def _fit_line(pts):
    c = pts.mean(0)
    _, _, vt = np.linalg.svd(pts - c, full_matrices=False)
    return c, vt[0]


def _refine_quad(sm, quad, r=7.0, n=40):
    """고른 사각형의 네 변을 기울기 봉우리(서브픽셀)에 맞춰 다시 맞춘다. quad: TL,TR,BL,BR (sm 픽셀)."""
    h, w = sm.shape[:2]
    gx, gy = _gray_gradients(sm, 1.2)
    TL, TR, BL, BR = [np.asarray(p, np.float64) for p in quad]
    cen = (TL + TR + BL + BR) / 4
    offs = np.arange(-r, r + 0.01, 0.5)
    lines = []
    for a, b in ((TL, TR), (BL, BR), (TL, BL), (TR, BR)):
        d = b - a
        L = np.linalg.norm(d)
        d = d / L
        nrm = np.array([-d[1], d[0]])
        if np.dot(nrm, cen - (a + b) / 2) < 0:
            nrm = -nrm                                   # 안쪽을 향하게
        base = a[None] + (np.linspace(0.08, 0.92, n) * L)[:, None] * d[None]
        P = base[:, None, :] + offs[None, :, None] * nrm[None, None, :]
        px, py = P[..., 0].astype(np.float32), P[..., 1].astype(np.float32)
        ok = (px > 1) & (px < w - 2) & (py > 1) & (py < h - 2)
        val = cv2.remap(gx, px, py, cv2.INTER_LINEAR) * nrm[0] + cv2.remap(gy, px, py, cv2.INTER_LINEAR) * nrm[1]
        val = np.where(ok, val, 0)
        k = np.argmax(val * (1.0 - 0.6 * np.abs(offs)[None] / r), 1)
        rows = np.arange(n)
        good = (val[rows, k] > 2.0) & (k > 0) & (k < len(offs) - 1)
        pts = []
        for i in np.flatnonzero(good):
            kk = k[i]
            y0, y1, y2 = val[i, kk - 1], val[i, kk], val[i, kk + 1]
            den = y0 - 2 * y1 + y2
            dl = 0.5 * (y0 - y2) / den if abs(den) > 1e-6 else 0.0
            pts.append(base[i] + (offs[kk] + np.clip(dl, -0.5, 0.5) * 0.5) * nrm)
        pts = np.array(pts)
        if len(pts) < 8:
            lines.append((a, d))
            continue
        for _ in range(3):
            c, dd = _fit_line(pts)
            res = np.abs((pts - c) @ np.array([-dd[1], dd[0]]))
            keep = res <= max(1.0, 2.0 * np.median(res))
            if keep.sum() < 8:
                break
            pts = pts[keep]
        c, dd = _fit_line(pts)
        lines.append((c, dd if np.dot(dd, d) >= 0 else -dd))

    def inter(l1, l2):
        (p1, d1), (p2, d2) = l1, l2
        A = np.array([d1, -d2]).T
        if abs(np.linalg.det(A)) < 1e-6:
            return None
        return p1 + np.linalg.solve(A, p2 - p1)[0] * d1

    T, B, Lf, Rt = lines
    out = [inter(T, Lf), inter(T, Rt), inter(B, Lf), inter(B, Rt)]
    if any(o is None for o in out):
        return np.asarray(quad, np.float64)
    out = np.array(out)
    if np.abs(out - np.asarray(quad)).max() > 0.05 * max(w, h):   # 과하게 움직이면 미세조정이 아니다
        return np.asarray(quad, np.float64)
    return out


def _refine_multi(img, sm, quad):
    """해상도 640·960·1280 에서 각각 미세조정한 뒤 꼭짓점별 중앙값을 쓴다(한 해상도의 잡음에 덜 흔들린다)."""
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.shape[2] == 4:
        img = img[:, :, :3]
    h0, w0 = img.shape[:2]
    h, w = sm.shape[:2]
    outs = []
    for size, r in ((640, 7.0), (960, 10.0), (1280, 12.0)):
        sc = min(1.0, size / max(h0, w0))
        if size == 640 or sc == 1.0 and max(h0, w0) <= 640:
            big = sm
        else:
            big = cv2.resize(img, (max(int(round(w0 * sc)), 8), max(int(round(h0 * sc)), 8)),
                             interpolation=cv2.INTER_AREA)
        f = np.array([(big.shape[1] - 1) / (w - 1), (big.shape[0] - 1) / (h - 1)])
        outs.append(_refine_quad(big, np.asarray(quad) * f, r=r) / f)
    return np.median(np.stack(outs), 0)


def _prepare(img):
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.shape[2] == 4:
        img = img[:, :, :3]
    h, w = img.shape[:2]
    sc = _WS / max(h, w)
    return cv2.resize(img, (max(int(round(w * sc)), 8), max(int(round(h * sc)), 8)), interpolation=cv2.INTER_AREA)


def detect_screen(img):
    """사진 속 스크린(투사 화면) 사각형을 찾는다.

    반환: (norm_quad, conf). norm_quad 는 정규화 [TL,TR,BL,BR] (각 [x,y] in [0,1]), 못 찾으면 None.
    conf 는 0~1 — 정답(모서리 오차 5% 이내)일 확률 추정. 낮으면 사람이 확인해야 한다."""
    sm = _prepare(img)
    C = _candidates(sm)
    if C is None:
        return None, 0.0
    F, valid, _ = _features(C)
    if not valid.any():
        return None, 0.0
    z = _score(F)
    z[~valid] = -1e9
    k = int(np.argmax(z))
    w, h = C["w"], C["h"]
    quad = np.stack([C["X"][k], C["Y"][k]], -1)
    quad = _refine_multi(img, sm, quad)
    quad = np.clip(quad, [0, 0], [w - 1, h - 1]) / [w - 1, h - 1]
    a, b = _CALIB
    conf = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, a * float(z[k]) + b))))
    if not np.isfinite(quad).all():
        return None, 0.0
    return [[float(x), float(y)] for x, y in quad], float(conf)


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
