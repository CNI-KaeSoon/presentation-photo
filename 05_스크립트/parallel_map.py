#!/usr/bin/env python3
"""순서 보존 병렬 맵. 직렬 결과와 픽셀 단위로 동일해야 한다(C2)."""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any

_THREAD_ENV = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)


def _memory_gb() -> float | None:
    """표준 라이브러리만 사용해 확인한 물리 메모리(GB)."""
    try:
        if sys.platform == "win32":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_phys", ctypes.c_ulonglong),
                    ("avail_phys", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("avail_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("avail_virtual", ctypes.c_ulonglong),
                    ("avail_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.total_phys / (1024 ** 3)
        elif sys.platform == "darwin":
            raw = subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], text=True, stderr=subprocess.DEVNULL)
            return int(raw.strip()) / (1024 ** 3)
        else:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return float(pages * page_size) / (1024 ** 3)
    except (AttributeError, OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def default_workers() -> int:
    """논리코어 수. 단 메모리를 고려해 상한을 둔다.
    os.cpu_count() 가 None 이면 1.
    """
    workers = os.cpu_count() or 1
    memory_gb = _memory_gb()
    if memory_gb is not None and memory_gb < 8:
        workers = min(workers, 4)
    return max(1, workers)


def _init_worker():
    """OpenCV·BLAS 내부 스레드와 프로세스 풀이 겹치지 않게 한다."""
    for name in _THREAD_ENV:
        os.environ[name] = "1"
    import cv2

    cv2.setNumThreads(1)


def ordered_parallel_map(
    fn: Callable[[Any], Any],
    items: Iterable[Any],
    workers: int | None = None,
    initializer: Callable[[], None] | None = None,
) -> Iterator[Any]:
    """items 를 fn 으로 병렬 처리하되 입력 순서 그대로 결과를 돌려준다.

    workers<=1 이면 프로세스풀을 만들지 않고 순차 실행한다. fn 과 item 은
    Windows spawn 환경을 위해 picklable 해야 한다.
    """
    queue = list(items)
    resolved_workers = default_workers() if workers is None else workers
    if resolved_workers <= 1 or len(queue) <= 1:
        for item in queue:
            yield fn(item)
        return

    for name in _THREAD_ENV:
        os.environ[name] = "1"
    pool_initializer = initializer or _init_worker
    original_sysconf = None
    if sys.platform == "darwin":
        try:
            os.sysconf("SC_SEM_NSEMS_MAX")
        except PermissionError:
            original_sysconf = os.sysconf

            def sandbox_safe_sysconf(name):
                if name == "SC_SEM_NSEMS_MAX":
                    return -1
                return original_sysconf(name)

            # Python 3.10의 ProcessPoolExecutor 사전 조회만 EPERM인 샌드박스 대응.
            # 실제 풀/세마포어 생성 실패는 그대로 호출자에게 전파된다.
            os.sysconf = sandbox_safe_sysconf
    try:
        executor = ProcessPoolExecutor(
            max_workers=resolved_workers,
            initializer=pool_initializer,
        )
    finally:
        if original_sysconf is not None:
            os.sysconf = original_sysconf
    with executor:
        pending = {
            executor.submit(fn, item): idx
            for idx, item in enumerate(queue)
        }
        buffered = {}
        next_idx = 0
        for future in as_completed(pending):
            idx = pending.pop(future)
            buffered[idx] = future
            while next_idx in buffered:
                yield buffered.pop(next_idx).result()
                next_idx += 1
