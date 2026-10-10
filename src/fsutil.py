"""
src/fsutil.py — crash-safe file I/O.

atomic_write_*: write to a temp file in the same dir, fsync, os.replace().
A crash/OOM-kill mid-write leaves the OLD file intact, never a truncated one.

load_json: on a parse error the corrupt file is renamed to
<name>.corrupt-<ts> (preserved for recovery) and the default is returned —
callers can never silently overwrite good data with an empty state.
"""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def atomic_write_text(path, text: str, mode: int | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        elif path.exists():
            try:
                os.chmod(tmp, path.stat().st_mode & 0o777)
            except OSError:
                pass
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path, data: Any, mode: int | None = None) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False), mode)


def load_json(path, default: Any) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        bak = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            path.rename(bak)
            print(f"  [fsutil] ⚠️ corrupt {path.name} preserved as {bak.name}")
        except OSError:
            pass
        return default
    except OSError:
        return default
