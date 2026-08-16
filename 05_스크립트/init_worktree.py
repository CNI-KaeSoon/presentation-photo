#!/usr/bin/env python3
"""[섹터 1] 워크트리 만들기 — 사진을 어떤 그룹으로 나눌지 정한다.

그룹 = 폴더 = 최종 PDF 한 권. 발표자별·세션별·과목별 등 나누고 싶은 기준대로 만든다.

이 스크립트는 **계획만 세운다.** 사진은 건드리지 않는다 —
실제 배치는 [섹터 2] `prepare_photos.py --plan worktree.json` 이 한다.
계획 파일(worktree.json)은 그냥 텍스트라 손으로 고쳐도 된다.

나누는 방법 네 가지
───────────────────────────────────────────────────────────────────────
  --by-gap N       촬영 간격이 N분 이상 벌어지면 새 그룹  ★ 기본·추천
                   쉬는 시간마다 자연히 끊긴다. 일정표가 없어도 된다.

  --schedule FILE  일정표대로 배정. 시간표가 있을 때 가장 정확하다.
                   파일 형식(한 줄에 하나, `#` 는 주석):
                       09:00  1교시 반도체 개론
                       10:30  2교시 패키징 공정
                       13:00  3교시 실습

  --groups A B C   이름만 만들어 두고 사진 배정은 나중에 손으로.

  --by-subfolder   이미 폴더로 나눠 둔 경우 그 구조를 그대로.

사용 예:
  python3 05_스크립트/init_worktree.py --src 01_원본사진 --out 02_작업장
  python3 05_스크립트/init_worktree.py --src 01_원본사진 --out 02_작업장 --by-gap 30
  python3 05_스크립트/init_worktree.py --src 01_원본사진 --out 02_작업장 --schedule 일정.txt
  python3 05_스크립트/init_worktree.py --out 02_작업장 --groups 오전 오후
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import PurePosixPath

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from photo_io import backend_report, nfc, scan_photos  # noqa: E402

# Windows 콘솔 기본 인코딩(cp949)에는 '—'·'·'·'⚠' 같은 문자가 없어, 그대로 print 하면
# UnicodeEncodeError 로 스크립트가 죽는다(실측: init_worktree 가 U+2014 에서 중단).
# 출력 스트림을 UTF-8 로 고정해 어떤 콘솔에서도 깨지거나 죽지 않게 한다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 파이프·구버전 등 재설정 불가 시 무시
        pass

BAD_CHARS = ("#", "?", "%", "/", "\\", ":")


def portable_path(path, plan_dir):
    """계획 파일 위치를 기준으로 경로를 저장하고 저장 방식을 함께 돌려준다."""
    if path is None:
        return None, "none"
    absolute = os.path.abspath(path)
    try:
        relative = os.path.relpath(absolute, plan_dir)
    except ValueError:
        # Windows 에서 드라이브가 다르면 상대경로를 만들 수 없다.
        return PurePosixPath(absolute.replace("\\", "/")).as_posix(), "absolute"
    return PurePosixPath(relative.replace("\\", "/")).as_posix(), "relative"


def safe_name(s: str) -> str:
    """폴더명으로 쓸 수 있게 다듬는다. 도구가 경로를 URL 인코딩 없이 쓰므로 엄격하게."""
    s = nfc(s).strip()
    for c in BAD_CHARS:
        s = s.replace(c, "_")
    s = re.sub(r"\s+", "_", s)
    return s.strip("_. ") or "그룹"


def numbered(idx: int, name: str) -> str:
    """01_이름 — 번호를 앞에 붙여 발표 순서를 폴더 정렬로 유지한다."""
    return f"{idx:02d}_{name}"


# ---------------------------------------------------------------------------
# 방법 1: 촬영 간격으로 자동 분할
# ---------------------------------------------------------------------------
def split_by_gap(photos, gap_min):
    timed = [p for p in photos if p["when"]]
    if not timed:
        return None, "촬영시각을 읽을 수 있는 사진이 없다"
    groups, cur, prev = [], [], None
    for p in timed:
        if prev is not None and (p["when"] - prev) > timedelta(minutes=gap_min):
            groups.append(cur); cur = []
        cur.append(p); prev = p["when"]
    if cur:
        groups.append(cur)
    plan = {}
    for i, g in enumerate(groups, 1):
        label = numbered(i, g[0]["when"].strftime("%H%M"))
        plan[label] = g
    return plan, None


# ---------------------------------------------------------------------------
# 방법 2: 일정표
# ---------------------------------------------------------------------------
def parse_schedule(path):
    """`09:00  1교시 이름` 형식을 [(time, name)] 로. 날짜가 붙어도 받는다."""
    rows = []
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            m = re.match(r"^(?:(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s+)?"
                         r"(\d{1,2}):(\d{2})\s*[,\t ]\s*(.+)$", line)
            if not m:
                raise ValueError(f"{path}:{lineno} 형식을 못 읽음 → {raw.strip()!r}\n"
                                 f"  기대 형식:  09:00  과목이름")
            y, mo, d, hh, mm, name = m.groups()
            rows.append(dict(y=y and int(y), mo=mo and int(mo), d=d and int(d),
                             hh=int(hh), mm=int(mm), name=name.strip()))
    if not rows:
        raise ValueError(f"{path} 에 유효한 줄이 없다")
    return rows


def split_by_schedule(photos, sched_rows):
    timed = [p for p in photos if p["when"]]
    if not timed:
        return None, "촬영시각을 읽을 수 있는 사진이 없다"
    base = timed[0]["when"].date()

    starts = []
    for r in sched_rows:
        day = (datetime(r["y"], r["mo"], r["d"]).date()
               if r["y"] else base)
        starts.append((datetime.combine(day, datetime.min.time())
                       + timedelta(hours=r["hh"], minutes=r["mm"]), r["name"]))
    starts.sort(key=lambda t: t[0])

    plan = {numbered(i, safe_name(n)): [] for i, (_, n) in enumerate(starts, 1)}
    labels = list(plan)
    leftover = []
    for p in timed:
        idx = None
        for i, (st, _) in enumerate(starts):
            if p["when"] >= st:
                idx = i
            else:
                break
        if idx is None:                       # 첫 일정보다 이른 사진
            leftover.append(p)
        else:
            plan[labels[idx]].append(p)
    if leftover:
        plan = {"00_일정전": leftover, **plan}
    return plan, None


# ---------------------------------------------------------------------------
# 방법 4: 기존 하위 폴더
# ---------------------------------------------------------------------------
def split_by_subfolder(src, photos):
    plan = {}
    for p in photos:
        rel = os.path.relpath(os.path.dirname(p["path"]), src)
        label = safe_name(rel) if rel not in (".", "") else "기타"
        plan.setdefault(label, []).append(p)
    return plan, None


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="워크트리를 만들 곳 (보통 tool)")
    ap.add_argument("--src", help="원본 사진 폴더 (--groups 만 쓸 땐 생략 가능)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--by-gap", type=float, metavar="분",
                   help="촬영 간격이 이 값(분) 이상이면 새 그룹 (기본 20)")
    g.add_argument("--schedule", metavar="파일", help="일정표대로 배정")
    g.add_argument("--groups", nargs="+", metavar="이름", help="이름만 지정")
    g.add_argument("--by-subfolder", action="store_true", help="기존 하위폴더 구조 유지")
    ap.add_argument("--plan", default=None,
                    help="계획 파일 경로 (기본 <out>/worktree.json)")
    ap.add_argument("--force", action="store_true", help="기존 계획 파일 덮어쓰기")
    a = ap.parse_args()

    plan_path = a.plan or os.path.join(a.out, "worktree.json")
    if os.path.exists(plan_path) and not a.force:
        print(f"‼️ 계획 파일이 이미 있다: {plan_path}\n"
              f"   덮어쓰려면 --force. (사진을 이미 넣었다면 그룹 이름을 바꾸는 순간\n"
              f"    도구에 저장된 작업이 어긋나므로 신중히.)", file=sys.stderr)
        return 1

    # ---- 이름만 만드는 경우 ----
    if a.groups:
        labels = [numbered(i, safe_name(n)) for i, n in enumerate(a.groups, 1)]
        plan_out = {lab: [] for lab in labels}
        assigned = {}
    else:
        if not a.src:
            print("‼️ --src 가 필요하다 (또는 --groups 로 이름만 지정).", file=sys.stderr)
            return 1
        if not os.path.isdir(a.src):
            print(f"‼️ --src 폴더가 없다: {a.src}", file=sys.stderr)
            return 1

        print(backend_report())
        photos = scan_photos(a.src)
        if not photos:
            print(f"‼️ 이미지가 없다: {a.src}", file=sys.stderr)
            return 1
        n_timed = sum(1 for p in photos if p["when"])
        srcs = {}
        for p in photos:
            srcs[p["when_src"]] = srcs.get(p["when_src"], 0) + 1
        print(f"사진 {len(photos)}장 · 촬영시각 확보 {n_timed}장 "
              f"({', '.join(f'{k} {v}' for k, v in sorted(srcs.items()))})")

        if a.by_subfolder:
            assigned, err = split_by_subfolder(a.src, photos)
        elif a.schedule:
            try:
                rows = parse_schedule(a.schedule)
            except (OSError, ValueError) as exc:
                print(f"‼️ {exc}", file=sys.stderr)
                return 1
            print(f"일정표 {len(rows)}건")
            assigned, err = split_by_schedule(photos, rows)
        else:
            gap = a.by_gap if a.by_gap is not None else 20.0
            print(f"촬영 간격 {gap:g}분 이상에서 분할")
            assigned, err = split_by_gap(photos, gap)
        if err:
            print(f"‼️ {err}", file=sys.stderr)
            return 1
        plan_out = assigned

    # ---- 미리보기 ----
    print("\n=== 워크트리 계획 ===")
    total = 0
    for label, items in plan_out.items():
        total += len(items)
        if items and items[0]["when"]:
            t0 = items[0]["when"].strftime("%m-%d %H:%M")
            t1 = items[-1]["when"].strftime("%H:%M")
            span = f"  {t0} ~ {t1}"
        else:
            span = ""
        print(f"  {label:34s} {len(items):4d}장{span}")
    print(f"  {'합계':34s} {total:4d}장")

    # ---- 폴더 + 계획 파일 ----
    for label in plan_out:
        os.makedirs(os.path.join(a.out, label, "img"), exist_ok=True)

    plan_dir = os.path.dirname(os.path.abspath(plan_path))
    stored_root, root_mode = portable_path(a.out, plan_dir)
    stored_source, source_mode = portable_path(a.src, plan_dir)
    doc = {
        "_type": "slide_tool_worktree",
        "_version": 2,
        "_path_mode": {"root": root_mode, "source": source_mode},
        "root": stored_root,
        "source": stored_source,
        "groups": {label: [p["name"] for p in items]
                   for label, items in plan_out.items()},
    }
    if "absolute" in (root_mode, source_mode):
        print("⚠️ 다른 드라이브 경로는 상대화할 수 없어 절대경로로 기록했다.",
              file=sys.stderr)
    os.makedirs(plan_dir, exist_ok=True)
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)

    print(f"\n계획 파일 → {plan_path}")
    print("폴더 생성 완료.")
    print("\n그룹 이름을 바꾸고 싶으면 **지금** 바꿔라 — 계획 파일의 그룹명을 고치고")
    print("폴더도 같은 이름으로 바꾸면 된다. 사진을 넣고 도구로 작업한 뒤에는 바꾸지 말 것.")
    print(f"\n다음 단계 [섹터 2]:")
    print(f"  python3 05_스크립트/prepare_photos.py --plan {plan_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
