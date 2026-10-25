"""Tests for the disk health script that runs on the Proxmox host."""

import importlib.machinery
import importlib.util
from pathlib import Path
import types

SCRIPT = (
    Path(__file__).parents[1]
    / "custom_components/proxmox_host_monitor/host/report-disk-health"
)
loader = importlib.machinery.SourceFileLoader("report_disk_health", str(SCRIPT))
spec = importlib.util.spec_from_loader("report_disk_health", loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)

def fake(outputs, calls):
    """Return a run() replacement with canned command output."""
    def run(*args):
        calls.append(args)
        key = " ".join(args)
        for k, v in outputs.items():
            if key.startswith(k):
                return types.SimpleNamespace(stdout=v, stderr="", returncode=0)
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)
    return run

async def test_removal_states():
    """Transient removal units are reported as running, done or failed."""
    calls = []
    m.run = fake({
        "systemctl list-units": "proxmox-host-monitor-remove-A.service loaded active exited x\nproxmox-host-monitor-remove-B.service loaded failed failed y\nproxmox-host-monitor-remove-C.service loaded active running z\n",
        "systemctl show -p ActiveState,SubState,Result proxmox-host-monitor-remove-A": "ActiveState=active\nSubState=exited\nResult=success\n",
        "systemctl show -p ActiveState,SubState,Result proxmox-host-monitor-remove-B": "ActiveState=failed\nSubState=failed\nResult=exit-code\n",
        "systemctl show -p ActiveState,SubState,Result proxmox-host-monitor-remove-C": "ActiveState=active\nSubState=running\nResult=success\n",
    }, calls)
    assert [(o["serial"], o["state"]) for o in m.removal_operations()] == [("A","done"),("B","failed"),("C","running")]

POOL = {"uuid":"u","mount":"/mnt/s","balance_running":False,"operations":[],
        "devices":[{"devid":6,"name":"sde","serial":"S1","missing_after_removal":0},
                   {"devid":1,"name":"sda","serial":"S2","missing_after_removal":5}]}

async def test_execute_checks_again():
    """A queued removal is only started when it is still safe."""
    calls = []
    m.run = fake({}, calls)
    assert m.execute({"action":"btrfs_remove","serial":"x;rm","pool":"u"}, [POOL]) == "invalid serial"
    assert m.execute({"action":"btrfs_remove","serial":"S9","pool":"u"}, [POOL]).startswith("disk S9")
    assert m.execute({"action":"btrfs_remove","serial":"S2","pool":"u"}, [POOL]).startswith("not enough space")
    assert m.execute({"action":"btrfs_remove","serial":"S1","pool":"u"}, [dict(POOL, balance_running=True)]).startswith("a balance")
    assert m.execute({"action":"btrfs_remove","serial":"S1","pool":"u"}, [dict(POOL, operations=[{"state":"running"}])]) == "another removal is running"
    assert not calls
    assert m.execute({"action":"btrfs_remove","serial":"S1","pool":"u"}, [POOL]) == "started"
    run = calls[-1]
    assert run[0] == "systemd-run" and run[-4:] == ("device", "remove", "6", "/mnt/s")
    assert "--unit=proxmox-host-monitor-remove-S1" in run

async def test_fits():
    """The space estimate respects mirror profiles and device sizes."""
    T = 1000 * m.GIB
    # raid1 data of 900 GiB fits on two 1 TB disks, not on one.
    assert m.fits({"raid1": 100 * m.GIB}, [T]) > 0
    assert m.fits({"raid1": 100 * m.GIB}, [T, T]) == 0
    # one big and one small disk: raid1 limited by the small one
    assert m.fits({"raid1": 300 * m.GIB}, [4 * T, 200 * m.GIB]) > 0
    assert m.fits({"raid1c3": 10 * m.GIB}, [T, T]) > 0
    assert m.fits({"raid5": 1500 * m.GIB}, [T, T, T]) == 0
