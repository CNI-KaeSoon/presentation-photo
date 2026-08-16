#!/usr/bin/env python3
"""배포 패키지의 실행 환경을 점검하고 필요한 항목을 설치한다.

이 파일은 의존성 설치 전에도 실행되어야 하므로 Python 표준 라이브러리만 사용한다.
"""
from __future__ import annotations

import argparse
import ctypes
import dataclasses
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from typing import Optional, Union


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

PORT = 8770
MIN_PYTHON = (3, 9)
REQUIRED_PKGS = ("cv2", "numpy", "PIL")
OPTIONAL_PKGS = ("pillow_heif",)
PACKAGE_LABELS = {
    "cv2": "opencv-python",
    "numpy": "numpy",
    "PIL": "Pillow",
    "pillow_heif": "pillow-heif",
}


@dataclasses.dataclass
class CheckResult:
    name: str
    ok: bool
    required: bool
    detail: str
    fix_hint: str = ""

    def public(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "required": self.required,
            "detail": self.detail,
        }


def package_root(value: Optional[Union[str, os.PathLike[str]]] = None) -> Path:
    if value is None:
        return Path(__file__).resolve().parent.parent
    return Path(value).expanduser().resolve()


def venv_python(pkg_root: Path) -> Optional[Path]:
    candidates = (
        pkg_root / ".venv" / "Scripts" / "python.exe",
        pkg_root / ".venv" / "bin" / "python3",
        pkg_root / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            # venv 의 python3 는 시스템 인터프리터를 가리키는 symlink 일 수 있다.
            # resolve() 하면 venv 경로 정보가 사라져 pip 가 시스템 환경에 설치된다.
            return candidate.absolute()
    return None


def status_venv_valid(pkg_root: Path, status: dict[str, object]) -> bool:
    """저장된 인터프리터가 현재 패키지의 실행 가능한 venv인지 확인한다."""
    stored = status.get("venv_py")
    current = venv_python(pkg_root)
    if not isinstance(stored, str) or current is None:
        return False
    stored_path = Path(stored).expanduser()
    return (
        stored_path.is_file()
        and os.access(stored_path, os.X_OK)
        and os.path.normcase(os.path.abspath(stored_path))
        == os.path.normcase(os.path.abspath(current))
    )


def check_python() -> CheckResult:
    version = ".".join(str(part) for part in sys.version_info[:3])
    ok = sys.version_info >= MIN_PYTHON
    return CheckResult(
        "Python 버전",
        ok,
        True,
        version,
        "Python 3.9 이상을 설치하세요.",
    )


def _run_probe(command: list[str], timeout: float = 20.0) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    detail = (proc.stdout.strip() or proc.stderr.strip()).splitlines()
    return proc.returncode == 0, (detail[-1] if detail else f"exit {proc.returncode}")


def check_pip(py: Path) -> CheckResult:
    ok, detail = _run_probe([str(py), "-m", "pip", "--version"])
    return CheckResult("pip", ok, True, detail, "pip를 설치하거나 복구하세요.")


def check_venv(pkg_root: Path) -> CheckResult:
    py = venv_python(pkg_root)
    return CheckResult(
        "가상환경",
        py is not None,
        True,
        str(py) if py else f"없음: {pkg_root / '.venv'}",
        "메뉴 [1]에서 가상환경 생성을 허용하세요.",
    )


def check_packages(py: Optional[Path]) -> list[CheckResult]:
    results: list[CheckResult] = []
    for module in REQUIRED_PKGS + OPTIONAL_PKGS:
        required = module in REQUIRED_PKGS
        label = PACKAGE_LABELS[module]
        if py is None:
            results.append(CheckResult(
                f"패키지 {label}",
                False,
                required,
                "가상환경 없음",
                "먼저 가상환경을 만든 뒤 패키지를 설치하세요.",
            ))
            continue
        code = (
            f"import {module} as m; "
            "print(getattr(m, '__version__', '설치됨'))"
        )
        ok, detail = _run_probe([str(py), "-c", code])
        results.append(CheckResult(
            f"패키지 {label}",
            ok,
            required,
            detail,
            f"{label} 패키지를 설치하세요.",
        ))
    return results


def check_cpu() -> CheckResult:
    cpu = os.cpu_count() or 0
    return CheckResult(
        "CPU 논리코어",
        cpu >= 1,
        True,
        f"논리코어 {cpu}개" if cpu else "확인 실패",
        "운영체제의 CPU 정보를 확인하세요.",
    )


def memory_gb() -> Optional[float]:
    try:
        if sys.platform == "win32":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return status.ullTotalPhys / (1024 ** 3)
        if sys.platform == "darwin":
            proc = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
                check=False,
            )
            if proc.returncode == 0:
                return int(proc.stdout.strip()) / (1024 ** 3)
        meminfo = Path("/proc/meminfo")
        if meminfo.is_file():
            for line in meminfo.read_text(encoding="ascii", errors="ignore").splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024 / (1024 ** 3)
    except (OSError, ValueError, AttributeError):
        return None
    return None


