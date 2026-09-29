#!/usr/bin/env python3
"""경계 자동 찾기(auto_detect_screen.py)의 점수 모델을 다시 학습한다.

행사가 끝나 사람이 4모서리를 손본 뒤 `백업 내보내기` 한 JSON 을 정답으로 삼아, 후보 사각형을 고르는
작은 신경망(_MODEL)과 conf 환산값(_CALIB)을 새로 만든다. 후보·특징 추출은 auto_detect_screen.py 의
함수(_prepare·_candidates·_features·_refine_multi)를 그대로 불러 쓰므로 학습과 추론의 특징이 어긋나지 않는다.

정답 세트: `--set <백업.json>::<02_작업장>` 을 여러 번 줄 수 있다(행사마다 하나). 백업의 키와 작업장 사진을
짝짓는 규칙(파일명 대체 매칭, 손대지 않은 기본값 제외)은 eval_auto_detect.py 와 같다.

단계
  1) 후보·특징 추출(병렬). 사진마다 후보 사각형 중 대표 표본(점수 상위·무작위·정답 근접)의 특징과 오차를 얻는다.
  2) 학습. 표본으로 은닉 12 신경망을 맞추고, 그 모델이 실제로 1등으로 고른 오답 후보(어려운 오답)를 표본에
     더해 다시 맞추는 과정을 2번 반복한다. 하이퍼파라미터·seed 는 코드에 고정되어 같은 입력이면 같은 가중치가 나온다.
  3) 폴더 단위 2분할 교차검증. 세트마다 발표 폴더를 이름순으로 번갈아 두 묶음으로 나눠, 한 묶음으로
     학습하고 다른 묶음으로 잰다(hit@2%·hit@5%: 모서리 오차/대각선). 이 점수가 새 사진에서 기대할 수준에 가깝다.
     맨 끝 "전체 학습(학습에 쓴 사진)" 줄은 학습에 쓴 사진으로 잰 값이라 낙관적이다.
  4) conf 캘리브레이션. 교차검증에서 학습에 안 쓴 사진의 1등 점수를 "5% 이내로 맞을 확률"로 환산한다.
  5) 결과 저장. `--out weights.json` 으로 저장하고, `--apply` 면 auto_detect_screen.py 안의
     `# BEGIN AUTO-DETECT WEIGHTS` ~ `# END AUTO-DETECT WEIGHTS` 블록만 다시 쓴다.

사용 순서(재학습)
  1. 행사 후 브라우저 도구에서 손본 경계를 `백업 내보내기` 한다(03_결과물/백업/ 에 저장됨).
  2. 학습: (--apply 없이 먼저)
       python 05_스크립트/train_auto_detect.py \\
           --set 03_결과물/백업/slide_tool_backup_….json::02_작업장 --out weights.json
     세트가 여러 개면 --set 을 반복한다. 오래 걸리는 특징 추출은 --cache 폴더에 남겨 두면 다음에 재사용한다.
  3. 전후 비교: 현재 가중치로 eval_auto_detect.py 를 돌려 두고, --apply 한 뒤 다시 돌려 hit@2%·hit@5% 를 비교한다.
       python 05_스크립트/eval_auto_detect.py --backup <백업.json> --workspace 02_작업장
  4. 새 가중치가 더 나쁘면 `git checkout 05_스크립트/auto_detect_screen.py` 로 되돌린다.
     이미 만든 weights.json 만 다시 적용하려면 `--from-weights weights.json --apply` (학습은 건너뜀).

주의: 학습·평가 사진에는 손본 정답이 있어야 하고, 가중치를 바꾸면 detect_screen 결과가 달라진다.
사진·백업·weights.json 은 작업 산출물이므로 저장소에 커밋하지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import auto_detect_screen as ads  # noqa: E402
import eval_auto_detect as ev  # noqa: E402
from parallel_map import ordered_parallel_map  # noqa: E402

# ---- 학습 설정(바꾸면 같은 입력에서도 다른 가중치가 나온다) ----
HIT_TH = 0.03        # 학습 표본의 정답 기준: 모서리 오차/대각선 < 3%
HIDDEN = 12
EPOCHS = 40
SEED = 0
LR = 3e-3
L2 = 1e-3
BATCH = 2048
MINING_ROUNDS = 2    # 어려운 오답을 표본에 더해 다시 학습하는 횟수
N_TOP, N_RANDOM, N_GOOD = 300, 300, 40   # 사진당 표본: 지지도 상위 · 무작위 · 정답 근접
N_HARD = 80          # 어려운 오답: 사진당 점수 상위 후보 수
BUILD_VERSION = "1"  # 표본 추출 방식을 바꾸면 올려 캐시를 무효화한다
BEGIN, END = "# BEGIN AUTO-DETECT WEIGHTS", "# END AUTO-DETECT WEIGHTS"


# --------------------------------------------------------------------------
# 사진 하나 단위 작업(프로세스 풀에서 실행되므로 최상위 함수)
# --------------------------------------------------------------------------
def _errors(C, truth, W, H):
    """모든 후보의 오차: 모서리 픽셀 거리 최댓값 / 원본 대각선(작업 해상도 좌표를 원본 비율로 환산)."""
    w, h = C["w"], C["h"]
    X = np.clip(C["X"], 0, w - 1) / (w - 1)
    Y = np.clip(C["Y"], 0, h - 1) / (h - 1)
    tr = np.array(truth)
    return np.sqrt(((X - tr[:, 0]) * W) ** 2 + ((Y - tr[:, 1]) * H) ** 2).max(-1) / math.hypot(W, H)


def build_job(args):
    """사진 → (표본 특징 F, 표본 오차) 또는 None(후보를 못 뽑는 사진)."""
    path, truth = args
    img = cv2.imread(path)
    if img is None:
        return None
    H, W = img.shape[:2]
    sm = ads._prepare(img)
    C = ads._candidates(sm)
    if C is None:
        return None
    F, valid, _ = ads._features(C)
    err = _errors(C, truth, W, H)
    idx = np.flatnonzero(valid)
    if len(idx) == 0:
        return None
    rng = np.random.default_rng(1)
    top = idx[np.argsort(-F[idx, 0:4].mean(1))[:N_TOP]]
    rnd = rng.choice(idx, min(N_RANDOM, len(idx)), replace=False)
    good = idx[np.argsort(err[idx])[:N_GOOD]]
    sel = np.unique(np.concatenate([top, rnd, good]))
    return F[sel], err[sel]


def eval_job(args):
    """사진 → (미세조정 전 오차, 전체 파이프라인 오차, 1등 점수, 어려운 오답 표본 또는 None).

    전체 파이프라인 오차는 detect_screen 과 같은 경로(1등 고르기 → _refine_multi)로 잰 값이다."""
    path, truth, model, refine, want_hard = args
    img = cv2.imread(path)
    if img is None:
        return 9.0, 9.0, -9.0, None
    H, W = img.shape[:2]
    sm = ads._prepare(img)
    C = ads._candidates(sm)
    if C is None:
        return 9.0, 9.0, -9.0, None
    F, valid, _ = ads._features(C)
    z = score(F, model)
    z[~valid] = -1e9
    k = int(np.argmax(z))
    err = _errors(C, truth, W, H)
    e_ref = err[k]
    if refine:
        q = np.stack([C["X"][k], C["Y"][k]], -1)
        q = ads._refine_multi(img, sm, q)
        qq = np.clip(q, [0, 0], [C["w"] - 1, C["h"] - 1]) / [C["w"] - 1, C["h"] - 1]
        e_ref = max(ev.corner_errors(qq.tolist(), truth, W, H))
    hard = None
    if want_hard:
        top = np.argsort(-z)[:N_HARD]
        top = top[valid[top]]
        hard = (F[top], err[top])
    return err[k], e_ref, z[k], hard


# --------------------------------------------------------------------------
# 모델
# --------------------------------------------------------------------------
def fit_mlp(F, y, hid=HIDDEN, l2=L2, epochs=EPOCHS, seed=SEED, lr=LR, bs=BATCH):
    """은닉 hid 개 ReLU 1층 + 시그모이드 출력, Adam. 반환 dict(mu, sd, W1, b1, w2, b2)."""
    rng = np.random.default_rng(seed)
    mu = F.mean(0)
    sd = F.std(0) + 1e-6
    Z = ((F - mu) / sd).astype(np.float32)
    n, d = Z.shape
    W1 = (rng.standard_normal((d, hid)) * np.sqrt(2 / d)).astype(np.float32)
    b1 = np.zeros(hid, np.float32)
    w2 = (rng.standard_normal(hid) * np.sqrt(1 / hid)).astype(np.float32)
    b2 = np.float32(-1.0)
    params = [W1, b1, w2, np.array([b2], np.float32)]
    mom = [np.zeros_like(p) for p in params]
    var = [np.zeros_like(p) for p in params]
    t = 0
    y = y.astype(np.float32)
    for _ in range(epochs):
        perm = rng.permutation(n)
        for i in range(0, n, bs):
            idx = perm[i:i + bs]
            x = Z[idx]
            yy = y[idx]
            a = x @ params[0] + params[1]
            h = np.maximum(a, 0)
            z = h @ params[2] + params[3][0]
            p = 1 / (1 + np.exp(-z))
            g = (p - yy) / len(idx)
            gw2 = h.T @ g + l2 * params[2]
            gb2 = np.array([g.sum()], np.float32)
            gh = np.outer(g, params[2]) * (a > 0)
            gW1 = x.T @ gh + l2 * params[0]
            gb1 = gh.sum(0)
            t += 1
            for k, gr in enumerate((gW1, gb1, gw2, gb2)):
                mom[k] = 0.9 * mom[k] + 0.1 * gr
                var[k] = 0.999 * var[k] + 0.001 * gr * gr
                params[k] -= lr * (mom[k] / (1 - 0.9 ** t)) / (np.sqrt(var[k] / (1 - 0.999 ** t)) + 1e-8)
    return {"mu": mu, "sd": sd, "W1": params[0], "b1": params[1], "w2": params[2], "b2": float(params[3][0])}


def score(F, m):
    """후보 특징 → 점수 z. 수식은 auto_detect_screen._score 와 같다."""
    return np.maximum(((F - m["mu"]) / m["sd"]) @ m["W1"] + m["b1"], 0) @ m["w2"] + m["b2"]


def fit_calib(z, e, th=0.05):
    """1등 점수 z → 정답(오차 < th) 확률의 로지스틱 환산 (a, b). conf = sigmoid(a*z + b)."""
    y = (np.asarray(e) < th).astype(float)
    Z = np.c_[z, np.ones(len(z))]
    b = np.zeros(2)
    for _ in range(50):
        pp = 1 / (1 + np.exp(-Z @ b))
        W = pp * (1 - pp) + 1e-6
        b -= np.linalg.solve((Z * W[:, None]).T @ Z + 1e-3 * np.eye(2), Z.T @ (pp - y))
    return round(float(b[0]), 4), round(float(b[1]), 4), float(y.mean())


# --------------------------------------------------------------------------
# 자료 모으기 · 캐시
# --------------------------------------------------------------------------
def parse_set(spec):
    parts = spec.split("::")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise SystemExit(f"--set 형식은 <백업.json>::<작업장 폴더> 입니다: {spec!r}")
    return parts[0], parts[1]


def collect(set_specs):
    """--set 들 → 사진 목록. 세트 안에서 발표 폴더를 이름순으로 번갈아 par 0/1 로 나눈다(교차검증 묶음)."""
    items = []
    for si, spec in enumerate(set_specs, 1):
        backup, ws = parse_set(spec)
        if not os.path.isfile(backup):
            raise SystemExit(f"백업 JSON 이 없습니다: {backup}")
        if not os.path.isdir(ws):
            raise SystemExit(f"작업장 폴더가 없습니다: {ws}")
        matched, n_truth, skipped, missing, by_base = ev.match_truth(backup, ws)
        print(f"세트 {si}: 정답 {n_truth}건 | 기본값 제외 {skipped} | 파일 없음 {missing} | "
              f"파일명 대체 매칭 {by_base} | 사용 {len(matched)}")
        rows = [{"key": k, "path": p, "truth": q,
                 "folder": k.split("/")[1] if k.count("/") >= 2 else ""} for k, p, q in matched]
        par = {f: i % 2 for i, f in enumerate(sorted({r["folder"] for r in rows}))}
        for r in rows:
            items.append({"set": f"set{si}", "par": par[r["folder"]], **r})
    if not items:
        raise SystemExit("학습에 쓸 정답 사진이 없습니다.")
    return items


def feature_fingerprint():
    """캐시 열쇠에 넣을 '특징 추출 코드' 지문: auto_detect_screen.py 에서 가중치 블록을 뺀 내용 + 라이브러리 버전."""
    with open(ads.__file__, encoding="utf-8") as f:
        src = f.read()
    src = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), "", src, flags=re.S)
    h = hashlib.sha1()
    for part in (src, cv2.__version__, np.__version__, BUILD_VERSION):
        h.update(part.encode("utf-8"))
    return h.hexdigest()


def cache_path(cache_dir, fp, item):
    st = os.stat(item["path"])
    h = hashlib.sha1()
    h.update(f"{fp}|{os.path.abspath(item['path'])}|{st.st_size}|{st.st_mtime_ns}|"
             f"{json.dumps(item['truth'])}".encode("utf-8"))
    return os.path.join(cache_dir, h.hexdigest() + ".npz")


def build_all(items, jobs, cache_dir):
    """모든 사진의 표본을 만든다(캐시가 있으면 재사용). 반환: 사진과 같은 순서의 (F, err) 또는 None."""
    built = [None] * len(items)
    todo = []
    fp = feature_fingerprint() if cache_dir else None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
    hits = 0
    for i, it in enumerate(items):
        if cache_dir:
            cp = cache_path(cache_dir, fp, it)
            if os.path.isfile(cp):
                try:
                    with np.load(cp) as z:
                        built[i] = (z["F"], z["err"]) if int(z["ok"]) else None
                    hits += 1
                    continue
                except Exception:
                    pass  # 깨진 캐시는 다시 만든다
        todo.append(i)
    if cache_dir:
        print(f"  캐시 적중 {hits}/{len(items)}")
    res = ordered_parallel_map(build_job, [(items[i]["path"], items[i]["truth"]) for i in todo], workers=jobs)
    for i, r in zip(todo, res):
        built[i] = r
        if cache_dir:
            cp = cache_path(cache_dir, fp, items[i])
            tmp = cp + ".tmp.npz"
            if r is None:
                np.savez_compressed(tmp, ok=np.array(0))
            else:
                np.savez_compressed(tmp, ok=np.array(1), F=r[0], err=r[1])
            os.replace(tmp, cp)
    return built


# --------------------------------------------------------------------------
# 학습 · 평가
# --------------------------------------------------------------------------
def train_on(items, built, sel, jobs):
    """items 중 sel(인덱스 목록) 로 학습한다: 표본으로 맞추고, 1등으로 뽑힌 오답을 더해 MINING_ROUNDS 번 반복."""
    ok = [i for i in sel if built[i] is not None]
    if not ok:
        raise SystemExit("학습할 사진이 없습니다(모든 사진에서 후보를 못 찾음).")
    Fs = [built[i][0] for i in ok]
    Es = [built[i][1] for i in ok]
    for r in range(MINING_ROUNDS + 1):
        m = fit_mlp(np.concatenate(Fs), (np.concatenate(Es) < HIT_TH).astype(float))
        if r == MINING_ROUNDS:
            break
        res = ordered_parallel_map(
            eval_job, [(items[i]["path"], items[i]["truth"], m, False, True) for i in ok], workers=jobs)
        for j, (_, _, _, hard) in enumerate(res):
            if hard is not None:
                Fs[j] = np.concatenate([Fs[j], hard[0]])
                Es[j] = np.concatenate([Es[j], hard[1]])
    return m, len(ok)


def evaluate(items, sel, m, jobs):
    res = list(ordered_parallel_map(
        eval_job, [(items[i]["path"], items[i]["truth"], m, True, False) for i in sel], workers=jobs))
    return res


def hit_line(errs):
    e = np.asarray(errs, float)
    return f"n={len(e):<4d} hit@2%={np.mean(e < .02):.3f} hit@5%={np.mean(e < .05):.3f}"


def report(title, items, sel, res, verbose):
    print(title)
    by_set = defaultdict(list)
    by_folder = defaultdict(list)
    for i, r in zip(sel, res):
        by_set[items[i]["set"]].append(r[1])
        by_folder[(items[i]["set"], items[i]["folder"])].append(r[1])
    for name in sorted(by_set):
        print(f"  {name}: {hit_line(by_set[name])}")
    if len(by_set) > 1:
        print(f"  전체 : {hit_line([r[1] for r in res])}")
    if verbose:
        for (name, fld), v in sorted(by_folder.items()):
            print(f"    {name} {fld:<40} {hit_line(v)}")


def fmt_list(v):
    return "[" + ", ".join("%.4g" % x for x in np.ravel(v)) + "]"


def render_block(m, calib, n_images):
    """auto_detect_screen.py 에 들어갈 가중치 블록 텍스트(BEGIN/END 표식 포함)."""
    hid = np.asarray(m["W1"]).shape[1]
    lines = [
        BEGIN,
        "# 이 블록은 05_스크립트/train_auto_detect.py --apply 가 통째로 다시 쓴다. 손으로 고치지 않는다.",
        f"# 학습된 점수 가중치(표준화 평균·표준편차·은닉 {hid}개짜리 1층 신경망). 특징 열 순서는 _features 와 같다.",
        f"# 학습 자료: 사람이 손본 정답 {n_images}장. _MODEL 이 None 이면 변 지지도 평균으로 대신한다.",
        "_MODEL = (",
        "    # 특징 평균, 특징 표준편차",
        f"    {fmt_list(m['mu'])},",
        f"    {fmt_list(m['sd'])},",
        f"    # 은닉층 가중치 W1 (특징수 x {hid}, 행 우선), 편향 b1",
        f"    {fmt_list(m['W1'])},",
        f"    {fmt_list(m['b1'])},",
        "    # 출력 가중치 w2, 편향 b2",
        f"    {fmt_list(m['w2'])},",
        "    %.4g," % m["b2"],
        ")",
        "# 점수 → 정답(5% 이내) 확률 환산 (a, b): conf = sigmoid(a*z + b)",
        f"_CALIB = ({calib[0]}, {calib[1]})",
        END,
    ]
    return "\n".join(lines)


def apply_block(path, block):
    """path 안의 BEGIN~END 블록만 block 으로 바꾼다. 표식이 없거나 둘 이상이면 아무것도 쓰지 않고 실패."""
    with open(path, encoding="utf-8", newline="") as f:
        src = f.read()
    pat = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
    found = pat.findall(src)
    if len(found) != 1:
        raise SystemExit(f"{path} 에 '{BEGIN}' ~ '{END}' 블록이 정확히 하나 있어야 합니다(현재 {len(found)}개).")
    nl = "\r\n" if "\r\n" in src else "\n"
    new = pat.sub(lambda _m: block.replace("\n", nl), src, count=1)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    os.replace(tmp, path)


def weights_to_json(m, calib, meta):
    return {"model": {k: (np.asarray(v).tolist() if k != "b2" else float(v)) for k, v in m.items()},
            "calib": list(calib), "meta": meta}


def weights_from_json(d):
    mm = d["model"]
    m = {k: np.asarray(mm[k], np.float32) for k in ("mu", "sd", "W1", "b1", "w2")}
    m["b2"] = float(mm["b2"])
    return m, tuple(d["calib"]), d.get("meta", {})


def main():
    ap = argparse.ArgumentParser(description="경계 자동 찾기 점수 모델 재학습(자세한 순서는 파일 첫머리 설명 참고).",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", action="append", default=[], metavar="백업.json::작업장",
                    help="정답 세트(백업 JSON 과 그 02_작업장). 여러 번 줄 수 있다")
    ap.add_argument("--out", metavar="weights.json", help="학습 결과 저장 경로")
    ap.add_argument("--apply", action="store_true", help="auto_detect_screen.py 의 가중치 블록을 새 값으로 교체")
    ap.add_argument("--from-weights", metavar="weights.json", help="학습 없이 저장된 weights.json 을 읽어 --apply 만 수행")
    ap.add_argument("--cache", metavar="DIR", help="특징 추출 결과 캐시 폴더(선택)")
    ap.add_argument("--jobs", type=int, default=None, help="병렬 프로세스 수(기본: 코어 수)")
    ap.add_argument("--no-cv", action="store_true", help="교차검증을 건너뜀(conf 는 학습 사진으로 맞춰 낙관적이 된다)")
    ap.add_argument("--verbose", action="store_true", help="교차검증 폴더별 점수도 출력")
    ap.add_argument("--target", default=os.path.join(HERE, "auto_detect_screen.py"), help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.from_weights:
        with open(args.from_weights, encoding="utf-8") as f:
            m, calib, meta = weights_from_json(json.load(f))
        if not args.apply:
            print("--from-weights 는 --apply 와 함께 씁니다(학습은 하지 않음).")
            return 2
        apply_block(args.target, render_block(m, calib, meta.get("n_images", "?")))
        print(f"적용: {args.target}")
        return 0
    if not args.set:
        ap.error("--set <백업.json>::<작업장> 이 하나 이상 필요합니다")
    if not args.out and not args.apply:
        ap.error("--out 이나 --apply 중 하나는 있어야 결과가 남습니다")

    t_all = time.perf_counter()
    items = collect(args.set)
    print(f"학습 사진 {len(items)}장, 병렬 {args.jobs or 'auto'}")

    t = time.perf_counter()
    print("[1/4] 후보·특징 추출")
    built = build_all(items, args.jobs, args.cache)
    n_no = sum(b is None for b in built)
    print(f"  완료 {time.perf_counter() - t:.0f}s" + (f" (후보를 못 찾은 사진 {n_no}장 제외)" if n_no else ""))

    picks = []
    if not args.no_cv:
        print("[2/4] 폴더 단위 2분할 교차검증")
        cv_res = {}
        for p in (0, 1):
            tr_sel = [i for i, it in enumerate(items) if it["par"] == p]
            te_sel = [i for i, it in enumerate(items) if it["par"] != p]
            if not tr_sel or not te_sel:
                print(f"  묶음 {p}: 폴더가 부족해 건너뜀")
                continue
            m, _ = train_on(items, built, tr_sel, args.jobs)
            res = evaluate(items, te_sel, m, args.jobs)
            report(f"  묶음 {p} 로 학습 → 묶음 {1 - p} 로 평가(학습에 안 쓴 사진)", items, te_sel, res, args.verbose)
            cv_res[p] = (te_sel, res)
            picks += [(r[2], r[1]) for r in res]
        if cv_res:
            sel_all = [i for p in sorted(cv_res) for i in cv_res[p][0]]
            res_all = [r for p in sorted(cv_res) for r in cv_res[p][1]]
            report("  교차검증 합계", items, sel_all, res_all, False)
    else:
        print("[2/4] 교차검증 건너뜀")

    print("[3/4] 전체 학습")
    all_sel = list(range(len(items)))
    m, n_used = train_on(items, built, all_sel, args.jobs)
    res = evaluate(items, all_sel, m, args.jobs)
    report("  전체 학습(학습에 쓴 사진 — 낙관적인 값)", items, all_sel, res, False)

    print("[4/4] conf 캘리브레이션")
    if picks:
        src = "교차검증(학습에 안 쓴 사진)"
    else:
        picks = [(r[2], r[1]) for r in res]
        src = "학습 사진(낙관적 — 교차검증을 켜면 더 정확)"
    a, b, base = fit_calib(np.array([p[0] for p in picks]), np.array([p[1] for p in picks]))
    print(f"  calib=({a}, {b}) 표본 {len(picks)}개, 5% 이내 비율 {base:.3f} — 근거: {src}")

    meta = {"n_images": n_used, "n_sets": len(args.set), "hidden": HIDDEN, "epochs": EPOCHS, "seed": SEED,
            "elapsed_sec": round(time.perf_counter() - t_all, 1)}
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(weights_to_json(m, (a, b), meta), f, ensure_ascii=False)
        print(f"저장: {args.out}")
    if args.apply:
        apply_block(args.target, render_block(m, (a, b), n_used))
        print(f"적용: {args.target} (가중치 블록만 교체). 전후는 eval_auto_detect.py 로 비교하세요.")
    print(f"총 소요 {time.perf_counter() - t_all:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
