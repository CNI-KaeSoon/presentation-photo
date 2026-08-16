#!/usr/bin/env python3
"""브라우저 생명주기에 맞춰 자동 종료되는 로컬 정적 파일 서버."""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import collections
import datetime as dt
import functools
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
import unicodedata
import urllib.parse
import uuid
import webbrowser
from pathlib import Path
from typing import Optional

import envcheck


BIND = "127.0.0.1"
DEFAULT_PORT = 8770
DEFAULT_TIMEOUT = 15.0
DEFAULT_GRACE = 40.0
GOODBYE_GRACE = 4.5

UPLOAD_EXTS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    ".bmp",
    ".webp",
    ".heic",
    ".heif",
)
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_EXPORT_BODY_BYTES = 64 * 1024 * 1024
MAX_RENAME_BODY_BYTES = 64 * 1024
STREAM_CHUNK_BYTES = 1024 * 1024
WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)


def _error_payload(code: str, detail: str) -> dict[str, object]:
    return {"ok": False, "error": code, "detail": detail}


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def resolve_pkg_root(root: Path, pkg_root_arg: Optional[str]) -> Optional[Path]:
    """서버 루트와 선택 인자에서 완전한 배포 패키지 루트를 찾는다."""
    try:
        candidate = (
            Path(pkg_root_arg).expanduser().resolve()
            if pkg_root_arg is not None
            else root.resolve().parent
        )
    except (OSError, RuntimeError):
        return None

    required = (
        candidate / "00_시작",
        candidate / "01_원본사진",
        candidate / "02_작업장",
        candidate / "03_결과물",
        candidate / "05_스크립트",
    )
    if not candidate.is_dir() or not all(path.is_dir() for path in required):
        return None
    return candidate


def read_env_status(pkg_root: Path) -> dict[str, object]:
    """저장된 환경 상태를 읽고 현재 venv와 다시 대조한다."""
    path = pkg_root / "00_시작" / "_env_status.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    workers = raw.get("workers")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        workers = 1
    try:
        venv_valid = envcheck.status_venv_valid(pkg_root, raw)
    except (OSError, TypeError, ValueError):
        venv_valid = False
    return {
        "ok": bool(raw.get("ok")) and venv_valid,
        "workers": workers,
        "heic": bool(raw.get("heic")),
    }


def worker_python(pkg_root: Path) -> Optional[Path]:
    return envcheck.venv_python(pkg_root)


