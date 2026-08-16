#!/usr/bin/env python3
"""환경 준비부터 사진 처리, 도구 실행, PDF 생성까지 안내하는 단일 런처."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import webbrowser
from typing import Optional, Union

import envcheck


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".webp"
}


def package_root(value: Optional[Union[str, os.PathLike[str]]] = None) -> Path:
    if value is None:
        return Path(__file__).resolve().parent.parent
    return Path(value).expanduser().resolve()


def _status_path(pkg_root: Path) -> Path:
    return pkg_root / "00_시작" / "_env_status.json"


def load_status(pkg_root: Path) -> dict[str, object]:
    try:
        data = json.loads(_status_path(pkg_root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if not envcheck.status_venv_valid(pkg_root, data):
        data["ok"] = False
    return data


def env_ready(pkg_root: Path) -> bool:
    status = load_status(pkg_root)
    return bool(status.get("ok") and envcheck.status_venv_valid(pkg_root, status))


def ensure_env(pkg_root: Path) -> bool:
    return envcheck.interactive_setup(pkg_root)


def auto_flow(pkg_root: Path) -> int:
    """환경을 비대화형으로 준비한 뒤 브라우저 워크플로를 연다."""
    snapshot = envcheck.inspect_environment(pkg_root)
    if not snapshot.get("ok"):
        print("필수 환경을 자동으로 준비합니다.")
        if not envcheck.interactive_setup(pkg_root, assume_yes=True):
            print("필수 환경을 준비하지 못했습니다.", file=sys.stderr)
            return 1
    if not env_ready(pkg_root):
        print("필수 환경이 아직 준비되지 않았습니다.", file=sys.stderr)
        return 1
    print("브라우저 작업 화면을 엽니다.")
    return 0 if do_serve(pkg_root) else 1


def _input(prompt: str, default: str = "") -> Optional[str]:
    try:
        answer = input(prompt)
    except EOFError:
        return None
    answer = answer.strip()
    return answer if answer else default


def _run_step(command: list[str], title: str) -> bool:
    print(f"\n--- {title} ---")
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        print(f"실행하지 못했습니다: {exc}", file=sys.stderr)
        return False
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.stderr:
        print(proc.stderr.rstrip(), file=sys.stderr)
    if proc.returncode != 0:
        print(f"{title} 실패(exit {proc.returncode}). 다음 단계는 실행하지 않습니다.", file=sys.stderr)
        return False
    return True


def _has_images(folder: Path) -> bool:
    try:
        return any(
            path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
            for path in folder.rglob("*")
        )
    except OSError:
        return False


def do_prepare(pkg_root: Path, status: dict[str, object]) -> bool:
    src = pkg_root / "01_원본사진"
    work = pkg_root / "02_작업장"
    scripts = pkg_root / "05_스크립트"
    plan = work / "worktree.json"
    venv_py = envcheck.venv_python(pkg_root)
    if venv_py is None:
        print("가상환경을 찾을 수 없습니다. 먼저 [1] 환경 점검·설치를 실행하세요.")
        return False
    if not _has_images(src):
        print(f"사진이 없습니다. 먼저 이 폴더에 사진을 넣으세요: {src}")
        return False

    recreate = False
    if plan.is_file():
        choice = _input("기존 그룹 계획이 있습니다. [1] 그대로 쓰기  [2] 다시 나누기: ", "1")
        if choice is None:
            return False
        recreate = choice == "2"

    if not plan.is_file() or recreate:
        method = _input(
            "나누기 방식: [엔터] 촬영간격 20분 자동  [2] 일정표 파일  [3] 이름 직접: ",
            "1",
        )
        if method is None:
            return False
        command = [
            str(venv_py), str(scripts / "init_worktree.py"),
            "--src", str(src), "--out", str(work),
        ]
        if method == "2":
            schedule = _input("일정표 파일 경로: ")
            if schedule is None:
                return False
            schedule_path = Path(schedule).expanduser().resolve()
            if not schedule_path.is_file():
                print(f"일정표 파일이 없습니다: {schedule_path}")
                return False
            command.extend(["--schedule", str(schedule_path)])
        elif method == "3":
            raw_names = _input("그룹 이름을 공백으로 구분해 입력하세요(공백 포함 이름은 따옴표 사용): ")
            if raw_names is None:
                return False
            try:
                names = shlex.split(raw_names)
            except ValueError as exc:
                print(f"그룹 이름을 읽지 못했습니다: {exc}")
                return False
            if not names:
                print("그룹 이름이 없습니다.")
                return False
            command.extend(["--groups", *names])
        else:
            command.extend(["--by-gap", "20"])
        if recreate:
            command.append("--force")
        if not _run_step(command, "그룹 계획 만들기"):
            return False

    workers = max(1, int(status.get("workers") or 1))
    if not _run_step(
        [
            str(venv_py), str(scripts / "prepare_photos.py"),
            "--plan", str(plan), "--workers", str(workers),
        ],
        "사진 준비",
    ):
        return False
    if not _run_step(
        [str(venv_py), str(work / "slide_tool" / "gen_manifest.py")],
        "사진 목록 만들기",
    ):
        return False
    print("사진 준비가 끝났습니다. 메뉴 [3]에서 도구를 여세요.")
    return True


def do_serve(pkg_root: Path) -> bool:
    command = [
        sys.executable,
        str(pkg_root / "00_시작" / "serve_tool.py"),
        "--root",
        str(pkg_root / "02_작업장"),
    ]
    try:
        proc = subprocess.run(command, check=False)
    except OSError as exc:
        print(f"서버를 실행하지 못했습니다: {exc}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(f"서버 실행 실패(exit {proc.returncode}).", file=sys.stderr)
        return False
    print("서버를 내렸습니다. 백업 JSON 을 받았는지 확인하세요")
    return True


def find_latest_backup() -> Optional[Path]:
    downloads = Path.home() / "Downloads"
    try:
        candidates = [path for path in downloads.glob("slide_tool_backup_*.json") if path.is_file()]
    except OSError:
        candidates = []
    if candidates:
        return max(candidates, key=lambda path: path.stat().st_mtime)
    entered = _input("다운로드 폴더에서 백업 JSON을 찾지 못했습니다. 파일 경로를 입력하세요: ")
    if entered is None:
        return None
    path = Path(entered).expanduser().resolve()
    return path if path.is_file() else None


def _open_folder(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            webbrowser.open(path.resolve().as_uri())
    except OSError:
        pass


def do_export(pkg_root: Path, status: dict[str, object]) -> bool:
    backup = find_latest_backup()
    if backup is None:
        print("백업 JSON 파일을 찾지 못했습니다.")
        return False
    answer = _input(f"이 백업 파일이 맞습니까? {backup} [Y/n] ", "y")
    if answer is None or answer.lower() not in {"y", "yes", "1", "예", "네"}:
        entered = _input("사용할 백업 JSON 파일 경로: ")
        if entered is None:
            return False
        backup = Path(entered).expanduser().resolve()
        if not backup.is_file():
            print(f"백업 JSON 파일이 없습니다: {backup}")
            return False

    venv_py = envcheck.venv_python(pkg_root)
    if venv_py is None:
        print("가상환경을 찾을 수 없습니다. 먼저 [1] 환경 점검·설치를 실행하세요.")
        return False
    out = pkg_root / "03_결과물"
    out.mkdir(parents=True, exist_ok=True)
    workers = max(1, int(status.get("workers") or 1))
    ok = _run_step(
        [
            str(venv_py), str(pkg_root / "05_스크립트" / "export_pdf.py"),
            "--backup", str(backup),
            "--root", str(pkg_root / "02_작업장"),
            "--src-dir", str(pkg_root / "01_원본사진"),
            "--out", str(out),
            "--workers", str(workers),
        ],
        "PDF 만들기",
    )
    if ok:
        print(f"PDF 생성 완료: {out}")
        _open_folder(out)
    return ok


def render_menu(ready: bool, status: dict[str, object]) -> None:
    print("\n========================================")
    print(" 슬라이드 도구 시작")
    print("========================================")
    if ready:
        print(f" 환경: 준비됨 · 작업 워커 {status.get('workers', 1)}개")
    else:
        print(" 환경: 준비 필요 — 먼저 [1]을 실행하세요")
    print(" [1] 환경 점검·설치")
    lock = "" if ready else "  (잠김 — 환경 셋팅을 먼저 하세요)"
    print(f" [2] 사진 준비{lock}  (브라우저 화면에서도 가능)")
    print(f" [3] 도구 열기{lock}")
    print(f" [4] PDF 만들기{lock}  (브라우저 화면에서도 가능)")
    print(" [0] 종료")


def menu_loop(pkg_root: Path, smoke: bool = False) -> int:
    while True:
        ready = env_ready(pkg_root)
        status = load_status(pkg_root)
        render_menu(ready, status)
        if smoke:
            return 0
        choice = _input("선택: ")
        if choice is None:
            print("입력이 끝나 런처를 종료합니다.")
            return 0
        if choice == "0":
            print("종료합니다.")
            return 0
        if choice == "1":
            ensure_env(pkg_root)
            continue
        if choice in {"2", "3", "4"} and not ready:
            print("환경 셋팅을 먼저 해야 합니다. [1] 환경 점검·설치를 실행하세요.")
            continue
        if choice == "2":
            do_prepare(pkg_root, status)
        elif choice == "3":
            do_serve(pkg_root)
        elif choice == "4":
            do_export(pkg_root, status)
        else:
            print("0~4 중에서 선택하세요.")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auto", action="store_true", help="환경 준비 후 브라우저 워크플로 실행")
    parser.add_argument("--smoke", action="store_true", help="메뉴를 한 번 표시하고 종료")
    parser.add_argument("--pkg-root", default=None, metavar="DIR", help="배포 패키지 루트")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    root = package_root(args.pkg_root)
    if args.auto:
        return auto_flow(root)
    return menu_loop(root, smoke=args.smoke)


if __name__ == "__main__":
    raise SystemExit(main())
