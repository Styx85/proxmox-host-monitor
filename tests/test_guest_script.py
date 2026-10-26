"""Tests for the container update script that runs on the Proxmox host."""

import importlib.machinery
import importlib.util
from pathlib import Path
import types

SCRIPT = (
    Path(__file__).parents[1]
    / "custom_components/proxmox_host_monitor/host/report-guest-updates"
)
loader = importlib.machinery.SourceFileLoader("report_guest_updates", str(SCRIPT))
spec = importlib.util.spec_from_loader("report_guest_updates", loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)


def fake(outputs, calls):
    """Return a run() replacement with canned command output."""
    def run(*args, timeout=120):
        calls.append(args)
        key = " ".join(args)
        for k, v in outputs.items():
            if key.startswith(k):
                code, out = v if isinstance(v, tuple) else (0, v)
                return types.SimpleNamespace(stdout=out, stderr="", returncode=code)
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)
    return run


async def test_release_check():
    """The release check of a community script is read without running it."""
    pinned = (
        '  RELEASE="v3.3.5"\n'
        '  if check_for_gh_release "Immich" "immich-app/immich" "${RELEASE}" "tested"; then\n'
    )
    assert m.release_check(pinned) == ("Immich", "immich-app/immich", "v3.3.5")
    latest = 'if check_for_gh_release "yuvomi" "ulsklyc/yuvomi"; then\n'
    assert m.release_check(latest) == ("yuvomi", "ulsklyc/yuvomi", None)
    # Unknown variables and tag prefix filters are not guessed.
    assert m.release_check('check_for_gh_release "a" "o/r" "$UNKNOWN"\n') is None
    assert m.release_check('check_for_gh_release "a" "o/r" "" "" "web-v"\n') is None
    assert m.release_check("apt-get install -y jellyfin\n") is None


async def test_pending_packages():
    """Upgrades and new packages of a simulated dist-upgrade are listed."""
    calls = []
    m.run = fake({"pct exec 101 -- apt-get -s": (
        "Inst libc6 [2.41-1] (2.41-2 Debian:13/stable [amd64])\n"
        "Inst linux-new (1.0 Debian:13/stable [amd64])\n"
        "Conf libc6 (2.41-2 Debian:13/stable [amd64])\n"
    )}, calls)
    assert m.pending_packages(101) == [
        {"name": "libc6", "installed": "2.41-1", "version": "2.41-2"},
        {"name": "linux-new", "installed": None, "version": "1.0"},
    ]
    m.run = fake({"pct exec 101 -- apt-get -s": (100, "")}, calls)
    assert m.pending_packages(101) is None


async def test_app_info_only_community_scripts():
    """Only update entrypoints of the community scripts are followed."""
    class Releases:
        def script(self, base, name):
            return 'if check_for_gh_release "yuvomi" "ulsklyc/yuvomi"; then\n'

        def latest(self, repo, pinned):
            return ("v2.82.0", "https://github.com/ulsklyc/yuvomi/releases/tag/v2.82.0")

    entrypoint = (
        'export UPDATE_SCRIPT_NAME="yuvomi"\n'
        'export COMMUNITY_SCRIPTS_URL="https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main"\n'
    )
    calls = []
    m.run = fake({"pct exec 104 -- cat /usr/bin/update": entrypoint,
                  "pct exec 104 -- cat /root/.yuvomi": "2.81.0\n"}, calls)
    assert m.app_info(104, Releases()) == {
        "script": "yuvomi",
        "app": "yuvomi",
        "installed": "2.81.0",
        "latest": "2.82.0",
        "release_url": "https://github.com/ulsklyc/yuvomi/releases/tag/v2.82.0",
    }

    m.run = fake({"pct exec 104 -- cat /usr/bin/update": entrypoint.replace(
        "raw.githubusercontent.com/community-scripts", "evil.example.org/x")}, calls)
    assert m.app_info(104, Releases()) is None


GUESTS = [{"vmid": 103, "name": "immich", "status": "running"},
          {"vmid": 104, "name": "yuvomi", "status": "stopped"}]


