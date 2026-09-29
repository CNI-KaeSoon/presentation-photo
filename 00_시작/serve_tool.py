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
import shutil
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
MAX_AUTO_DETECT_BODY_BYTES = 64 * 1024
MAX_AUTO_DETECT_KEYS = 32
AUTO_DETECT_TIMEOUT = 120
# conf(정답과 5% 이내로 맞을 확률 0~1)가 이 값보다 낮거나 검출이 null 이면 review=true.
# 교차 검증에서 conf<0.5 사진은 hit@5% 가 약 0.3 이었다.
AUTO_DETECT_REVIEW_BELOW = 0.5
AUTO_DETECT_EXTS = (".jpg", ".jpeg", ".png")
STREAM_CHUNK_BYTES = 1024 * 1024
# export_pdf.py 가 끝에 찍는 기계용 요약 한 줄의 머리말(같은 값 — 테스트가 일치를 확인한다).
PDF_SUMMARY_PREFIX = "@@PDF_SUMMARY@@ "
# 업로드 사진의 수정 시각(File.lastModified, epoch ms)으로 받아들이는 범위.
MIN_LAST_MODIFIED_MS = 946_684_800_000          # 2000-01-01
# 새 행사 시작 때 이전 작업을 옮겨 두는 폴더 이름(02_작업장 바로 아래). `_` 로 시작하는 폴더는
# 보관·예약용이라 그룹으로 취급하지 않는다(그룹 목록·내보내기·자동 찾기 모두 제외).
ARCHIVE_PREFIX = "_이전작업_"
BACKUP_NAME_RE = re.compile(r"^slide_tool_backup_[0-9]{8}-[0-9]{6}(?:_[0-9]{1,3})?\.json$")
MAX_BACKUP_LIST = 30
MAX_NEW_EVENT_BODY_BYTES = 64 * 1024 * 1024
MAX_PHOTO_ORDER_ITEMS = 20_000
MAX_PHOTO_ORDER_KEY_CHARS = 1024
WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)

STAGED_EXPORT_CODE = r"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

export_command = json.loads(sys.argv[1])
mode = sys.argv[2]
order = json.loads(sys.argv[3])
out_dir = Path(sys.argv[4])

def publish(source, destination):
    descriptor, temp_name = tempfile.mkstemp(
        prefix=".slide_tool_export_", suffix=".tmp", dir=str(destination.parent)
    )
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        shutil.copyfile(source, temp_path)
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)

with tempfile.TemporaryDirectory(prefix="slide_tool_export_") as temp_dir:
    merge_name = "_slide_tool_merged.pdf"
    command = export_command + ["--out", temp_dir]
    if mode == "ordered":
        command += ["--only", *order]
    else:
        command += ["--merge", merge_name]
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)
    staging = Path(temp_dir)
    merged = staging / merge_name
    if mode == "ordered":
        inputs = [staging / f"{name}.pdf" for name in order]
        skipped = [name for name, path in zip(order, inputs) if not path.is_file()]
        inputs = [path for path in inputs if path.is_file()]
        for name in skipped:
            print(f"0쪽 그룹 건너뜀: {name}")
        if not inputs:
            print("!! 병합할 페이지가 없습니다. 완료 상태를 확인하세요.", file=sys.stderr)
            raise SystemExit(1)
        pdfunite = shutil.which("pdfunite")
        if pdfunite is None:
            print("!! 통합 PDF에 필요한 pdfunite(poppler)를 찾을 수 없습니다.", file=sys.stderr)
            raise SystemExit(127)
        subprocess.run([pdfunite, *(str(path) for path in inputs), str(merged)], check=True)
        outputs = [(merged, out_dir / "전체.pdf")]
    else:
        if not merged.is_file():
            print("!! pdfunite 병합이 실패해 통합 PDF가 생성되지 않았습니다.", file=sys.stderr)
            raise SystemExit(1)
        outputs = [
            (path, out_dir / ("전체.pdf" if path == merged else path.name))
            for path in sorted(staging.glob("*.pdf"), key=lambda item: item.name)
        ]
    out_dir.mkdir(parents=True, exist_ok=True)
    for source, destination in outputs:
        publish(source, destination)
        print(f"결과 파일: {destination.name}")