def check_memory() -> CheckResult:
    ram = memory_gb()
    return CheckResult(
        "메모리",
        ram is not None,
        False,
        f"{ram:.1f} GB" if ram is not None else "확인 실패",
        "메모리 확인 실패는 실행을 막지 않습니다.",
    )


def check_disk(pkg_root: Path) -> CheckResult:
    try:
        free_gb = shutil.disk_usage(pkg_root).free / (1024 ** 3)
    except OSError as exc:
        return CheckResult("여유 디스크", False, False, str(exc), "2 GB 이상 확보를 권장합니다.")
    return CheckResult(
        "여유 디스크",
        free_gb >= 2.0,
        False,
        f"{free_gb:.1f} GB",
        "사진 변환과 PDF 생성을 위해 2 GB 이상 확보를 권장합니다.",
    )


def check_port(port: int = PORT) -> CheckResult:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
    except OSError:
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/heartbeat",
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=1.5) as response:
                ours = 200 <= response.status < 300
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            ours = False
        detail = "슬라이드 도구 서버가 이미 실행 중" if ours else "다른 프로그램이 사용 중"
        return CheckResult("포트 8770", False, False, detail, "점유 프로그램을 종료하세요.")
    finally:
        sock.close()
    return CheckResult("포트 8770", True, False, "사용 가능")


def check_browser() -> CheckResult:
    try:
        browser = webbrowser.get()
        detail = getattr(browser, "name", None) or browser.__class__.__name__
        return CheckResult("브라우저", True, False, str(detail))
    except webbrowser.Error as exc:
        return CheckResult("브라우저", False, False, str(exc), "기본 브라우저를 설정하세요.")


def default_workers(cpu: int, ram_gb: Optional[float]) -> int:
    cpu = max(1, int(cpu or 1))
    if ram_gb is not None and ram_gb < 8.0:
        return min(cpu, 4)
    return cpu


def run_all(pkg_root: Path) -> list[CheckResult]:
    pkg_root = package_root(pkg_root)
    py = venv_python(pkg_root)
    pip_py = py or Path(sys.executable)
    return [
        check_python(),
        check_pip(pip_py),
        check_venv(pkg_root),
        *check_packages(py),
        check_cpu(),
        check_memory(),
        check_disk(pkg_root),
        check_port(),
        check_browser(),
    ]


def _snapshot(pkg_root: Path, results: list[CheckResult]) -> dict[str, object]:
    cpu = os.cpu_count() or 0
    ram = memory_gb()
    py = venv_python(pkg_root)
    ok = all(result.ok for result in results if result.required)
    heic = next(
        (result.ok for result in results if result.name == "패키지 pillow-heif"),
        False,
    )
    return {
        "ok": ok,
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "workers": default_workers(cpu, ram),
        "cpu": cpu,
        "venv_py": str(py) if py else None,
        "heic": heic,
        "results": [result.public() for result in results],
    }


def _write_status(pkg_root: Path, snapshot: dict[str, object]) -> None:
    status_path = pkg_root / "00_시작" / "_env_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = status_path.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(status_path)


