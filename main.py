#!/usr/bin/env python3
"""
Kitchen Barcode Scanner Service (single-file)

Goal
----
Read barcode scans from a "keyboard-wedge" barcode scanner connected to a Raspberry Pi,
parse scans into commands/items/product codes, keep a small state machine (STOCK/USE),
auto-reset to default mode (USE) after inactivity, and call local HTTP endpoints.

Endpoints (as requested)
------------------------
When in STOCK mode and scanning an item code:
  POST http://localhost:8000/kitchen/kitchen/items/<id>/stock

When in USE mode and scanning an item code:
  POST http://localhost:8000/kitchen/kitchen/items/<id>/use

Mode switching scans
--------------------
  kitchen:scanner::stock   -> switch mode to STOCK
  kitchen:scanner::use     -> switch mode to USE

Item identification scans
-------------------------
  kitchen:item::<identifier>

Generic product barcodes (EAN/UPC/etc.)
---------------------------------------
This file includes a stub handler for generic product codes. You can wire it up later to
whatever endpoint you decide. It logs them for now.

Reliability strategy
--------------------
- Read scanner via Linux input events (evdev) so it works headless and doesn't depend on focus.
- Use asyncio for a robust main loop + background workers + idle timer.
- Put HTTP calls onto a queue; a worker performs HTTP with retries/backoff.
- Catch exceptions at boundaries; never let a single bad scan crash the process.
- Run under systemd for restart-on-crash and boot start (recommended).

Dependencies
------------
pip install evdev httpx tenacity python-dotenv

Run
---
sudo -E python3 kitchen_scanner_service.py

Note: reading /dev/input/event* typically requires:
- run as root, OR
- add service user to 'input' group and ensure permissions via udev.

"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

import httpx
from dotenv import load_dotenv
from evdev import InputDevice, ecodes, list_devices
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

# ----------------------------
# Configuration (edit as needed)
# ----------------------------

load_dotenv()

# Default mode after inactivity:
DEFAULT_MODE = "use"  # per your request

# Auto-reset mode after this many seconds without a scan:
IDLE_TIMEOUT_SECONDS = int(os.getenv("KITCHEN_SCANNER_IDLE_TIMEOUT", "30"))

# If you know your scanner device name substring, set it here for stable selection.
# Example: "Barcode" or "Honeywell" or "Zebra". Leave empty to auto-pick the first keyboard-like device.
SCANNER_NAME_HINT = os.getenv("KITCHEN_SCANNER_NAME_HINT", "").strip()

# Base URL of your local API:
API_BASE = os.getenv("KITCHEN_SCANNER_API_BASE", "http://localhost:8000").rstrip("/")

# API key for Authorization header (Bearer token)
API_KEY = os.getenv("KITCHEN_SCANNER_API_KEY", "").strip()

# HTTP timeouts (seconds): connect, read
HTTP_TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=5.0)

# Max attempts for HTTP retries
HTTP_RETRY_ATTEMPTS = int(os.getenv("KITCHEN_SCANNER_HTTP_RETRY_ATTEMPTS", "5"))

# ----------------------------
# Logging setup
# ----------------------------

logging.basicConfig(
    level=os.getenv("KITCHEN_SCANNER_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("kitchen_scanner")

# ----------------------------
# State machine
# ----------------------------


class Mode(str, Enum):
    """
    Two operating modes:
      - STOCK: scanning means "add to stock"
      - USE:   scanning means "consume/use"
    """
    STOCK = "stock"
    USE = "use"


@dataclass
class State:
    """
    Holds the current scanner mode and last activity timestamp.
    """
    mode: Mode = Mode.USE  # default state: use
    last_activity_monotonic: float = time.monotonic()

    def touch(self) -> None:
        """Record activity time (called for every scan)."""
        self.last_activity_monotonic = time.monotonic()

    def maybe_reset_on_idle(self, idle_timeout_seconds: int) -> bool:
        """
        Reset state to DEFAULT_MODE if idle timeout has passed.
        Returns True if reset happened, False otherwise.
        """
        now = time.monotonic()
        if now - self.last_activity_monotonic >= idle_timeout_seconds:
            default_mode = Mode(DEFAULT_MODE)
            if self.mode != default_mode:
                logger.info("Idle timeout reached (%ss). Resetting mode %s -> %s",
                            idle_timeout_seconds, self.mode.value, default_mode.value)
                self.mode = default_mode
                return True
        return False


# ----------------------------
# Parsing and routing
# ----------------------------

MODE_STOCK_CMD = "kitchen:scanner::stock"
MODE_USE_CMD = "kitchen:scanner::use"
ITEM_PREFIX = "kitchen:item::"

EAN_LIKE_RE = re.compile(r"^\d{8,14}$")  # loose: EAN-8..EAN-14/GTIN-ish


class ScanKind(Enum):
    MODE_SWITCH = "mode_switch"
    ITEM = "item"
    PRODUCT = "product"
    UNKNOWN = "unknown"


@dataclass
class ScanEvent:
    raw: str
    kind: ScanKind
    mode_target: Optional[Mode] = None
    item_id: Optional[str] = None
    product_code: Optional[str] = None


def parse_scan(scan: str) -> ScanEvent:
    """
    Convert the scanned string to a structured event.

    Priority:
      1) mode commands
      2) item codes
      3) EAN/UPC-ish numeric codes
      4) unknown
    """
    s = scan.strip()

    if s == MODE_STOCK_CMD:
        return ScanEvent(raw=s, kind=ScanKind.MODE_SWITCH, mode_target=Mode.STOCK)
    if s == MODE_USE_CMD:
        return ScanEvent(raw=s, kind=ScanKind.MODE_SWITCH, mode_target=Mode.USE)

    if s.startswith(ITEM_PREFIX):
        item_id = s[len(ITEM_PREFIX):].strip()
        if item_id:
            return ScanEvent(raw=s, kind=ScanKind.ITEM, item_id=item_id)

    if EAN_LIKE_RE.match(s):
        return ScanEvent(raw=s, kind=ScanKind.PRODUCT, product_code=s)

    return ScanEvent(raw=s, kind=ScanKind.UNKNOWN)


# ----------------------------
# HTTP client + retry policy
# ----------------------------

# Tenacity retry: only on network-type exceptions or transient server errors.
# We implement transient server errors by raising for 5xx ourselves.
def _is_transient_http_status(status_code: int) -> bool:
    return 500 <= status_code <= 599


def _retry_decorator():
    return retry(
        retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(HTTP_RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )


@_retry_decorator()
async def post_item_action(
    client: httpx.AsyncClient,
    item_id: str,
    action: str,
) -> httpx.Response:
    """
    POST item action. Retries on network errors and on 5xx responses.
    Non-2xx responses are returned for logging without retry unless 5xx.

    action is "stock" or "use"
    """
    url = f"{API_BASE}/kitchen/kitchen/items/{item_id}/{action}"
    resp = await client.post(url, timeout=HTTP_TIMEOUT)
    # Treat 5xx as transient for retry:
    if _is_transient_http_status(resp.status_code):
        resp.raise_for_status()
    return resp


# ----------------------------
# Scanner reading (evdev)
# ----------------------------

# Basic keycode -> character mapping for common scanner outputs.
# Most barcode scanners emit digits and letters as normal key events.
# We'll handle:
# - digits 0-9
# - letters a-z
# - a few punctuation characters typically used in your codes: ':', '-', '_'
#
# If your scanner layout differs, you may need to expand this mapping.
KEYCODE_TO_CHAR = {
    # digits
    ecodes.KEY_0: "0", ecodes.KEY_1: "1", ecodes.KEY_2: "2", ecodes.KEY_3: "3", ecodes.KEY_4: "4",
    ecodes.KEY_5: "5", ecodes.KEY_6: "6", ecodes.KEY_7: "7", ecodes.KEY_8: "8", ecodes.KEY_9: "9",
    # letters
    ecodes.KEY_A: "a", ecodes.KEY_B: "b", ecodes.KEY_C: "c", ecodes.KEY_D: "d", ecodes.KEY_E: "e",
    ecodes.KEY_F: "f", ecodes.KEY_G: "g", ecodes.KEY_H: "h", ecodes.KEY_I: "i", ecodes.KEY_J: "j",
    ecodes.KEY_K: "k", ecodes.KEY_L: "l", ecodes.KEY_M: "m", ecodes.KEY_N: "n", ecodes.KEY_O: "o",
    ecodes.KEY_P: "p", ecodes.KEY_Q: "q", ecodes.KEY_R: "r", ecodes.KEY_S: "s", ecodes.KEY_T: "t",
    ecodes.KEY_U: "u", ecodes.KEY_V: "v", ecodes.KEY_W: "w", ecodes.KEY_X: "x", ecodes.KEY_Y: "y",
    ecodes.KEY_Z: "z",
    # punctuation (unshifted)
    ecodes.KEY_MINUS: "-",
    ecodes.KEY_SEMICOLON: ";",
    ecodes.KEY_APOSTROPHE: "'",
    ecodes.KEY_COMMA: ",",
    ecodes.KEY_DOT: ".",
    ecodes.KEY_SLASH: "/",
    ecodes.KEY_SPACE: " ",
}

# Shifted punctuation map for common US layout. For your codes we mainly need ':' which is shift+semicolon.
SHIFTED_KEYCODE_TO_CHAR = {
    ecodes.KEY_SEMICOLON: ":",  # shift+;
    ecodes.KEY_MINUS: "_",      # shift+-
}


def find_scanner_device(name_hint: str = "") -> InputDevice:
    """
    Find an evdev input device that is likely the scanner.

    Strategy:
    - if name_hint is provided, pick the first device whose name contains it (case-insensitive)
    - else, pick the first device that advertises keyboard-like keys and has KEY_ENTER.

    You can hardcode the event path via env var if you want:
      KITCHEN_SCANNER_DEVICE=/dev/input/eventX
    """
    forced = os.getenv("KITCHEN_SCANNER_DEVICE", "").strip()
    if forced:
        dev = InputDevice(forced)
        logger.info("Using forced scanner device: %s (%s)", forced, dev.name)
        return dev

    devices = [InputDevice(path) for path in list_devices()]
    if not devices:
        raise RuntimeError("No input devices found under /dev/input. Is evdev available?")

    if name_hint:
        nh = name_hint.lower()
        for dev in devices:
            if nh in dev.name.lower():
                logger.info("Selected scanner device by name hint '%s': %s (%s)", name_hint, dev.path, dev.name)
                return dev

    # Heuristic: keyboard-like device with ENTER
    for dev in devices:
        caps = dev.capabilities().get(ecodes.EV_KEY, [])
        if ecodes.KEY_ENTER in caps and ecodes.KEY_1 in caps and ecodes.KEY_0 in caps:
            logger.info("Selected scanner device by heuristic: %s (%s)", dev.path, dev.name)
            return dev

    # Fallback: first device
    dev = devices[0]
    logger.warning("Falling back to first input device: %s (%s). Consider setting KITCHEN_SCANNER_NAME_HINT or KITCHEN_SCANNER_DEVICE.",
                   dev.path, dev.name)
    return dev


async def scans_from_device(dev: InputDevice):
    """
    Async generator yielding complete scanned strings.

    The scanner typically sends key events followed by KEY_ENTER.
    We buffer characters until ENTER, then yield the string.

    This implementation:
    - handles shift for ':' and '_' (common in your command format)
    - preserves case for letters based on shift state
    """
    logger.info("Listening for scans on %s (%s)", dev.path, dev.name)
    buffer = []
    shift = False

    async for event in dev.async_read_loop():
        if event.type != ecodes.EV_KEY:
            continue
        key_event = event
        keycode = key_event.code
        keystate = key_event.value  # 1=down, 0=up, 2=hold

        # Track shift state (left/right)
        if keycode in (ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT):
            shift = (keystate != 0)
            continue

        # Only act on key-down
        if keystate != 1:
            continue

        if keycode in (ecodes.KEY_ENTER, ecodes.KEY_KPENTER, ecodes.KEY_TAB):
            scan = "".join(buffer).strip()
            buffer.clear()
            if scan:
                logger.debug("Scan read: %s", scan)
                yield scan
            continue

        # Map keycode to character
        if shift and keycode in SHIFTED_KEYCODE_TO_CHAR:
            buffer.append(SHIFTED_KEYCODE_TO_CHAR[keycode])
        elif keycode in KEYCODE_TO_CHAR:
            char = KEYCODE_TO_CHAR[keycode]
            if shift and char.isalpha():
                char = char.upper()
            buffer.append(char)
        else:
            # Unknown key: ignore (or log at debug)
            logger.debug("Ignoring unmapped keycode: %s", keycode)


# ----------------------------
# Application logic
# ----------------------------

@dataclass
class ApiJob:
    """Represents a unit of work for the HTTP worker."""
    item_id: str
    action: str  # "stock" or "use"


async def http_worker(queue: asyncio.Queue[ApiJob], stop_event: asyncio.Event) -> None:
    """
    Worker consuming ApiJob tasks and sending HTTP requests.
    Retries are handled by tenacity in post_item_action().
    """
    headers = {}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    async with httpx.AsyncClient(headers=headers) as client:
        while not stop_event.is_set():
            try:
                job = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            try:
                resp = await post_item_action(client, job.item_id, job.action)
                if 200 <= resp.status_code < 300:
                    logger.info("OK: item %s -> %s", job.item_id, job.action)
                else:
                    logger.error(
                        "FAILED: item %s -> %s (status=%s response=%s)",
                        job.item_id,
                        job.action,
                        resp.status_code,
                        resp.text,
                    )
            except Exception as e:
                # At this point retries are exhausted or error is non-retriable (e.g. 4xx).
                # We log and drop the job. If you want "never lose", persist to SQLite instead.
                logger.error("FAILED: item %s -> %s (%s)", job.item_id, job.action, e)
            finally:
                queue.task_done()


async def idle_reset_task(state: State, stop_event: asyncio.Event) -> None:
    """
    Background task: periodically checks idle timeout and resets mode if needed.
    """
    while not stop_event.is_set():
        state.maybe_reset_on_idle(IDLE_TIMEOUT_SECONDS)
        await asyncio.sleep(1)


async def _scan_loop(
    dev: InputDevice,
    state: State,
    queue: asyncio.Queue[ApiJob],
    stop_event: asyncio.Event,
) -> None:
    async for scan in scans_from_device(dev):
        if stop_event.is_set():
            break

        state.touch()
        event = parse_scan(scan)

        # ----- State machine behavior -----
        #
        # State = current mode (STOCK/USE)
        # Inputs:
        #   - MODE_SWITCH events set state.mode
        #   - ITEM events enqueue an API job depending on current state.mode
        #   - PRODUCT events currently just log (stub)
        #   - UNKNOWN events log and ignore
        #
        # Default state: USE
        # Idle timeout: resets state.mode back to USE after IDLE_TIMEOUT_SECONDS
        #
        if event.kind == ScanKind.MODE_SWITCH and event.mode_target:
            old = state.mode
            state.mode = event.mode_target
            logger.info("Mode switch: %s -> %s", old.value, state.mode.value)
            continue

        if event.kind == ScanKind.ITEM and event.item_id:
            action = "stock" if state.mode == Mode.STOCK else "use"
            # Enqueue without blocking forever; if queue is full, drop and log.
            job = ApiJob(item_id=event.item_id, action=action)
            try:
                queue.put_nowait(job)
                logger.info("Enqueued: item %s in mode %s -> %s",
                            event.item_id, state.mode.value, action)
            except asyncio.QueueFull:
                logger.error("Queue full; dropping scan for item %s", event.item_id)
            continue

        if event.kind == ScanKind.PRODUCT and event.product_code:
            # Stub: wire up later as you decide. We log it so you can see it works.
            logger.info("Product barcode scanned (mode=%s): %s (no endpoint wired yet)",
                        state.mode.value, event.product_code)
            continue

        logger.warning("Unknown scan ignored: %r", event.raw)


async def run() -> None:
    """
    Main entry point.

    Pipeline:
      - scanner yields scan strings
      - we parse scan to ScanEvent
      - state machine updates mode or enqueues API jobs
      - a worker sends jobs (HTTP) with retries/backoff
      - an idle task resets mode to default after inactivity
    """
    state = State(mode=Mode(DEFAULT_MODE))
    stop_event = asyncio.Event()
    queue: asyncio.Queue[ApiJob] = asyncio.Queue(maxsize=1000)
    dev: Optional[InputDevice] = None
    scan_task: Optional[asyncio.Task[None]] = None

    # Handle SIGTERM/SIGINT for clean shutdown
    loop = asyncio.get_running_loop()

    interrupt_count = 0

    def _request_stop():
        nonlocal interrupt_count
        interrupt_count += 1
        if interrupt_count == 1:
            logger.info("Stop requested, shutting down...")
            if logger.isEnabledFor(logging.DEBUG):
                pending_jobs = queue.qsize()
                logger.debug("Pending jobs in queue: %s", pending_jobs)
                for task in asyncio.all_tasks(loop):
                    if task is asyncio.current_task(loop=loop):
                        continue
                    if task.done():
                        continue
                    logger.debug("Pending task: %s", task.get_name())
            stop_event.set()
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    logger.debug("Failed to close scanner device on shutdown.", exc_info=True)
            return
        logger.warning("Second interrupt received; forcing exit.")
        os._exit(1)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Some environments may not support this; ignore.
            pass

    # Start background tasks
    worker_task = asyncio.create_task(http_worker(queue, stop_event))
    idle_task = asyncio.create_task(idle_reset_task(state, stop_event))

    # Open scanner device
    dev = find_scanner_device(SCANNER_NAME_HINT)

    try:
        scan_task = asyncio.create_task(
            _scan_loop(dev, state, queue, stop_event),
            name="scan_loop",
        )
        await scan_task

    finally:
        stop_event.set()
        if dev is not None:
            try:
                dev.close()
            except Exception:
                logger.debug("Failed to close scanner device on shutdown.", exc_info=True)
        # Give tasks a moment to exit
        await asyncio.sleep(0.1)
        idle_task.cancel()
        if queue.qsize() > 0:
            try:
                await asyncio.wait_for(queue.join(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Timed out waiting for queued API jobs to finish.")
        worker_task.cancel()
        # Drain cancellation
        for t in (worker_task, idle_task):
            if t is None:
                continue
            try:
                await t
            except asyncio.CancelledError:
                pass
        if scan_task is not None:
            try:
                await scan_task
            except asyncio.CancelledError:
                pass
        logger.info("Exited cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
