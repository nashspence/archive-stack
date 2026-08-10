from __future__ import annotations

import pytest
from riverhog_provenance import (
    MacOSFileStateObserver,
    UbuntuFileStateObserver,
    WindowsFileStateObserver,
    get_observer,
)


def test_explicit_factory_platforms() -> None:
    linux = get_observer("linux")
    macos = get_observer("macos")
    windows = get_observer("windows")
    assert isinstance(linux, UbuntuFileStateObserver)
    assert isinstance(macos, MacOSFileStateObserver)
    assert isinstance(windows, WindowsFileStateObserver)


def test_unknown_factory_platform() -> None:
    with pytest.raises(ValueError):
        get_observer("plan9")


def test_package_import_does_not_require_posix_fcntl(tmp_path) -> None:
    import os
    import subprocess
    import sys

    script = r"""
import builtins
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "fcntl":
        raise ImportError("simulated Windows: no fcntl")
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
import riverhog_provenance
assert hasattr(riverhog_provenance, "WindowsFileStateObserver")
"""
    environment = dict(os.environ)
    # The test's cwd-independent source root is supplied by pytest's invocation.
    package_source = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
    environment["PYTHONPATH"] = package_source
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
