"""Tests for the guest update script that runs on the Proxmox host."""

import importlib.machinery
import importlib.util
from pathlib import Path

SCRIPT = (
    Path(__file__).parents[1]
    / "custom_components/proxmox_host_monitor/host/report-guest-updates"
)
loader = importlib.machinery.SourceFileLoader("report_guest_updates", str(SCRIPT))
spec = importlib.util.spec_from_loader("report_guest_updates", loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)


def test_terminal_for_app_updates():
    """The community-scripts core runs `clear`, which fails on a dumb terminal."""
    term = [e for e in m.ENV if e.startswith("TERM=")]
    assert term and term[0] != "TERM=dumb"


def test_timeout_output_is_text(monkeypatch):
    """A timeout keeps the output as text, so the log can join it with stderr."""
    def timeout(*args, **kwargs):
        raise m.subprocess.TimeoutExpired(args[0], 1, output=b"partial \xff")
    monkeypatch.setattr(m.subprocess, "run", timeout)
    result = m.run("true", timeout=1)
    assert result.returncode == 124
    assert isinstance(result.stdout, str) and result.stdout.startswith("partial")
    assert (result.stdout + result.stderr).endswith("timeout after 1 s")
