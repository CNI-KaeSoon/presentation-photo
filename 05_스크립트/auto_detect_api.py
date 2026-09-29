#!/usr/bin/env python3
"""브라우저 도구의 '자동 찾기' 러너 — stdin JSON → stdout JSON.

serve_tool.py 는 OpenCV 가 없는 파이썬으로 돌 수 있어서, 검출은 이 스크립트를 venv 파이썬
하위 프로세스로 띄워 맡긴다(prepare/export 와 같은 방식). 경로 검증은 서버가 끝낸 뒤라
여기서는 받은 경로를 그대로 읽는다 — 사람이 직접 실행할 용도가 아니다.

  입력 : {"items": [{"path": "<절대경로>", "rot": 0~3}, ...]}
         rot = 브라우저에서 원본 화면을 시계방향 90° 돌린 횟수(코너 배열 순환에서 파생된 값).
  출력 : 마지막 줄에 {"results": [{"corners": [[x,y]x4] | null, "conf": 0~1, "error"?: "..."}, ...]}
         입력과 같은 순서. corners 는 **원본 사진 좌표**의 정규화 값이고 순서는 브라우저가 저장하는
         [TL, TR, BL, BR] 그대로다(화면에서 본 TL 이 배열 0번) — 곧바로 cornersStore 에 넣으면 된다.

회전 처리: 카메라를 눕혀 찍은 사진은 파일 안에서 스크린이 옆으로 누워 있다. 사용자가 이미 90° 돌려
세워 뒀다면(rot≠0) 그 방향으로 사진을 돌려 세운 뒤 검출하고, 결과를 다시 파일 좌표로 되돌린다.
toDisp/fromDisp 는 index.html 의 같은 이름 함수와 같은 식이다.
"""
from __future__ import annotations

import json
import math
import sys

import numpy as np

# Windows 콘솔 기본 인코딩(cp949)에는 '—'·'·'·'⚠' 같은 문자가 없어, 그대로 print 하면
# UnicodeEncodeError 로 스크립트가 죽는다(실측: init_worktree 가 U+2014 에서 중단).
# 출력 스트림을 UTF-8 로 고정해 어떤 콘솔에서도 깨지거나 죽지 않게 한다.
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 파이프·구버전 등 재설정 불가 시 무시
        pass

from auto_detect_screen import detect_screen  # noqa: E402  (05_스크립트 는 sys.path[0])
from photo_io import read_bgr  # noqa: E402


def from_disp(point, k):
    """화면(시계방향 k×90° 돌린 상자) 정규좌표 → 원본 사진 정규좌표. index.html fromDisp 와 동일."""
    x, y = point
    if k == 1:
        return [y, 1 - x]
    if k == 2:
        return [1 - x, 1 - y]
    if k == 3:
        return [1 - y, x]
    return [x, y]


def detect_one(path, rot):
    img = read_bgr(path)
    if img is None:
        return {"corners": None, "conf": 0.0, "error": "read_failed"}
    k = rot % 4
    if k:
        # np.rot90 은 반시계가 양수 — 시계방향 k 번은 -k.
        img = np.ascontiguousarray(np.rot90(img, -k))
    quad, conf = detect_screen(img)
    if quad is None:
        return {"corners": None, "conf": 0.0}
    corners = []
    for point in quad:
        x, y = from_disp([float(point[0]), float(point[1])], k)
        if not (math.isfinite(x) and math.isfinite(y)):
            return {"corners": None, "conf": 0.0, "error": "bad_quad"}
        corners.append([round(min(1.0, max(0.0, x)), 5), round(min(1.0, max(0.0, y)), 5)])
    conf = float(conf)
    if not math.isfinite(conf):
        conf = 0.0
    return {"corners": corners, "conf": round(min(1.0, max(0.0, conf)), 4)}


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        items = request["items"]
    except (ValueError, KeyError, TypeError):
        print(json.dumps({"error": "bad_input"}))
        return 2
    results = []
    for item in items:
        try:
            results.append(detect_one(str(item["path"]), int(item.get("rot", 0))))
        except Exception as exc:  # 한 장의 실패가 나머지를 막지 않게 한다.
            results.append({"corners": None, "conf": 0.0, "error": f"{type(exc).__name__}"})
    print(json.dumps({"results": results}, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
