#!/usr/bin/env python3
"""브라우저 생명주기에 맞춰 자동 종료되는 로컬 정적 파일 서버."""

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import functools
import http.server
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Optional


BIND = "127.0.0.1"
DEFAULT_PORT = 8770
DEFAULT_TIMEOUT = 15.0
DEFAULT_GRACE = 40.0
GOODBYE_GRACE = 4.5


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
        """응답을 먼저 보낸 뒤 종료하되, 새 핑이 오면 새로고침으로 간주한다."""
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
            if not cancelled:
                self.request_shutdown(server, "창 닫힘 감지")

        threading.Thread(target=finish, daemon=True).start()


class ToolHandler(http.server.SimpleHTTPRequestHandler):
    """정적 파일과 서버 생명주기 엔드포인트를 함께 제공한다."""

    server: http.server.ThreadingHTTPServer

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

    def do_GET(self) -> None:
        if self._endpoint() == "/heartbeat":
            self.server.lifecycle.touch()  # type: ignore[attr-defined]
            self._no_content()
            return
        super().do_GET()

    def do_POST(self) -> None:
        endpoint = self._endpoint()
        if endpoint == "/heartbeat":
            self.server.lifecycle.touch()  # type: ignore[attr-defined]
            self._no_content()
            return
        if endpoint == "/goodbye":
            self._no_content()
            self.server.lifecycle.schedule_goodbye(self.server)  # type: ignore[attr-defined]
            return
        self.send_error(http.HTTPStatus.NOT_FOUND, "Not Found")

    def log_message(self, _format: str, *args: object) -> None:
        # 하트비트를 포함한 반복 요청으로 터미널을 도배하지 않는다.
        return


class Watchdog(threading.Thread):
    def __init__(
        self,
        server: http.server.ThreadingHTTPServer,
        lifecycle: Lifecycle,
        timeout: float,
        grace: float,
    ) -> None:
        super().__init__(daemon=True)
        self.server = server
        self.lifecycle = lifecycle
        self.timeout = timeout
        self.grace = grace

    def run(self) -> None:
        while True:
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

    handler = functools.partial(ToolHandler, directory=str(root))
    try:
        server = http.server.ThreadingHTTPServer((BIND, args.port), handler)
    except OSError as exc:
        print(f"오류: {BIND}:{args.port} 포트가 이미 사용 중이거나 열 수 없습니다. ({exc})")
        print("주의: 포트를 바꾸면 브라우저가 다른 사이트로 인식해 이전 작업이 보이지 않습니다.")
        return 1

    lifecycle = Lifecycle()
    server.lifecycle = lifecycle  # type: ignore[attr-defined]
    address = f"http://{BIND}:{args.port}/slide_tool/"
    print(f"슬라이드 도구 접속 주소: {address}", flush=True)
    if args.port != DEFAULT_PORT:
        print("주의: 포트가 바뀌면 이전 포트의 브라우저 작업이 보이지 않을 수 있습니다.", flush=True)
    if args.no_watchdog:
        print("자동 종료를 사용하지 않습니다. 끝나면 서버를 직접 종료하세요.", flush=True)
    else:
        Watchdog(server, lifecycle, args.timeout, args.grace).start()

    if not args.no_open:
        threading.Thread(target=webbrowser.open, args=(address,), daemon=True).start()

    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        with lifecycle.lock:
            lifecycle.stop_reason = "수동 종료"
    finally:
        server.server_close()

    reason = lifecycle.stop_reason or "수동 종료"
    if reason == "유예 초과":
        print("서버 종료: 유예 초과 — 브라우저의 첫 핑이 도착하지 않았습니다.", flush=True)
    else:
        print(f"서버 종료: {reason}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