def inspect_environment(pkg_root: Path) -> dict[str, object]:
    results = run_all(pkg_root)
    snapshot = _snapshot(pkg_root, results)
    _write_status(pkg_root, snapshot)
    return snapshot


def _print_results(results: list[dict[str, object]]) -> None:
    print("\n=== 환경 점검 ===")
    for result in results:
        mark = "OK" if result["ok"] else ("필수" if result["required"] else "선택")
        print(f"  [{mark:4s}] {result['name']}: {result['detail']}")


def _ask(prompt: str, default: bool) -> bool:
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in {"y", "yes", "1", "예", "네"}


def _run_install(command: list[str], quiet: bool) -> bool:
    try:
        if quiet:
            proc = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        else:
            proc = subprocess.run(command, check=False)
    except OSError as exc:
        if not quiet:
            print(f"실행 실패: {exc}", file=sys.stderr)
        return False
    if proc.returncode != 0 and quiet and proc.stderr:
        print(proc.stderr.rstrip(), file=sys.stderr)
    return proc.returncode == 0


def interactive_setup(pkg_root: Path, assume_yes: bool = False, quiet: bool = False) -> bool:
    pkg_root = package_root(pkg_root)
    snapshot = inspect_environment(pkg_root)
    if not quiet:
        _print_results(snapshot["results"])

    py = venv_python(pkg_root)
    if py is None:
        consent = assume_yes or _ask("가상환경을 만들까요? [Y/n] ", True)
        if consent:
            if not quiet:
                print(f"가상환경 생성: {pkg_root / '.venv'}")
            _run_install([sys.executable, "-m", "venv", str(pkg_root / ".venv")], quiet)
            py = venv_python(pkg_root)

    if py is not None:
        package_results = check_packages(py)
        required_missing = [r for r in package_results if r.required and not r.ok]
        pip_ok = check_pip(py).ok
        if not pip_ok:
            consent = assume_yes or _ask("가상환경의 pip를 복구할까요? [Y/n] ", True)
            if consent:
                _run_install([str(py), "-m", "ensurepip", "--upgrade"], quiet)
                pip_ok = check_pip(py).ok
        if required_missing:
            consent = assume_yes or _ask(
                "opencv-python·numpy·Pillow 를 설치할까요? [Y/n] ",
                True,
            )
            if consent:
                requirements = pkg_root / "requirements.txt"
                _run_install([str(py), "-m", "pip", "install", "-r", str(requirements)], quiet)

        heif_ok = next((r.ok for r in check_packages(py) if r.name == "패키지 pillow-heif"), False)
        if not heif_ok:
            consent = assume_yes or _ask(
                "아이폰 HEIC 사진을 쓰시나요? pillow-heif 설치 [y/N] ",
                False,
            )
            if consent:
                _run_install([str(py), "-m", "pip", "install", "pillow-heif"], quiet)

    snapshot = inspect_environment(pkg_root)
    if not quiet:
        _print_results(snapshot["results"])
        if snapshot["ok"]:
            print(f"\n환경 준비 완료 — 작업 워커 {snapshot['workers']}개")
        else:
            print("\n필수 환경이 아직 준비되지 않았습니다.", file=sys.stderr)
    return bool(snapshot["ok"])


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="한 줄 JSON만 출력")
    parser.add_argument("--yes", action="store_true", help="설치 질문에 자동으로 예")
    parser.add_argument(
        "--pkg-root",
        default=None,
        metavar="DIR",
        help="배포 패키지 루트(기본: 이 파일의 상위 폴더)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    root = package_root(args.pkg_root)
    if args.yes:
        interactive_setup(root, assume_yes=True, quiet=args.json)
        snapshot = inspect_environment(root)
    elif args.json:
        snapshot = inspect_environment(root)
    else:
        interactive_setup(root)
        snapshot = inspect_environment(root)

    if args.json:
        public = {
            key: snapshot[key]
            for key in ("ok", "workers", "cpu", "venv_py", "results")
        }
        print(json.dumps(public, ensure_ascii=False, separators=(",", ":")))
    return 0 if snapshot["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
