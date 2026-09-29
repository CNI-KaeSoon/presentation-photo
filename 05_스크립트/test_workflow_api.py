#!/usr/bin/env python3
"""워크플로 서버 API의 인증·업로드·잡 보안 통합 게이트."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

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
SERVE_TEST = SCRIPT_DIR / "test_serve_tool.py"
SECURITY_TEST = SCRIPT_DIR / "test_security.py"
GEN_MANIFEST = PACKAGE_ROOT / "02_작업장" / "slide_tool" / "gen_manifest.py"
TERMINAL_STATES = {"done", "error", "cancelled"}
CORNERS = [[0, 0], [1, 0], [0, 1], [1, 1]]


def report(label: str, action: Callable[[], None]) -> bool:
    try:
        action()
    except Exception as exc:
        print(f"[FAIL] {label}: {exc}")
        return False
    print(f"[OK] {label}")
    return True


def empty_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_fake_venv(pkg: Path, block_dir: Optional[Path] = None) -> None:
    """block_dir 가 있으면 그 폴더를 PYTHONPATH 맨 앞에 둔 래퍼를 만든다(모듈 가리기 시뮬레이션용)."""
    bindir = pkg / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
    bindir.mkdir(parents=True, exist_ok=True)
    name = "python.exe" if sys.platform == "win32" else "python3"
    target = bindir / name
    if sys.platform == "win32":
        shutil.copy2(sys.executable, target)
        target.chmod(0o755)
    else:
        # resolve() 하면 시스템 파이썬을 가리켜 venv site-packages(cv2 등)를 잃는다.
        # 테스트를 실행한 venv 인터프리터를 그대로 exec 하는 셸 래퍼를 쓴다.
        prefix = (
            f'PYTHONPATH="{block_dir}${{PYTHONPATH:+:$PYTHONPATH}}"; export PYTHONPATH\n'
            if block_dir is not None
            else ""
        )
        target.write_text(
            f'#!/bin/sh\n{prefix}exec "{os.path.abspath(sys.executable)}" "$@"\n',
            encoding="utf-8",
        )
        target.chmod(0o755)
    status = {
        "ok": True,
        "workers": 1,
        "heic": False,
        "venv_py": str(target.absolute()),
    }
    (pkg / "00_시작" / "_env_status.json").write_text(
        json.dumps(status, ensure_ascii=False), encoding="utf-8"
    )


def make_pkg(temp: Path, block_dir: Optional[Path] = None) -> Path:
    pkg = temp / "package"
    for relative in (
        "00_시작",
        "01_원본사진",
        "02_작업장/slide_tool",
        "03_결과물",
        "05_스크립트",
    ):
        (pkg / relative).mkdir(parents=True, exist_ok=True)
    (pkg / "02_작업장" / "slide_tool" / "index.html").write_text(
        "<!doctype html><meta charset=\"utf-8\"><title>test</title>",
        encoding="utf-8",
    )
    shutil.copy2(GEN_MANIFEST, pkg / "02_작업장" / "slide_tool" / "gen_manifest.py")
    for source in SCRIPT_DIR.glob("*.py"):
        destination = pkg / "05_스크립트" / source.name
        try:
            destination.symlink_to(source)
        except OSError:
            shutil.copy2(source, destination)
    Image.new("RGB", (120, 90), (30, 120, 210)).save(
        pkg / "01_원본사진" / "IMG_1.jpg"
    )
    Image.new("RGB", (120, 90), (210, 90, 30)).save(
        pkg / "01_원본사진" / "IMG_2.jpg"
    )
    make_fake_venv(pkg, block_dir)
    return pkg


def make_group(pkg: Path, name: str, image_name: str = "GROUP.jpg") -> Path:
    image_dir = pkg / "02_작업장" / name / "img"
    image_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (120, 90), (80, 160, 60)).save(image_dir / image_name)
    plan = {
        "_type": "slide_tool_worktree",
        "_version": 2,
        "root": ".",
        "source": "../01_원본사진",
        "groups": {name: [image_name]},
    }
    (pkg / "02_작업장" / "worktree.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result = subprocess.run(
        [sys.executable, str(pkg / "02_작업장" / "slide_tool" / "gen_manifest.py")],
        cwd=pkg,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, (result.stdout + result.stderr).strip()
    return image_dir.parent


def make_export_groups(pkg: Path) -> tuple[list[str], dict[str, tuple[int, int, int]]]:
    """순서 병합을 화면 색으로 확인할 수 있는 2개 그룹을 만든다."""
    groups = ["01_RED", "02_BLUE"]
    colors = {"01_RED": (220, 30, 30), "02_BLUE": (30, 30, 220)}
    plan_groups: dict[str, list[str]] = {}
    for index, name in enumerate(groups, 1):
        image_name = f"EXPORT_{index}.jpg"
        color = colors[name]
        Image.new("RGB", (160, 120), color).save(pkg / "01_원본사진" / image_name)
        image_dir = pkg / "02_작업장" / name / "img"
        image_dir.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (160, 120), color).save(image_dir / image_name)
        plan_groups[name] = [image_name]
    plan = {
        "_type": "slide_tool_worktree",
        "_version": 2,
        "root": ".",
        "source": "../01_원본사진",
        "groups": plan_groups,
    }
    (pkg / "02_작업장" / "worktree.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result = subprocess.run(
        [sys.executable, str(pkg / "02_작업장" / "slide_tool" / "gen_manifest.py")],
        cwd=pkg,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, (result.stdout + result.stderr).strip()
    return groups, colors


def http_request(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    body: Optional[bytes] = None,
    host_override: Optional[str] = None,
) -> tuple[int, dict[str, str], bytes]:
    parts = urllib.parse.urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    request_headers = dict(headers or {})
    if host_override is None:
        connection.request(method, path, body=body, headers=request_headers)
    else:
        connection.putrequest(method, path, skip_host=True)
        connection.putheader("Host", host_override)
        for key, value in request_headers.items():
            connection.putheader(key, value)
        if body is not None and "Content-Length" not in request_headers:
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
    response = connection.getresponse()
    data = response.read()
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    status = response.status
    connection.close()
    return status, response_headers, data


def declared_request(
    method: str,
    url: str,
    length: int,
    headers: dict[str, str],
) -> tuple[int, dict[str, str], bytes]:
    parts = urllib.parse.urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    path = parts.path or "/"
    connection.putrequest(method, path, skip_host=True)
    connection.putheader("Host", parts.netloc)
    connection.putheader("Content-Length", str(length))
    for key, value in headers.items():
        connection.putheader(key, value)
    connection.endheaders()
    response = connection.getresponse()
    data = response.read()
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    status = response.status
    connection.close()
    return status, response_headers, data


def decode_json(body: bytes) -> dict[str, object]:
    value = json.loads(body.decode("utf-8"))
    assert isinstance(value, dict), "JSON 응답 최상위가 객체가 아닙니다."
    return value


def api_headers(base: str, token: str) -> dict[str, str]:
    return {
        "Origin": base,
        "X-Workflow-Token": token,
        "Content-Type": "application/json",
    }


def json_post(
    base: str, path: str, token: str, payload: dict[str, object]
) -> tuple[int, dict[str, str], dict[str, object]]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    status, headers, raw = http_request(
        "POST", base + path, headers=api_headers(base, token), body=body
    )
    return status, headers, decode_json(raw)


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    if process.stdout is not None and not process.stdout.closed:
        process.communicate(timeout=2)


def start_server(
    pkg: Path,
    processes: list[subprocess.Popen[str]],
    *,
    root: Optional[Path] = None,
    pkg_arg: Optional[Path] = None,
    watchdog: bool = False,
    timeout: float = 3.0,
    grace: float = 8.0,
) -> tuple[subprocess.Popen[str], str, str]:
    port = empty_port()
    root = root or pkg / "02_작업장"
    pkg_arg = pkg if pkg_arg is None else pkg_arg
    command = [
        sys.executable,
        str(SERVER_SCRIPT),
        "--root",
        str(root),
        "--pkg-root",
        str(pkg_arg),
        "--port",
        str(port),
        "--no-open",
        "--timeout",
        str(timeout),
        "--grace",
        str(grace),
    ]
    if not watchdog:
        command.append("--no-watchdog")
    environment = os.environ.copy()
    environment["SLIDE_TOOL_NO_OS_OPEN"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )
    processes.append(process)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(
                f"서버가 준비 전에 종료됐습니다: {stdout.strip()} {stderr.strip()}"
            )
        try:
            status, _, _ = http_request("GET", base + "/slide_tool/index.html")
            if status == 200:
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise TimeoutError("서버가 8초 안에 준비되지 않았습니다.")
    status, _, raw = http_request(
        "GET", base + "/api/token", headers={"Sec-Fetch-Site": "same-origin"}
    )
    assert status == 200, f"토큰 부트스트랩 HTTP {status}"
    token = decode_json(raw).get("token")
    assert isinstance(token, str), "토큰이 문자열이 아닙니다."
    return process, base, token


def wait_job(base: str, token: str, timeout: float = 60.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: Optional[dict[str, object]] = None
    while time.monotonic() < deadline:
        status, _, raw = http_request(
            "GET", base + "/api/job?after=0", headers={"X-Workflow-Token": token}
        )
        assert status == 200, f"GET /api/job HTTP {status}"
        payload = decode_json(raw)
        job = payload.get("job")
        if isinstance(job, dict):
            last = job
            if job.get("state") in TERMINAL_STATES:
                return job
        time.sleep(0.08)
    raise TimeoutError(f"잡이 {timeout:g}초 안에 끝나지 않았습니다: {last}")


def job_log(job: dict[str, object]) -> str:
    lines = job.get("lines")
    if not isinstance(lines, list):
        return ""
    return "\n".join(
        str(row[1]) for row in lines if isinstance(row, list) and len(row) == 2
    )


def pdf_page_colors(path: Path, page_count: int) -> list[tuple[int, int, int]]:
    """Poppler로 각 페이지를 렌더링해 중앙 화소의 RGB를 읽는다."""
    pdftoppm = shutil.which("pdftoppm")
    assert pdftoppm is not None, "PDF 페이지 순서 검증에 필요한 pdftoppm이 없습니다."
    colors: list[tuple[int, int, int]] = []
    with tempfile.TemporaryDirectory(prefix="workflow_pdf_pages_") as temp_dir:
        for page in range(1, page_count + 1):
            prefix = Path(temp_dir) / f"page_{page}"
            result = subprocess.run(
                [
                    pdftoppm,
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    "-singlefile",
                    "-scale-to",
                    "32",
                    "-png",
                    str(path),
                    str(prefix),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            assert result.returncode == 0, result.stderr.strip()
            with Image.open(prefix.with_suffix(".png")) as image:
                rgb = image.convert("RGB")
                colors.append(rgb.getpixel((rgb.width // 2, rgb.height // 2)))
    return colors


def outside_pdf_snapshot(temp: Path, allowed_out: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    allowed = allowed_out.resolve()
    for path in temp.rglob("*.pdf"):
        resolved = path.resolve()
        if resolved == allowed or allowed in resolved.parents:
            continue
        snapshot[resolved.relative_to(temp.resolve()).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return snapshot


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def load_server_module():
    module_name = "serve_tool_workflow_test"
    sys.path.insert(0, str(PACKAGE_ROOT / "00_시작"))
    spec = importlib.util.spec_from_file_location(module_name, SERVER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def subprocess_gate(script: Path, success_line: str, timeout: float) -> None:
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=PACKAGE_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, (result.stdout + result.stderr).strip()
    assert success_line in result.stdout, result.stdout.strip()


def run_auth() -> list[bool]:
    results: list[bool] = []
    processes: list[subprocess.Popen[str]] = []
    with tempfile.TemporaryDirectory(prefix="workflow_auth_") as temp_dir:
        temp = Path(temp_dir)
        pkg = make_pkg(temp)
        try:
            _, base, token = start_server(pkg, processes)

            def a1() -> None:
                status, headers, raw = http_request("GET", base + "/api/status")
                payload = decode_json(raw)
                assert status == 401, status
                assert payload.get("ok") is False and payload.get("error"), payload
                assert headers.get("content-type") == "application/json; charset=utf-8"

            results.append(report("A1 토큰 없는 상태 조회 차단", a1))

            def a2() -> None:
                status, _, _ = http_request(
                    "GET",
                    base + "/api/status",
                    headers={"X-Workflow-Token": "wrong"},
                )
                assert status == 403, status

            results.append(report("A2 틀린 토큰 차단", a2))

            def a3() -> None:
                headers = api_headers(base, token)
                headers["Origin"] = "http://evil.example"
                status, _, _ = http_request(
                    "POST",
                    base + "/api/open-folder",
                    headers=headers,
                    body=b'{"target":"src"}',
                )
                assert status == 403, status

            results.append(report("A3 악성 Origin 차단", a3))

            def a4() -> None:
                status, _, _ = http_request(
                    "POST",
                    base + "/api/open-folder",
                    headers={"X-Workflow-Token": token, "Content-Type": "application/json"},
                    body=b'{"target":"src"}',
                )
                assert status == 403, status

            results.append(report("A4 Origin 부재 차단", a4))

            def a5() -> None:
                static_status, _, _ = http_request(
                    "GET", base + "/slide_tool/index.html", host_override="evil.example"
                )
                token_status, _, _ = http_request(
                    "GET",
                    base + "/api/token",
                    headers={"Sec-Fetch-Site": "same-origin"},
                    host_override="evil.example",
                )
                assert (static_status, token_status) == (403, 403)

            results.append(report("A5 Host 위조 정적·토큰 차단", a5))

            def a6() -> None:
                denied, _, _ = http_request(
                    "GET",
                    base + "/api/token",
                    headers={"Sec-Fetch-Site": "cross-site"},
                )
                allowed, _, raw = http_request(
                    "GET",
                    base + "/api/token",
                    headers={"Sec-Fetch-Site": "same-origin"},
                )
                fresh = decode_json(raw).get("token")
                assert denied == 403 and allowed == 200
                assert isinstance(fresh, str) and len(fresh) == 43

            results.append(report("A6 토큰 same-origin 판정", a6))

            def a7() -> None:
                status, _, payload = json_post(
                    base, "/api/open-folder", token, {"target": "../etc"}
                )
                assert status == 400 and payload.get("error") == "bad_target", payload

            results.append(report("A7 폴더 target enum 강제", a7))

            def a8() -> None:
                status, headers, raw = http_request(
                    "GET",
                    base + "/api/status",
                    headers={"X-Workflow-Token": token},
                )
                payload = decode_json(raw)
                assert status == 200 and payload.get("workflow") is True, payload
                assert payload.get("srcCount") == 2, payload
                assert headers.get("content-type") == "application/json; charset=utf-8"

            results.append(report("A8 정상 상태·UTF-8 JSON", a8))

            def a9() -> None:
                disabled_root = temp / "disabled" / "work"
                (disabled_root / "slide_tool").mkdir(parents=True)
                (disabled_root / "slide_tool" / "index.html").write_text(
                    "<!doctype html>", encoding="utf-8"
                )
                bad_pkg = temp / "disabled" / "not-a-package"
                _, disabled_base, disabled_token = start_server(
                    pkg,
                    processes,
                    root=disabled_root,
                    pkg_arg=bad_pkg,
                )
                status, _, raw = http_request(
                    "GET",
                    disabled_base + "/api/status",
                    headers={"X-Workflow-Token": disabled_token},
                )
                payload = decode_json(raw)
                post_status, _, post_payload = json_post(
                    disabled_base,
                    "/api/prepare",
                    disabled_token,
                    {"regroup": False},
                )
                assert status == 200 and payload.get("workflow") is False, payload
                assert post_status == 503 and post_payload.get("error") == "workflow_disabled"

            results.append(report("A9 패키지 해석 실패 시 API 비활성", a9))
            results.append(
                report(
                    "A10 기존 serve_tool 회귀",
                    lambda: subprocess_gate(SERVE_TEST, "SERVE: ALL PASS", 70),
                )
            )
        finally:
            for process in processes:
                stop_process(process)
    return results


def run_upload() -> list[bool]:
    results: list[bool] = []
    processes: list[subprocess.Popen[str]] = []
    with tempfile.TemporaryDirectory(prefix="workflow_upload_") as temp_dir:
        temp = Path(temp_dir)
        pkg = make_pkg(temp)
        src = pkg / "01_원본사진"
        try:
            _, base, token = start_server(pkg, processes)

            def upload(name: str, content: bytes) -> tuple[int, dict[str, object]]:
                headers = api_headers(base, token)
                headers["X-Filename"] = name
                status, _, raw = http_request(
                    "POST", base + "/api/upload", headers=headers, body=content
                )
                return status, decode_json(raw)

            def u1() -> None:
                before_outside = {
                    path.relative_to(temp).as_posix()
                    for path in temp.rglob("*")
                    if path.is_file() and src not in path.parents
                }
                for name in (
                    "../x.jpg",
                    "..%2F..%2Fx.jpg",
                    "/tmp/x.jpg",
                    "C:\\x.jpg",
                    "a/b.jpg",
                    "x.jpg%00.exe",
                ):
                    status, _ = upload(name, b"blocked")
                    assert status == 400, (name, status)
                after_outside = {
                    path.relative_to(temp).as_posix()
                    for path in temp.rglob("*")
                    if path.is_file() and src not in path.parents
                }
                assert after_outside == before_outside

            results.append(report("U1 경로형 파일명·탈출 차단", u1))

            def u2() -> None:
                before = {path.name for path in src.iterdir()}
                for name in (
                    "x.exe",
                    "x.jpg.exe",
                    "x",
                    ".hidden.jpg",
                    "x#1.jpg",
                    "x?.png",
                    "CON.jpg",
                ):
                    status, _ = upload(name, b"blocked")
                    assert status == 400, (name, status)
                assert {path.name for path in src.iterdir()} == before

            results.append(report("U2 위장 확장자·금지 파일명 차단", u2))

            def u3() -> None:
                original = src / "IMG_1.jpg"
                before = hashlib.sha256(original.read_bytes()).hexdigest()
                status, payload = upload("IMG_1.jpg", b"different image bytes")
                after = hashlib.sha256(original.read_bytes()).hexdigest()
                assert status == 200, payload
                assert payload.get("renamed") is True
                assert payload.get("saved") == "IMG_1_2.jpg", payload
                assert before == after
                assert (src / "IMG_1_2.jpg").read_bytes() == b"different image bytes"

            results.append(report("U3 기존 원본 불변·충돌 이름 변경", u3))

            def u4() -> None:
                original = src / "IMG_2.jpg"
                before = len(list(src.iterdir()))
                status, payload = upload("IMG_2.jpg", original.read_bytes())
                assert status == 200 and payload.get("dedup") is True, payload
                assert len(list(src.iterdir())) == before

            results.append(report("U4 동일 내용 재업로드 dedup", u4))

            def u5() -> None:
                headers = api_headers(base, token)
                headers["X-Filename"] = "large.jpg"
                status, _, _ = declared_request(
                    "POST", base + "/api/upload", 101 * 1024 * 1024, headers
                )
                assert status == 413, status
                assert not list(src.glob(".업로드중-*.part"))

            results.append(report("U5 101MB 선언·part 잔존 차단", u5))

            def u6() -> None:
                content = b"portable korean filename"
                encoded = urllib.parse.quote("한글 이름.jpg", safe="")
                status, payload = upload(encoded, content)
                assert status == 200, payload
                saved = payload.get("saved")
                assert saved == "한글 이름.jpg", saved
                assert (src / str(saved)).read_bytes() == content

            results.append(report("U6 한글 NFC 파일명 정상 저장", u6))

            def u7() -> None:
                server_module = load_server_module()
                sys.path.insert(0, str(SCRIPT_DIR))
                import photo_io

                assert server_module.UPLOAD_EXTS == tuple(photo_io.ALL_EXTS)

            results.append(report("U7 업로드 확장자 상수 동기화", u7))

            def upload_with_mtime(
                name: str, content: bytes, last_modified: Optional[str]
            ) -> tuple[int, dict[str, object]]:
                headers = api_headers(base, token)
                headers["X-Filename"] = name
                if last_modified is not None:
                    headers["X-Last-Modified"] = last_modified
                status, _, raw = http_request(
                    "POST", base + "/api/upload", headers=headers, body=content
                )
                return status, decode_json(raw)

            def plain_jpeg(color: tuple[int, int, int]) -> bytes:
                buffer = io.BytesIO()
                Image.new("RGB", (64, 48), color).save(buffer, "JPEG")
                return buffer.getvalue()

            def u8() -> None:
                # EXIF 없는 사진: 브라우저 File.lastModified 가 파일 수정 시각으로 남아야 한다.
                wanted_ms = 1_700_000_000_000          # 2023-11-14
                status, payload = upload_with_mtime(
                    "NOEXIF_A.jpg", plain_jpeg((10, 20, 30)), str(wanted_ms)
                )
                assert status == 200 and payload.get("mtimeApplied") is True, payload
                saved = src / str(payload["saved"])
                assert abs(saved.stat().st_mtime - wanted_ms / 1000) < 1.0, saved.stat().st_mtime
                # 시각이 다른 두 장은 촬영순 계산(photo_io.capture_time)에서도 그 차이가 살아 있다.
                status, second = upload_with_mtime(
                    "NOEXIF_B.jpg", plain_jpeg((30, 20, 10)), str(wanted_ms + 3_600_000)
                )
                assert status == 200 and second.get("mtimeApplied") is True, second
                sys.path.insert(0, str(SCRIPT_DIR))
                import photo_io

                first_when, first_source = photo_io.capture_time(str(saved))
                second_when, _ = photo_io.capture_time(str(src / str(second["saved"])))
                assert first_source == "mtime", first_source
                assert first_when is not None and second_when is not None
                assert (second_when - first_when).total_seconds() == 3600, (first_when, second_when)

            results.append(report("U8 업로드 lastModified → 수정 시각 적용", u8))

            def u9() -> None:
                before = time.time()
                bad_values = (
                    "abc",
                    "-5",
                    "1.5e12",
                    "1700000000000.5",
                    "",
                    "0",                       # 1970 — 범위 밖
                    "946684799999",            # 2000-01-01 직전 — 범위 밖
                    "99999999999999999999",     # 자릿수 초과
                    "9999999999999",           # 2286 — 미래
                )
                for index, bad in enumerate(bad_values):
                    status, payload = upload_with_mtime(
                        f"BADMTIME_{index}.jpg", plain_jpeg((index, 0, 0)), bad
                    )
                    # 시각만 무시하고 사진은 정상 저장한다.
                    assert status == 200, (bad, status, payload)
                    assert payload.get("mtimeApplied") is False, (bad, payload)
                    saved = src / str(payload["saved"])
                    assert saved.stat().st_mtime >= before - 5, (bad, saved.stat().st_mtime)
                # 헤더가 아예 없어도 정상.
                status, payload = upload_with_mtime("NOHEADER.jpg", plain_jpeg((1, 2, 3)), None)
                assert status == 200 and payload.get("mtimeApplied") is False, payload

            results.append(report("U9 잘못된 lastModified 는 무시하고 저장", u9))
        finally:
            for process in processes:
                stop_process(process)
    return results


def run_rename() -> list[bool]:
    results: list[bool] = []
    processes: list[subprocess.Popen[str]] = []
    with tempfile.TemporaryDirectory(prefix="workflow_rename_") as temp_dir:
        temp = Path(temp_dir)
        pkg = make_pkg(temp)
        work = pkg / "02_작업장"
        original_name = "01_원본"
        renamed_name = "01_발표"
        make_group(pkg, original_name)
        try:
            _, base, token = start_server(pkg, processes)

            def r1() -> None:
                status, _, payload = json_post(
                    base,
                    "/api/rename-group",
                    token,
                    {"from": original_name, "to": renamed_name},
                )
                assert status == 200 and payload.get("ok") is True, payload
                assert unicodedata.normalize("NFC", str(payload.get("from"))) == original_name
                assert unicodedata.normalize("NFC", str(payload.get("to"))) == renamed_name
                assert not (work / original_name).exists()
                assert (work / renamed_name / "img" / "GROUP.jpg").is_file()
                plan = json.loads((work / "worktree.json").read_text(encoding="utf-8"))
                assert list(plan["groups"]) == [renamed_name], plan
                data_js = unicodedata.normalize(
                    "NFC", (work / "slide_tool" / "data.js").read_text(encoding="utf-8")
                )
                assert f"../{renamed_name}/img/GROUP.jpg" in data_js
                assert f"../{original_name}/img/GROUP.jpg" not in data_js

            results.append(report("R1 정상 rename·계획·목록 갱신", r1))

            def r2() -> None:
                for bad in ("../x", "/tmp/x", "a/b", "C:\\x"):
                    status, _, payload = json_post(
                        base,
                        "/api/rename-group",
                        token,
                        {"from": renamed_name, "to": bad},
                    )
                    assert status == 400 and payload.get("error") == "bad_group_name", (
                        bad,
                        status,
                        payload,
                    )
                assert (work / renamed_name / "img").is_dir()

            results.append(report("R2 경로형 그룹 이름 차단", r2))

            def r3() -> None:
                for bad in ("CON", "a#b", "a?b", "말미."):
                    status, _, payload = json_post(
                        base,
                        "/api/rename-group",
                        token,
                        {"from": renamed_name, "to": bad},
                    )
                    assert status == 400 and payload.get("error") == "bad_group_name", (
                        bad,
                        status,
                        payload,
                    )
                assert (work / renamed_name / "img").is_dir()

            results.append(report("R3 예약어·금지문자 그룹 이름 차단", r3))

            def r4() -> None:
                existing = work / "02_기존" / "img"
                existing.mkdir(parents=True)
                status, _, payload = json_post(
                    base,
                    "/api/rename-group",
                    token,
                    {"from": renamed_name, "to": "02_기존"},
                )
                assert status == 409 and payload.get("error") == "group_exists", payload
                assert (work / renamed_name / "img" / "GROUP.jpg").is_file()
                assert existing.is_dir()

            results.append(report("R4 대상 충돌 시 원본 폴더 불변", r4))

            def r5() -> None:
                body = json.dumps(
                    {"from": renamed_name, "to": "03_인증검사"}, ensure_ascii=False
                ).encode("utf-8")
                no_token, _, _ = http_request(
                    "POST",
                    base + "/api/rename-group",
                    headers={"Origin": base, "Content-Type": "application/json"},
                    body=body,
                )
                no_origin, _, _ = http_request(
                    "POST",
                    base + "/api/rename-group",
                    headers={
                        "X-Workflow-Token": token,
                        "Content-Type": "application/json",
                    },
                    body=body,
                )
                assert (no_token, no_origin) == (401, 403)
                assert (work / renamed_name / "img" / "GROUP.jpg").is_file()

            results.append(report("R5 토큰·Origin 없는 rename 차단", r5))

            def r6() -> None:
                busy_pkg = make_pkg(temp / "busy_case")
                busy_group = "01_실행중"
                make_group(busy_pkg, busy_group)
                prepare_script = busy_pkg / "05_스크립트" / "prepare_photos.py"
                prepare_script.unlink()
                prepare_script.write_text(
                    "import time\nprint('busy rename test', flush=True)\ntime.sleep(30)\n",
                    encoding="utf-8",
                )
                _, busy_base, busy_token = start_server(busy_pkg, processes)
                start_status, _, start_payload = json_post(
                    busy_base,
                    "/api/prepare",
                    busy_token,
                    {"regroup": False, "gapMinutes": 20},
                )
                assert start_status == 202, start_payload
                rename_status, _, rename_payload = json_post(
                    busy_base,
                    "/api/rename-group",
                    busy_token,
                    {"from": busy_group, "to": "02_거부"},
                )
                assert rename_status == 409 and rename_payload.get("error") == "busy", (
                    rename_status,
                    rename_payload,
                )
                assert (busy_pkg / "02_작업장" / busy_group / "img").is_dir()
                cancel_status, _, cancel_payload = json_post(
                    busy_base, "/api/job/cancel", busy_token, {}
                )
                assert cancel_status == 200, cancel_payload

                rollback_pkg = make_pkg(temp / "rollback_case")
                rollback_work = rollback_pkg / "02_작업장"
                rollback_from = "01_복구검사"
                rollback_to = "01_복구됨"
                make_group(rollback_pkg, rollback_from)
                plan_path = rollback_work / "worktree.json"
                data_path = rollback_work / "slide_tool" / "data.js"
                plan_before = plan_path.read_bytes()
                data_before = data_path.read_bytes()
                (rollback_work / "slide_tool" / "gen_manifest.py").write_text(
                    "raise SystemExit(7)\n", encoding="utf-8"
                )
                _, rollback_base, rollback_token = start_server(rollback_pkg, processes)
                rollback_status, _, rollback_payload = json_post(
                    rollback_base,
                    "/api/rename-group",
                    rollback_token,
                    {"from": rollback_from, "to": rollback_to},
                )
                assert rollback_status == 500, rollback_payload
                assert rollback_payload.get("error") == "rename_failed", rollback_payload
                assert (rollback_work / rollback_from / "img" / "GROUP.jpg").is_file()
                assert not (rollback_work / rollback_to).exists()
                assert plan_path.read_bytes() == plan_before
                assert data_path.read_bytes() == data_before

                mismatch_pkg = make_pkg(temp / "mismatch_case")
                mismatch_work = mismatch_pkg / "02_작업장"
                mismatch_from = "01_불일치"
                mismatch_to = "01_거부"
                make_group(mismatch_pkg, mismatch_from)
                mismatch_plan = mismatch_work / "worktree.json"
                mismatch_data = mismatch_work / "slide_tool" / "data.js"
                plan_doc = json.loads(mismatch_plan.read_text(encoding="utf-8"))
                plan_doc["groups"] = {"09_다른계획": ["GROUP.jpg"]}
                mismatch_plan.write_text(
                    json.dumps(plan_doc, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                mismatch_plan_before = mismatch_plan.read_bytes()
                mismatch_data_before = mismatch_data.read_bytes()
                _, mismatch_base, mismatch_token = start_server(mismatch_pkg, processes)
                mismatch_status, _, mismatch_payload = json_post(
                    mismatch_base,
                    "/api/rename-group",
                    mismatch_token,
                    {"from": mismatch_from, "to": mismatch_to},
                )
                assert mismatch_status == 409, mismatch_payload
                assert mismatch_payload.get("error") == "worktree_mismatch", mismatch_payload
                assert (mismatch_work / mismatch_from / "img" / "GROUP.jpg").is_file()
                assert not (mismatch_work / mismatch_to).exists()
                assert mismatch_plan.read_bytes() == mismatch_plan_before
                assert mismatch_data.read_bytes() == mismatch_data_before

            results.append(report("R6 잡 실행 중 rename busy", r6))
        finally:
            for process in processes:
                stop_process(process)
    return results


def make_backup(
    keys: list[str],
    *,
    moves: Optional[dict[str, object]] = None,
    ratios: Optional[dict[str, object]] = None,
    statuses: Optional[dict[str, object]] = None,
) -> dict[str, object]:
    maps = {
        "slideCorners_v1": {key: CORNERS for key in keys},
        "slideStatus_v1": statuses or {},
        "slideRatios_v1": ratios or {},
        "slideMoves_v1": moves or {},
        "slideColor_v1": {},
    }
    return {
        "_type": "slide_tool_backup",
        "_version": 1,
        "data": {
            name: json.dumps(value, ensure_ascii=False) for name, value in maps.items()
        },
    }


def export_job(
    base: str, token: str, backup: dict[str, object], timeout: float = 60
) -> dict[str, object]:
    status, _, payload = json_post(
        base,
        "/api/export-pdf",
        token,
        {"backup": backup, "onlyDone": False, "merge": False},
    )
    assert status == 202, payload
    job = wait_job(base, token, timeout)
    assert job.get("state") == "done", job_log(job)
    return job


def export_mode_job(
    base: str,
    token: str,
    backup: dict[str, object],
    mode: str,
    *,
    order: Optional[list[str]] = None,
    only_done: bool = False,
    timeout: float = 90,
) -> dict[str, object]:
    request: dict[str, object] = {
        "backup": backup,
        "mode": mode,
        "onlyDone": only_done,
    }
    if order is not None:
        request["order"] = order
    status, _, payload = json_post(base, "/api/export-pdf", token, request)
    assert status == 202, payload
    job = wait_job(base, token, timeout)
    assert job.get("state") == "done", job_log(job)
    return job


def run_export_modes() -> list[bool]:
    results: list[bool] = []
    processes: list[subprocess.Popen[str]] = []
    with tempfile.TemporaryDirectory(prefix="workflow_export_modes_") as temp_dir:
        temp = Path(temp_dir)

        def setup_case(name: str) -> tuple[Path, str, str, list[str], dict[str, object]]:
            pkg = make_pkg(temp / name)
            groups, _colors = make_export_groups(pkg)
            _, base, token = start_server(pkg, processes)
            keys = [
                f"../{group}/img/EXPORT_{index}.jpg"
                for index, group in enumerate(groups, 1)
            ]
            return pkg, base, token, groups, make_backup(keys)

        def e1() -> None:
            pkg, base, token, groups, backup = setup_case("per_folder")
            job = export_mode_job(base, token, backup, "per-folder")
            out = pkg / "03_결과물"
            assert sorted(path.name for path in out.glob("*.pdf")) == [
                f"{name}.pdf" for name in groups
            ]
            assert "전체.pdf" not in job_log(job)

        results.append(report("E1 per-folder 그룹별 PDF·통합본 없음", e1))

        def e2() -> None:
            pkg, base, token, groups, backup = setup_case("merged")
            job = export_mode_job(base, token, backup, "merged")
            out = pkg / "03_결과물"
            assert sorted(path.name for path in out.glob("*.pdf")) == sorted(
                [*(f"{name}.pdf" for name in groups), "전체.pdf"]
            )
            assert "전체.pdf" in job_log(job)

        results.append(report("E2 merged 그룹별 PDF+통합본", e2))

        def e3() -> None:
            pkg, base, token, groups, backup = setup_case("ordered")
            requested = list(reversed(groups))
            job = export_mode_job(base, token, backup, "ordered", order=requested)
            out = pkg / "03_결과물"
            assert [path.name for path in out.glob("*.pdf")] == ["전체.pdf"]
            page_colors = pdf_page_colors(out / "전체.pdf", 2)
            assert page_colors[0][2] > page_colors[0][0], page_colors
            assert page_colors[1][0] > page_colors[1][2], page_colors
            assert "결과 파일: 전체.pdf" in job_log(job)

        results.append(report("E3 ordered 지정 순서 통합 PDF 1개", e3))

        def e4() -> None:
            _pkg, base, token, groups, backup = setup_case("bad_order")
            bad_orders = (
                ["../escape"],
                [str(temp / "absolute")],
                ["missing"],
                [groups[0], groups[0]],
                [groups[0]],
            )
            for order in bad_orders:
                status, _, payload = json_post(
                    base,
                    "/api/export-pdf",
                    token,
                    {"backup": backup, "mode": "ordered", "order": order},
                )
                assert status == 400, (order, status, payload)
                assert payload.get("error") == "bad_order", payload

        results.append(report("E4 order 경로·미존재·중복·일부만 지정 거부", e4))

        def e5() -> None:
            pkg, base, token, groups, _backup = setup_case("ordered_only_done")
            keys = [
                f"../{group}/img/EXPORT_{index}.jpg"
                for index, group in enumerate(groups, 1)
            ]
            one_done = make_backup(
                keys,
                statuses={keys[0]: {"done": False}, keys[1]: {"done": True}},
            )
            job = export_mode_job(
                base,
                token,
                one_done,
                "ordered",
                order=groups,
                only_done=True,
            )
            merged = pkg / "03_결과물" / "전체.pdf"
            assert merged.is_file()
            colors = pdf_page_colors(merged, 1)
            assert colors[0][2] > colors[0][0], colors
            assert f"0쪽 그룹 건너뜀: {groups[0]}" in job_log(job)

            before = hashlib.sha256(merged.read_bytes()).hexdigest()
            none_done = make_backup(
                keys,
                statuses={key: {"done": False} for key in keys},
            )
            status, _, payload = json_post(
                base,
                "/api/export-pdf",
                token,
                {
                    "backup": none_done,
                    "mode": "ordered",
                    "order": groups,
                    "onlyDone": True,
                },
            )
            assert status == 202, payload
            failed = wait_job(base, token, 90)
            assert failed.get("state") == "error", job_log(failed)
            assert "병합할 페이지가 없습니다" in job_log(failed)
            assert hashlib.sha256(merged.read_bytes()).hexdigest() == before

        results.append(report("E5 ordered onlyDone 0쪽 건너뜀·전체 0쪽 실패", e5))

        def e6() -> None:
            module = load_server_module()
            out = temp / "merged_failure_out"
            out.mkdir()
            existing = out / "전체.pdf"
            existing.write_bytes(b"existing-result")
            fake_export = [sys.executable, "-c", "raise SystemExit(0)"]
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    module.STAGED_EXPORT_CODE,
                    json.dumps(fake_export),
                    "merged",
                    "[]",
                    str(out),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            assert result.returncode != 0
            assert "통합 PDF가 생성되지 않았습니다" in result.stderr
            assert existing.read_bytes() == b"existing-result"

        results.append(report("E6 merged 통합본 미생성 시 job error·기존 결과 보존", e6))
        for process in processes:
            stop_process(process)
    for process in processes:
        stop_process(process)
    return results


def pdf_page_count(path: Path) -> int:
    """poppler pdfinfo 로 쪽 수를 읽는다(pdfunite 결과처럼 객체 스트림에 든 쪽도 센다)."""
    pdfinfo = shutil.which("pdfinfo")
    assert pdfinfo is not None, "쪽 수 검증에 필요한 pdfinfo(poppler)가 없습니다."
    result = subprocess.run(
        [pdfinfo, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr.strip()
    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1])
    raise AssertionError(f"pdfinfo 에 Pages 줄이 없습니다: {result.stdout}")


# 색으로 쪽을 식별한다(중앙 화소).
MIX_COLORS = {
    "A_1.jpg": (220, 30, 30),          # 빨강
    "A_2.jpg": (30, 200, 30),          # 초록
    "A_3.jpg": (30, 30, 220),          # 파랑
    "DECK05_p001.jpg": (220, 220, 30),  # 노랑 — 발표자료 쪽
}


def color_name(rgb: tuple[int, int, int]) -> str:
    red, green, blue = rgb
    if red > 150 and green > 150 and blue < 120:
        return "deck"
    if green > 150 and blue > 150:
        return "B_1"
    if red > 150 and green < 100:
        return "A_1"
    if green > 150 and red < 100:
        return "A_2"
    if blue > 150 and red < 100:
        return "A_3"
    return f"?{rgb}"


def make_mix_groups(pkg: Path, plan_order: Optional[list[str]] = None) -> tuple[str, str]:
    """모서리 없는 사진 3장 + 발표자료 1쪽이 든 그룹과, 사진 1장뿐인 둘째 그룹을 만든다.

    발표자료(DECK05_p001.jpg)는 원본 폴더에 없고 작업 폴더 img 에만 있다(실사용과 같다).
    """
    mix, other = "01_MIX", "02_B"
    src = pkg / "01_원본사진"
    work = pkg / "02_작업장"
    for group in (mix, other):
        (work / group / "img").mkdir(parents=True, exist_ok=True)
    for name, color in MIX_COLORS.items():
        Image.new("RGB", (160, 120), color).save(work / mix / "img" / name)
        if not name.startswith("DECK"):
            Image.new("RGB", (160, 120), color).save(src / name)
    Image.new("RGB", (160, 120), (30, 220, 220)).save(work / other / "img" / "B_1.jpg")
    Image.new("RGB", (160, 120), (30, 220, 220)).save(src / "B_1.jpg")
    write_mix_plan(pkg, plan_order)
    return mix, other


def write_mix_plan(pkg: Path, plan_order: Optional[list[str]] = None) -> None:
    plan = {
        "_type": "slide_tool_worktree",
        "_version": 2,
        "root": ".",
        "source": "../01_원본사진",
        "groups": {
            "01_MIX": plan_order or ["A_1.jpg", "A_2.jpg", "A_3.jpg", "DECK05_p001.jpg"],
            "02_B": ["B_1.jpg"],
        },
    }
    (pkg / "02_작업장" / "worktree.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result = subprocess.run(
        [sys.executable, str(pkg / "02_작업장" / "slide_tool" / "gen_manifest.py")],
        cwd=pkg,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, (result.stdout + result.stderr).strip()


def mix_key(name: str, group: str = "01_MIX") -> str:
    return f"../{group}/img/{name}"


def page_names(path: Path) -> list[str]:
    count = pdf_page_count(path)
    return [color_name(color) for color in pdf_page_colors(path, count)]


def expect_pages(path: Path, expected: list[str]) -> None:
    actual = page_names(path)
    assert actual == expected, f"{path.name}: 기대 {expected} 실제 {actual}"


def export_request(
    base: str, token: str, request: dict[str, object], timeout: float = 90
) -> dict[str, object]:
    status, _, payload = json_post(base, "/api/export-pdf", token, request)
    assert status == 202, payload
    job = wait_job(base, token, timeout)
    assert job.get("state") == "done", job_log(job)
    return job


def run_export_regress() -> list[bool]:
    """PDF 결과 정확성 회귀: 모서리 없는 사진·발표자료·화면 순서·쪽 수 요약."""
    results: list[bool] = []
    processes: list[subprocess.Popen[str]] = []
    with tempfile.TemporaryDirectory(prefix="workflow_export_regress_") as temp_dir:
        temp = Path(temp_dir)

        def setup_case(
            name: str, plan_order: Optional[list[str]] = None
        ) -> tuple[Path, str, str]:
            pkg = make_pkg(temp / name)
            make_mix_groups(pkg, plan_order)
            _, base, token = start_server(pkg, processes)
            return pkg, base, token

        def r1() -> None:
            # 모서리는 A_1 에만 있다. A_2·A_3·발표자료도 PDF 에 들어가야 하고 제외만 빠진다.
            pkg, base, token = setup_case("untouched")
            backup = make_backup([mix_key("A_1.jpg")])
            job = export_request(base, token, {"backup": backup, "mode": "per-folder"})
            mix_pdf = pkg / "03_결과물" / "01_MIX.pdf"
            expect_pages(mix_pdf, ["A_1", "A_2", "A_3", "deck"])
            assert (pkg / "03_결과물" / "02_B.pdf").is_file(), "손대지 않은 둘째 그룹 PDF 없음"
            groups = {g["name"]: g["pages"] for g in job["result"]["groups"]}
            assert groups == {"01_MIX": 4, "02_B": 1}, job["result"]
            assert job["result"]["emptyGroups"] == [], job["result"]

            excluded = make_backup(
                [mix_key("A_1.jpg")],
                statuses={mix_key("A_2.jpg"): {"excluded": True}},
            )
            export_request(base, token, {"backup": excluded, "mode": "per-folder"})
            expect_pages(mix_pdf, ["A_1", "A_3", "deck"])

        results.append(report("R1 모서리 없는 사진도 PDF 포함·제외만 빠짐·그룹별 쪽 수", r1))

        def r2() -> None:
            # 완료본만: 발표자료는 완료 표시 없이도 남고, 미완료 그룹은 요약에 빠진 그룹으로 나온다.
            pkg, base, token = setup_case("deck_modes")
            done = make_backup(
                [mix_key("A_1.jpg")],
                statuses={mix_key("A_1.jpg"): {"done": True}},
            )
            out = pkg / "03_결과물"
            groups = ["01_MIX", "02_B"]

            job = export_request(
                base, token, {"backup": done, "mode": "per-folder", "onlyDone": True}
            )
            expect_pages(out / "01_MIX.pdf", ["A_1", "deck"])
            assert not (out / "02_B.pdf").exists()
            assert job["result"]["emptyGroups"] == ["02_B"], job["result"]

            job = export_request(
                base, token, {"backup": done, "mode": "merged", "onlyDone": True}
            )
            expect_pages(out / "전체.pdf", ["A_1", "deck"])
            expect_pages(out / "01_MIX.pdf", ["A_1", "deck"])

            export_request(
                base,
                token,
                {"backup": done, "mode": "ordered", "order": groups, "onlyDone": True},
            )
            expect_pages(out / "전체.pdf", ["A_1", "deck"])
            # 완료본만이 아닐 때는 모든 방식에서 4+1쪽(발표자료 포함).
            everything = make_backup([mix_key("A_1.jpg")])
            export_request(base, token, {"backup": everything, "mode": "merged"})
            expect_pages(out / "전체.pdf", ["A_1", "A_2", "A_3", "deck", "B_1"])
            export_request(
                base,
                token,
                {"backup": everything, "mode": "ordered", "order": list(reversed(groups))},
            )
            expect_pages(out / "전체.pdf", ["B_1", "A_1", "A_2", "A_3", "deck"])

        results.append(report("R2 DECK 쪽이 그룹별·통합·지정 순서·완료본만에서 모두 포함", r2))

        def r3() -> None:
            # 화면 순서: 브라우저가 보낸 그룹별 최종 순서가 파일명 정렬보다 우선한다.
            pkg, base, token = setup_case("photo_order")
            backup = make_backup([mix_key("A_1.jpg")])
            out = pkg / "03_결과물"
            wanted = [
                mix_key("A_3.jpg"),
                mix_key("DECK05_p001.jpg"),
                mix_key("A_1.jpg"),
                mix_key("A_2.jpg"),
            ]
            job = export_request(
                base,
                token,
                {"backup": backup, "mode": "per-folder", "photoOrder": {"01_MIX": wanted}},
            )
            expect_pages(out / "01_MIX.pdf", ["A_3", "deck", "A_1", "A_2"])
            assert "화면 순서 사용" in job_log(job)
            # 통합 방식에서도 같은 순서로 이어 붙는다.
            export_request(
                base,
                token,
                {"backup": backup, "mode": "merged", "photoOrder": {"01_MIX": wanted}},
            )
            expect_pages(out / "전체.pdf", ["A_3", "deck", "A_1", "A_2", "B_1"])
            # 목록에 없는 사진(새로 들어온 것)은 뒤에 이름순으로 붙는다.
            export_request(
                base,
                token,
                {
                    "backup": backup,
                    "mode": "per-folder",
                    "photoOrder": {"01_MIX": [mix_key("A_2.jpg")]},
                },
            )
            expect_pages(out / "01_MIX.pdf", ["A_2", "A_1", "A_3", "deck"])

        results.append(report("R3 PDF 쪽 순서 = 화면이 보낸 그룹별 최종 순서", r3))

        def r4() -> None:
            # photoOrder 없이(옛 화면·CLI) 불러도 화면 규칙(촬영순 seq → 백업 slideOrder)을 재현한다.
            # 계획 순서가 파일명 순서와 다른 세션(카운터 롤오버)을 흉내낸다.
            pkg, base, token = setup_case(
                "fallback_order", ["A_3.jpg", "A_1.jpg", "A_2.jpg", "DECK05_p001.jpg"]
            )
            out = pkg / "03_결과물"
            backup = make_backup([mix_key("A_1.jpg")])
            export_request(base, token, {"backup": backup, "mode": "per-folder"})
            expect_pages(out / "01_MIX.pdf", ["A_3", "A_1", "A_2", "deck"])

            with_user_order = make_backup([mix_key("A_1.jpg")])
            data = with_user_order["data"]
            assert isinstance(data, dict)
            data["slideOrder_v1"] = json.dumps(
                {"01_MIX": [mix_key("DECK05_p001.jpg"), mix_key("A_2.jpg")]}
            )
            export_request(base, token, {"backup": with_user_order, "mode": "per-folder"})
            # 지정 순서에 없는 사진은 화면 규칙대로 뒤에 이름순(A_1, A_3).
            expect_pages(out / "01_MIX.pdf", ["deck", "A_2", "A_1", "A_3"])

        results.append(report("R4 photoOrder 없을 때 seq·slideOrder 로 화면 순서 재현", r4))

        def r5() -> None:
            _pkg, base, token = setup_case("bad_photo_order")
            backup = make_backup([mix_key("A_1.jpg")])
            for bad in (
                "text",
                ["A_1.jpg"],
                {"01_MIX": "A_1.jpg"},
                {"01_MIX": [1, 2]},
                {"01_MIX": ["ok\u0000bad"]},
                {"": [mix_key("A_1.jpg")]},
            ):
                status, _, payload = json_post(
                    base,
                    "/api/export-pdf",
                    token,
                    {"backup": backup, "mode": "per-folder", "photoOrder": bad},
                )
                assert status == 400, (bad, status, payload)
                assert payload.get("error") == "bad_photo_order", payload

        results.append(report("R5 잘못된 photoOrder 400 거부", r5))

        def r6() -> None:
            module = load_server_module()
            sys.path.insert(0, str(SCRIPT_DIR))
            import export_pdf

            assert module.PDF_SUMMARY_PREFIX == export_pdf.SUMMARY_PREFIX
            for name in ("DECK05_p001.jpg", "deck1_p12.PNG", "DECK_p3.jpg"):
                assert export_pdf.is_deck(name), name
            for name in ("DECK05.jpg", "IMG_DECK1_p1.jpg", "DECK1_p1", "xDECK1_p1.jpg"):
                assert not export_pdf.is_deck(name), name

        results.append(report("R6 요약 머리말·DECK 판별 규약 동기화", r6))
    for process in processes:
        stop_process(process)
    return results


def run_job() -> list[bool]:
    results: list[bool] = []
    processes: list[subprocess.Popen[str]] = []
    with tempfile.TemporaryDirectory(prefix="workflow_job_") as temp_dir:
        temp = Path(temp_dir)
        pkg = make_pkg(temp)
        out = pkg / "03_결과물"
        try:
            _, base, token = start_server(pkg, processes)

            prepare_error: Optional[Exception] = None
            busy_error: Optional[Exception] = None
            try:
                first_status, _, first = json_post(
                    base,
                    "/api/prepare",
                    token,
                    {"regroup": False, "gapMinutes": 20},
                )
                assert first_status == 202, first
                second_status, _, second = json_post(
                    base,
                    "/api/prepare",
                    token,
                    {"regroup": False, "gapMinutes": 20},
                )
                try:
                    assert second_status == 409 and second.get("error") == "busy", second
                except Exception as exc:
                    busy_error = exc
                final = wait_job(base, token, 90)
                assert final.get("state") == "done", job_log(final)
                groups = [
                    path
                    for path in (pkg / "02_작업장").iterdir()
                    if path.is_dir() and path.name != "slide_tool"
                ]
                assert groups and list((groups[0] / "img").glob("*.jpg"))
                assert (pkg / "02_작업장" / "slide_tool" / "data.js").is_file()
                log = job_log(final)
                assert "phase 1/3" in log and "phase 2/3" in log and "phase 3/3" in log
            except Exception as exc:
                prepare_error = exc
            if prepare_error is None:
                print("[OK] J1 prepare 3단계·산출물")
                results.append(True)
            else:
                print(f"[FAIL] J1 prepare 3단계·산출물: {prepare_error}")
                results.append(False)
            if busy_error is None and prepare_error is None:
                print("[OK] J2 잡 상호 배타 busy")
                results.append(True)
            else:
                print(f"[FAIL] J2 잡 상호 배타 busy: {busy_error or prepare_error}")
                results.append(False)

            group_dirs = [
                path
                for path in (pkg / "02_작업장").iterdir()
                if path.is_dir() and path.name != "slide_tool"
            ]
            group = group_dirs[0].name if group_dirs else "missing"
            key1 = f"../{group}/img/IMG_1.jpg"
            key2 = f"../{group}/img/IMG_2.jpg"

            def j3() -> None:
                absolute = temp / "outside_absolute"
                parent_pdf = temp / "탈출.pdf"
                before_outside = outside_pdf_snapshot(temp, out)
                backup = make_backup(
                    [key1, key2],
                    moves={key1: str(absolute), key2: "../탈출"},
                )
                job = export_job(base, token, backup, 90)
                assert not absolute.with_suffix(".pdf").exists()
                assert not parent_pdf.exists()
                assert outside_pdf_snapshot(temp, out) == before_outside
                assert "이동 폴더명 거부" in job_log(job)

            results.append(report("J3 API 관통 이동명 경로탈출 차단", j3))

            def j4() -> None:
                secret = temp / "secret.png"
                Image.new("RGB", (120, 90), (220, 20, 30)).save(secret)
                # 그룹 폴더의 사진은 모서리 없이도 다시 만들어지므로(내용 해시는 생성 시각에 따라
                # 달라질 수 있다) 해시 대신 PDF 이름과 쪽 수를 비교한다 — 탈출 키의 비밀 이미지가
                # 끼면 쪽이 늘어난다.
                before = {path.name: pdf_page_count(path) for path in out.glob("*.pdf")}
                before_outside = outside_pdf_snapshot(temp, out)
                bad_key = "../g/img/../../../secret.png"
                job = export_job(base, token, make_backup([bad_key]), 60)
                after = {path.name: pdf_page_count(path) for path in out.glob("*.pdf")}
                assert before == after, (before, after)
                assert outside_pdf_snapshot(temp, out) == before_outside
                assert "백업 키 거부" in job_log(job)

            results.append(report("J4 API 관통 백업 키 경로탈출 차단", j4))

            def j5() -> None:
                job = export_job(
                    base,
                    token,
                    make_backup([key1], ratios={key1: 0.000001}),
                    90,
                )
                assert "비율 값 거부" in job_log(job)
                assert (out / f"{group}.pdf").is_file()

            results.append(report("J5 API 관통 거대비율 안전 폴백", j5))

            def j6() -> None:
                job = export_job(
                    base,
                    token,
                    make_backup([key1], ratios={key1: "4:3"}),
                    90,
                )
                assert job.get("state") == "done"
                assert (out / f"{group}.pdf").is_file()
                assert list((out / "백업").glob("slide_tool_backup_*.json"))

            results.append(report("J6 정상 백업 PDF·서버 백업 생성", j6))

            def j7() -> None:
                status, _, raw = http_request(
                    "GET",
                    base + "/api/job",
                    headers={"X-Workflow-Token": token},
                )
                assert status == 200
                before_job = decode_json(raw).get("job")
                before_id = before_job.get("id") if isinstance(before_job, dict) else None
                headers = api_headers(base, token)
                oversized, _, _ = declared_request(
                    "POST",
                    base + "/api/export-pdf",
                    64 * 1024 * 1024 + 1,
                    headers,
                )
                assert oversized == 413, oversized
                _, _, after_raw = http_request(
                    "GET",
                    base + "/api/job",
                    headers={"X-Workflow-Token": token},
                )
                after_job = decode_json(after_raw).get("job")
                after_id = after_job.get("id") if isinstance(after_job, dict) else None
                assert after_id == before_id

            results.append(report("J7 export 64MB 본문 상한", j7))

            def j8() -> None:
                cancel_pkg = make_pkg(temp / "cancel_case")
                cancel_python = cancel_pkg / ".venv" / (
                    "Scripts" if sys.platform == "win32" else "bin"
                )
                cancel_python = cancel_python / (
                    "python.exe" if sys.platform == "win32" else "python3"
                )
                cancel_python.unlink()
                cancel_python.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json, os, pathlib, subprocess, sys, time\n"
                    "pkg = pathlib.Path(__file__).resolve().parents[2]\n"
                    "child = subprocess.Popen([sys.executable, '-c', "
                    "'import time; time.sleep(30)'])\n"
                    "marker = pkg / '00_시작' / 'cancel_pids.json'\n"
                    "marker.write_text(json.dumps([os.getpid(), child.pid]), encoding='utf-8')\n"
                    "time.sleep(30)\n",
                    encoding="utf-8",
                )
                cancel_python.chmod(0o755)
                cancel_status_path = cancel_pkg / "00_시작" / "_env_status.json"
                cancel_status_data = json.loads(
                    cancel_status_path.read_text(encoding="utf-8")
                )
                cancel_status_data["venv_py"] = str(cancel_python.absolute())
                cancel_status_path.write_text(
                    json.dumps(cancel_status_data), encoding="utf-8"
                )
                _, cancel_base, cancel_token = start_server(cancel_pkg, processes)
                status, _, payload = json_post(
                    cancel_base,
                    "/api/export-pdf",
                    cancel_token,
                    {"backup": {"opaque": True}, "merge": False},
                )
                assert status == 202, payload
                marker = cancel_pkg / "00_시작" / "cancel_pids.json"
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not marker.is_file():
                    time.sleep(0.02)
                assert marker.is_file(), "API 자식 PID 표식이 생성되지 않았습니다."
                pids = json.loads(marker.read_text(encoding="utf-8"))
                assert isinstance(pids, list) and len(pids) == 2
                cancel_status, _, cancelled = json_post(
                    cancel_base, "/api/job/cancel", cancel_token, {}
                )
                assert cancel_status == 200, cancelled
                cancelled_job = cancelled.get("job")
                assert isinstance(cancelled_job, dict)
                assert cancelled_job.get("state") == "cancelled"
                final = wait_job(cancel_base, cancel_token, 10)
                assert final.get("state") == "cancelled", final
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and any(
                    pid_alive(int(pid)) for pid in pids
                ):
                    time.sleep(0.05)
                assert not any(pid_alive(int(pid)) for pid in pids), (
                    "API 취소 뒤 동일 프로세스 그룹이 남았습니다.",
                    pids,
                )

                module = load_server_module()
                manager = module.JobManager()
                direct = manager.start(
                    "export",
                    [("slow", [sys.executable, "-c", "import time; time.sleep(30)"])],
                )
                assert direct is not None
                deadline = time.monotonic() + 5
                process = None
                while time.monotonic() < deadline:
                    with manager.lock:
                        process = manager._process
                    if process is not None:
                        break
                    time.sleep(0.02)
                assert process is not None, "직접 취소 검증용 자식이 시작되지 않았습니다."
                assert manager.cancel() is True
                assert process.poll() is not None, "취소 후 자식 프로세스가 남았습니다."
                manager.shutdown()

            results.append(report("J8 API 취소·자식 프로세스 종료", j8))

            def j9() -> None:
                watch_pkg = make_pkg(temp / "watchdog_case")
                python = watch_pkg / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
                python = python / ("python.exe" if sys.platform == "win32" else "python3")
                python.unlink()
                python.write_text(
                    "#!/usr/bin/env python3\n"
                    "import time\n"
                    "print('slow job start', flush=True)\n"
                    "time.sleep(3.5)\n"
                    "print('slow job done', flush=True)\n",
                    encoding="utf-8",
                )
                python.chmod(0o755)
                status_path = watch_pkg / "00_시작" / "_env_status.json"
                status_data = json.loads(status_path.read_text(encoding="utf-8"))
                status_data["venv_py"] = str(python.absolute())
                status_path.write_text(json.dumps(status_data), encoding="utf-8")
                process, watch_base, watch_token = start_server(
                    watch_pkg,
                    processes,
                    watchdog=True,
                    timeout=2,
                    grace=4,
                )
                beat_status, _, _ = http_request("POST", watch_base + "/heartbeat", body=b"")
                assert beat_status == 204
                start_status, _, start_payload = json_post(
                    watch_base,
                    "/api/export-pdf",
                    watch_token,
                    {"backup": {"opaque": True}},
                )
                assert start_status == 202, start_payload
                time.sleep(2.5)
                assert process.poll() is None, "잡 실행 중 워치독이 서버를 종료했습니다."
                final = wait_job(watch_base, watch_token, 5)
                assert final.get("state") == "done", final
                return_code = process.wait(timeout=5)
                assert return_code == 0

            results.append(report("J9 잡 실행 중 워치독 보류·후속 종료", j9))
            results.append(
                report(
                    "J10 기존 export 보안 게이트",
                    lambda: subprocess_gate(SECURITY_TEST, "SECURITY: ALL PASS", 120),
                )
            )
        finally:
            for process in processes:
                stop_process(process)
    return results


# 자동 찾기 API 합성 사진의 정답 꼭짓점 — 화면에서 본 방향 [TL, TR, BL, BR] 정규좌표.
DETECT_TRUTH = [[0.20, 0.16], [0.80, 0.22], [0.17, 0.84], [0.83, 0.79]]
DETECT_TOLERANCE = 0.03


def ccw_point(point: list[float]) -> list[float]:
    """PIL ROTATE_90(반시계) 이 사진 안의 한 점(정규좌표)에 하는 일."""
    return [point[1], 1.0 - point[0]]


def make_screen_photo(path: Path, turns: int = 0, size: tuple[int, int] = (800, 600)) -> list[list[float]]:
    """어두운 배경 위 밝은 원근 사각형 사진을 만들고, 파일 좌표계의 정답 [TL,TR,BL,BR] 을 돌려준다.

    turns = 파일을 반시계 90° 돌린 횟수 — 카메라를 눕혀 찍은 사진을 흉내 낸다. 브라우저는 이 사진을
    시계방향 turns 번 돌려 세워 보므로 서버에는 rotations={키: turns} 를 보낸다.
    """
    from PIL import ImageDraw

    width, height = size
    image = Image.new("RGB", size, (24, 24, 30))
    draw = ImageDraw.Draw(image)
    quad = [(x * width, y * height) for x, y in DETECT_TRUTH]
    draw.polygon([quad[0], quad[1], quad[3], quad[2]], fill=(235, 235, 225))
    # 슬라이드 내용처럼 보이는 어두운 블록 몇 개.
    for left, top, right, bottom in ((0.30, 0.30, 0.70, 0.36), (0.30, 0.45, 0.62, 0.50), (0.30, 0.58, 0.55, 0.63)):
        draw.rectangle([left * width, top * height, right * width, bottom * height], fill=(60, 70, 110))
    truth = [list(point) for point in DETECT_TRUTH]
    for _ in range(turns):
        image = image.transpose(Image.Transpose.ROTATE_90)
        truth = [ccw_point(point) for point in truth]
    image.save(path, quality=95)
    return truth


def max_corner_error(found: list[list[float]], truth: list[list[float]]) -> float:
    return max(
        max(abs(fx - tx), abs(fy - ty))
        for (fx, fy), (tx, ty) in zip(found, truth)
    )


def run_auto_detect() -> list[bool]:
    results: list[bool] = []
    processes: list[subprocess.Popen[str]] = []
    with tempfile.TemporaryDirectory(prefix="workflow_detect_") as temp_dir:
        temp = Path(temp_dir)
        pkg = make_pkg(temp)
        work = pkg / "02_작업장"
        group = "01_발표"
        image_dir = work / group / "img"
        image_dir.mkdir(parents=True)
        keys = {
            "upright": f"../{group}/img/UPRIGHT.jpg",
            "side": f"../{group}/img/SIDE.jpg",
            "flip": f"../{group}/img/FLIP.png",
            "blank": f"../{group}/img/BLANK.jpg",
        }
        truths = {
            "upright": make_screen_photo(image_dir / "UPRIGHT.jpg"),
            "side": make_screen_photo(image_dir / "SIDE.jpg", turns=1),
            "flip": make_screen_photo(image_dir / "FLIP.png", turns=2),
        }
        Image.new("RGB", (400, 300), (90, 90, 90)).save(image_dir / "BLANK.jpg")
        secret = temp / "secret.png"
        Image.new("RGB", (120, 90), (220, 20, 30)).save(secret)
        try:
            _, base, token = start_server(pkg, processes)
            endpoint = "/api/auto-detect"

            def d1() -> None:
                status, headers, payload = json_post(
                    base,
                    endpoint,
                    token,
                    {
                        "keys": [keys["upright"], keys["side"], keys["flip"], keys["blank"]],
                        "rotations": {keys["side"]: 1, keys["flip"]: 2},
                    },
                )
                assert status == 200 and payload.get("ok") is True, (status, payload)
                assert headers.get("content-type") == "application/json; charset=utf-8"
                rows = payload.get("results")
                assert isinstance(rows, dict) and set(rows) == set(keys.values()), rows
                for name, truth in truths.items():
                    row = rows[keys[name]]
                    assert isinstance(row, dict), row
                    corners = row.get("corners")
                    assert isinstance(corners, list) and len(corners) == 4, (name, row)
                    error = max_corner_error(corners, truth)
                    assert error < DETECT_TOLERANCE, f"{name}: 최대 오차 {error:.4f} {corners} != {truth}"
                    conf = row.get("conf")
                    assert isinstance(conf, (int, float)) and 0.0 <= conf <= 1.0, (name, row)
                    assert row.get("review") is False, (name, row)
                blank = rows[keys["blank"]]
                assert blank.get("corners") is None and blank.get("review") is True, blank

            results.append(report("D1 합성 사진 검출 오차·회전 좌표·null 확인", d1))

            def d2() -> None:
                # 회전 힌트 없이 눕힌 사진을 보내면 좌표계가 어긋난다는 것도 확인 — rotations 가 실제로 쓰인다.
                status, _, payload = json_post(base, endpoint, token, {"keys": [keys["side"]]})
                assert status == 200, (status, payload)
                row = payload["results"][keys["side"]]  # type: ignore[index]
                corners = row.get("corners")
                if corners is not None:
                    assert max_corner_error(corners, truths["side"]) >= DETECT_TOLERANCE, row

            results.append(report("D2 rotations 없으면 회전 사진 좌표 불일치", d2))

            def d3() -> None:
                (image_dir / "LINK.png").symlink_to(secret)
                bad_keys = [
                    ("../01_발표/img/../../../secret.png", 400),
                    ("../../secret.png", 400),
                    ("../01_발표/img/..%2f..%2fsecret.png", 400),
                    ("/etc/passwd", 400),
                    (str(secret), 400),
                    ("../01_발표/img/sub/x.png", 400),
                    ("../01_발표/img/..\\x.png", 400),
                    ("../../package/02_작업장/01_발표/img/UPRIGHT.jpg", 400),
                    ("../../01_발표/img/UPRIGHT.jpg", 400),
                    ("../slide_tool/img/UPRIGHT.jpg", 404),
                    ("../01_발표/img/UPRIGHT.txt", 400),
                    ("../01_발표/img/.hidden.jpg", 400),
                    ("../01_발표/img/NOFILE.jpg", 404),
                    ("../없는그룹/img/UPRIGHT.jpg", 404),
                    ("../01_발표/img/LINK.png", 400),
                    ("", 400),
                    (None, 400),
                    (7, 400),
                ]
                for key, expected in bad_keys:
                    status, _, payload = json_post(base, endpoint, token, {"keys": [key]})
                    assert status == expected, (key, status, payload)
                    assert payload.get("ok") is False and payload.get("error"), (key, payload)
                # 한 장이라도 나쁜 키가 섞이면 요청 전체를 거부한다.
                status, _, payload = json_post(
                    base, endpoint, token, {"keys": [keys["upright"], "../01_발표/img/NOFILE.jpg"]}
                )
                assert status == 404, (status, payload)
                for body in (
                    {},
                    {"keys": []},
                    {"keys": "../01_발표/img/UPRIGHT.jpg"},
                    {"keys": [keys["upright"]] * 33},
                    {"keys": [keys["upright"]], "rotations": []},
                    {"keys": [keys["upright"]], "rotations": {keys["upright"]: 4}},
                    {"keys": [keys["upright"]], "rotations": {keys["upright"]: True}},
                    {"keys": [keys["upright"]], "rotations": {keys["side"]: 1}},
                ):
                    status, _, payload = json_post(base, endpoint, token, body)
                    assert status == 400 and payload.get("error") == "bad_request", (body, status, payload)
                assert secret.read_bytes() and not (temp / "01_발표").exists()

            results.append(report("D3 경로 탈출·없는 파일·잘못된 본문 거부", d3))

            def d4() -> None:
                body = json.dumps({"keys": [keys["upright"]]}).encode("utf-8")
                status, _, raw = http_request(
                    "POST", base + endpoint,
                    headers={"Origin": base, "Content-Type": "application/json"},
                    body=body,
                )
                assert status == 401 and decode_json(raw).get("error") == "token_missing", (status, raw)
                status, _, raw = http_request(
                    "POST", base + endpoint,
                    headers={"Origin": base, "X-Workflow-Token": "wrong", "Content-Type": "application/json"},
                    body=body,
                )
                assert status == 403 and decode_json(raw).get("error") == "token_invalid", (status, raw)
                status, _, raw = http_request(
                    "POST", base + endpoint,
                    headers={"X-Workflow-Token": token, "Content-Type": "application/json"},
                    body=body,
                )
                assert status == 403 and decode_json(raw).get("error") == "bad_origin", (status, raw)
                status, _, raw = http_request(
                    "POST", base + endpoint,
                    headers={"Origin": "http://evil.example", "X-Workflow-Token": token,
                             "Content-Type": "application/json"},
                    body=body,
                )
                assert status == 403 and decode_json(raw).get("error") == "bad_origin", (status, raw)
                status, _, raw = http_request("GET", base + endpoint, headers={"X-Workflow-Token": token})
                assert status == 404, (status, raw)
                status, _, raw = http_request(
                    "POST", base + endpoint, headers=api_headers(base, token), body=b"{not json"
                )
                assert status == 400 and decode_json(raw).get("error") == "bad_json", (status, raw)
                status, _, raw = declared_request(
                    "POST", base + endpoint, 1024 * 1024, api_headers(base, token)
                )
                assert status == 413, (status, raw)

            results.append(report("D4 토큰·Origin·본문 검증", d4))

            def d5() -> None:
                # 워크플로 API 가 꺼진 서버 루트(패키지 해석 실패)에서는 503.
                bare = temp / "bare_server"
                (bare / "slide_tool").mkdir(parents=True)
                (bare / "slide_tool" / "index.html").write_text("<!doctype html>", encoding="utf-8")
                _, bare_base, bare_token = start_server(pkg, processes, root=bare, pkg_arg=bare)
                status, _, payload = json_post(
                    bare_base, endpoint, bare_token, {"keys": [keys["upright"]]}
                )
                assert status == 503 and payload.get("error") == "workflow_disabled", (status, payload)

            results.append(report("D5 워크플로 비활성 서버 503", d5))
        finally:
            for process in processes:
                stop_process(process)
    return results


def token_get(base: str, path: str, token: Optional[str]) -> tuple[int, dict[str, object]]:
    headers = {"X-Workflow-Token": token} if token is not None else {}
    status, _, raw = http_request("GET", base + path, headers=headers)
    return status, decode_json(raw)


def work_children(pkg: Path) -> list[str]:
    return sorted(path.name for path in (pkg / "02_작업장").iterdir())


def make_event_pkg(temp: Path) -> tuple[Path, list[str]]:
    """그룹 2개 + 계획 + data.js + 원본 사진 2장이 있는 패키지."""
    pkg = make_pkg(temp)
    work = pkg / "02_작업장"
    names = ["01_가나", "02_다라"]
    plan_groups: dict[str, list[str]] = {}
    for index, name in enumerate(names):
        image_dir = work / name / "img"
        image_dir.mkdir(parents=True)
        photos = [f"P{index}_{n}.jpg" for n in (1, 2)]
        for photo in photos:
            Image.new("RGB", (60, 40), (40 + index * 60, 100, 100)).save(image_dir / photo)
        plan_groups[name] = photos
    plan = {
        "_type": "slide_tool_worktree",
        "_version": 2,
        "root": ".",
        "source": "../01_원본사진",
        "groups": plan_groups,
    }
    (work / "worktree.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(work / "slide_tool" / "gen_manifest.py")],
        cwd=pkg, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    assert result.returncode == 0, result.stdout
    (pkg / "01_원본사진" / "여기에_사진을_넣으세요.txt").write_text("안내", encoding="utf-8")
    return pkg, names


def run_new_event() -> list[bool]:
    results: list[bool] = []
    processes: list[subprocess.Popen[str]] = []
    archive_re = re.compile(r"^_이전작업_[0-9]{6}_[0-9]{4}(_[0-9]+)?$")
    try:
        with tempfile.TemporaryDirectory(prefix="workflow_event_") as temp_dir:
            temp = Path(temp_dir)
            pkg, names = make_event_pkg(temp)
            work = pkg / "02_작업장"
            out = pkg / "03_결과물"
            (out / "기존.pdf").write_bytes(b"%PDF-1.4 keep")
            outside = temp / "outside"
            (outside / "img").mkdir(parents=True)
            Image.new("RGB", (40, 30), (1, 2, 3)).save(outside / "img" / "o.jpg")
            (work / "link_group").symlink_to(outside, target_is_directory=True)
            _, base, token = start_server(pkg, processes)
            first_archive: list[str] = []

            def e1() -> None:
                status, payload = token_get(base, "/api/status", token)
                assert status == 200, payload
                assert [g["name"] for g in payload["groups"]] == names + ["link_group"], payload
                backup = make_backup([f"../{names[0]}/img/P0_1.jpg"])
                code, _, res = json_post(base, "/api/new-event", token, {"backup": backup})
                assert code == 200 and res.get("ok") is True, (code, res)
                archive = str(res["archive"])
                assert archive_re.match(archive), archive
                first_archive.append(archive)
                moved = res["moved"]
                assert isinstance(moved, dict), res
                assert moved["groups"] == 2 and moved["photos"] == 4, moved
                assert moved["worktree"] is True and moved["dataJs"] is True and moved["originals"] == 0, moved
                folder = work / archive
                for index, name in enumerate(names):
                    assert (folder / name / "img" / f"P{index}_1.jpg").is_file(), name
                assert (folder / "worktree.json").is_file() and (folder / "data.js").is_file()
                assert (folder / "백업.json").is_file()
                saved = res["backup"]
                assert isinstance(saved, str) and saved.startswith("백업/slide_tool_backup_"), res
                assert (out / saved).is_file()
                # 작업장·도구·결과물·원본 상태
                assert not (work / "worktree.json").exists()
                assert not (work / "slide_tool" / "data.js").exists()
                assert (work / "slide_tool" / "index.html").is_file()
                assert (work / "slide_tool" / "gen_manifest.py").is_file()
                for name in names:
                    assert not (work / name).exists(), name
                assert (out / "기존.pdf").read_bytes() == b"%PDF-1.4 keep"
                assert len(list((pkg / "01_원본사진").glob("IMG_*.jpg"))) == 2
                # 심볼릭 링크 그룹은 옮기지 않고 바깥 폴더도 그대로다
                assert (work / "link_group").is_symlink() and (outside / "img" / "o.jpg").is_file()

            results.append(report("N1 새 행사 시작: 이동 결과·보관 폴더 구성·결과물 보존·링크 제외", e1))

            def e2() -> None:
                status, payload = token_get(base, "/api/status", token)
                assert status == 200, payload
                # 보관 폴더(안에 img 가 있는 하위 그룹이 들어 있어도)와 링크 그룹 외에는 그룹이 없다
                assert [g["name"] for g in payload["groups"]] == ["link_group"], payload["groups"]
                assert payload["worktree"] is False and payload["planMismatch"] is None, payload
                assert payload["dataJs"] is False, payload

            results.append(report("N2 새 행사 뒤 status: 보관 폴더는 그룹이 아님·계획 없음", e2))

            def e3() -> None:
                # 보관 폴더에 img 가 바로 들어 있어도 그룹으로 취급하지 않는다(gen_manifest·내보내기·자동 찾기·이름 변경).
                decoy = work / "_이전작업_수동" / "img"
                decoy.mkdir(parents=True)
                Image.new("RGB", (60, 40), (9, 9, 9)).save(decoy / "d.jpg")
                real = work / "01_진짜" / "img"
                real.mkdir(parents=True)
                Image.new("RGB", (60, 40), (99, 9, 9)).save(real / "r.jpg")
                run = subprocess.run(
                    [sys.executable, str(work / "slide_tool" / "gen_manifest.py")],
                    cwd=pkg, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", check=False,
                )
                assert run.returncode == 0, run.stdout
                data = (work / "slide_tool" / "data.js").read_text(encoding="utf-8")
                assert "01_진짜" in data and "_이전작업_" not in data, data
                status, payload = token_get(base, "/api/status", token)
                assert "_이전작업_수동" not in [g["name"] for g in payload["groups"]], payload["groups"]
                code, _, res = json_post(
                    base, "/api/auto-detect", token, {"keys": ["../_이전작업_수동/img/d.jpg"]}
                )
                assert code == 404 and res.get("error") == "image_not_found", (code, res)
                code, _, res = json_post(
                    base, "/api/export-pdf", token,
                    {"backup": make_backup([]), "mode": "ordered", "order": ["_이전작업_수동", "01_진짜"]},
                )
                assert code == 400 and res.get("error") == "bad_order", (code, res)
                code, _, res = json_post(
                    base, "/api/rename-group", token, {"from": "01_진짜", "to": "_보관"}
                )
                assert code == 400 and res.get("error") == "bad_group_name", (code, res)
                job = export_job(base, token, make_backup([f"../01_진짜/img/r.jpg"]), 90)
                names_out = sorted(path.name for path in out.glob("*.pdf"))
                assert "01_진짜.pdf" in names_out and not any(n.startswith("_") for n in names_out), names_out
                assert "_이전작업_수동" not in job_log(job), job_log(job)

            results.append(report("N3 `_` 폴더는 그룹 아님: 목록·내보내기·자동 찾기·이름 변경", e3))

            def e4() -> None:
                plan = temp / "bad_plan"
                (plan / "out").mkdir(parents=True)
                (plan / "src").mkdir()
                Image.new("RGB", (30, 20), (0, 0, 0)).save(plan / "src" / "a.jpg")
                (plan / "worktree.json").write_text(
                    json.dumps({
                        "_type": "slide_tool_worktree", "_version": 2, "root": "out", "source": "src",
                        "groups": {"_숨김": ["a.jpg"]},
                    }, ensure_ascii=False),
                    encoding="utf-8",
                )
                run = subprocess.run(
                    [sys.executable, str(SCRIPT_DIR / "prepare_photos.py"), "--plan", str(plan / "worktree.json")],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                    errors="replace", check=False,
                )
                assert run.returncode != 0 and "밑줄" in run.stdout, run.stdout
                assert not (plan / "out" / "_숨김").exists()

            results.append(report("N4 prepare_photos 는 `_` 시작 그룹명을 거부", e4))

        with tempfile.TemporaryDirectory(prefix="workflow_event2_") as temp_dir:
            temp = Path(temp_dir)
            pkg, names = make_event_pkg(temp)
            work = pkg / "02_작업장"
            src = pkg / "01_원본사진"
            Image.new("RGB", (30, 20), (5, 5, 5)).save(src / "IMG_1.jpg")
            (src / "발표A").mkdir()
            Image.new("RGB", (30, 20), (6, 6, 6)).save(src / "발표A" / "IMG_3.jpg")
            (src / ".숨김").write_text("keep", encoding="utf-8")
            _, base, token = start_server(pkg, processes)

            def e5() -> None:
                code, _, res = json_post(
                    base, "/api/new-event", token, {"moveOriginals": True}
                )
                assert code == 200, (code, res)
                archive = work / str(res["archive"])
                moved = res["moved"]
                assert isinstance(moved, dict) and moved["originals"] == 3, moved
                assert res["backup"] is None and not (archive / "백업.json").exists(), res
                assert (archive / "01_원본사진" / "IMG_1.jpg").is_file()
                assert (archive / "01_원본사진" / "IMG_2.jpg").is_file()
                assert (archive / "01_원본사진" / "발표A" / "IMG_3.jpg").is_file()
                left = sorted(path.name for path in src.iterdir())
                assert left == [".숨김", "여기에_사진을_넣으세요.txt"], left
                status, payload = token_get(base, "/api/status", token)
                assert payload["srcCount"] == 0 and payload["groups"] == [], payload

            results.append(report("N5 moveOriginals: 원본 사진만 보관 폴더로(안내문·숨김 파일 제외)", e5))

            def e6() -> None:
                code, _, res = json_post(base, "/api/new-event", token, {})
                assert code == 409 and res.get("error") == "nothing_to_archive", (code, res)
                first = sorted(p.name for p in work.iterdir() if p.name.startswith("_이전작업_"))
                # 같은 분 안에 다시 시작해도 이전 보관 폴더를 덮어쓰지 않고 접미사로 구분한다.
                (work / "03_새" / "img").mkdir(parents=True)
                Image.new("RGB", (20, 20), (7, 7, 7)).save(work / "03_새" / "img" / "n.jpg")
                code, _, res = json_post(base, "/api/new-event", token, {})
                assert code == 200, (code, res)
                after = sorted(p.name for p in work.iterdir() if p.name.startswith("_이전작업_"))
                assert len(after) == len(first) + 1 and set(first) < set(after), (first, after)
                assert (work / str(res["archive"]) / "03_새" / "img" / "n.jpg").is_file()
                for bad in ({"moveOriginals": "yes"}, {"backup": [1]}):
                    code, _, res = json_post(base, "/api/new-event", token, bad)
                    assert code == 400, (bad, code, res)

            results.append(report("N6 보관할 것 없음 409·이름 충돌 접미사·잘못된 본문 400", e6))

            def e7() -> None:
                url = base + "/api/new-event"
                body = b"{}"
                good = {"Origin": base, "X-Workflow-Token": token, "Content-Type": "application/json"}
                no_token = {k: v for k, v in good.items() if k != "X-Workflow-Token"}
                status, _, _ = http_request("POST", url, headers=no_token, body=body)
                assert status == 401, status
                status, _, _ = http_request("POST", url, headers={**good, "X-Workflow-Token": "x"}, body=body)
                assert status == 403, status
                status, _, _ = http_request("POST", url, headers={**good, "Origin": "http://evil.example"}, body=body)
                assert status == 403, status
                status, _, _ = http_request("POST", url, headers=good, body=body, host_override="evil.example")
                assert status == 403, status

            results.append(report("N7 새 행사: 토큰 없음·오답·Origin·Host 거부", e7))

        with tempfile.TemporaryDirectory(prefix="workflow_event3_") as temp_dir:
            temp = Path(temp_dir)
            pkg, names = make_event_pkg(temp)
            work = pkg / "02_작업장"
            _, base, token = start_server(pkg, processes)

            def e8() -> None:
                code, _, first = json_post(
                    base, "/api/prepare", token, {"regroup": True, "gapMinutes": 20}
                )
                assert code == 202, first
                code, _, res = json_post(base, "/api/new-event", token, {})
                try:
                    assert code == 409 and res.get("error") == "busy", (code, res)
                finally:
                    wait_job(base, token, 90)
                assert (work / "worktree.json").is_file(), "실행 중 거부인데 작업장이 바뀌었습니다."

            results.append(report("N8 잡 실행 중 새 행사는 409 busy", e8))

        with tempfile.TemporaryDirectory(prefix="workflow_event4_") as temp_dir:
            temp = Path(temp_dir)
            pkg, names = make_event_pkg(temp)
            work = pkg / "02_작업장"
            src = pkg / "01_원본사진"
            _, base, token = start_server(pkg, processes)

            def diff() -> object:
                status, payload = token_get(base, "/api/status", token)
                assert status == 200, payload
                return payload["planMismatch"]

            def m1() -> None:
                # 계획: P0_1 P0_2 P1_1 P1_2 / 원본: IMG_1 IMG_2 → 4 없어짐, 2 새로 옴
                assert diff() == {"missing": 4, "added": 2}, diff()
                for index in range(2):
                    for n in (1, 2):
                        Image.new("RGB", (30, 20), (index, n, 0)).save(src / f"P{index}_{n}.jpg")
                assert diff() == {"missing": 0, "added": 2}, diff()
                for name in ("IMG_1.jpg", "IMG_2.jpg"):
                    (src / name).unlink()
                assert diff() == {"missing": 0, "added": 0}, diff()
                (src / "P0_2.jpg").unlink()
                assert diff() == {"missing": 1, "added": 0}, diff()
                (src / ".가려짐.jpg").write_bytes(b"x")
                (src / "NEW_9.png").write_bytes(b"x")
                assert diff() == {"missing": 1, "added": 1}, diff()
                for path in list(src.glob("*.jpg")) + list(src.glob("*.png")):
                    path.unlink()
                assert diff() == {"missing": 4, "added": 0}, diff()

            results.append(report("M1 planMismatch: 새 사진·없어진 사진·일치·원본 0장·숨김 파일 무시", m1))

            def m2() -> None:
                (work / "worktree.json").unlink()
                assert diff() is None, diff()
                (work / "worktree.json").write_text("{깨짐", encoding="utf-8")
                assert diff() is None, diff()

            results.append(report("M2 계획 파일 없음/손상이면 planMismatch null", m2))

        with tempfile.TemporaryDirectory(prefix="workflow_backups_") as temp_dir:
            temp = Path(temp_dir)
            pkg = make_pkg(temp)
            backup_dir = pkg / "03_결과물" / "백업"
            _, base, token = start_server(pkg, processes)

            def b1() -> None:
                status, payload = token_get(base, "/api/backups", token)
                assert status == 200 and payload["backups"] == [], payload
                backup_dir.mkdir(parents=True)
                old = backup_dir / "slide_tool_backup_20260101-090000.json"
                new = backup_dir / "slide_tool_backup_20260102-090000.json"
                old.write_text(json.dumps(make_backup(["../a/img/x.jpg"])), encoding="utf-8")
                new.write_text(json.dumps(make_backup(["../b/img/y.jpg"])), encoding="utf-8")
                os.utime(old, (1_767_000_000, 1_767_000_000))
                os.utime(new, (1_768_000_000, 1_768_000_000))
                (backup_dir / "메모.json").write_text("{}", encoding="utf-8")
                (backup_dir / "slide_tool_backup_bad.json").write_text("{}", encoding="utf-8")
                secret = temp / "secret.json"
                secret.write_text(json.dumps(make_backup(["../s/img/s.jpg"])), encoding="utf-8")
                (backup_dir / "slide_tool_backup_20260103-090000.json").symlink_to(secret)
                status, payload = token_get(base, "/api/backups", token)
                assert status == 200, payload
                names = [row["name"] for row in payload["backups"]]
                assert names == [new.name, old.name], names
                assert all(isinstance(row["size"], int) and row["modified"] > 0 for row in payload["backups"])
                status, payload = token_get(base, "/api/backup?name=" + urllib.parse.quote(new.name), token)
                assert status == 200 and payload["name"] == new.name, payload
                assert payload["backup"]["_type"] == "slide_tool_backup", payload
                assert "../b/img/y.jpg" in payload["backup"]["data"]["slideCorners_v1"]

            results.append(report("B1 백업 목록(최신 순·형식 필터·링크 제외)과 내용 조회", b1))

            def b2() -> None:
                for name in (
                    "../slide_tool_backup_20260101-090000.json",
                    "..%2f..%2f00_시작%2fserve_tool.py",
                    "/etc/passwd",
                    "slide_tool_backup_20260101-090000.json/../x",
                    "slide_tool_backup_20260101-090000.json%00.txt",
                    "메모.json",
                    "",
                ):
                    status, payload = token_get(
                        base, "/api/backup?name=" + urllib.parse.quote(name, safe="%"), token
                    )
                    assert status == 400 and payload.get("error") == "bad_backup_name", (name, status, payload)
                status, payload = token_get(base, "/api/backup", token)
                assert status == 400, (status, payload)
                status, payload = token_get(
                    base, "/api/backup?name=slide_tool_backup_20300101-000000.json", token
                )
                assert status == 404 and payload.get("error") == "backup_not_found", (status, payload)
                # 이름은 맞지만 심볼릭 링크(작업 폴더 밖 파일)는 읽지 않는다
                status, payload = token_get(
                    base, "/api/backup?name=slide_tool_backup_20260103-090000.json", token
                )
                assert status == 404, (status, payload)
                # 백업이 아닌 JSON 은 422
                (backup_dir / "slide_tool_backup_20260104-090000.json").write_text(
                    json.dumps({"hello": 1}), encoding="utf-8"
                )
                status, payload = token_get(
                    base, "/api/backup?name=slide_tool_backup_20260104-090000.json", token
                )
                assert status == 422 and payload.get("error") == "bad_backup", (status, payload)

            results.append(report("B2 백업 조회: 경로 탈출·링크·비백업 JSON 거부", b2))

            def b3() -> None:
                for path in ("/api/backups", "/api/backup?name=slide_tool_backup_20260102-090000.json"):
                    status, _ = token_get(base, path, None)
                    assert status == 401, (path, status)
                    status, _ = token_get(base, path, "wrong")
                    assert status == 403, (path, status)

            results.append(report("B3 백업 API: 토큰 없음·오답 거부", b3))

        with tempfile.TemporaryDirectory(prefix="workflow_goodbye_") as temp_dir:
            temp = Path(temp_dir)
            pkg = make_pkg(temp)
            process, base, token = start_server(pkg, processes)   # --no-watchdog

            def g1() -> None:
                status, _, _ = http_request("POST", base + "/heartbeat")
                assert status == 204, status
                status, _, _ = http_request("POST", base + "/goodbye")
                assert status == 204, status
                time.sleep(5.6)   # 종료 유예(4.5초)를 넘겨도 살아 있어야 한다
                assert process.poll() is None, "--no-watchdog 인데 /goodbye 로 서버가 꺼졌습니다."
                status, payload = token_get(base, "/api/status", token)
                assert status == 200 and payload.get("workflow") is True, (status, payload)

            results.append(report("G1 --no-watchdog: /goodbye 뒤에도 서버가 살아 있음", g1))
    finally:
        for process in processes:
            stop_process(process)
    return results


# ---------------------------------------------------------------------------
# 발표자료 PDF → DECK 쪽 (deck_to_pages.py · POST /api/deck-import)
# ---------------------------------------------------------------------------
DECK_PAGE_COLORS = [(220, 30, 30), (30, 200, 30), (30, 30, 220)]   # 빨강·초록·파랑 = 1·2·3쪽
DECK_SCRIPT = SCRIPT_DIR / "deck_to_pages.py"
DECK_RESULT_PREFIX = "@@DECK_RESULT@@ "


def deck_color_name(rgb: tuple[int, int, int]) -> str:
    """PDF 쪽 중앙 색 → 이름. 발표자료 1·2·3쪽(d1·d2·d3), 사진 P(청록)·Q(노랑)."""
    red, green, blue = rgb
    if red > 150 and green < 100 and blue < 100:
        return "d1"
    if green > 150 and red < 100 and blue < 100:
        return "d2"
    if blue > 150 and red < 100 and green < 100:
        return "d3"
    if green > 150 and blue > 150 and red < 100:
        return "P"
    if red > 150 and green > 150 and blue < 120:
        return "Q"
    if all(100 < channel < 160 for channel in rgb):
        return "R"
    return f"?{rgb}"


def deck_pdf_pages(path: Path) -> list[str]:
    return [deck_color_name(c) for c in pdf_page_colors(path, pdf_page_count(path))]


def make_deck_pdf(path: Path, colors: Optional[list[tuple[int, int, int]]] = None) -> Path:
    """쪽마다 색이 다른 PDF 를 Pillow 로 만든다(1600x900 화소를 100dpi 로 = 1152x648pt)."""
    pages = [Image.new("RGB", (1600, 900), color) for color in (colors or DECK_PAGE_COLORS)]
    pages[0].save(
        str(path), "PDF", resolution=100.0, save_all=True, append_images=pages[1:]
    )
    return path


def make_deck_groups(pkg: Path) -> None:
    """01_G1(사진 P_1) · 02_G2(사진 Q_1) · 기타발표(번호 없는 이름, 사진 R_1). 사진은 원본·작업 img 양쪽에 둔다.

    make_pkg 가 넣어 둔 IMG_1·IMG_2 는 계획에 없으므로 치운다(계획 불일치 0 에서 시작하려고).
    """
    for leftover in ("IMG_1.jpg", "IMG_2.jpg"):
        (pkg / "01_원본사진" / leftover).unlink()
    layout = {
        "01_G1": ("P_1.jpg", (30, 200, 220)),
        "02_G2": ("Q_1.jpg", (220, 220, 30)),
        "기타발표": ("R_1.jpg", (128, 128, 128)),
    }
    plan_groups: dict[str, list[str]] = {}
    for group, (name, color) in layout.items():
        (pkg / "02_작업장" / group / "img").mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (160, 120), color).save(pkg / "01_원본사진" / name)
        Image.new("RGB", (160, 120), color).save(pkg / "02_작업장" / group / "img" / name)
        plan_groups[group] = [name]
    plan = {
        "_type": "slide_tool_worktree",
        "_version": 2,
        "root": ".",
        "source": "../01_원본사진",
        "groups": plan_groups,
    }
    (pkg / "02_작업장" / "worktree.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    run_gen_manifest(pkg)


def run_gen_manifest(pkg: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(pkg / "02_작업장" / "slide_tool" / "gen_manifest.py")],
        cwd=pkg,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, (result.stdout + result.stderr).strip()


def plan_groups(pkg: Path) -> dict[str, list[str]]:
    doc = json.loads((pkg / "02_작업장" / "worktree.json").read_text(encoding="utf-8"))
    return doc["groups"]


def manifest_names(pkg: Path, group: str) -> list[str]:
    text = (pkg / "02_작업장" / "slide_tool" / "data.js").read_text(encoding="utf-8")
    body = text.split("=", 1)[1].strip().rstrip(";")
    return [row["name"] for row in json.loads(body)[group]]


def deck_cli(
    pkg: Path,
    *args: str,
    env: Optional[dict[str, str]] = None,
    script: Optional[Path] = None,
) -> tuple[int, str, dict[str, object]]:
    """deck_to_pages.py 를 실행한다. (종료 코드, 출력, 기계용 결과 줄)."""
    environment = os.environ.copy()
    environment.update(env or {})
    result = subprocess.run(
        [sys.executable, str(script or DECK_SCRIPT), "--root", str(pkg), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=120,
        check=False,
    )
    machine: dict[str, object] = {}
    for line in result.stdout.splitlines():
        if line.startswith(DECK_RESULT_PREFIX):
            machine = json.loads(line[len(DECK_RESULT_PREFIX):])
    return result.returncode, result.stdout + result.stderr, machine


def deck_names(deck_no: int, count: int) -> list[str]:
    return [f"DECK{deck_no:02d}_p{index:03d}.jpg" for index in range(1, count + 1)]


def deck_tree_bytes(pkg: Path) -> dict[str, str]:
    """01_원본사진·02_작업장 전체의 파일 해시 — '아무것도 바뀌지 않았다' 확인용."""
    snapshot: dict[str, str] = {}
    for top in ("01_원본사진", "02_작업장"):
        for path in sorted((pkg / top).rglob("*")):
            if path.is_file() and path.name != "data.js":
                snapshot[path.relative_to(pkg).as_posix()] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    return snapshot


def pdf_first_page_width_pt(path: Path) -> float:
    pdfinfo = shutil.which("pdfinfo")
    assert pdfinfo is not None, "pdfinfo(poppler)가 필요합니다."
    result = subprocess.run(
        [pdfinfo, "-f", "1", "-l", "1", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    match = re.search(r"Page\s+1 size:\s+([0-9.]+) x", result.stdout)
    assert match, result.stdout
    return float(match.group(1))


def deck_request(
    base: str,
    token: str,
    pdf: bytes,
    group: str,
    *,
    mode: str = "import",
    replace: bool = False,
    name: str = "deck.pdf",
    deck_no: Optional[str] = None,
    origin: Optional[str] = None,
    with_token: bool = True,
    raw_group: Optional[str] = None,
) -> tuple[int, dict[str, object]]:
    headers = {
        "Origin": origin or base,
        "Content-Type": "application/pdf",
        "X-Filename": urllib.parse.quote(name, safe=""),
        "X-Group": raw_group if raw_group is not None else urllib.parse.quote(group, safe=""),
        "X-Deck-Mode": mode,
        "X-Replace": "1" if replace else "0",
    }
    if with_token:
        headers["X-Workflow-Token"] = token
    if deck_no is not None:
        headers["X-Deck-No"] = deck_no
    status, _, raw = http_request("POST", base + "/api/deck-import", headers=headers, body=pdf)
    return status, decode_json(raw)


def run_deck() -> list[bool]:
    """발표자료 PDF 를 DECK 쪽으로: CLI 변환·교체·오류, 서버 API, 내보내기 반영, 준비·나누기와의 공존."""
    results: list[bool] = []

    def k0() -> None:
        # 규약 동기화: 서버·CLI·내보내기·화면이 같은 이름 규칙과 기계용 머리말을 쓴다.
        sys.path.insert(0, str(SCRIPT_DIR))
        import deck_to_pages
        import export_pdf

        assert load_server_module().DECK_RESULT_PREFIX == deck_to_pages.RESULT_PREFIX
        for name in (
            "DECK05_p001.jpg", "deck1_p12.PNG", "DECK_p3.jpg", "DECK05.jpg",
            "IMG_DECK1_p1.jpg", "DECK1_p1", "xDECK1_p1.jpg", "DECK01_p001.jp2",
        ):
            assert (deck_to_pages.deck_no_of(name) is not None) == export_pdf.is_deck(name), name
        assert deck_to_pages.deck_no_of("DECK03_p001.jpg") == deck_to_pages.deck_no_of("DECK3_p2.jpg") == 3

    results.append(report("DK0 DECK 이름 규약·기계용 머리말이 서버·CLI·내보내기에서 같다", k0))
    if importlib.util.find_spec("pypdfium2") is None:
        print("[SKIP] 발표자료 PDF 테스트 — pypdfium2 가 설치되어 있지 않다(pip install pypdfium2).")
        return results
    processes: list[subprocess.Popen[str]] = []
    with tempfile.TemporaryDirectory(prefix="workflow_deck_") as temp_dir:
        temp = Path(temp_dir)
        pdf3 = make_deck_pdf(temp / "deck3.pdf")
        pdf2 = make_deck_pdf(temp / "deck2.pdf", [DECK_PAGE_COLORS[2], DECK_PAGE_COLORS[0]])

        def setup(name: str, block_dir: Optional[Path] = None) -> Path:
            pkg = make_pkg(temp / name, block_dir)
            make_deck_groups(pkg)
            return pkg

        try:
            def k1() -> None:
                # 변환: 파일명·쪽 수·해상도·저장 위치·계획 반영·화면 목록·계획 불일치 0.
                pkg = setup("cli")
                _, base, token = start_server(pkg, processes)
                status, _, raw = http_request(
                    "GET", base + "/api/status", headers={"X-Workflow-Token": token}
                )
                before = decode_json(raw)
                assert before["planMismatch"] == {"missing": 0, "added": 0}, before

                code, output, machine = deck_cli(pkg, "--pdf", str(pdf3), "--group", "01_G1")
                assert code == 0 and machine.get("ok") is True, output
                assert machine["pages"] == 3 and machine["deckNo"] == 1, machine
                names = deck_names(1, 3)
                src_dir = pkg / "01_원본사진" / "발표자료" / "01_G1"
                img_dir = pkg / "02_작업장" / "01_G1" / "img"
                for name in names:
                    assert (src_dir / name).is_file(), name
                    assert (img_dir / name).is_file(), name
                    assert (src_dir / name).read_bytes() == (img_dir / name).read_bytes()
                    with Image.open(src_dir / name) as image:
                        assert image.format == "JPEG", image.format
                        assert image.size == (2400, 1350), image.size
                assert (src_dir / "DECK01_원본.pdf").read_bytes() == pdf3.read_bytes()
                assert not list(src_dir.glob(".render-*")), "임시 폴더가 남았다"
                groups = plan_groups(pkg)
                assert groups["01_G1"] == names + ["P_1.jpg"], groups
                assert groups["02_G2"] == ["Q_1.jpg"], groups
                run_gen_manifest(pkg)
                assert manifest_names(pkg, "01_G1") == names + ["P_1.jpg"]
                status, _, raw = http_request(
                    "GET", base + "/api/status", headers={"X-Workflow-Token": token}
                )
                after = decode_json(raw)
                assert after["planMismatch"] == {"missing": 0, "added": 0}, after["planMismatch"]

            results.append(report("DK1 CLI 변환: 이름·2400px·저장 위치·계획·목록·불일치 0", k1))

            def k2() -> None:
                # 해상도 옵션과 번호 규칙.
                pkg = setup("opts")
                code, output, machine = deck_cli(
                    pkg, "--pdf", str(pdf3), "--group", "02_G2", "--width", "1000"
                )
                assert code == 0, output
                with Image.open(pkg / "02_작업장" / "02_G2" / "img" / "DECK02_p001.jpg") as image:
                    assert image.size == (1000, 563), image.size
                code, output, machine = deck_cli(
                    pkg, "--pdf", str(pdf3), "--group", "기타발표", "--dpi", "72"
                )
                assert code == 0 and machine["deckNo"] == 1, (output, machine)   # 2 는 쓰는 중 → 가장 작은 빈 번호
                with Image.open(pkg / "02_작업장" / "기타발표" / "img" / "DECK01_p001.jpg") as image:
                    assert image.size == (1152, 648), image.size
                code, output, machine = deck_cli(
                    pkg, "--pdf", str(pdf3), "--group", "01_G1", "--deck-no", "7"
                )
                assert code == 0 and machine["deckNo"] == 7, output
                assert (pkg / "02_작업장" / "01_G1" / "img" / "DECK07_p003.jpg").is_file()
                # 이미 다른 발표가 쓰는 번호는 거부
                code, output, machine = deck_cli(
                    pkg, "--pdf", str(pdf3), "--group", "01_G1", "--deck-no", "2"
                )
                assert code == 1 and machine.get("code") == "number_in_use", (output, machine)
                for bad in (("--width", "5"), ("--dpi", "5"), ("--deck-no", "1000")):
                    code, output, machine = deck_cli(
                        pkg, "--pdf", str(pdf3), "--group", "01_G1", *bad
                    )
                    assert code == 1 and machine.get("code") == "bad_args", (bad, output)

            results.append(report("DK2 --width/--dpi·번호 기본값·다른 발표 번호 거부", k2))

            def k3() -> None:
                # 이미 있으면 거부(무변경) → --replace 는 기존 쪽을 보관 폴더로 옮기고 교체.
                pkg = setup("replace")
                code, output, _ = deck_cli(pkg, "--pdf", str(pdf3), "--group", "01_G1")
                assert code == 0, output
                snapshot = deck_tree_bytes(pkg)
                plan_before = (pkg / "02_작업장" / "worktree.json").read_bytes()
                code, output, machine = deck_cli(pkg, "--pdf", str(pdf2), "--group", "01_G1")
                assert code == 1 and machine.get("code") == "exists", (output, machine)
                assert deck_tree_bytes(pkg) == snapshot, "거부했는데 파일이 바뀌었다"
                assert (pkg / "02_작업장" / "worktree.json").read_bytes() == plan_before

                old_first = (pkg / "02_작업장" / "01_G1" / "img" / "DECK01_p001.jpg").read_bytes()
                code, output, machine = deck_cli(
                    pkg, "--pdf", str(pdf2), "--group", "01_G1", "--replace"
                )
                assert code == 0 and machine["pages"] == 2 and machine["replaced"] == 3, (output, machine)
                names = deck_names(1, 2)
                assert plan_groups(pkg)["01_G1"] == names + ["P_1.jpg"], plan_groups(pkg)
                img_dir = pkg / "02_작업장" / "01_G1" / "img"
                src_dir = pkg / "01_원본사진" / "발표자료" / "01_G1"
                assert sorted(p.name for p in img_dir.glob("DECK*")) == names
                assert sorted(p.name for p in src_dir.glob("DECK*.jpg")) == names
                assert (src_dir / "DECK01_원본.pdf").read_bytes() == pdf2.read_bytes()
                archives = sorted((pkg / "02_작업장" / "01_G1").glob("_이전발표자료_*"))
                assert len(archives) == 1, archives
                assert machine["archive"] == archives[0].name
                assert sorted(p.name for p in (archives[0] / "원본").iterdir()) == sorted(
                    deck_names(1, 3) + ["DECK01_원본.pdf"]
                )
                assert sorted(p.name for p in (archives[0] / "작업본").iterdir()) == deck_names(1, 3)
                assert (archives[0] / "작업본" / "DECK01_p001.jpg").read_bytes() == old_first
                assert (img_dir / "DECK01_p001.jpg").read_bytes() != old_first
                # 한 번 더 교체해도 보관 폴더는 덮어쓰지 않고 새로 만든다.
                code, output, _ = deck_cli(pkg, "--pdf", str(pdf3), "--group", "01_G1", "--replace")
                assert code == 0, output
                assert len(list((pkg / "02_작업장" / "01_G1").glob("_이전발표자료_*"))) == 2
                assert plan_groups(pkg)["01_G1"] == deck_names(1, 3) + ["P_1.jpg"]
                # 보관 폴더는 화면 목록·계획 불일치에 들어오지 않는다.
                run_gen_manifest(pkg)
                assert manifest_names(pkg, "01_G1") == deck_names(1, 3) + ["P_1.jpg"]

            results.append(report("DK3 이미 있으면 거부(무변경)·--replace 보관 이동·반복 교체", k3))

            def k4() -> None:
                # 오류: 손상 PDF·없는 파일·경로 탈출·`_` 발표·없는 발표·암호 PDF·계획 없음. 흔적이 남지 않는다.
                pkg = setup("errors")
                snapshot = deck_tree_bytes(pkg)
                junk = temp / "junk.pdf"
                junk.write_bytes(b"this is not a pdf at all")
                cases = [
                    (("--pdf", str(junk), "--group", "01_G1"), "bad_pdf"),
                    (("--pdf", str(temp / "none.pdf"), "--group", "01_G1"), "bad_pdf"),
                    (("--pdf", str(pdf3), "--group", "../evil"), "bad_group"),
                    (("--pdf", str(pdf3), "--group", "a/b"), "bad_group"),
                    (("--pdf", str(pdf3), "--group", "_이전작업_260929"), "bad_group"),
                    (("--pdf", str(pdf3), "--group", "없는발표"), "group_not_found"),
                ]
                qpdf = shutil.which("qpdf")
                if qpdf:
                    locked = temp / "locked.pdf"
                    subprocess.run(
                        [qpdf, "--encrypt", "user-pw", "owner-pw", "256", "--", str(pdf3), str(locked)],
                        check=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    cases.append((("--pdf", str(locked), "--group", "01_G1"), "encrypted"))
                for args, expected in cases:
                    code, output, machine = deck_cli(pkg, *args)
                    assert code == 1 and machine.get("code") == expected, (args, code, output)
                assert deck_tree_bytes(pkg) == snapshot, "오류인데 파일이 바뀌었다"
                assert not list((pkg / "01_원본사진").rglob(".render-*"))
                assert not (pkg / "01_원본사진" / "발표자료").exists() or not any(
                    (pkg / "01_원본사진" / "발표자료").rglob("*")
                )
                # 계획 없음
                empty = make_pkg(temp / "noplan")
                code, output, machine = deck_cli(empty, "--pdf", str(pdf3), "--group", "01_G1")
                assert code == 1 and machine.get("code") == "no_plan", output

            results.append(report("DK4 손상·없음·경로 탈출·`_`·없는 발표·암호·계획 없음 거부·무변경", k4))

            def k5() -> None:
                # pypdfium2 없음(모듈 가림): CLI 는 종료 코드 2 와 설치 안내.
                block = temp / "block_cli"
                (block / "pypdfium2").mkdir(parents=True, exist_ok=True)
                (block / "pypdfium2" / "__init__.py").write_text(
                    'raise ImportError("blocked for test")\n', encoding="utf-8"
                )
                pkg = setup("nopdfium_cli")
                snapshot = deck_tree_bytes(pkg)
                code, output, machine = deck_cli(
                    pkg, "--pdf", str(pdf3), "--group", "01_G1", env={"PYTHONPATH": str(block)}
                )
                assert code == 2 and machine.get("code") == "pdfium_missing", (code, output)
                assert "pip install pypdfium2" in output, output
                assert deck_tree_bytes(pkg) == snapshot

            results.append(report("DK5 pypdfium2 없음(모듈 가림) → 종료 코드 2·설치 안내·무변경", k5))

            def k6() -> None:
                # 준비(그대로 준비·--force)와 다시 나누기가 발표자료 쪽을 해치지 않는다.
                pkg = setup("coexist")
                code, output, _ = deck_cli(pkg, "--pdf", str(pdf3), "--group", "01_G1")
                assert code == 0, output
                names = deck_names(1, 3)
                img_dir = pkg / "02_작업장" / "01_G1" / "img"
                before = {n: (img_dir / n).read_bytes() for n in names}
                result = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT_DIR / "prepare_photos.py"),
                        "--plan",
                        str(pkg / "02_작업장" / "worktree.json"),
                        "--force",
                        "--workers",
                        "1",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                assert result.returncode == 0, result.stdout + result.stderr
                assert "못 찾은" not in result.stderr, result.stderr
                for name in names:
                    assert (img_dir / name).read_bytes() == before[name], f"{name}: 준비가 줄였다"
                # 다시 나누기(하위 폴더 기준): 발표자료 폴더는 발표가 아니고, 같은 이름의 발표에 다시 이어진다.
                src3 = temp / "regroup_src"
                (src3 / "01_G1").mkdir(parents=True)
                Image.new("RGB", (100, 80), (1, 2, 3)).save(src3 / "01_G1" / "X_1.jpg")
                (src3 / "발표자료" / "01_G1").mkdir(parents=True)
                for name in names:
                    shutil.copyfile(img_dir / name, src3 / "발표자료" / "01_G1" / name)
                (src3 / "발표자료" / "01_G1" / "DECK01_원본.pdf").write_bytes(pdf3.read_bytes())
                (src3 / "발표자료" / "옛발표").mkdir(parents=True)
                shutil.copyfile(img_dir / names[0], src3 / "발표자료" / "옛발표" / "DECK09_p001.jpg")
                out3 = temp / "regroup_out"
                result = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT_DIR / "init_worktree.py"),
                        "--src",
                        str(src3),
                        "--out",
                        str(out3),
                        "--by-subfolder",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                assert result.returncode == 0, result.stdout + result.stderr
                groups = json.loads((out3 / "worktree.json").read_text(encoding="utf-8"))["groups"]
                assert groups == {"01_G1": names + ["X_1.jpg"]}, groups
                assert "옛발표" in result.stderr, result.stderr      # 이을 발표가 없는 자료는 알려 준다

            results.append(report("DK6 그대로 준비(--force)가 자료 쪽을 안 줄임·다시 나누기가 자료를 안 나누고 다시 이음", k6))

            def a1() -> None:
                # 서버 API: 확인(inspect) → 넣기(job) → 목록·계획·불일치 → 중복 거부 → 교체.
                pkg = setup("api")
                src = pkg / "01_원본사진"
                _, base, token = start_server(pkg, processes)
                status, _, raw = http_request(
                    "GET", base + "/api/status", headers={"X-Workflow-Token": token}
                )
                assert decode_json(raw)["env"]["pdfium"] is True

                snapshot = deck_tree_bytes(pkg)
                status, payload = deck_request(
                    base, token, pdf3.read_bytes(), "01_G1", mode="inspect"
                )
                assert status == 200, payload
                assert payload["pages"] == 3 and payload["existing"] == 0 and payload["deckNo"] == 1, payload
                assert deck_tree_bytes(pkg) == snapshot, "inspect 가 파일을 바꿨다"
                assert not list(src.glob(".발표자료-업로드중-*")), "업로드 임시 파일이 남았다"

                status, payload = deck_request(base, token, pdf3.read_bytes(), "01_G1")
                assert status == 202 and payload["job"]["kind"] == "deck-import", payload
                assert payload["pages"] == 3, payload
                job = wait_job(base, token)
                assert job["state"] == "done", job_log(job)
                assert job["result"]["ok"] is True and job["result"]["pages"] == 3, job["result"]
                assert "@@DECK_RESULT@@" not in job_log(job), "기계용 줄이 화면 기록에 실렸다"
                assert any("쪽 3/3" in line for line in job_log(job).splitlines()), job_log(job)
                names = deck_names(1, 3)
                assert manifest_names(pkg, "01_G1") == names + ["P_1.jpg"]        # 목록 단계까지 끝났다
                assert plan_groups(pkg)["01_G1"] == names + ["P_1.jpg"]
                assert not list(src.glob(".발표자료-업로드중-*")), "잡이 끝났는데 임시 PDF 가 남았다"
                status, _, raw = http_request(
                    "GET", base + "/api/status", headers={"X-Workflow-Token": token}
                )
                assert decode_json(raw)["planMismatch"] == {"missing": 0, "added": 0}
                counts = {g["name"]: g["count"] for g in decode_json(raw)["groups"]}
                assert counts["01_G1"] == 4, counts

                # 같은 번호가 이미 있으면 기본 거부(무변경), replace 면 교체.
                snapshot = deck_tree_bytes(pkg)
                status, payload = deck_request(base, token, pdf2.read_bytes(), "01_G1")
                assert status == 409 and payload["error"] == "deck_exists", payload
                assert deck_tree_bytes(pkg) == snapshot
                status, payload = deck_request(base, token, pdf2.read_bytes(), "01_G1", mode="inspect")
                assert status == 200 and payload["existing"] == 3 and payload["pages"] == 2, payload
                status, payload = deck_request(
                    base, token, pdf2.read_bytes(), "01_G1", replace=True
                )
                assert status == 202, payload
                job = wait_job(base, token)
                assert job["state"] == "done", job_log(job)
                assert job["result"]["replaced"] == 3 and job["result"]["pages"] == 2, job["result"]
                assert manifest_names(pkg, "01_G1") == deck_names(1, 2) + ["P_1.jpg"]
                archives = list((pkg / "02_작업장" / "01_G1").glob("_이전발표자료_*"))
                assert len(archives) == 1, archives
                status, _, raw = http_request(
                    "GET", base + "/api/status", headers={"X-Workflow-Token": token}
                )
                assert decode_json(raw)["planMismatch"] == {"missing": 0, "added": 0}
                assert [g["name"] for g in decode_json(raw)["groups"]] == ["01_G1", "02_G2", "기타발표"]

            results.append(report("DP1 API 확인→넣기 job→목록·계획·불일치 0→중복 409→교체", a1))

            def a2() -> None:
                # 서버 API 거부: 토큰·Origin·경로 탈출·`_` 발표·없는 발표·확장자·PDF 아님·크기·잘못된 머리글.
                pkg = setup("api_reject")
                src = pkg / "01_원본사진"
                _, base, token = start_server(pkg, processes)
                data = pdf3.read_bytes()
                snapshot = deck_tree_bytes(pkg)

                status, payload = deck_request(base, token, data, "01_G1", with_token=False)
                assert status == 401 and payload["error"] == "token_missing", payload
                status, payload = deck_request(base, "wrong-token", data, "01_G1")
                assert status == 403 and payload["error"] == "token_invalid", payload
                status, payload = deck_request(
                    base, token, data, "01_G1", origin="http://evil.example"
                )
                assert status == 403 and payload["error"] == "bad_origin", payload
                for raw_group in ("../evil", "..%2Fevil", "a%2Fb", "%2e%2e", "C%3A%5Cx", "%E0%A4%A"):
                    status, payload = deck_request(base, token, data, "", raw_group=raw_group)
                    assert status == 400 and payload["error"] == "bad_group_name", (raw_group, status, payload)
                status, payload = deck_request(base, token, data, "_이전작업_260929_1200")
                assert status == 400 and payload["error"] == "bad_group_name", payload
                status, payload = deck_request(base, token, data, "없는발표")
                assert status == 404 and payload["error"] == "group_not_found", payload
                for name in ("deck.txt", "deck.jpg", "../deck.pdf", ".deck.pdf", "de#ck.pdf", "deck"):
                    status, payload = deck_request(base, token, data, "01_G1", name=name)
                    assert status == 400 and payload["error"] == "bad_filename", (name, status, payload)
                status, payload = deck_request(base, token, b"plain text, not a pdf", "01_G1")
                assert status == 400 and payload["error"] == "not_pdf", payload
                status, payload = deck_request(base, token, b"%PDF-1.4 but broken", "01_G1")
                assert status == 400 and payload["error"] == "bad_pdf", payload
                status, payload = deck_request(base, token, b"", "01_G1")
                assert status == 400, payload
                status, payload = deck_request(base, token, data, "01_G1", mode="delete")
                assert status == 400 and payload["error"] == "bad_request", payload
                status, payload = deck_request(base, token, data, "01_G1", deck_no="12x")
                assert status == 400 and payload["error"] == "bad_request", payload
                headers = api_headers(base, token)
                headers.update(
                    {"X-Filename": "big.pdf", "X-Group": urllib.parse.quote("01_G1", safe="")}
                )
                status, _, raw = declared_request(
                    "POST", base + "/api/deck-import", 200 * 1024 * 1024 + 1, headers
                )
                assert status == 413 and decode_json(raw)["error"] == "upload_too_large"
                assert deck_tree_bytes(pkg) == snapshot, "거부했는데 파일이 바뀌었다"
                assert not (src / "발표자료").exists()
                assert not list(src.glob(".발표자료-업로드중-*")), "업로드 임시 파일이 남았다"

            results.append(report("DP2 API 거부: 토큰·Origin·탈출·`_`·없는 발표·파일명·PDF 아님·크기·머리글", a2))

            def a3() -> None:
                # 다른 발표가 이미 쓰는 번호는 409, 안 쓰는 번호는 지정할 수 있다.
                pkg = setup("api_number")
                _, base, token = start_server(pkg, processes)
                data = pdf3.read_bytes()
                status, payload = deck_request(base, token, data, "02_G2")
                assert status == 202, payload
                assert wait_job(base, token)["state"] == "done"
                status, payload = deck_request(base, token, data, "01_G1", deck_no="2")
                assert status == 409 and payload["error"] == "deck_number_in_use", payload
                status, payload = deck_request(base, token, data, "01_G1", deck_no="5")
                assert status == 202, payload
                assert wait_job(base, token)["state"] == "done"
                assert manifest_names(pkg, "01_G1") == deck_names(5, 3) + ["P_1.jpg"]

            results.append(report("DP3 API DECK 번호: 다른 발표가 쓰는 번호 409·지정 번호 허용", a3))

            def a4() -> None:
                # 실행 중인 작업이 있으면 409(자료 넣기는 준비·PDF 와 같은 잡 하나를 쓴다).
                # 목록 만들기를 4초 늦춰 "잡이 도는 중"을 결정적으로 만든다.
                pkg = setup("api_busy")
                tool = pkg / "02_작업장" / "slide_tool"
                shutil.copy2(tool / "gen_manifest.py", tool / "gen_manifest_real.py")
                (tool / "gen_manifest.py").write_text(
                    "import runpy, time\n"
                    "from pathlib import Path\n"
                    "time.sleep(4)\n"
                    "runpy.run_path(str(Path(__file__).with_name('gen_manifest_real.py')), run_name='__main__')\n",
                    encoding="utf-8",
                )
                _, base, token = start_server(pkg, processes)
                data = pdf3.read_bytes()
                status, payload = deck_request(base, token, data, "01_G1")
                assert status == 202, payload
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    _, _, raw = http_request(
                        "GET", base + "/api/job?after=0", headers={"X-Workflow-Token": token}
                    )
                    job = decode_json(raw)["job"]
                    if job["phase"] >= 2:
                        break
                    time.sleep(0.05)
                assert job["state"] == "running" and job["phase"] == 2, job
                status, payload = deck_request(base, token, data, "02_G2")
                assert status == 409 and payload["error"] == "busy", payload
                status, payload = deck_request(base, token, data, "02_G2", mode="inspect")
                assert status == 200, payload          # 확인만 하는 요청은 잡과 겹쳐도 된다
                status, _, payload = json_post(
                    base, "/api/prepare", token, {"regroup": False, "gapMinutes": 20}
                )
                assert status == 409 and payload["error"] == "busy", payload
                job = wait_job(base, token)
                assert job["state"] == "done", job_log(job)
                assert not list((pkg / "01_원본사진").glob(".발표자료-업로드중-*"))
                assert not (pkg / "01_원본사진" / "발표자료" / "02_G2").exists()

            results.append(report("DP4 진행 중 작업과 겹치면 409(확인만은 허용)·임시 PDF 정리", a4))

            def e1() -> None:
                # 이후 내보내기: 방식 A·B·C 모두 자료 쪽이 원본 그대로·순서대로 들어간다.
                pkg = setup("export")
                _, base, token = start_server(pkg, processes)
                status, payload = deck_request(base, token, pdf3.read_bytes(), "01_G1")
                assert status == 202, payload
                assert wait_job(base, token)["state"] == "done"
                out = pkg / "03_결과물"
                backup = make_backup(["../01_G1/img/P_1.jpg", "../02_G2/img/Q_1.jpg"])
                groups = ["01_G1", "02_G2", "기타발표"]

                export_mode_job(base, token, backup, "per-folder")
                assert deck_pdf_pages(out / "01_G1.pdf") == ["d1", "d2", "d3", "P"]
                assert deck_pdf_pages(out / "02_G2.pdf") == ["Q"]
                # 원본 그대로 = 렌더한 2400px 이 150dpi 기준 1152pt 폭으로 들어간다(보정·재표본 없음).
                assert abs(pdf_first_page_width_pt(out / "01_G1.pdf") - 1152.0) < 2.0

                export_mode_job(base, token, backup, "merged")
                assert deck_pdf_pages(out / "전체.pdf") == ["d1", "d2", "d3", "P", "Q", "R"]
                assert deck_pdf_pages(out / "01_G1.pdf") == ["d1", "d2", "d3", "P"]

                export_mode_job(base, token, backup, "ordered", order=list(reversed(groups)))
                assert deck_pdf_pages(out / "전체.pdf") == ["R", "Q", "d1", "d2", "d3", "P"]

                # 완료본만이어도 자료 쪽은 빠지지 않는다.
                done = make_backup(
                    ["../01_G1/img/P_1.jpg"],
                    statuses={"../01_G1/img/P_1.jpg": {"done": True}},
                )
                export_mode_job(base, token, done, "per-folder", only_done=True)
                assert deck_pdf_pages(out / "01_G1.pdf") == ["d1", "d2", "d3", "P"]

            results.append(report("DX1 넣은 뒤 내보내기 A·B·C·완료본만: 자료 쪽 원본 그대로·순서대로", e1))

            def s1() -> None:
                # pypdfium2 없음(모듈 가림): 상태는 pdfium=false, API 는 503 + 안내, 아무것도 바뀌지 않는다.
                block = temp / "block_api"
                (block / "pypdfium2").mkdir(parents=True, exist_ok=True)
                (block / "pypdfium2" / "__init__.py").write_text(
                    'raise ImportError("blocked for test")\n', encoding="utf-8"
                )
                pkg = setup("nopdfium_api", block)
                _, base, token = start_server(pkg, processes)
                status, _, raw = http_request(
                    "GET", base + "/api/status", headers={"X-Workflow-Token": token}
                )
                assert decode_json(raw)["env"]["pdfium"] is False
                snapshot = deck_tree_bytes(pkg)
                for mode in ("inspect", "import"):
                    status, payload = deck_request(base, token, pdf3.read_bytes(), "01_G1", mode=mode)
                    assert status == 503 and payload["error"] == "pdfium_missing", (mode, status, payload)
                    assert "pypdfium2" in str(payload["detail"]), payload
                assert deck_tree_bytes(pkg) == snapshot
                assert not list((pkg / "01_원본사진").glob(".발표자료-업로드중-*"))
                # 다른 API 는 그대로 동작한다.
                status, _, raw = http_request(
                    "GET", base + "/api/backups", headers={"X-Workflow-Token": token}
                )
                assert status == 200, raw

            if sys.platform == "win32":
                print("[SKIP] DZ1 모듈 가림 시뮬레이션 — 셸 래퍼를 쓰지 못하는 Windows 에서는 건너뜀")
            else:
                results.append(report("DZ1 pypdfium2 없음(모듈 가림) → 상태 false·API 503+안내·무변경", s1))
        finally:
            for process in processes:
                stop_process(process)
    return results


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=("auth", "upload", "rename", "job", "export", "regress", "detect", "event", "deck"))
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    runners = {
        "auth": run_auth,
        "upload": run_upload,
        "rename": run_rename,
        "job": run_job,
        "export": run_export_modes,
        "regress": run_export_regress,
        "detect": run_auto_detect,
        "event": run_new_event,
        "deck": run_deck,
    }
    selected = (
        [args.only]
        if args.only
        else ["auth", "upload", "rename", "job", "export", "regress", "detect", "event", "deck"]
    )
    results: list[bool] = []
    try:
        for name in selected:
            results.extend(runners[name]())
    except Exception as exc:
        print(f"[FAIL] 테스트 환경 준비: {exc}")
        results.append(False)
    if results and all(results):
        print("WORKFLOW: ALL PASS")
        return 0
    print("WORKFLOW: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