"""


def _error_payload(code: str, detail: str) -> dict[str, object]:
    return {"ok": False, "error": code, "detail": detail}


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def is_group_folder_name(name: str) -> bool:
    """작업장 직계 폴더 이름이 그룹 후보인지. slide_tool·`_`(보관/예약)·`.`(숨김)로 시작하면 아니다."""
    return name != "slide_tool" and not name.startswith(("_", "."))


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


def parse_last_modified(raw: object) -> Optional[float]:
    """브라우저 File.lastModified(epoch ms 정수 문자열) → epoch 초. 이상하면 None(무시).

    숫자 형식·범위(2000-01-01 ~ 지금+1일)를 벗어난 값은 시각으로 쓰지 않는다.
    """
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,16}", raw.strip()):
        return None
    millis = int(raw.strip())
    if millis < MIN_LAST_MODIFIED_MS or millis > (time.time() + 86400) * 1000:
        return None
    return millis / 1000.0


def sanitize_photo_order(raw: object) -> tuple[Optional[dict[str, list[str]]], str]:
    """브라우저가 보낸 그룹별 최종 사진 순서 {그룹: [이미지 키…]} 를 검증한다.

    값은 export_pdf 가 조회용 문자열로만 쓰고 경로로 열지 않는다. 그래도 크기·형식은 제한한다.
    """
    if not isinstance(raw, dict):
        return None, "photoOrder는 그룹별 이미지 키 배열을 담은 객체여야 합니다."
    clean: dict[str, list[str]] = {}
    total = 0
    for group, keys in raw.items():
        if not isinstance(group, str) or not group or len(group) > MAX_PHOTO_ORDER_KEY_CHARS:
            return None, "photoOrder의 그룹 이름이 올바르지 않습니다."
        if not isinstance(keys, list):
            return None, "photoOrder의 값은 이미지 키 배열이어야 합니다."
        total += len(keys)
        if total > MAX_PHOTO_ORDER_ITEMS:
            return None, "photoOrder 항목이 너무 많습니다."
        for key in keys:
            if (
                not isinstance(key, str)
                or not key
                or len(key) > MAX_PHOTO_ORDER_KEY_CHARS
                or "\x00" in key
            ):
                return None, "photoOrder의 이미지 키가 올바르지 않습니다."
        clean[group] = list(keys)
    return clean, ""


def is_within(path: Path, root: Path) -> bool:
    """resolve 된 경로가 root 안(자신 포함)인지 확인한다. export_pdf.is_within 과 같은 판정."""
    try:
        common = os.path.commonpath((str(path), str(root)))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(str(root))


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
        self.result: Optional[dict[str, object]] = None   # export 의 그룹별 쪽 수 요약
        self.cancel_requested = False
        self.finished = threading.Event()
        self.lock = threading.Lock()
        self._seq = 0
        self._lines: collections.deque[tuple[int, str]] = collections.deque(maxlen=2000)

    def append_line(self, line: str) -> None:
        clean = str(line).rstrip("\r\n")
        if clean.startswith(PDF_SUMMARY_PREFIX):
            # 화면 로그에는 싣지 않고 잡 결과로만 보관한다. 깨진 줄은 조용히 버린다.
            try:
                parsed = json.loads(clean[len(PDF_SUMMARY_PREFIX):])
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                with self.lock:
                    self.result = parsed
            return
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
            if self.result is not None:
                result["result"] = self.result
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
        self.detect_lock = threading.Lock()
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
        # --no-watchdog 이면 탭이 닫혀도(/goodbye) 서버를 끄지 않는다 — main 이 False 로 바꾼다.
        self.goodbye_enabled = True

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
            if (
                not self.goodbye_enabled
                or self.shutdown_started
                or self.goodbye_pending.is_set()
            ):
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
            if endpoint == "/api/backups":
                self.api_backups()
                return
            if endpoint == "/api/backup":
                self.api_backup()
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
            "/api/auto-detect": self.api_auto_detect,
            "/api/new-event": self.api_new_event,
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
                if (
                    not folder.is_dir()
                    or not is_group_folder_name(folder.name)
                    or not image_dir.is_dir()
                ):
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
                "planMismatch": self._plan_mismatch(),
                "dataJs": (self.workflow.work / "slide_tool" / "data.js").is_file(),
                "groups": groups,
                "job": current.snapshot(0) if current is not None else None,
                "resultCount": result_count,
            }
        )

    def _plan_mismatch(self) -> Optional[dict[str, int]]:
        """worktree.json 계획의 사진 이름과 원본 폴더의 사진 이름을 견줘 어긋난 수를 센다.

        missing = 계획에는 있는데 원본에 없는 사진, added = 원본에는 있는데 계획에 없는 사진.
        계획 파일이 없거나 읽을 수 없으면 None(견줄 기준이 없다).
        """
        if self.workflow.work is None or self.workflow.src is None:
            return None
        plan_path = self.workflow.work / "worktree.json"
        try:
            doc = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return None
        groups = doc.get("groups") if isinstance(doc, dict) else None
        if not isinstance(groups, dict):
            return None
        planned: set[str] = set()
        for names in groups.values():
            if isinstance(names, list):
                planned.update(nfc(str(name)) for name in names)
        present: set[str] = set()
        try:
            for path in self.workflow.src.rglob("*"):
                if (
                    path.is_file()
                    and not path.name.startswith(".")
                    and path.suffix.lower() in UPLOAD_EXTS
                ):
                    present.add(nfc(path.name))
        except OSError:
            return None
        return {"missing": len(planned - present), "added": len(present - planned)}

    def api_backups(self) -> None:
        """03_결과물/백업/ 의 백업 파일 목록(최신 순). 파일명은 서버가 만든 형식만 인정한다."""
        if not self._workflow_required():
            return
        assert self.workflow.out is not None
        backup_dir = self.workflow.out / "백업"
        rows: list[tuple[float, str, int]] = []
        try:
            entries = list(backup_dir.iterdir()) if backup_dir.is_dir() else []
        except OSError:
            entries = []
        for path in entries:
            if not BACKUP_NAME_RE.match(path.name) or path.is_symlink():
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            if path.is_file():
                rows.append((info.st_mtime, path.name, info.st_size))
        rows.sort(reverse=True)
        self._send_json(
            {
                "ok": True,
                "backups": [
                    {"name": name, "size": size, "modified": mtime}
                    for mtime, name, size in rows[:MAX_BACKUP_LIST]
                ],
            }
        )

    def api_backup(self) -> None:
        """백업 파일 하나의 내용. name 은 목록의 이름과 같은 형식만 허용해 경로 탈출을 막는다."""
        if not self._workflow_required():
            return
        assert self.workflow.out is not None
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        name = query.get("name", [""])[0]
        if not BACKUP_NAME_RE.match(name):
            self._error(400, "bad_backup_name", "백업 파일 이름 형식이 올바르지 않습니다.")
            return
        backup_dir = self.workflow.out / "백업"
        path = backup_dir / name
        try:
            root = backup_dir.resolve()
            if path.is_symlink() or not path.is_file() or not is_within(path.resolve(), root):
                raise FileNotFoundError(name)
            if path.stat().st_size > MAX_EXPORT_BODY_BYTES:
                self._error(413, "body_too_large", "백업 파일이 64MB 상한을 넘었습니다.")
                return
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, NotADirectoryError):
            self._error(404, "backup_not_found", "백업 파일을 찾지 못했습니다.")
            return
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self._error(422, "bad_backup", f"백업 파일을 읽지 못했습니다: {exc}")
            return
        if not isinstance(doc, dict) or doc.get("_type") != "slide_tool_backup":
            self._error(422, "bad_backup", "슬라이드 도구 백업 파일이 아닙니다.")
            return
        self._send_json({"ok": True, "name": name, "backup": doc})

    @staticmethod
    def _count_group_images(folder: Path) -> int:
        try:
            return sum(
                1
                for path in (folder / "img").iterdir()
                if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
            )
        except OSError:
            return 0

    @staticmethod
    def _unique_child(parent: Path, name: str) -> Path:
        for index in range(1, 1000):
            candidate = parent / (name if index == 1 else f"{name}_{index}")
            if not os.path.lexists(candidate):
                return candidate
        raise OSError("같은 이름이 너무 많아 보관 이름을 정할 수 없습니다.")

    def api_new_event(self) -> None:
        """이전 작업(그룹 폴더·계획·목록, 선택 시 원본 사진)을 보관 폴더로 옮기고 작업장을 비운다.

        요청 {"backup": {...}|없음, "moveOriginals": false}. 삭제는 하지 않고 이동만 한다.
        03_결과물(PDF)과 slide_tool 의 도구 파일은 건드리지 않는다.
        응답 {"ok", "archive": "_이전작업_YYMMDD_HHMM", "moved": {...}, "backup": 백업 상대경로|null}.
        """
        payload = self._read_json_body(MAX_NEW_EVENT_BODY_BYTES)
        if payload is None:
            return
        move_originals = payload.get("moveOriginals", False)
        backup = payload.get("backup")
        if not isinstance(move_originals, bool):
            self._error(400, "bad_request", "moveOriginals는 true 또는 false여야 합니다.")
            return
        if backup is not None and not isinstance(backup, dict):
            self._error(400, "bad_backup", "backup은 JSON 객체여야 합니다.")
            return

        assert self.workflow.work is not None
        assert self.workflow.src is not None
        assert self.workflow.out is not None
        work = self.workflow.work
        src = self.workflow.src
        plan_path = work / "worktree.json"
        data_path = work / "slide_tool" / "data.js"

        with self.workflow.jobs.lock:
            current = self.workflow.jobs.current
            if current is not None and current.state == "running":
                self._error(409, "busy", "다른 작업이 실행 중입니다.")
                return

            # ---- 옮길 대상 수집 (이 단계에서는 아무것도 바꾸지 않는다) ----
            try:
                group_dirs = sorted(
                    (
                        child
                        for child in work.iterdir()
                        if is_group_folder_name(child.name)
                        and child.is_dir()
                        and not child.is_symlink()
                        and (child / "img").is_dir()
                    ),
                    key=lambda path: path.name,
                )
            except OSError as exc:
                self._error(500, "group_read_failed", f"그룹 목록을 읽지 못했습니다: {exc}")
                return
            photo_count = sum(self._count_group_images(folder) for folder in group_dirs)
            original_entries: list[Path] = []
            original_count = 0
            if move_originals:
                try:
                    for entry in sorted(src.iterdir(), key=lambda path: path.name):
                        if entry.name.startswith(".") or entry.is_symlink():
                            continue
                        if entry.is_file() and entry.suffix.lower() in UPLOAD_EXTS:
                            original_entries.append(entry)
                            original_count += 1
                        elif entry.is_dir():
                            inner = sum(
                                1
                                for path in entry.rglob("*")
                                if path.is_file()
                                and not path.name.startswith(".")
                                and path.suffix.lower() in UPLOAD_EXTS
                            )
                            if inner:
                                original_entries.append(entry)
                                original_count += inner
                except OSError as exc:
                    self._error(500, "src_read_failed", f"원본 폴더를 읽지 못했습니다: {exc}")
                    return
            has_plan = plan_path.is_file()
            has_data = data_path.is_file()
            if not (group_dirs or has_plan or has_data or original_entries):
                self._error(409, "nothing_to_archive", "보관할 이전 작업이 없습니다.")
                return

            # ---- 백업 저장 (실패하면 아무것도 옮기지 않는다) ----
            backup_path: Optional[Path] = None
            if backup is not None:
                try:
                    backup_path = self._save_backup(backup)
                except ValueError as exc:
                    self._error(413, "body_too_large", str(exc))
                    return
                except OSError as exc:
                    self._error(500, "backup_save_failed", f"백업을 저장하지 못했습니다: {exc}")
                    return

            # ---- 보관 폴더를 만들고 옮긴다 (실패하면 옮긴 것을 되돌린다) ----
            stamp = dt.datetime.now().strftime("%y%m%d_%H%M")
            archive: Optional[Path] = None
            moved: list[tuple[Path, Path]] = []   # (옮긴 뒤 경로, 원래 경로)
            try:
                for index in range(1, 1000):
                    candidate = work / (
                        f"{ARCHIVE_PREFIX}{stamp}" if index == 1 else f"{ARCHIVE_PREFIX}{stamp}_{index}"
                    )
                    try:
                        candidate.mkdir()
                    except FileExistsError:
                        continue
                    archive = candidate
                    break
                if archive is None:
                    raise OSError("보관 폴더 이름을 정할 수 없습니다.")
                if backup_path is not None:
                    shutil.copyfile(backup_path, archive / "백업.json")
                if original_entries:
                    originals_dir = archive / "01_원본사진"
                    originals_dir.mkdir()
                    for entry in original_entries:
                        target = self._unique_child(originals_dir, entry.name)
                        entry.rename(target)
                        moved.append((target, entry))
                for source, keep_name in ((plan_path, "worktree.json"), (data_path, "data.js")):
                    if source.is_file():
                        target = self._unique_child(archive, keep_name)
                        source.rename(target)
                        moved.append((target, source))
                for folder in group_dirs:
                    target = self._unique_child(archive, folder.name)
                    folder.rename(target)
                    moved.append((target, folder))
            except OSError as exc:
                rollback_errors: list[str] = []
                for target, origin in reversed(moved):
                    try:
                        if os.path.lexists(target) and not os.path.lexists(origin):
                            target.rename(origin)
                    except OSError as rollback_exc:
                        rollback_errors.append(f"{origin.name}: {rollback_exc}")
                if archive is not None:
                    try:
                        for leftover in (archive / "01_원본사진", archive):
                            if leftover.is_dir() and not any(leftover.iterdir()):
                                leftover.rmdir()
                    except OSError:
                        pass
                if rollback_errors:
                    self._error(
                        500,
                        "new_event_rollback_failed",
                        f"새 행사 시작과 복구에 실패했습니다: {exc}; " + "; ".join(rollback_errors),
                    )
                else:
                    self._error(500, "new_event_failed", f"이전 작업을 옮기지 못했습니다: {exc}")
                return

        self._send_json(
            {
                "ok": True,
                "archive": archive.name,
                "moved": {
                    "groups": len(group_dirs),
                    "photos": photo_count,
                    "worktree": has_plan,
                    "dataJs": has_data,
                    "originals": original_count,
                },
                "backup": (
                    f"{backup_path.parent.name}/{backup_path.name}" if backup_path is not None else None
                ),
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
            # 화면에서 끌어 놓은 사진은 서버가 받은 시각이 수정 시각이 된다. EXIF 없는 사진은
            # 수정 시각으로 촬영순·그룹을 나누므로, 브라우저가 알려 준 원래 수정 시각을 되돌린다.
            mtime = parse_last_modified(self.headers.get("X-Last-Modified"))
            mtime_applied = False
            if mtime is not None:
                try:
                    os.utime(final_path, (mtime, mtime))
                    mtime_applied = True
                except OSError:
                    mtime_applied = False
            self._send_json(
                {
                    "ok": True,
                    "saved": final_path.name,
                    "renamed": renamed,
                    "dedup": False,
                    "mtimeApplied": mtime_applied,
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
        if target_name is not None and not is_group_folder_name(target_name):
            target_name, target_reason = None, "밑줄(_)로 시작하는 이름은 보관용이라 그룹 이름으로 쓸 수 없습니다."
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

    def _export_group_names(self) -> dict[str, str]:
        """작업장 직계 폴더만 실제 내보내기 그룹으로 인정한다."""
        assert self.workflow.work is not None
        work_root = self.workflow.work.resolve()
        groups: dict[str, str] = {}
        try:
            children = list(self.workflow.work.iterdir())
        except OSError:
            return groups
        for child in children:
            if (
                not is_group_folder_name(child.name)
                or not child.is_dir()
                or not (child / "img").is_dir()
            ):
                continue
            safe_name, _detail = safe_group_name(child.name)
            if safe_name is None:
                continue
            try:
                resolved = child.resolve()
            except (OSError, RuntimeError):
                continue
            if resolved.parent != work_root:
                continue
            normalized = nfc(safe_name)
            if normalized in groups and groups[normalized] != child.name:
                groups.pop(normalized, None)
                continue
            groups[normalized] = child.name
        return groups

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
        mode = payload.get("mode")
        order = payload.get("order", [])
        if not isinstance(backup, dict):
            self._error(400, "bad_backup", "backup은 JSON 객체여야 합니다.")
            return
        if not isinstance(only_done, bool) or not isinstance(merge, bool):
            self._error(400, "bad_request", "onlyDone과 merge는 true 또는 false여야 합니다.")
            return
        if mode is None:
            mode = "merged" if merge else "per-folder"
        if not isinstance(mode, str) or mode not in {
            "per-folder",
            "merged",
            "ordered",
        }:
            self._error(
                400,
                "bad_request",
                "mode는 per-folder, merged, ordered 중 하나여야 합니다.",
            )
            return
        if not isinstance(order, list):
            self._error(400, "bad_order", "order는 그룹 이름 배열이여야 합니다.")
            return
        photo_order: Optional[dict[str, list[str]]] = None
        if payload.get("photoOrder") is not None:
            photo_order, detail = sanitize_photo_order(payload.get("photoOrder"))
            if photo_order is None:
                self._error(400, "bad_photo_order", detail)
                return

        existing_groups = self._export_group_names()
        normalized_order: list[str] = []
        seen: set[str] = set()
        for raw_name in order:
            safe_name, detail = safe_group_name(raw_name)
            if safe_name is None:
                self._error(400, "bad_order", f"order 그룹 이름 거부: {detail}")
                return
            normalized = nfc(safe_name)
            disk_name = existing_groups.get(normalized)
            if disk_name is None:
                self._error(400, "bad_order", f"존재하지 않는 그룹입니다: {safe_name}")
                return
            if normalized in seen:
                self._error(400, "bad_order", f"order에 중복된 그룹이 있습니다: {safe_name}")
                return
            seen.add(normalized)
            normalized_order.append(disk_name)
        if mode == "ordered" and not normalized_order:
            self._error(400, "bad_order", "ordered 모드는 order에 그룹을 하나 이상 보내야 합니다.")
            return
        if mode == "ordered" and seen != set(existing_groups):
            self._error(
                400,
                "bad_order",
                "ordered 모드의 order는 현재 그룹 전체를 한 번씩 포함해야 합니다.",
            )
            return
        if mode in {"merged", "ordered"} and shutil.which("pdfunite") is None:
            self._error(
                503,
                "pdfunite_missing",
                "통합 PDF에 필요한 pdfunite(poppler)를 찾을 수 없습니다.",
            )
            return
        python, env = self._job_prerequisites()
        if python is None:
            return
        if photo_order is not None:
            # 화면 순서를 백업 사본에 실어 export_pdf 가 읽게 한다(백업 스키마 6키는 그대로).
            backup = {**backup, "_photoOrder": photo_order}
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
            "--workers",
            str(env["workers"]),
        ]
        if only_done:
            command.append("--only-done")
        if mode in {"merged", "ordered"}:
            # export_pdf.py는 그대로 두고 신선한 임시 폴더에서 먼저 완성본을
            # 검증한다. ordered는 생성된 그룹만 요청 순서로 병합하고,
            # merged는 조용한 pdfunite 실패를 통합본 존재 검사로 잡아낸다.
            staged_command = [
                str(python),
                "-c",
                STAGED_EXPORT_CODE,
                json.dumps(command, ensure_ascii=False),
                mode,
                json.dumps(normalized_order, ensure_ascii=False),
                str(self.workflow.out),
            ]
            phase_name = "지정 순서로 PDF 만들기" if mode == "ordered" else "통합 PDF 만들기"
            steps = [(phase_name, staged_command)]
        else:
            command.extend(("--out", str(self.workflow.out)))
            steps = [("PDF 만들기", command)]
        job = self.workflow.jobs.start("export", steps)
        if job is None:
            self._error(409, "busy", "다른 작업이 실행 중입니다.")
            return
        self._send_json({"ok": True, "job": {"id": job.id, "kind": job.kind}}, 202)

    def _resolve_work_image(self, key: object) -> tuple[Optional[Path], str, str]:
        """도구의 이미지 키 `../<그룹>/img/<파일>` 을 작업장 안의 실제 사진으로 바꾼다."""
        if not isinstance(key, str) or not key or "\x00" in key:
            return None, "bad_key", "이미지 키는 비어 있지 않은 문자열이어야 합니다."
        parts = key.split("/")
        if len(parts) != 4 or parts[0] != ".." or parts[2] != "img":
            return None, "bad_key", "이미지 키는 ../<그룹>/img/<파일> 형식이어야 합니다."
        group, reason = safe_group_name(parts[1])
        if group is None:
            return None, "bad_key", f"이미지 키의 그룹 이름 거부: {reason}"
        name = nfc(parts[3])
        if (
            not name
            or name in {".", ".."}
            or name.startswith(".")
            or "\\" in name
            or re.match(r"^[A-Za-z]:", name)
            or any(unicodedata.category(char) == "Cc" for char in name)
        ):
            return None, "bad_key", "이미지 키의 파일 이름이 올바르지 않습니다."
        if Path(name).suffix.lower() not in AUTO_DETECT_EXTS:
            return None, "bad_key", "jpg·png 사진만 찾을 수 있습니다."
        disk_group = self._export_group_names().get(group)
        if disk_group is None:
            return None, "image_not_found", f"작업장에 없는 그룹입니다: {group}"
        assert self.workflow.work is not None
        image_dir = self.workflow.work / disk_group / "img"
        try:
            image_root = image_dir.resolve()
            candidate = image_dir / parts[3]
            if not candidate.exists():
                # macOS 파일시스템은 한글을 NFD 로 저장한다 — NFC 로 맞춰 다시 찾는다.
                matches = [path for path in image_dir.iterdir() if nfc(path.name) == name]
                if len(matches) != 1:
                    return None, "image_not_found", f"사진을 찾지 못했습니다: {name}"
                candidate = matches[0]
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            return None, "image_not_found", f"사진을 찾지 못했습니다: {name}"
        if resolved.parent != image_root or not is_within(resolved, image_root):
            return None, "bad_key", "작업장 img 폴더 밖 파일은 사용할 수 없습니다."
        if not resolved.is_file():
            return None, "image_not_found", f"사진을 찾지 못했습니다: {name}"
        return resolved, "", ""

    def api_auto_detect(self) -> None:
        """사진 여러 장의 슬라이드 경계(4꼭짓점)를 자동으로 찾는다 — 동기 응답.

        요청 {"keys": ["../그룹/img/파일.jpg", ...], "rotations": {키: 0~3}}  (rotations 선택)
        응답 {"ok": true, "results": {키: {"corners": [[x,y]x4]|null, "conf": 0~1, "review": bool}}}
        corners 는 원본 사진 정규좌표 [TL,TR,BL,BR](화면에서 본 방향 기준). 그룹 전체는 클라이언트가
        여러 번에 나눠 부른다 — 요청마다 끝나므로 진행률·취소를 잡 체계 없이 처리할 수 있다.
        """
        payload = self._read_json_body(MAX_AUTO_DETECT_BODY_BYTES)
        if payload is None:
            return
        keys = payload.get("keys")
        rotations = payload.get("rotations", {})
        if not isinstance(keys, list) or not keys or len(keys) > MAX_AUTO_DETECT_KEYS:
            self._error(
                400,
                "bad_request",
                f"keys는 1~{MAX_AUTO_DETECT_KEYS}개의 이미지 키 배열이어야 합니다.",
            )
            return
        if not isinstance(rotations, dict):
            self._error(400, "bad_request", "rotations는 {이미지 키: 0~3} 객체여야 합니다.")
            return
        ordered: list[str] = []
        paths: dict[str, Path] = {}
        for key in keys:
            path, code, detail = self._resolve_work_image(key)
            if path is None:
                self._error(404 if code == "image_not_found" else 400, code, detail)
                return
            assert isinstance(key, str)
            if key not in paths:
                ordered.append(key)
                paths[key] = path
        if any(key not in paths for key in rotations):
            self._error(400, "bad_request", "rotations에 keys에 없는 키가 있습니다.")
            return
        items: list[dict[str, object]] = []
        for key in ordered:
            rot = rotations.get(key, 0)
            if isinstance(rot, bool) or not isinstance(rot, int) or not 0 <= rot <= 3:
                self._error(400, "bad_request", "rotations 값은 0~3 정수여야 합니다.")
                return
            items.append({"path": str(paths[key]), "rot": rot})
        if self.workflow.jobs.busy:
            self._error(409, "busy", "다른 작업이 실행 중입니다.")
            return
        python, _env = self._job_prerequisites()
        if python is None:
            return
        assert self.workflow.scripts is not None
        runner = self.workflow.scripts / "auto_detect_api.py"
        if not runner.is_file():
            self._error(500, "detector_missing", "auto_detect_api.py를 찾지 못했습니다.")
            return
        if not self.workflow.detect_lock.acquire(blocking=False):
            self._error(409, "busy", "다른 자동 찾기가 실행 중입니다.")
            return
        try:
            child_env = dict(os.environ)
            child_env["PYTHONIOENCODING"] = "utf-8:replace"
            child_env["PYTHONUTF8"] = "1"
            try:
                completed = subprocess.run(
                    [str(python), str(runner)],
                    input=json.dumps({"items": items}, ensure_ascii=False),
                    cwd=str(runner.parent),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=child_env,
                    timeout=AUTO_DETECT_TIMEOUT,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                self._error(504, "detect_timeout", "자동 찾기가 시간 안에 끝나지 않았습니다.")
                return
            except OSError as exc:
                self._error(500, "detect_failed", f"자동 찾기를 실행하지 못했습니다: {exc}")
                return
        finally:
            self.workflow.detect_lock.release()
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        try:
            if completed.returncode != 0 or not lines:
                raise ValueError("비정상 종료")
            parsed = json.loads(lines[-1])
            rows = parsed["results"]
            if not isinstance(rows, list) or len(rows) != len(ordered):
                raise ValueError("결과 개수 불일치")
        except (ValueError, KeyError, TypeError):
            tail = (completed.stderr or completed.stdout).strip()[-300:]
            self._error(500, "detect_failed", f"자동 찾기가 실패했습니다: {tail or completed.returncode}")
            return
        results: dict[str, object] = {}
        for key, row in zip(ordered, rows):
            corners = row.get("corners") if isinstance(row, dict) else None
            try:
                conf = float(row.get("conf", 0.0)) if isinstance(row, dict) else 0.0
            except (TypeError, ValueError):
                conf = 0.0
            if not (
                isinstance(corners, list)
                and len(corners) == 4
                and all(
                    isinstance(pair, list)
                    and len(pair) == 2
                    and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in pair)
                    for pair in corners
                )
            ):
                corners = None
            if corners is None:
                conf = 0.0
            results[key] = {
                "corners": corners,
                "conf": conf,
                "review": corners is None or conf < AUTO_DETECT_REVIEW_BELOW,
            }
        self._send_json({"ok": True, "results": results})

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
        lifecycle.goodbye_enabled = False   # 탭 닫힘(/goodbye)으로도 끄지 않는다
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