async def test_execute_checks():
    """Queued commands are validated before a unit is started."""
    calls = []
    m.run = fake({"systemctl is-active": (3, "")}, calls)
    assert m.execute({"action": "rm", "vmid": 103, "id": "abcdef01"}, GUESTS).startswith("unknown")
    assert m.execute({"action": "guest_update", "vmid": 999, "id": "abcdef01"}, GUESTS) == "container not found"
    assert m.execute({"action": "guest_update", "vmid": "103", "id": "abcdef01"}, GUESTS) == "container not found"
    assert m.execute({"action": "guest_update", "vmid": 103, "id": "x;y"}, GUESTS) == "invalid id"
    assert m.execute({"action": "guest_update", "vmid": 104, "id": "abcdef01"}, GUESTS) == "container is not running"
    assert m.execute({"action": "guest_rollback", "vmid": 103, "id": "abcdef01", "snapshot": "x;y"}, GUESTS) == "invalid snapshot"
    assert not any(call[0] == "systemd-run" for call in calls)

    assert m.execute({"action": "guest_update", "vmid": 103, "id": "abcdef01"}, GUESTS) == "started"
    run = calls[-1]
    assert run[0] == "systemd-run" and "--unit=proxmox-host-monitor-guest-103" in run
    assert run[-4:] == (m.SELF, "--update", "103", "abcdef01")

    assert m.execute(
        {"action": "guest_rollback", "vmid": 104, "id": "abcdef01", "snapshot": "phm_20261026_1200"},
        GUESTS,
    ) == "started"
    assert calls[-1][-5:] == (m.SELF, "--rollback", "104", "phm_20261026_1200", "abcdef01")

    m.run = fake({}, calls)
    assert m.execute({"action": "guest_update", "vmid": 103, "id": "abcdef01"}, GUESTS) == "another update is running"


async def test_interrupted_update(tmp_path):
    """An update whose unit is gone is reported as failed."""
    m.STATE_DIR = str(tmp_path)
    m.save_state(103, {"id": "abcdef01", "action": "update", "state": "running"})
    m.run = fake({"systemctl is-active": (3, "")}, [])
    state = m.last_action(103)
    assert state["state"] == "failed" and state["error"] == "interrupted"
    assert m.load_state(103)["state"] == "failed"


class StepLog:
    def __init__(self):
        self.steps = []

    def step(self, title, result):
        self.steps.append(title)
        return result.returncode == 0


async def test_backup_when_snapshot_impossible():
    """Containers with bind mounts get a protected backup instead of a snapshot."""
    backup = "pool:backup/vzdump-lxc-107-2026_10_26-09_30_00.tar.zst"
    listings = iter(["[]", f'[{{"volid": "{backup}", "notes": "{m.BACKUP_NOTES}", "ctime": 2}}]'])
    calls = []

    def run(*args, timeout=120):
        calls.append(args)
        key = " ".join(args)
        out, code = "", 0
        if key.startswith("pct snapshot"):
            code = 255
        elif key.startswith("pct config"):
            out = "mp0: /mnt/storage/watch,mp=/srv/watch\nrootfs: local-lvm:vm-107-disk-0,size=4G\n"
        elif key.startswith("pvesm status"):
            out = ("Name Type Status Total Used Available %\n"
                   "local dir active 100 50 50 50%\npool btrfs active 900 100 800 11%\n")
        elif key.startswith("pvesh get"):
            out = next(listings) if "/pool/" in key else "[]"
        return types.SimpleNamespace(stdout=out, stderr="", returncode=code)

    m.run = run
    m.BACKUP_STORAGE_FILE = "/nonexistent"
    assert m.restore_point(107, StepLog()) == (backup, None)
    vzdump = next(call for call in calls if call[0] == "vzdump")
    assert vzdump[1] == "107" and "pool" in vzdump and "--protected" in vzdump


async def test_no_backup_with_unsaved_volumes():
    """A mount point on a storage without backup=1 would be lost on restore."""
    def run(*args, timeout=120):
        key = " ".join(args)
        out = "mp0: local-lvm:vm-107-disk-1,mp=/data,size=8G\n" if key.startswith("pct config") else ""
        return types.SimpleNamespace(stdout=out, stderr="", returncode=255 if "snapshot" in key else 0)

    m.run = run
    point, error = m.restore_point(107, StepLog())
    assert point is None and "mp0" in error


async def test_rollback_validation():
    """Only snapshots and backups of the same container can be restored."""
    calls = []
    m.run = fake({"systemctl is-active": (3, "")}, calls)
    command = {"action": "guest_rollback", "vmid": 103, "id": "abcdef01"}
    assert m.execute({**command, "snapshot": "pool:backup/vzdump-lxc-103-2026_10_26.tar.zst"}, GUESTS) == "started"
    assert m.execute({**command, "snapshot": "pool:backup/vzdump-lxc-104-2026_10_26.tar.zst"}, GUESTS) == "invalid snapshot"
    assert m.execute({**command, "snapshot": "pool:backup/../etc/passwd"}, GUESTS) == "invalid snapshot"
