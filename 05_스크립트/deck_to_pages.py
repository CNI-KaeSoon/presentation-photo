#!/usr/bin/env python3
"""발표자료 PDF 를 쪽 이미지(DECK<번호>_p<쪽>.jpg)로 바꿔 발표에 넣는다.

이 도구는 발표자료 쪽을 `DECK<번호>_p<쪽>.jpg` 이름으로 알아본다. 화면에서는 `자료` 배지가 붙고
위치가 고정되며, PDF 를 만들 때 경계·색보정 없이 원본 그대로 들어간다. 이 스크립트가 PDF 의
쪽마다 그림을 만들고 발표 계획(worktree.json)에 쪽을 더해 준다.

두 곳에 저장한다 (원본 보존 원칙 — 원본 해상도는 01_원본사진 쪽에 둔다):

    01_원본사진/발표자료/<발표>/DECK03_p001.jpg   ← 원본 해상도 쪽 그림 (준비 단계가 여기서 찾는다)
    01_원본사진/발표자료/<발표>/DECK03_원본.pdf    ← 받은 PDF 사본
    02_작업장/<발표>/img/DECK03_p001.jpg          ← 도구가 읽는 작업용 그림 (PDF 만들 때도 이것을 쓴다)

쪽 이름은 worktree.json 의 해당 발표 목록 맨 앞에 쪽 순서대로 들어간다. 사진 준비의
`그대로 준비`·`다시 나누기`·원본 변경 감지(계획 불일치)와 충돌하지 않는다.

같은 번호의 쪽이 이미 있으면 기본은 거부한다. `--replace` 를 주면 기존 쪽(원본 해상도·작업용·PDF 사본)을
`02_작업장/<발표>/_이전발표자료_<날짜_시각>/` 으로 옮겨 두고 새로 넣는다. 삭제하지 않는다.

사용 예:
  python3 05_스크립트/deck_to_pages.py --pdf 발표자료.pdf --group 03_반도체_개론
  python3 05_스크립트/deck_to_pages.py --pdf 발표자료.pdf --group 03_반도체_개론 --replace
  python3 05_스크립트/deck_to_pages.py --pdf 발표자료.pdf --group 03_반도체_개론 --deck-no 7 --width 3000

번호 기본값은 발표 폴더 이름 앞의 숫자다(`03_…` → DECK03). 숫자가 없으면 아직 안 쓴 가장 작은 번호를 쓴다.
쪽을 넣은 뒤 화면에 반영하려면 목록을 다시 만든다:  python3 02_작업장/slide_tool/gen_manifest.py

PDF 를 그림으로 바꾸는 데 pypdfium2 가 필요하다(선택 의존성):  pip install pypdfium2
종료 코드: 0 성공 · 1 오류 · 2 pypdfium2 없음. 끝에 기계용 한 줄(@@DECK_RESULT@@ {json})을 찍는다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import signal
import sys
import uuid
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from photo_io import nfc  # noqa: E402

# Windows 콘솔 기본 인코딩(cp949)에는 '—'·'·' 같은 문자가 없어 그대로 print 하면 죽는다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

RESULT_PREFIX = "@@DECK_RESULT@@ "          # 서버가 잡 결과로 읽는 기계용 한 줄(화면 로그에는 안 나옴)
DECK_NAME_RE = re.compile(r"^DECK(\d*)_p(\d+)\.[a-z]+$", re.IGNORECASE)   # export_pdf.DECK_RE 와 같은 규약
DECK_DIR = "발표자료"                        # 01_원본사진 아래 발표자료 전용 폴더
ARCHIVE_PREFIX = "_이전발표자료_"             # 교체 때 기존 쪽을 옮겨 두는 폴더(발표 폴더 안)
QUALITY = 92
DEFAULT_LONG_SIDE = 2400                    # 긴 변 px — PDF 만들기(원본 해상도)에 충분하고 파일이 과하지 않은 크기
MIN_LONG_SIDE = 200
MAX_LONG_SIDE = 8000
MAX_PIXELS = 80_000_000
MAX_PAGES = 1000


class DeckError(Exception):
    """사용자에게 그대로 보여 줄 수 있는 실패. code 는 서버가 HTTP 상태로 옮기는 분류다."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def emit(ok: bool, code: str, detail: str = "", **extra: object) -> None:
    body = {"ok": ok, "code": code, "detail": detail}
    body.update(extra)
    print(RESULT_PREFIX + json.dumps(body, ensure_ascii=False), flush=True)


def is_single_basename(value: object) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    if value in {".", ".."} or os.path.isabs(value):
        return False
    if "/" in value or "\\" in value or re.match(r"^[A-Za-z]:", value):
        return False
    return Path(value).name == value