def safe_upload_name(raw_quoted: str) -> tuple[Optional[str], str]:
    """인코딩된 업로드 파일명을 단일 휴대 가능 basename으로 제한한다."""
    if not isinstance(raw_quoted, str) or not raw_quoted:
        return None, "파일명이 비어 있습니다."
    try:
        value = nfc(urllib.parse.unquote(raw_quoted, encoding="utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError):
        return None, "파일명 인코딩이 올바르지 않습니다."

    if not value or "\x00" in value or value in {".", ".."}:
        return None, "단일 파일명을 사용하세요."
    if os.path.isabs(value) or "/" in value or "\\" in value:
        return None, "경로가 아닌 파일명만 사용할 수 있습니다."
    if re.match(r"^[A-Za-z]:", value) or Path(value).name != value:
        return None, "드라이브나 경로가 포함된 파일명은 사용할 수 없습니다."
    if value.startswith("."):
        return None, "숨김 파일명은 사용할 수 없습니다."
    if any(ord(char) < 0x20 for char in value):
        return None, "제어 문자가 포함된 파일명은 사용할 수 없습니다."
    if any(char in value for char in "#?%"):
        return None, "파일명에 #, ?, % 문자를 사용할 수 없습니다."
    if any(char in value for char in '<>:"|*'):
        return None, "운영체제에서 금지된 문자가 포함되어 있습니다."
    if value.endswith((" ", ".")):
        return None, "파일명 끝의 공백이나 점은 사용할 수 없습니다."

    suffix = Path(value).suffix.lower()
    if suffix not in UPLOAD_EXTS:
        return None, "지원하는 사진 확장자가 아닙니다."
    stem = Path(value).stem.casefold()
    if stem in WINDOWS_RESERVED or stem.split(".", 1)[0] in WINDOWS_RESERVED:
        return None, "운영체제 예약 파일명은 사용할 수 없습니다."
    return value, ""


def safe_group_name(raw: object) -> tuple[Optional[str], str]:
    """그룹 이름을 경로가 아닌 휴대 가능한 단일 basename으로 제한한다."""
    if not isinstance(raw, str):
        return None, "그룹 이름은 문자열이어야 합니다."
    value = nfc(raw)
    if not value or value in {".", ".."} or "\x00" in value:
        return None, "비어 있지 않은 단일 그룹 이름을 사용하세요."
    if os.path.isabs(value) or "/" in value or "\\" in value:
        return None, "경로가 아닌 그룹 이름만 사용할 수 있습니다."
    if re.match(r"^[A-Za-z]:", value) or Path(value).name != value:
        return None, "드라이브나 경로가 포함된 그룹 이름은 사용할 수 없습니다."
    if value.startswith("."):
        return None, "점으로 시작하는 그룹 이름은 사용할 수 없습니다."
    if any(unicodedata.category(char) == "Cc" for char in value):
        return None, "제어 문자가 포함된 그룹 이름은 사용할 수 없습니다."
    if any(char in value for char in "#?%"):
        return None, "그룹 이름에 #, ?, % 문자를 사용할 수 없습니다."
    if any(char in value for char in '<>:"|*'):
        return None, "운영체제에서 금지된 문자가 포함되어 있습니다."
    if value.endswith((" ", ".")):
        return None, "그룹 이름 끝의 공백이나 점은 사용할 수 없습니다."
    if value.split(".", 1)[0].casefold() in WINDOWS_RESERVED:
        return None, "운영체제 예약 그룹 이름은 사용할 수 없습니다."
    return value, ""


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """같은 디렉터리의 임시 파일을 거쳐 파일 하나를 원자적으로 교체한다."""
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("xb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(STREAM_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def unique_dst(
    dir_: Path, name: str, content_sha: str
) -> tuple[Optional[Path], bool, bool]:
    """기존 파일을 보존하면서 최종 이름을 배타 생성으로 예약한다."""
    original = Path(name)
    for index in range(1, 1000):
        candidate_name = (
            name if index == 1 else f"{original.stem}_{index}{original.suffix}"
        )
        candidate = dir_ / candidate_name
        if candidate.exists():
            if (
                index == 1
                and candidate.is_file()
                and _sha256_file(candidate) == content_sha
            ):
                return None, False, True
            continue
        try:
            with candidate.open("xb"):
                pass
        except FileExistsError:
            continue
        return candidate, index > 1, False
    raise OSError("같은 이름의 파일이 너무 많아 새 이름을 정할 수 없습니다.")


class Job:
    """단일 CLI 파이프라인의 상태와 제한된 로그를 보관한다."""

    def __init__(self, kind: str, phase_total: int) -> None:
        self.id = secrets.token_urlsafe(12)
        self.kind = kind
        self.state = "running"
        self.phase = 0
        self.phase_total = phase_total
        self.phase_name = "대기"
        self.started_at = time.time()
        self.exit_code: Optional[int] = None
        self.cancel_requested = False
        self.finished = threading.Event()
        self.lock = threading.Lock()
        self._seq = 0
        self._lines: collections.deque[tuple[int, str]] = collections.deque(maxlen=2000)

    def append_line(self, line: str) -> None:
        clean = str(line).rstrip("\r\n")
        with self.lock:
            self._seq += 1
            self._lines.append((self._seq, clean))

    def snapshot(self, after: int) -> dict[str, object]:
        with self.lock:
            result: dict[str, object] = {
                "id": self.id,
                "kind": self.kind,
                "state": self.state,
                "phase": self.phase,
                "phaseTotal": self.phase_total,
                "phaseName": self.phase_name,
                "startedAt": self.started_at,
                "lines": [[seq, line] for seq, line in self._lines if seq > after],
                "nextAfter": self._seq,
            }
            if self.exit_code is not None:
                result["exitCode"] = self.exit_code
            return result


class JobManager:
    """prepare/export CLI를 한 번에 하나만 실행하고 종료를 책임진다."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current: Optional[Job] = None
        self._process: Optional[subprocess.Popen[str]] = None

    @property
    def busy(self) -> bool:
        with self.lock:
            return self.current is not None and self.current.state == "running"

    def start(
        self, kind: str, steps: list[tuple[str, list[str]]]
    ) -> Optional[Job]:
        with self.lock:
            if self.current is not None and self.current.state == "running":
                return None
            job = Job(kind, len(steps))
            self.current = job
            self._process = None
        threading.Thread(target=self._run, args=(job, steps), daemon=True).start()
        return job

    def _run(self, job: Job, steps: list[tuple[str, list[str]]]) -> None:
        try:
            self._run_steps(job, steps)
        finally:
            job.finished.set()

    def _run_steps(self, job: Job, steps: list[tuple[str, list[str]]]) -> None:
        for phase, (phase_name, argv) in enumerate(steps, 1):
            with job.lock:
                if job.cancel_requested:
                    job.state = "cancelled"
                    return
                job.phase = phase
                job.phase_name = phase_name
            job.append_line(f"phase {phase}/{len(steps)}: {phase_name}")
            try:
                popen_options: dict[str, object] = {}
                if os.name == "nt":
                    popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    popen_options["start_new_session"] = True
                # 자식이 콘솔 기본 인코딩(Windows=cp949)으로 쓰면 '—'·'⚠' 에서 UnicodeEncodeError 로
                # 죽고, 살아남아도 부모의 utf-8 디코딩과 어긋나 로그가 깨진다. 자식 쪽을 강제한다.
                child_env = dict(os.environ)
                child_env["PYTHONIOENCODING"] = "utf-8:replace"
                child_env["PYTHONUTF8"] = "1"
                process = subprocess.Popen(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=child_env,
                    **popen_options,
                )
            except OSError as exc:
                job.append_line(f"실행 실패: {exc}")
                with job.lock:
                    job.state = "error"
                    job.exit_code = -1
                return

            with self.lock:
                if self.current is job:
                    self._process = process
                cancel_now = job.cancel_requested
            if cancel_now:
                self._stop_process(process)

            if process.stdout is not None:
                for line in process.stdout:
                    job.append_line(line)
            exit_code = process.wait()
            with self.lock:
                if self._process is process:
                    self._process = None
            with job.lock:
                job.exit_code = exit_code
                if job.cancel_requested:
                    job.state = "cancelled"
                    return
                if exit_code != 0:
                    job.state = "error"
                    return

        with job.lock:
            if job.cancel_requested:
                job.state = "cancelled"
            else:
                job.state = "done"
                job.phase_name = "완료"

    @staticmethod
    def _stop_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
            except (OSError, ValueError):
                process.terminate()
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                result = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                if result.returncode != 0 and process.poll() is None:
                    process.kill()
            else:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def cancel(self) -> bool:
        with self.lock:
            job = self.current
            if job is None or job.state != "running":
                return False
            with job.lock:
                job.cancel_requested = True
            process = self._process
        if process is not None:
            self._stop_process(process)
        job.finished.wait(timeout=10)
        with job.lock:
            if job.state == "running":
                job.state = "cancelled"
        return True

    def shutdown(self) -> None:
        with self.lock:
            job = self.current
            process = self._process
            if job is not None and job.state == "running":
                with job.lock:
                    job.cancel_requested = True
        if process is not None:
            self._stop_process(process)
        if job is not None:
            job.finished.wait(timeout=10)
            with job.lock:
                if job.state == "running":
                    job.state = "cancelled"


class WorkflowContext:
    def __init__(self, pkg_root: Optional[Path], port: int) -> None:
        self.pkg_root = pkg_root
        self.port = port
        self.token = secrets.token_urlsafe(32)
        self.jobs = JobManager()
        self.src = pkg_root / "01_원본사진" if pkg_root else None
        self.work = pkg_root / "02_작업장" if pkg_root else None
        self.out = pkg_root / "03_결과물" if pkg_root else None
        self.scripts = pkg_root / "05_스크립트" if pkg_root else None

    @property
    def enabled(self) -> bool:
        return self.pkg_root is not None

    def allowed_hosts(self) -> frozenset[str]:
        return frozenset({f"127.0.0.1:{self.port}", f"localhost:{self.port}"})

    def allowed_origins(self) -> frozenset[str]:
        return frozenset(
            {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}
        )


class Lifecycle:
    """하트비트 시각과 한 번뿐인 종료 요청을 스레드 안전하게 관리한다."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started_at = time.monotonic()
        self.last_beat: Optional[float] = None
        self.beat_generation = 0
        self.stop_reason: Optional[str] = None
        self.shutdown_started = False
        self.goodbye_pending = threading.Event()

    def touch(self) -> None:
        with self.lock:
            self.last_beat = time.monotonic()
            self.beat_generation += 1

    def snapshot(self) -> tuple[float, Optional[float], bool]:
        with self.lock:
            return self.started_at, self.last_beat, self.shutdown_started

    def request_shutdown(
        self, server: http.server.ThreadingHTTPServer, reason: str
    ) -> None:
        with self.lock:
            if self.shutdown_started:
                return
            self.shutdown_started = True
            self.stop_reason = reason
        server.shutdown()

    def schedule_goodbye(self, server: http.server.ThreadingHTTPServer) -> None:
        """응답 후 종료하되 새 핑이나 실행 중인 잡이 있으면 안전하게 미룬다."""
        with self.lock:
            if self.shutdown_started or self.goodbye_pending.is_set():
                return
            self.goodbye_pending.set()
            generation = self.beat_generation

        def finish() -> None:
            time.sleep(GOODBYE_GRACE)
            with self.lock:
                cancelled = self.beat_generation != generation
                if cancelled:
                    self.goodbye_pending.clear()
            if cancelled:
                return

            jobs = server.workflow.jobs  # type: ignore[attr-defined]
            while jobs.busy:
                time.sleep(0.1)
            with self.lock:
                cancelled = self.beat_generation != generation
                self.goodbye_pending.clear()
            if cancelled:
                return
            if jobs.current is not None:
                self.touch()
                return
            self.request_shutdown(server, "창 닫힘 감지")

        threading.Thread(target=finish, daemon=True).start()


class ToolHandler(http.server.SimpleHTTPRequestHandler):
    """정적 파일, 생명주기 엔드포인트, 인증된 워크플로 API를 제공한다."""

    server: http.server.ThreadingHTTPServer

    @property
    def workflow(self) -> WorkflowContext:
        return self.server.workflow  # type: ignore[attr-defined,no-any-return]

    def guess_type(self, path: str) -> str:
        suffix = Path(path).suffix.lower()
        if suffix in {".html", ".htm"}:
            return "text/html; charset=utf-8"
        if suffix == ".js":
            return "text/javascript; charset=utf-8"
        if suffix == ".json":
            return "application/json; charset=utf-8"
        return super().guess_type(path)

    def _endpoint(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    def _no_content(self) -> None:
        self.send_response(http.HTTPStatus.NO_CONTENT)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _send_json(self, obj: dict[str, object], status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, code: str, detail: str) -> None:
        self._send_json(_error_payload(code, detail), status)

    def _check_host(self) -> bool:
        if self.headers.get("Host", "") in self.workflow.allowed_hosts():
            return True
        self._error(403, "bad_host", "허용되지 않은 Host 헤더입니다.")
        return False

    def _check_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin not in self.workflow.allowed_origins():
            self._error(403, "bad_origin", "같은 주소에서 시작한 요청만 허용됩니다.")
            return False
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site is not None and fetch_site != "same-origin":
            self._error(403, "bad_origin", "교차 사이트 요청은 허용되지 않습니다.")
            return False
        return True

    def _check_token(self) -> bool:
        supplied = self.headers.get("X-Workflow-Token")
        if supplied is None:
            self._error(401, "token_missing", "워크플로 토큰이 필요합니다.")
            return False
        if not hmac.compare_digest(supplied, self.workflow.token):
            self._error(403, "token_invalid", "워크플로 토큰이 올바르지 않습니다.")
            return False
        return True

    def _token_page_is_same_origin(self) -> bool:
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site is not None:
            return fetch_site == "same-origin"
        referer = self.headers.get("Referer")
        if not referer:
            return False
        parts = urllib.parse.urlsplit(referer)
        return f"{parts.scheme}://{parts.netloc}" in self.workflow.allowed_origins()

    def _read_json_body(self, limit: int) -> Optional[dict[str, object]]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            length = -1
        if length < 0:
            self._error(400, "bad_body", "Content-Length가 필요합니다.")
            return None
        if length > limit:
            self._error(413, "body_too_large", "요청 본문이 허용 크기를 넘었습니다.")
            return None
        try:
            body = self.rfile.read(length)
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "bad_json", "올바른 JSON 객체를 보내세요.")
            return None
        if not isinstance(parsed, dict):
            self._error(400, "bad_json", "JSON 최상위 값은 객체여야 합니다.")
            return None
        return parsed

    def _workflow_required(self) -> bool:
        if self.workflow.enabled:
            return True
        self._error(503, "workflow_disabled", "이 서버 루트에서는 워크플로 API를 사용할 수 없습니다.")
        return False

    def do_HEAD(self) -> None:
        if not self._check_host():
            return
        super().do_HEAD()

    def do_OPTIONS(self) -> None:
        if not self._check_host():
            return
        self._error(405, "method_not_allowed", "OPTIONS 요청은 지원하지 않습니다.")

    def do_GET(self) -> None:
        if not self._check_host():
            return
        endpoint = self._endpoint()
        if endpoint == "/heartbeat":
            self.server.lifecycle.touch()  # type: ignore[attr-defined]
            self._no_content()
            return
        if endpoint == "/api/token":
            self.api_token()
            return
        if endpoint.startswith("/api/"):
            if not self._check_token():
                return
            if endpoint == "/api/status":
                self.api_status()
                return
            if endpoint == "/api/job":
                self.api_job()
                return
            self._error(404, "not_found", "API 경로를 찾을 수 없습니다.")
            return
        super().do_GET()

    def do_POST(self) -> None:
        if not self._check_host():
            return
        endpoint = self._endpoint()
        if endpoint == "/heartbeat":
            self.server.lifecycle.touch()  # type: ignore[attr-defined]
            self._no_content()
            return
        if endpoint == "/goodbye":
            self._no_content()
            self.server.lifecycle.schedule_goodbye(self.server)  # type: ignore[attr-defined]
            return
        if not endpoint.startswith("/api/"):
            self.send_error(http.HTTPStatus.NOT_FOUND, "Not Found")
            return
        if not self._check_origin() or not self._check_token():
            return
        if not self._workflow_required():
            return

        routes = {
            "/api/upload": self.api_upload,
            "/api/open-folder": self.api_open_folder,
            "/api/rename-group": self.api_rename_group,
            "/api/prepare": self.api_prepare,
            "/api/export-pdf": self.api_export_pdf,
            "/api/job/cancel": self.api_job_cancel,
        }
        handler = routes.get(endpoint)
        if handler is None:
            self._error(404, "not_found", "API 경로를 찾을 수 없습니다.")
            return
        handler()

    def api_token(self) -> None:
        if not self._token_page_is_same_origin():
            self._error(403, "token_origin", "같은 주소에서 연 페이지에서만 토큰을 받을 수 있습니다.")
            return
        self._send_json({"ok": True, "token": self.workflow.token})

    def _src_count(self) -> int:
        if self.workflow.src is None:
            return 0
        try:
            return sum(
                1
                for path in self.workflow.src.rglob("*")
                if path.is_file() and path.suffix.lower() in UPLOAD_EXTS
            )
        except OSError:
            return 0

    def api_status(self) -> None:
        if not self.workflow.enabled:
            self._send_json({"ok": True, "workflow": False})
            return
        assert self.workflow.pkg_root is not None
        assert self.workflow.work is not None
        assert self.workflow.out is not None
        env = read_env_status(self.workflow.pkg_root)
        groups: list[dict[str, object]] = []
        try:
            for folder in sorted(self.workflow.work.iterdir(), key=lambda path: path.name):
                image_dir = folder / "img"
                if not folder.is_dir() or folder.name == "slide_tool" or not image_dir.is_dir():
                    continue
                try:
                    count = sum(
                        1
                        for path in image_dir.iterdir()
                        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
                    )
                except OSError:
                    count = 0
                groups.append({"name": folder.name, "count": count})
        except OSError:
            groups = []
        try:
            result_count = sum(
                1
                for path in self.workflow.out.iterdir()
                if path.is_file() and path.suffix.lower() == ".pdf"
            )
        except OSError:
            result_count = 0
        current = self.workflow.jobs.current
        self._send_json(
            {
                "ok": True,
                "workflow": True,
                "env": env,
                "srcCount": self._src_count(),
                "worktree": (self.workflow.work / "worktree.json").is_file(),
                "dataJs": (self.workflow.work / "slide_tool" / "data.js").is_file(),
                "groups": groups,
                "job": current.snapshot(0) if current is not None else None,
                "resultCount": result_count,
            }
        )

    def api_open_folder(self) -> None:
        payload = self._read_json_body(1024 * 1024)
        if payload is None:
            return
        target = payload.get("target")
        mapping = {"src": self.workflow.src, "out": self.workflow.out}
        if not isinstance(target, str) or target not in mapping:
            self._error(400, "bad_target", "target은 src 또는 out이어야 합니다.")
            return
        path = mapping[target]
        assert path is not None
        if os.environ.get("SLIDE_TOOL_NO_OS_OPEN") != "1":
            try:
                if sys.platform == "win32":
                    os.startfile(str(path))  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(path)])
                else:
                    subprocess.Popen(["xdg-open", str(path)])
            except OSError as exc:
                self._error(500, "open_failed", f"폴더를 열지 못했습니다: {exc}")
                return
        self._send_json({"ok": True})

    def api_upload(self) -> None:
        raw_name = self.headers.get("X-Filename", "")
        name, reason = safe_upload_name(raw_name)
        if name is None:
            self._error(400, "bad_filename", reason)
            return
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            length = -1
        if length < 0 or length > MAX_UPLOAD_BYTES:
            self._error(413, "upload_too_large", "Content-Length가 없거나 100MB 상한을 넘었습니다.")
            return

        assert self.workflow.src is not None
        temp_path = self.workflow.src / f".업로드중-{uuid.uuid4().hex}.part"
        final_path: Optional[Path] = None
        received = 0
        digest = hashlib.sha256()
        try:
            with temp_path.open("xb") as target:
                while received < length:
                    chunk = self.rfile.read(min(STREAM_CHUNK_BYTES, length - received))
                    if not chunk:
                        break
                    target.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
            if received != length:
                self._error(400, "incomplete_upload", "선언한 크기만큼 파일을 받지 못했습니다.")
                return

            final_path, renamed, dedup = unique_dst(
                self.workflow.src, name, digest.hexdigest()
            )
            if dedup:
                self._send_json(
                    {"ok": True, "saved": name, "renamed": False, "dedup": True}
                )
                return
            assert final_path is not None
            try:
                with temp_path.open("rb") as source, final_path.open("r+b") as target:
                    while True:
                        chunk = source.read(STREAM_CHUNK_BYTES)
                        if not chunk:
                            break
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
            except Exception:
                final_path.unlink(missing_ok=True)
                raise
            self._send_json(
                {
                    "ok": True,
                    "saved": final_path.name,
                    "renamed": renamed,
                    "dedup": False,
                }
            )
        except OSError as exc:
            self._error(500, "upload_failed", f"파일을 저장하지 못했습니다: {exc}")
        finally:
            temp_path.unlink(missing_ok=True)

    def api_rename_group(self) -> None:
        payload = self._read_json_body(MAX_RENAME_BODY_BYTES)
        if payload is None:
            return
        source_name, source_reason = safe_group_name(payload.get("from"))
        target_name, target_reason = safe_group_name(payload.get("to"))
        if source_name is None:
            self._error(400, "bad_group_name", source_reason)
            return
        if target_name is None:
            self._error(400, "bad_group_name", target_reason)
            return

        assert self.workflow.work is not None
        try:
            source_matches = [
                path
                for path in self.workflow.work.iterdir()
                if nfc(path.name) == source_name
            ]
            target_matches = [
                path
                for path in self.workflow.work.iterdir()
                if nfc(path.name) == target_name
            ]
        except OSError as exc:
            self._error(500, "group_read_failed", f"그룹 목록을 읽지 못했습니다: {exc}")
            return
        source = source_matches[0] if len(source_matches) == 1 else self.workflow.work / source_name
        source_disk_name = source.name
        target = self.workflow.work / target_name
        plan_path = self.workflow.work / "worktree.json"
        manifest_script = self.workflow.work / "slide_tool" / "gen_manifest.py"
        data_path = self.workflow.work / "slide_tool" / "data.js"

        # 잡 시작과 이름 변경이 서로 엇갈리지 않도록 JobManager의 시작 잠금을
        # 트랜잭션 전체에 유지한다. 실행 중인 잡은 여기서 즉시 거부한다.
        with self.workflow.jobs.lock:
            current = self.workflow.jobs.current
            if current is not None and current.state == "running":
                self._error(409, "busy", "다른 작업이 실행 중입니다.")
                return
            if (
                len(source_matches) != 1
                or not source.is_dir()
                or source.is_symlink()
                or not (source / "img").is_dir()
                or (source / "img").is_symlink()
            ):
                self._error(404, "group_not_found", "img 폴더가 있는 현재 그룹을 찾지 못했습니다.")
                return
            if target_matches or os.path.lexists(target):
                self._error(409, "group_exists", "같은 이름의 대상이 이미 있습니다.")
                return
            if not manifest_script.is_file():
                self._error(500, "manifest_missing", "목록 생성 스크립트를 찾지 못했습니다.")
                return

            plan_original: Optional[bytes] = None
            plan_updated: Optional[bytes] = None
            if plan_path.is_file():
                try:
                    plan_original = plan_path.read_bytes()
                    plan_doc = json.loads(plan_original.decode("utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._error(500, "worktree_invalid", f"worktree.json을 읽지 못했습니다: {exc}")
                    return
                if not isinstance(plan_doc, dict):
                    self._error(500, "worktree_invalid", "worktree.json 최상위 값은 객체여야 합니다.")
                    return
                groups_present = "groups" in plan_doc
                groups = plan_doc.get("groups")
                if groups_present and not isinstance(groups, dict):
                    self._error(500, "worktree_invalid", "worktree.json의 groups는 객체여야 합니다.")
                    return
                source_plan_names = [name for name in groups or {} if nfc(str(name)) == source_name]
                target_plan_names = [name for name in groups or {} if nfc(str(name)) == target_name]
                if groups_present:
                    assert isinstance(groups, dict)
                    if len(source_plan_names) != 1:
                        self._error(
                            409,
                            "worktree_mismatch",
                            "worktree.json에서 현재 그룹을 하나로 확인하지 못했습니다.",
                        )
                        return
                    source_plan_name = source_plan_names[0]
                    if target_plan_names:
                        self._error(409, "group_exists", "worktree.json에 같은 대상 그룹이 이미 있습니다.")
                        return
                    plan_doc["groups"] = {
                        target_name if name == source_plan_name else name: files
                        for name, files in groups.items()
                    }
                    plan_updated = json.dumps(
                        plan_doc, ensure_ascii=False, indent=2
                    ).encode("utf-8")

            try:
                data_original = data_path.read_bytes() if data_path.is_file() else None
            except OSError as exc:
                self._error(500, "manifest_read_failed", f"기존 data.js를 읽지 못했습니다: {exc}")
                return

            renamed = False
            plan_changed = False
            manifest_started = False
            target_disk_name = target_name
            try:
                source.rename(target)
                renamed = True
                if plan_updated is not None:
                    atomic_write_bytes(plan_path, plan_updated)
                    plan_changed = True
                child_env = dict(os.environ)
                child_env["PYTHONIOENCODING"] = "utf-8:replace"
                child_env["PYTHONUTF8"] = "1"
                manifest_started = True
                result = subprocess.run(
                    [sys.executable, str(manifest_script)],
                    cwd=str(manifest_script.parent),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=child_env,
                    timeout=30,
                    check=False,
                )
                if result.returncode != 0:
                    detail = result.stdout.strip() or f"종료코드 {result.returncode}"
                    raise OSError(f"data.js 갱신 실패: {detail}")
                target_disk_name = next(
                    (
                        path.name
                        for path in self.workflow.work.iterdir()
                        if nfc(path.name) == target_name
                    ),
                    target_name,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                rollback_errors: list[str] = []
                if manifest_started:
                    try:
                        if data_original is None:
                            data_path.unlink(missing_ok=True)
                        else:
                            atomic_write_bytes(data_path, data_original)
                    except OSError as rollback_exc:
                        rollback_errors.append(f"data.js: {rollback_exc}")
                if plan_changed and plan_original is not None:
                    try:
                        atomic_write_bytes(plan_path, plan_original)
                    except OSError as rollback_exc:
                        rollback_errors.append(f"worktree.json: {rollback_exc}")
                if renamed:
                    try:
                        if os.path.lexists(target) and not os.path.lexists(source):
                            target.rename(source)
                    except OSError as rollback_exc:
                        rollback_errors.append(f"폴더: {rollback_exc}")
                if rollback_errors:
                    self._error(
                        500,
                        "rename_rollback_failed",
                        f"그룹 이름 변경과 복구에 실패했습니다: {exc}; " + "; ".join(rollback_errors),
                    )
                else:
                    self._error(500, "rename_failed", f"그룹 이름을 바꾸지 못했습니다: {exc}")
                return

        self._send_json({"ok": True, "from": source_disk_name, "to": target_disk_name})

    def _job_prerequisites(self) -> tuple[Optional[Path], dict[str, object]]:
        assert self.workflow.pkg_root is not None
        python = worker_python(self.workflow.pkg_root)
        if python is None:
            self._error(503, "venv_missing", "시작 파일을 다시 실행해 환경 설치를 먼저 하세요.")
            return None, {}
        return python, read_env_status(self.workflow.pkg_root)

    def api_prepare(self) -> None:
        payload = self._read_json_body(1024 * 1024)
        if payload is None:
            return
        if self.workflow.jobs.busy:
            self._error(409, "busy", "다른 작업이 실행 중입니다.")
            return
        regroup = payload.get("regroup", False)
        gap = payload.get("gapMinutes", 20)
        if not isinstance(regroup, bool):
            self._error(400, "bad_request", "regroup은 true 또는 false여야 합니다.")
            return
        if isinstance(gap, bool) or not isinstance(gap, int) or not 1 <= gap <= 600:
            self._error(400, "bad_request", "gapMinutes는 1~600 정수여야 합니다.")
            return
        python, env = self._job_prerequisites()
        if python is None:
            return
        if self._src_count() == 0:
            self._error(409, "no_photos", "원본 사진을 먼저 넣으세요.")
            return

        assert self.workflow.pkg_root is not None
        assert self.workflow.src is not None
        assert self.workflow.work is not None
        assert self.workflow.scripts is not None
        plan = self.workflow.work / "worktree.json"
        steps: list[tuple[str, list[str]]] = []
        if regroup or not plan.is_file():
            command = [
                str(python),
                str(self.workflow.scripts / "init_worktree.py"),
                "--src",
                str(self.workflow.src),
                "--out",
                str(self.workflow.work),
                "--by-gap",
                str(gap),
            ]
            if regroup:
                command.append("--force")
            steps.append(("그룹 나누기", command))
        steps.append(
            (
                "축소본 만들기",
                [
                    str(python),
                    str(self.workflow.scripts / "prepare_photos.py"),
                    "--plan",
                    str(plan),
                    "--workers",
                    str(env["workers"]),
                ],
            )
        )
        steps.append(
            (
                "목록 만들기",
                [
                    str(python),
                    str(self.workflow.work / "slide_tool" / "gen_manifest.py"),
                ],
            )
        )
        job = self.workflow.jobs.start("prepare", steps)
        if job is None:
            self._error(409, "busy", "다른 작업이 실행 중입니다.")
            return
        self._send_json({"ok": True, "job": {"id": job.id, "kind": job.kind}}, 202)

    def _save_backup(self, backup: dict[str, object]) -> Path:
        assert self.workflow.out is not None
        backup_dir = self.workflow.out / "백업"
        backup_dir.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(backup, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_EXPORT_BODY_BYTES:
            raise ValueError("백업 JSON이 64MB 상한을 넘었습니다.")
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        for index in range(1, 1000):
            suffix = "" if index == 1 else f"_{index}"
            path = backup_dir / f"slide_tool_backup_{stamp}{suffix}.json"
            try:
                with path.open("xb") as target:
                    target.write(encoded)
                return path
            except FileExistsError:
                continue
        raise OSError("백업 파일 이름을 정할 수 없습니다.")

    def api_export_pdf(self) -> None:
        payload = self._read_json_body(MAX_EXPORT_BODY_BYTES)
        if payload is None:
            return
        if self.workflow.jobs.busy:
            self._error(409, "busy", "다른 작업이 실행 중입니다.")
            return
        backup = payload.get("backup")
        only_done = payload.get("onlyDone", False)
        merge = payload.get("merge", False)
        if not isinstance(backup, dict):
            self._error(400, "bad_backup", "backup은 JSON 객체여야 합니다.")
            return
        if not isinstance(only_done, bool) or not isinstance(merge, bool):
            self._error(400, "bad_request", "onlyDone과 merge는 true 또는 false여야 합니다.")
            return
        python, env = self._job_prerequisites()
        if python is None:
            return
        try:
            backup_path = self._save_backup(backup)
        except ValueError as exc:
            self._error(413, "body_too_large", str(exc))
            return
        except OSError as exc:
            self._error(500, "backup_save_failed", f"백업을 저장하지 못했습니다: {exc}")
            return

        assert self.workflow.pkg_root is not None
        assert self.workflow.src is not None
        assert self.workflow.work is not None
        assert self.workflow.out is not None
        assert self.workflow.scripts is not None
        command = [
            str(python),
            str(self.workflow.scripts / "export_pdf.py"),
            "--backup",
            str(backup_path),
            "--root",
            str(self.workflow.work),
            "--src-dir",
            str(self.workflow.src),
            "--out",
            str(self.workflow.out),
            "--workers",
            str(env["workers"]),
        ]
        if only_done:
            command.append("--only-done")
        if merge:
            command.extend(("--merge", "전체.pdf"))
        job = self.workflow.jobs.start("export", [("PDF 만들기", command)])
        if job is None:
            self._error(409, "busy", "다른 작업이 실행 중입니다.")
            return
        self._send_json({"ok": True, "job": {"id": job.id, "kind": job.kind}}, 202)

    def api_job(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        raw_after = query.get("after", ["0"])[0]
        try:
            after = int(raw_after)
        except ValueError:
            after = -1
        if after < 0:
            self._error(400, "bad_after", "after는 0 이상의 정수여야 합니다.")
            return
        current = self.workflow.jobs.current
        self._send_json(
            {"ok": True, "job": current.snapshot(after) if current is not None else None}
        )

    def api_job_cancel(self) -> None:
        if not self.workflow.jobs.cancel():
            self._error(409, "no_job", "취소할 실행 중인 작업이 없습니다.")
            return
        current = self.workflow.jobs.current
        self._send_json(
            {
                "ok": True,
                "job": current.snapshot(0) if current is not None else None,
                "detail": "작업을 취소했습니다. 일부 생성물은 남아 있을 수 있습니다.",
            }
        )

    def log_message(self, _format: str, *args: object) -> None:
        return


class Watchdog(threading.Thread):
    def __init__(
        self,
        server: http.server.ThreadingHTTPServer,
        lifecycle: Lifecycle,
        timeout: float,
        grace: float,
        jobs: JobManager,
    ) -> None:
        super().__init__(daemon=True)
        self.server = server
        self.lifecycle = lifecycle
        self.timeout = timeout
        self.grace = grace
        self.jobs = jobs

    def run(self) -> None:
        job_was_busy = False
        while True:
            if self.jobs.busy:
                job_was_busy = True
                time.sleep(0.1)
                continue
            if job_was_busy:
                self.lifecycle.touch()
                job_was_busy = False
            started_at, last_beat, shutting_down = self.lifecycle.snapshot()
            if shutting_down:
                return
            now = time.monotonic()
            if last_beat is None:
                if now - started_at > self.grace:
                    self.lifecycle.request_shutdown(self.server, "유예 초과")
                    return
            elif now - last_beat > self.timeout:
                self.lifecycle.request_shutdown(self.server, "창 닫힘 감지")
                return
            time.sleep(0.1)


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("0보다 큰 값을 입력하세요.")
    return number


def valid_port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("포트는 1~65535 범위여야 합니다.")
    return port


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    default_root = Path(__file__).resolve().parent.parent / "02_작업장"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=default_root, help="서버 루트")
    parser.add_argument("--pkg-root", default=None, help="배포 패키지 루트")
    parser.add_argument("--port", type=valid_port, default=DEFAULT_PORT, help="접속 포트")
    parser.add_argument(
        "--timeout",
        type=positive_float,
        default=DEFAULT_TIMEOUT,
        help="마지막 핑 이후 종료 시간(초)",
    )
    parser.add_argument(
        "--grace",
        type=positive_float,
        default=DEFAULT_GRACE,
        help="첫 핑을 기다리는 유예 시간(초)",
    )
    parser.add_argument("--no-open", action="store_true", help="브라우저를 열지 않음")
    parser.add_argument("--no-watchdog", action="store_true", help="자동 종료를 끔")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    root = args.root.expanduser().resolve()
    if not (root / "slide_tool" / "index.html").is_file():
        print(f"오류: 서버 루트에 slide_tool/index.html이 없습니다: {root}")
        return 2

    pkg_root = resolve_pkg_root(root, args.pkg_root)
    workflow = WorkflowContext(pkg_root, args.port)
    handler = functools.partial(ToolHandler, directory=str(root))
    try:
        server = http.server.ThreadingHTTPServer((BIND, args.port), handler)
    except OSError as exc:
        print(f"오류: {BIND}:{args.port} 포트가 이미 사용 중이거나 열 수 없습니다. ({exc})")
        print("주의: 포트를 바꾸면 브라우저가 다른 사이트로 인식해 이전 작업이 보이지 않습니다.")
        return 1

    lifecycle = Lifecycle()
    server.lifecycle = lifecycle  # type: ignore[attr-defined]
    server.workflow = workflow  # type: ignore[attr-defined]
    address = f"http://{BIND}:{args.port}/slide_tool/"
    print(f"슬라이드 도구 접속 주소: {address}", flush=True)
    print(f"워크플로 API: {'활성' if workflow.enabled else '비활성'}", flush=True)
    if args.port != DEFAULT_PORT:
        print("주의: 포트가 바뀌면 이전 포트의 브라우저 작업이 보이지 않을 수 있습니다.", flush=True)
    if args.no_watchdog:
        print("자동 종료를 사용하지 않습니다. 끝나면 서버를 직접 종료하세요.", flush=True)
    else:
        Watchdog(server, lifecycle, args.timeout, args.grace, workflow.jobs).start()

    if not args.no_open:
        threading.Thread(target=webbrowser.open, args=(address,), daemon=True).start()

    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        with lifecycle.lock:
            lifecycle.stop_reason = "수동 종료"
    finally:
        workflow.jobs.shutdown()
        server.server_close()

    reason = lifecycle.stop_reason or "수동 종료"
    if reason == "유예 초과":
        print("서버 종료: 유예 초과 — 브라우저의 첫 핑이 도착하지 않았습니다.", flush=True)
    else:
        print(f"서버 종료: {reason}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
