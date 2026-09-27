"""Application logging - CMTrace-formatted log files plus console output.

The Logging Standard is Windows-born but the useful parts port cleanly, so this
module keeps them and adapts only the location:

* **Log file** (always): every run writes a *new* file named with a timestamp
  and PID, so logs are instance-based and never overwritten. Lines follow the
  CMTrace format so they open with severity colouring and a sortable component
  column in CMTrace / OneTrace, which is what the rest of the estate uses.

* **Console** (when attached): a plain ``HH:MM:SS LEVEL component: message``
  stream, because this tool is frequently driven from a terminal on a bench
  machine or over SSH.

For a destructive tool the log is evidence, not just diagnostics. Every command
issued against a drive is logged before it runs, with its full argument vector,
so a disputed erase can be reconstructed from the log alone. Nothing here
truncates or sanitises device paths or serial numbers - unlike the Windows
tools this pattern came from, there are no secrets in this log, and the serials
are precisely the thing an auditor needs.
"""

from __future__ import annotations

import getpass
import logging
import os
import platform
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import __app_name__, __publisher__, __version__
from .paths import invoking_user_name, log_dir

# Map Python logging levels onto CMTrace severities (1=Info, 2=Warning, 3=Error).
_CMTRACE_TYPE = {
    logging.DEBUG: 1,
    logging.INFO: 1,
    logging.WARNING: 2,
    logging.ERROR: 3,
    logging.CRITICAL: 3,
}

_ROOT_LOGGER = "zeroize"
_configured = False  # guard so repeat calls do not stack handlers
_current_log_path: Path | None = None

# Log retention defaults, applied at startup. The current run's file is always
# the newest, so it is never pruned by either rule.
_MAX_LOG_FILES = 60
_MAX_LOG_AGE_DAYS = 365


class CMTraceFormatter(logging.Formatter):
    """Format a log record as a single CMTrace line.

    The ``component`` column is the short logger tag - the ``zeroize.``
    prefix is stripped, and the bare package logger maps to ``App``. ``time``
    carries the local UTC offset in minutes (the CMTrace "bias") so the viewer
    can normalise timestamps across machines.
    """

    @staticmethod
    def _component(name: str) -> str:
        if name == _ROOT_LOGGER:
            return "App"
        prefix = _ROOT_LOGGER + "."
        return name[len(prefix) :] if name.startswith(prefix) else name

    @staticmethod
    def _escape(text: str) -> str:
        """Keep a message from breaking the CMTrace record delimiters."""
        return text.replace("]LOG]!>", "]LOG]! >")

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        now = datetime.fromtimestamp(record.created).astimezone()
        offset = now.utcoffset() or timedelta(0)
        bias = int(offset.total_seconds() // 60)
        timestamp = f"{now:%H:%M:%S}.{record.msecs:03.0f}{bias:+04d}"
        message = self._escape(record.getMessage())
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        severity = _CMTRACE_TYPE.get(record.levelno, 1)
        source = f"{Path(record.pathname).name}:{record.lineno}"
        return (
            f"<![LOG[{message}]LOG]!>"
            f'<time="{timestamp}" date="{now:%m-%d-%Y}" '
            f'component="{self._component(record.name)}" context="" '
            f'type="{severity}" thread="{record.thread}" file="{source}">'
        )


class ConsoleFormatter(logging.Formatter):
    """Human-readable console line: ``14:23:45 INFO  Nvme: formatting ...``."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        component = CMTraceFormatter._component(record.name)
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return f"{stamp} {record.levelname:<7} {component}: {message}"


def _prune_old_logs(directory: Path, keep_newest: Path) -> None:
    """Apply the count and age retention rules, never touching *keep_newest*."""
    try:
        existing = sorted(
            (path for path in directory.glob("*.log") if path != keep_newest),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return

    cutoff = datetime.now() - timedelta(days=_MAX_LOG_AGE_DAYS)
    for index, path in enumerate(existing):
        too_many = index >= max(0, _MAX_LOG_FILES - 1)
        try:
            too_old = datetime.fromtimestamp(path.stat().st_mtime) < cutoff
        except OSError:
            continue
        if too_many or too_old:
            try:
                path.unlink()
            except OSError:
                pass


def _write_session_header(logger: logging.Logger, log_path: Path) -> None:
    """Write a Start-Transcript-style banner at the top of every session."""
    try:
        login_name = getpass.getuser()
    except Exception:  # pragma: no cover - getuser can fail with no passwd entry
        login_name = "unknown"

    # os.getuid/geteuid are Unix-only. Zeroize only runs on Linux, but the
    # certificate tooling and the test suite are exercised on other platforms,
    # and a logging banner must never be the thing that stops the tool starting.
    if hasattr(os, "getuid"):
        identity = f"{login_name} (uid {os.getuid()}, euid {os.geteuid()})"
    else:
        identity = f"{login_name} (no uid on this platform)"

    header = [
        f"{__publisher__} {__app_name__} {__version__}",
        f"Log file      : {log_path}",
        f"Started       : {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}",
        f"Process user  : {identity}",
        f"Invoked by    : {invoking_user_name()}",
        f"Machine       : {platform.node()}",
        f"OS            : {platform.system()} {platform.release()}",
        f"Kernel        : {platform.version()}",
        f"Architecture  : {platform.machine()}",
        f"Python        : {platform.python_version()} ({sys.executable})",
        f"PID           : {os.getpid()}",
        f"Command line  : {' '.join(sys.argv)}",
    ]
    for line in header:
        logger.info(line)


def setup_logging(
    *,
    verbose: bool = False,
    console: bool | None = None,
) -> Path:
    """Configure the package logger and return the path of this run's log file.

    ``console`` defaults to "attach a console handler when stderr is a TTY".
    Passing it explicitly forces the handler on or off, which the CLI uses for
    ``--console`` / ``--quiet``.
    """
    global _configured

    directory = log_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = directory / f"{stamp}-{os.getpid()}.log"

    global _current_log_path

    logger = logging.getLogger(_ROOT_LOGGER)
    if _configured:
        return _current_log_path or log_path

    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(CMTraceFormatter())
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    if console is None:
        console = sys.stderr is not None and sys.stderr.isatty()
    if console:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(ConsoleFormatter())
        stream_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        logger.addHandler(stream_handler)

    _configured = True
    _current_log_path = log_path
    _write_session_header(logger, log_path)
    _prune_old_logs(directory, keep_newest=log_path)
    return log_path


def current_log_path() -> Path | None:
    """The file this run is logging to, or ``None`` before setup.

    The log is evidence, not just diagnostics: it records every command issued
    against a drive, with timestamps and exit codes. On a live session it lives
    in RAM and dies at power off, so the certificate step copies it out beside
    the PDF.
    """
    return _current_log_path


def get_logger(component: str) -> logging.Logger:
    """Return the logger for *component*, e.g. ``get_logger("Nvme")``.

    The component name becomes the CMTrace component column, so keep it short
    and stable - operators filter on it.
    """
    return logging.getLogger(f"{_ROOT_LOGGER}.{component}")
