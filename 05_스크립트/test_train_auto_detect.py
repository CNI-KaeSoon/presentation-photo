#!/usr/bin/env python3
"""train_auto_detect.py 회귀 테스트(합성 사진만 쓴다, 수 초).

(1) 현재 가중치를 블록 형식으로 다시 쓰면 auto_detect_screen.py 와 글자 하나 다르지 않다(표식 블록 왕복).
(2) 표식이 없으면 파일을 건드리지 않고 실패한다.
(3) fit_mlp 는 같은 입력이면 같은 가중치를 낸다(seed 고정).
(4) 합성 사진 2폴더로 끝까지 돌려 weights.json 과 --apply 결과가 유효한지(다시 불러 detect_screen 이 동작) 본다.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import auto_detect_screen as ads  # noqa: E402
import train_auto_detect as tr  # noqa: E402


def current_weights():
    mu, sd, W1, b1, w2, b2 = ads._MODEL
    m = {"mu": np.asarray(mu), "sd": np.asarray(sd), "W1": np.asarray(W1).reshape(len(mu), -1),
         "b1": np.asarray(b1), "w2": np.asarray(w2), "b2": float(b2)}
    return m, tuple(ads._CALIB)


def test_block_roundtrip():
    src = (HERE / "auto_detect_screen.py").read_text(encoding="utf-8")
    assert src.count(tr.BEGIN) == 1 and src.count(tr.END) == 1, "가중치 표식이 정확히 하나씩 있어야 함"
    cur = re.search(re.escape(tr.BEGIN) + r".*?" + re.escape(tr.END), src, re.S).group(0)
    n = int(re.search(r"정답 (\d+)장", cur).group(1))
    m, calib = current_weights()
    assert tr.render_block(m, calib, n) == cur, "블록 재생성이 현재 블록과 다름"
    print("block roundtrip ok")


def test_apply_requires_markers():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.py"
        p.write_text("_MODEL = None\n", encoding="utf-8")
        try:
            tr.apply_block(str(p), "x")
        except SystemExit:
            pass
        else:
            raise AssertionError("표식 없는 파일에 적용됨")
        assert p.read_text(encoding="utf-8") == "_MODEL = None\n"
    print("apply guard ok")


def test_fit_deterministic():
    rng = np.random.default_rng(3)
    F = rng.standard_normal((500, 8)).astype(np.float32)
    y = (F[:, 0] + 0.3 * F[:, 1] > 0).astype(float)
    a = tr.fit_mlp(F, y, epochs=300)
    b = tr.fit_mlp(F, y, epochs=300)
    assert all(np.array_equal(np.asarray(a[k]), np.asarray(b[k])) for k in a)
    z = tr.score(F, a)
    assert np.mean((z > 0) == (y > 0.5)) > 0.8
    ca, cb, base = tr.fit_calib(z, np.where(y > 0.5, 0.01, 0.2))
    assert ca > 0 and 0.0 < base < 1.0
    print("fit deterministic ok")


def synth(seed, w=960, h=720):
    rng = np.random.default_rng(seed)
    img = np.full((h, w, 3), 25, np.uint8)
    j = rng.uniform(-0.03, 0.03, 8)
    quad = np.array([[0.15 + j[0], 0.18 + j[1]], [0.85 + j[2], 0.14 + j[3]],
                     [0.12 + j[4], 0.74 + j[5]], [0.88 + j[6], 0.70 + j[7]]])
    poly = (quad[[0, 1, 3, 2]] * [w, h]).astype(np.int32)
    cv2.fillConvexPoly(img, poly, (235, 235, 235))
    for i in range(5):
        y = int((0.28 + 0.07 * i) * h)
        cv2.line(img, (int(0.22 * w), y), (int(0.7 * w), y), (40, 40, 40), 6)
    return img, quad.tolist()


def test_end_to_end():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        truth = {}
        for fi, folder in enumerate(("A", "B")):
            (d / "ws" / folder / "img").mkdir(parents=True)
            for k in range(3):
                img, quad = synth(fi * 10 + k)
                cv2.imwrite(str(d / "ws" / folder / "img" / f"p{k}.jpg"), img)
                truth[f"../{folder}/img/p{k}.jpg"] = quad
        (d / "b.json").write_text(json.dumps(
            {"_type": "slide_tool_backup", "data": {"slideCorners_v1": json.dumps(truth)}}), encoding="utf-8")
        # 실제 파일은 건드리지 않도록 사본에 적용한다.
        target = d / "auto_detect_screen.py"
        target.write_text((HERE / "auto_detect_screen.py").read_text(encoding="utf-8"), encoding="utf-8")
        cmd = [sys.executable, str(HERE / "train_auto_detect.py"), "--set", f"{d / 'b.json'}::{d / 'ws'}",
               "--out", str(d / "w.json"), "--cache", str(d / "cache"), "--apply", "--jobs", "2",
               "--target", str(target)]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "교차검증 합계" in r.stdout and "calib=" in r.stdout, r.stdout
        w = json.loads((d / "w.json").read_text(encoding="utf-8"))
        assert len(w["model"]["mu"]) == 65 and len(w["calib"]) == 2 and w["meta"]["n_images"] == 6
        new = target.read_text(encoding="utf-8")
        assert new != (HERE / "auto_detect_screen.py").read_text(encoding="utf-8")
        # 블록 밖은 그대로다.
        strip = lambda s: re.sub(re.escape(tr.BEGIN) + r".*?" + re.escape(tr.END), "", s, flags=re.S)
        assert strip(new) == strip((HERE / "auto_detect_screen.py").read_text(encoding="utf-8"))
        # 캐시가 있으면 두 번째 실행도 같은 가중치를 낸다.
        r2 = subprocess.run(cmd[:cmd.index("--apply")] + ["--jobs", "2"], capture_output=True, text=True,
                            encoding="utf-8")
        assert r2.returncode == 0 and "캐시 적중 6/6" in r2.stdout, r2.stdout + r2.stderr
        w2 = json.loads((d / "w.json").read_text(encoding="utf-8"))
        assert w2["model"] == w["model"] and w2["calib"] == w["calib"]
        # 적용한 사본이 실제로 불러지고 동작한다.
        spec = __import__("importlib.util").util.spec_from_file_location("ads_copy", target)
        mod = __import__("importlib.util").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        img, _ = synth(99)
        q, c = mod.detect_screen(img)
        assert q is None or (len(q) == 4 and 0.0 <= c <= 1.0)
    print("end-to-end ok")


def main() -> int:
    test_block_roundtrip()
    test_apply_requires_markers()
    test_fit_deterministic()
    test_end_to_end()
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
