#!/usr/bin/env python3
"""serve_tool 통합 테스트 — 실서버 기동/HTTP 검증/워치독 종료."""

import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# Windows 콘솔 기본 인코딩(cp949)에는 '—'·'·'·'⚠' 같은 문자가 없어, 그대로 print 하면
# UnicodeEncodeError 로 스크립트가 죽는다(실측: init_worktree 가 U+2014 에서 중단).
# 출력 스트림을 UTF-8 로 고정해 어떤 콘솔에서도 깨지거나 죽지 않게 한다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 파이프·구버전 등 재설정 불가 시 무시
        pass


SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = SCRIPT_DIR.parent
SERVER_SCRIPT = PACKAGE_ROOT / "00_시작" / "serve_tool.py"
HEARTBEAT_JS = PACKAGE_ROOT / "02_작업장" / "slide_tool" / "heartbeat.js"
TIMEOUT = 3.0
GRACE = 10.0
KOREAN_TEXT = "한글 응답 확인"


def empty_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(url: str, method: str = "GET") -> tuple[int, object, bytes]:
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=2) as response:
        return response.status, response.headers, response.read()


def wait_ready(process: subprocess.Popen[str], base_url: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(
                f"서버가 준비 전에 종료됨(exit {process.returncode}): {stdout.strip()} {stderr.strip()}"
            )
        try:
            status, _, _ = request(f"{base_url}/slide_tool/index.html")
            if status == 200:
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.05)
    raise TimeoutError("서버가 5초 안에 준비되지 않았습니다.")


def start_server(root: Path, processes: list[subprocess.Popen[str]]) -> tuple[subprocess.Popen[str], str]:
    port = empty_port()
    command = [
        sys.executable,
        str(SERVER_SCRIPT),
        "--root",
        str(root),
        "--port",
        str(port),
        "--no-open",
        "--timeout",
        str(TIMEOUT),
        "--grace",
        str(GRACE),
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    processes.append(process)
    base_url = f"http://127.0.0.1:{port}"
    wait_ready(process, base_url)
    return process, base_url


def require_exit(process: subprocess.Popen[str], seconds: float) -> None:
    try:
        return_code = process.wait(timeout=seconds)
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(f"{seconds:.1f}초 안에 서버가 종료되지 않았습니다.") from exc
    stdout, stderr = process.communicate(timeout=1)
    if return_code != 0:
        raise AssertionError(
            f"서버 종료코드가 {return_code}입니다: {stdout.strip()} {stderr.strip()}"
        )


def report(label: str, action) -> bool:
    try:
        action()
    except Exception as exc:  # 각 게이트를 끝까지 실행하고 실패를 모아 보고한다.
        print(f"[FAIL] {label}: {exc}")
        return False
    print(f"[OK] {label}")
    return True


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        if process.stdout is not None and not process.stdout.closed:
            process.communicate()
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
    process.communicate()


def main() -> int:
    results: list[bool] = []
    processes: list[subprocess.Popen[str]] = []

    with tempfile.TemporaryDirectory(prefix="serve_tool_test_") as temp_dir:
        root = Path(temp_dir)
        slide_tool = root / "slide_tool"
        slide_tool.mkdir()
        (slide_tool / "index.html").write_text(
            f"<!doctype html><html><meta charset=\"utf-8\"><body>{KOREAN_TEXT}</body></html>",
            encoding="utf-8",
        )
        shutil.copy2(HEARTBEAT_JS, slide_tool / "heartbeat.js")

        try:
            def test_charset() -> None:
                process = None
                try:
                    process, base_url = start_server(root, processes)
                    status, headers, body = request(f"{base_url}/slide_tool/index.html")
                    assert status == 200, f"HTTP {status}"
                    content_type = headers.get("Content-Type", "")
                    assert "charset=utf-8" in content_type.lower(), content_type
                    assert KOREAN_TEXT in body.decode("utf-8"), "한글 본문이 일치하지 않습니다."
                finally:
                    if process is not None:
                        stop_process(process)

            results.append(report("T1 HTML charset와 한글 본문", test_charset))

            def test_heartbeat() -> None:
                process = None
                try:
                    process, base_url = start_server(root, processes)
                    get_status, _, _ = request(f"{base_url}/heartbeat")
                    post_status, _, _ = request(f"{base_url}/heartbeat", method="POST")
                    assert get_status == 204, f"GET /heartbeat: HTTP {get_status}"
                    assert post_status == 204, f"POST /heartbeat: HTTP {post_status}"
                finally:
                    if process is not None:
                        stop_process(process)

            results.append(report("T2 GET|POST /heartbeat 204", test_heartbeat))

            def test_timeout() -> None:
                process, base_url = start_server(root, processes)
                for index in range(3):
                    status, _, _ = request(f"{base_url}/heartbeat", method="POST")
                    assert status == 204, f"핑 {index + 1}: HTTP {status}"
                    if index < 2:
                        time.sleep(1)
                require_exit(process, TIMEOUT + 4)

            results.append(report("T3 핑 중단 후 timeout 자동 종료", test_timeout))

            def test_goodbye() -> None:
                goodbye_process, goodbye_url = start_server(root, processes)
                status, _, _ = request(f"{goodbye_url}/heartbeat", method="POST")
                assert status == 204, f"heartbeat: HTTP {status}"
                status, _, _ = request(f"{goodbye_url}/goodbye", method="POST")
                assert status == 204, f"goodbye: HTTP {status}"
                require_exit(goodbye_process, 5)

            results.append(report("T4 goodbye 후 5초 내 종료", test_goodbye))

            def test_startup_grace() -> None:
                grace_process, _ = start_server(root, processes)
                require_exit(grace_process, GRACE + 4)

            results.append(report("T5 첫 핑 없이 grace 초과 종료", test_startup_grace))
        except Exception as exc:
            print(f"[FAIL] 테스트 환경 준비: {exc}")
            results.append(False)
        finally:
            for process in processes:
                stop_process(process)

    if all(results) and len(results) == 5:
        print("SERVE: ALL PASS")
        return 0
    print("SERVE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
