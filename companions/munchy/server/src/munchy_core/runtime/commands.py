from __future__ import annotations

import logging
import logging.config
import subprocess
import time
from typing import Any

log = logging.getLogger("munchy.server")


def run_command(
    cmd: list[str],
    *,
    action: str,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    log.info("%s: %s", action, " ".join(cmd))
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
        check=False,
    )
    result = {
        "command": cmd,
        "returncode": proc.returncode,
        "duration_s": round(time.monotonic() - started, 3),
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "command failed")[-2000:]
        raise RuntimeError(f"{action} failed with {proc.returncode}: {detail}")
    return result
