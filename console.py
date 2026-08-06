"""UTF-8 text output, including on Windows.

The Windows console usually starts in cp1252, where any accented or box-drawing
character breaks printing. The usual fix is to re-wrap `sys.stdout` in a UTF-8
`TextIOWrapper` - but if two modules do that, the second wraps the first one's
buffer, and when the first is garbage-collected it closes the buffer underneath
the second. The symptom is a `ValueError: I/O operation on closed file` in the
middle of a run, far from its cause.

So the setup lives here and is idempotent: calling it again does nothing.
"""

from __future__ import annotations

import io
import sys

_done = False


def utf8() -> None:
    """Force stdout and stderr to UTF-8. Safe to call any number of times."""
    global _done
    if _done or sys.platform != "win32":
        _done = True
        return
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "buffer"):
            continue
        if (getattr(stream, "encoding", "") or "").lower().replace("-", "") == "utf8":
            continue
        setattr(sys, name, io.TextIOWrapper(
            stream.buffer, encoding="utf-8", errors="replace", line_buffering=True,
        ))
    _done = True