def deck_no_of(name: str) -> Optional[int]:
    """DECK 쪽 이름이면 번호(DECK_p1 처럼 번호가 없으면 -1), 아니면 None."""
    m = DECK_NAME_RE.match(nfc(str(name)))
    if not m:
        return None
    return int(m.group(1)) if m.group(1) else -1


def natural_key(value: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", value)]


# ---------------------------------------------------------------------------
# 패키지 위치·계획 읽기
# ---------------------------------------------------------------------------
class Layout:
    """패키지 안의 위치들. 준비 단계(prepare_photos.from_plan)가 해석하는 곳과 같은 곳을 쓴다."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.work_default = root / "02_작업장"
        self.plan_path = self.work_default / "worktree.json"
        if not self.plan_path.is_file():
            raise DeckError(
                "no_plan",
                "발표 나누기 계획(02_작업장/worktree.json)이 없습니다. 먼저 사진을 넣고 사진 준비를 하세요.")
        try:
            self.doc = json.loads(self.plan_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DeckError("no_plan", f"worktree.json 을 읽지 못했습니다: {exc}")
        if not isinstance(self.doc, dict) or self.doc.get("_type") != "slide_tool_worktree" \
                or not isinstance(self.doc.get("groups"), dict):
            raise DeckError("no_plan", "worktree.json 이 발표 나누기 계획 형식이 아닙니다.")
        self.work, self.src = self._resolve()

    def _resolve(self) -> tuple[Path, Path]:
        """계획의 root(작업장)·source(원본) 경로. 못 찾으면 패키지 기본 폴더를 쓴다."""
        import prepare_photos as pp

        plan_dir = str(self.plan_path.parent.resolve())
        work = src = None
        try:
            if int(self.doc.get("_version", 1)) >= 2:
                mode = self.doc.get("_path_mode") if isinstance(self.doc.get("_path_mode"), dict) else {}
                work = pp._v2_path(self.doc.get("root", "."), plan_dir, mode.get("root"))
                raw = self.doc.get("source")
                src = pp._v2_path(raw, plan_dir, mode.get("source")) if raw else None
            else:
                work, src = pp._resolve_v1_paths(self.doc, plan_dir)
        except (ValueError, OSError):
            work = src = None
        work_path = Path(work) if work and os.path.isdir(work) else self.work_default.resolve()
        src_path = Path(src) if src and os.path.isdir(src) else (self.root / "01_원본사진").resolve()
        return work_path, src_path

    def groups(self) -> dict:
        return self.doc["groups"]

    def find_group(self, group: str) -> str:
        """계획에 있는 그룹 이름(저장된 철자 그대로)."""
        matches = [g for g in self.groups() if nfc(str(g)) == group]
        if len(matches) != 1:
            raise DeckError("group_not_found", f"계획에 «{group}» 발표가 없습니다. 발표 이름을 확인하세요.")
        return matches[0]

    def deck_src_dir(self, group: str) -> Path:
        return self.src / DECK_DIR / group

    def work_img(self, group: str) -> Path:
        return self.work / group / "img"

    def save_plan(self) -> None:
        temp = self.plan_path.with_name(f".{self.plan_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(self.doc, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.plan_path)
        finally:
            if temp.exists():
                temp.unlink()


def list_deck_files(folder: Path) -> list[Path]:
    """폴더 안의 DECK 쪽 그림(숨김 파일 제외)."""
    try:
        return sorted((p for p in folder.iterdir()
                       if p.is_file() and not p.name.startswith(".") and deck_no_of(p.name) is not None),
                      key=lambda p: natural_key(p.name))
    except OSError:
        return []


def scan_decks(layout: Layout, group: str) -> tuple[dict[int, set[str]], dict[int, set[str]]]:
    """DECK 번호 → 이름 묶음. (대상 발표의 것, 다른 발표의 것)."""
    mine: dict[int, set[str]] = {}
    others: dict[int, set[str]] = {}

    def add(bucket: dict, name: str) -> None:
        no = deck_no_of(name)
        if no is not None:
            bucket.setdefault(no, set()).add(nfc(name))

    for grp, names in layout.groups().items():
        bucket = mine if nfc(str(grp)) == group else others
        for name in names if isinstance(names, list) else []:
            add(bucket, str(name))
    try:
        work_groups = [p for p in layout.work.iterdir()
                       if p.is_dir() and not p.name.startswith(("_", ".")) and (p / "img").is_dir()]
    except OSError:
        work_groups = []
    for folder in work_groups:
        bucket = mine if nfc(folder.name) == group else others
        for p in list_deck_files(folder / "img"):
            add(bucket, p.name)
    deck_root = layout.src / DECK_DIR
    try:
        deck_dirs = [p for p in deck_root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))]
    except OSError:
        deck_dirs = []
    for folder in deck_dirs:
        bucket = mine if nfc(folder.name) == group else others
        for p in list_deck_files(folder):
            add(bucket, p.name)
    return mine, others


def free_deck_no(mine: dict, others: dict) -> int:
    used = {n for n in (*mine, *others) if n >= 0}
    n = 1
    while n in used:
        n += 1
    return n


def default_deck_no(group: str, mine: dict, others: dict) -> int:
    m = re.match(r"^(\d+)", group)
    if m:
        return int(m.group(1))
    return free_deck_no(mine, others)


# ---------------------------------------------------------------------------
# PDF 읽기·쪽 그림 만들기
# ---------------------------------------------------------------------------
def import_pdfium():
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise DeckError(
            "pdfium_missing",
            "발표자료 PDF 를 그림으로 바꾸려면 pypdfium2 가 필요합니다.  설치:  pip install pypdfium2  "
            f"({exc})") from exc
    return pdfium


def open_pdf(pdfium, path: Path):
    """(문서, 열린 파일). 문서를 닫을 때 파일도 닫아야 한다."""
    try:
        handle = open(path, "rb")
    except OSError as exc:
        raise DeckError("bad_pdf", f"PDF 파일을 열지 못했습니다: {exc}") from exc
    try:
        doc = pdfium.PdfDocument(handle)
    except Exception as exc:  # PdfiumError 는 종류가 하나라 메시지로 나눈다
        handle.close()
        text = str(exc)
        if "password" in text.lower():
            raise DeckError("encrypted", "암호가 걸린 PDF 입니다. 암호를 풀어 저장한 파일을 넣으세요.") from exc
        raise DeckError("bad_pdf", f"PDF 를 읽지 못했습니다(손상됐거나 PDF 가 아닐 수 있습니다): {text}") from exc
    try:
        count = len(doc)
    except Exception as exc:
        doc.close()
        handle.close()
        raise DeckError("bad_pdf", f"PDF 쪽 수를 읽지 못했습니다: {exc}") from exc
    if count < 1:
        doc.close()
        handle.close()
        raise DeckError("no_pages", "PDF 에 쪽이 없습니다(0쪽).")
    if count > MAX_PAGES:
        doc.close()
        handle.close()
        raise DeckError("too_many_pages", f"쪽이 너무 많습니다({count}쪽, 한 번에 {MAX_PAGES}쪽까지).")
    return doc, handle


def render_scale(page_size: tuple[float, float], long_side: Optional[int], dpi: Optional[float]) -> float:
    width, height = page_size
    longest = max(width, height, 1.0)
    if dpi:
        scale = dpi / 72.0
    else:
        scale = float(long_side or DEFAULT_LONG_SIDE) / longest
    scale = min(scale, MAX_LONG_SIDE / longest)                 # 긴 변 상한
    scale = min(scale, (MAX_PIXELS / max(width * height, 1.0)) ** 0.5)   # 화소 상한
    return max(scale, MIN_LONG_SIDE / longest)


def render_pages(doc, dest: Path, names: list[str], long_side: Optional[int], dpi: Optional[float]) -> None:
    """쪽마다 렌더해 dest 에 숨김 이름(.<이름>)으로 저장한다. 한 번에 한 쪽만 메모리에 둔다."""
    total = len(names)
    for index, name in enumerate(names):
        page = doc[index]
        try:
            scale = render_scale(page.get_size(), long_side, dpi)
            bitmap = page.render(scale=scale)          # 배경은 흰색 — 투명한 쪽도 검게 나오지 않는다
            image = bitmap.to_pil().convert("RGB")
        except Exception as exc:
            raise DeckError("bad_pdf", f"{index + 1}쪽을 그림으로 바꾸지 못했습니다: {exc}") from exc
        finally:
            page.close()
        try:
            image.save(dest / f".{name}", "JPEG", quality=QUALITY, optimize=True)
        except OSError as exc:
            raise DeckError("io_error", f"{name} 을 저장하지 못했습니다: {exc}") from exc
        finally:
            image.close()
        print(f"쪽 {index + 1}/{total}", flush=True)


# ---------------------------------------------------------------------------
# 기존 쪽 보관(교체)
# ---------------------------------------------------------------------------
def unique_dir(parent: Path, name: str) -> Path:
    for index in range(1, 1000):
        candidate = parent / (name if index == 1 else f"{name}_{index}")
        try:
            candidate.mkdir(parents=True)
            return candidate
        except FileExistsError:
            continue
    raise DeckError("io_error", "보관 폴더 이름을 정할 수 없습니다.")


def archive_existing(layout: Layout, group: str, deck_no: int, existing: set[str]) -> Optional[Path]:
    """기존 쪽(원본 해상도·작업용·PDF 사본)을 발표 폴더 안 보관 폴더로 옮긴다. 옮긴 것이 없으면 None."""
    stamp = dt.datetime.now().strftime("%y%m%d_%H%M")
    archive: Optional[Path] = None
    moved: list[tuple[Path, Path]] = []                 # (옮긴 뒤, 원래)

    def move(src: Path, kind: str) -> None:
        nonlocal archive
        if archive is None:
            archive = unique_dir(layout.work / group, f"{ARCHIVE_PREFIX}{stamp}")
        target_dir = archive / kind
        target_dir.mkdir(exist_ok=True)
        target = target_dir / src.name
        os.replace(src, target)
        moved.append((target, src))

    try:
        deck_dir = layout.deck_src_dir(group)
        img_dir = layout.work_img(group)
        names = {nfc(n) for n in existing}
        # 원본 해상도: 발표자료 전용 폴더 + 이름이 같은 다른 위치의 파일(예전에 손으로 넣은 것)
        for path in list_deck_files(deck_dir):
            if nfc(path.name) in names:
                move(path, "원본")
        pdf_copy = deck_dir / pdf_copy_name(deck_no)
        if pdf_copy.is_file():
            move(pdf_copy, "원본")
        left = {n for n in names if not (archive is not None and (archive / "원본" / n).exists())}
        if left:
            for root, dirs, files in os.walk(layout.src):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for filename in files:
                    if nfc(filename) in left and deck_no_of(filename) == deck_no:
                        move(Path(root) / filename, "원본")
        for path in list_deck_files(img_dir):
            if nfc(path.name) in names:
                move(path, "작업본")
    except (OSError, DeckError):
        for target, origin in reversed(moved):          # 실패하면 옮긴 것을 되돌린다
            try:
                if os.path.lexists(target) and not os.path.lexists(origin):
                    os.replace(target, origin)
            except OSError:
                pass
        raise
    return archive


def pdf_copy_name(deck_no: int) -> str:
    return f"DECK{deck_no:02d}_원본.pdf"


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", required=True, help="발표자료 PDF 파일")
    ap.add_argument("--group", required=True, help="넣을 발표 폴더 이름 (예: 03_반도체_개론)")
    ap.add_argument("--deck-no", type=int, default=None, metavar="N",
                    help="DECK 번호 (기본: 발표 폴더 이름 앞 숫자)")
    size = ap.add_mutually_exclusive_group()
    size.add_argument("--width", type=int, default=None, metavar="PX",
                      help=f"쪽 그림의 긴 변 픽셀 (기본 {DEFAULT_LONG_SIDE}, {MIN_LONG_SIDE}~{MAX_LONG_SIDE})")
    size.add_argument("--dpi", type=float, default=None, help="해상도(dpi)로 지정 — 예: 200")
    ap.add_argument("--root", default=None, help="패키지 루트 (기본: 이 스크립트의 상위 폴더)")
    ap.add_argument("--replace", action="store_true",
                    help="같은 번호의 쪽이 이미 있으면 보관 폴더로 옮기고 새로 넣는다")
    ap.add_argument("--info", action="store_true", help="쪽 수·기존 쪽만 알려 주고 아무것도 바꾸지 않는다")
    return ap


def run(a: argparse.Namespace) -> dict:
    if a.width is not None and not MIN_LONG_SIDE <= a.width <= MAX_LONG_SIDE:
        raise DeckError("bad_args", f"--width 는 {MIN_LONG_SIDE}~{MAX_LONG_SIDE} 범위여야 합니다: {a.width}")
    if a.dpi is not None and not 20 <= a.dpi <= 1200:
        raise DeckError("bad_args", f"--dpi 는 20~1200 범위여야 합니다: {a.dpi}")
    if a.deck_no is not None and not 0 <= a.deck_no <= 999:
        raise DeckError("bad_args", f"--deck-no 는 0~999 범위여야 합니다: {a.deck_no}")
    group = nfc(a.group)
    if not is_single_basename(group) or group.startswith(("_", ".")) or any(c in group for c in "#?%"):
        raise DeckError("bad_group", f"발표 이름이 올바르지 않습니다: {a.group!r}")
    pdf = Path(a.pdf).expanduser()
    if not pdf.is_file():
        raise DeckError("bad_pdf", f"PDF 파일이 없습니다: {a.pdf}")

    pdfium = import_pdfium()
    root = Path(a.root).expanduser().resolve() if a.root else Path(__file__).resolve().parent.parent
    layout = Layout(root)
    plan_group = layout.find_group(group)
    if not (layout.work / plan_group).is_dir():
        raise DeckError("group_not_found", f"작업장에 «{group}» 폴더가 없습니다.")

    mine, others = scan_decks(layout, group)
    deck_no = a.deck_no if a.deck_no is not None else default_deck_no(group, mine, others)
    if deck_no in others:
        raise DeckError(
            "number_in_use",
            f"DECK{deck_no:02d} 번호를 다른 발표가 이미 쓰고 있습니다. --deck-no 로 다른 번호를 지정하세요.")
    existing = mine.get(deck_no, set())

    doc, handle = open_pdf(pdfium, pdf)
    try:
        pages = len(doc)
        info = dict(group=group, deckNo=deck_no, pages=pages, existing=len(existing))
        if a.info:
            return dict(info, info=True)
        if existing and not a.replace:
            raise DeckError(
                "exists",
                f"이 발표에는 DECK{deck_no:02d} 쪽이 이미 {len(existing)}쪽 있습니다. "
                "바꾸려면 --replace 를 주세요(기존 쪽은 보관 폴더로 옮깁니다).")

        digits = max(3, len(str(pages)))
        names = [f"DECK{deck_no:02d}_p{i:0{digits}d}.jpg" for i in range(1, pages + 1)]
        deck_dir = layout.deck_src_dir(group)
        deck_dir.mkdir(parents=True, exist_ok=True)
        temp = deck_dir / f".render-{uuid.uuid4().hex}"
        temp.mkdir()
        try:
            print(f"{pages}쪽을 그림으로 바꾸는 중 — DECK{deck_no:02d} → {group}", flush=True)
            render_pages(doc, temp, names, a.width, a.dpi)
            # ---- 여기부터 실제 반영 (모든 쪽이 만들어진 뒤에만) ----
            archive = archive_existing(layout, group, deck_no, existing) if existing else None
            img_dir = layout.work_img(group)
            img_dir.mkdir(parents=True, exist_ok=True)
            for name in names:
                os.replace(temp / f".{name}", deck_dir / name)
                shutil.copyfile(deck_dir / name, img_dir / name)
            shutil.copyfile(pdf, deck_dir / pdf_copy_name(deck_no))
        finally:
            shutil.rmtree(temp, ignore_errors=True)
    finally:
        doc.close()
        handle.close()

    # ---- 계획에 쪽 추가 (교체면 기존 쪽이 있던 자리에 넣는다) ----
    listing = layout.groups()[plan_group]
    old = {nfc(n) for n in existing}
    position = next((i for i, n in enumerate(listing) if nfc(str(n)) in old), 0)
    kept_before = sum(1 for n in listing[:position] if nfc(str(n)) not in old)
    rest = [n for n in listing if nfc(str(n)) not in old]
    layout.doc["groups"][plan_group] = rest[:kept_before] + names + rest[kept_before:]
    layout.save_plan()

    def short(path: Path) -> str:      # 개인 폴더 경로가 화면 기록에 길게 남지 않게 패키지 기준으로 적는다
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return str(path)

    print(f"완료 — DECK{deck_no:02d} {pages}쪽 (원본 해상도 {short(deck_dir)}, 작업용 {short(img_dir)})", flush=True)
    if archive is not None:
        print(f"기존 {len(existing)}쪽은 보관했습니다 → {short(archive)}", flush=True)
    print("화면에 반영하려면 목록을 다시 만드세요:  python3 02_작업장/slide_tool/gen_manifest.py", flush=True)
    return dict(info, replaced=len(existing),
                archive=archive.name if archive is not None else None,
                first=names[0], last=names[-1])


def main(argv: Optional[list[str]] = None) -> int:
    def _terminate(_signum, _frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGBREAK"):            # 서버가 작업을 취소하면 임시 폴더를 정리하고 끝낸다
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _terminate)
            except (OSError, ValueError):
                pass
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except DeckError as exc:
        print(f"‼️ {exc.detail}", file=sys.stderr, flush=True)
        emit(False, exc.code, exc.detail)
        return 2 if exc.code == "pdfium_missing" else 1
    except KeyboardInterrupt:
        print("‼️ 취소됐습니다.", file=sys.stderr, flush=True)
        emit(False, "cancelled", "취소됐습니다.")
        return 130
    emit(True, "ok", "", **result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
