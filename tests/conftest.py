import pytest


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """A test that forgot XDG_STATE_HOME wrote run records into the real
    ~/.local/state/sanduk. A test that sets its own still overrides this."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


@pytest.fixture
def short_tmp():
    """A tmp dir short enough to bind a unix socket in: macOS caps the path at
    104 bytes, and its `tmp_path` is longer than that."""
    import shutil
    import tempfile
    from pathlib import Path

    path = Path(tempfile.mkdtemp(prefix="sk", dir="/tmp")).resolve()
    yield path
    shutil.rmtree(path, ignore_errors=True)
