#!/usr/bin/env python3
"""워크플로 서버 API의 인증·업로드·잡 보안 통합 게이트."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import importlib.util
import json
import os
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


def make_fake_venv(pkg: Path) -> None:
    bindir = pkg / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
    bindir.mkdir(parents=True, exist_ok=True)
    name = "python.exe" if sys.platform == "win32" else "python3"
    target = bindir / name
    try:
        target.symlink_to(Path(sys.executable).resolve())
    except OSError:
        shutil.copy2(sys.executable, target)
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


def make_pkg(temp: Path) -> Path:
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
    make_fake_venv(pkg)
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
                before = {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in out.glob("*.pdf")
                }
                before_outside = outside_pdf_snapshot(temp, out)
                bad_key = "../g/img/../../../secret.png"
                job = export_job(base, token, make_backup([bad_key]), 60)
                after = {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in out.glob("*.pdf")
                }
                assert before == after
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


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=("auth", "upload", "rename", "job", "export"))
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    runners = {
        "auth": run_auth,
        "upload": run_upload,
        "rename": run_rename,
        "job": run_job,
        "export": run_export_modes,
    }
    selected = [args.only] if args.only else ["auth", "upload", "rename", "job", "export"]
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
