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


async def test_replacement_size():
    """A two-disk mirror needs a new disk as large as its data, not the remaining disk."""
    profiles = {"raid1": 40 * m.GIB}
    remaining = [120 * 10**9]
    # Removing a disk leaves one, which is never enough for raid1 ...
    assert m.fits(profiles, remaining) == sum(remaining)
    # ... but any new disk that holds the 40 GiB is.
    size = m.replacement_size(profiles, remaining)
    assert 40 * m.GIB <= size <= 42 * m.GIB
    assert m.fits(profiles, [*remaining, size]) == 0
    # Data larger than the remaining disk cannot be saved by one new disk.
    assert m.replacement_size({"raid1": 200 * m.GIB}, remaining) is None


async def test_pending_sectors_need_to_persist(monkeypatch):
    """Pending sectors are a warning first and critical when still there a day later."""
    data = _smart(1000, [], supported=False)
    data["ata_smart_attributes"] = {"table": [{"id": 197, "raw": {"value": 1}}]}
    m.smartctl = lambda *args: (data, 0)
    now = 1_800_000_000
    monkeypatch.setattr(m.time, "time", lambda: now)
    pending_since = {}

    disk = m.check_disk({"name": "/dev/sde", "type": "sat"}, None, pending_since)
    assert disk["status"] == "warning"
    assert disk["problems"] == [{"code": "pending_sectors", "severity": "warning", "value": 1}]
    assert pending_since == {"S": now}

    now += m.PENDING_CRITICAL_AFTER
    disk = m.check_disk({"name": "/dev/sde", "type": "sat"}, None, pending_since)
    assert disk["status"] == "critical"

    # The sector was rewritten: the disk is fine and the clock starts over.
    data["ata_smart_attributes"]["table"][0]["raw"]["value"] = 0
    disk = m.check_disk({"name": "/dev/sde", "type": "sat"}, None, pending_since)
    assert disk["status"] == "ok"
    assert pending_since == {}


def _smart(hours, tests, supported=True):
    return {
        "model_name": "X",
        "serial_number": "S",
        "smart_status": {"passed": True},
        "power_on_time": {"hours": hours},
        "ata_smart_data": {"capabilities": {"self_tests_supported": supported}},
        "ata_smart_self_test_log": {"standard": {"table": tests}},
    }


async def test_self_test_overdue():
    """A missing or old self-test is a warning when the disk supports tests."""
    test = {"type": {"string": "Short offline"}, "status": {"string": "Completed without error", "passed": True}}
    cases = [
        (_smart(1000, [test | {"lifetime_hours": 990}]), []),
        (_smart(1000, [test | {"lifetime_hours": 500}]), [("self_test_overdue", 500)]),
        (_smart(1000, []), [("self_test_overdue", None)]),
        (_smart(1000, [], supported=False), []),
    ]
    for data, expected in cases:
        m.smartctl = lambda *args, data=data: (data, 0)
        disk = m.check_disk({"name": "/dev/sda", "type": "sat"})
        assert [(p["code"], p["value"]) for p in disk["problems"]] == expected
        assert disk["status"] == ("warning" if expected else "ok")


async def test_disk_info_ssd():
    """NVMe disks report as solid state with their power cycles."""
    info = m.disk_info(
        {
            "firmware_version": "2B2QEXM7",
            "nvme_total_capacity": 500107862016,
            "nvme_smart_health_information_log": {"power_cycles": 57},
            "nvme_optional_admin_commands": {"self_test": True},
            "device": {"protocol": "NVMe"},
        }
    )
    assert info["rotation_rate"] == 0
    assert info["power_cycles"] == 57
    assert info["capacity"] == 500107862016
    assert info["self_test_supported"] is True


async def test_scrub_status():
    """The result of the last scrub is parsed, a missing scrub is None."""
    calls = []
    m.run = fake({"btrfs scrub status": "UUID: x\n\tno stats available\n"}, calls)
    assert m.scrub_status("/mnt") is None

    m.run = fake({"btrfs scrub status": (
        "UUID: x\nScrub started:    Sun Nov  1 03:00:01 2026\nStatus:           finished\n"
        "Duration:         2:31:02\n\tread_errors: 2\n\tcsum_errors: 1\n"
        "\tcorrected_errors: 3\n\tuncorrectable_errors: 0\n"
    )}, calls)
    scrub = m.scrub_status("/mnt")
    assert scrub["status"] == "finished"
    assert scrub["duration"] == 2 * 3600 + 31 * 60 + 2
    assert (scrub["errors"], scrub["corrected"], scrub["uncorrectable"]) == (3, 3, 0)
    assert scrub["last"] is not None


async def test_kernel_io_errors_and_thin_pools():
    """I/O errors are counted per disk; only thin pools are reported."""
    calls = []
    m.run = fake({
        "journalctl": "I/O error, dev sde, sector 1\nI/O error, dev sde, sector 2\n"
                      "I/O error, dev nvme0n1, sector 3\nother line\n",
        "lvs": '{"report":[{"lv":[{"vg_name":"pve","lv_name":"data","lv_attr":"twi-aotz--",'
               '"data_percent":"16.75","metadata_percent":"1.01"},{"vg_name":"pve","lv_name":"root",'
               '"lv_attr":"-wi-ao----","data_percent":"","metadata_percent":""}]}]}',
    }, calls)
    assert m.kernel_io_errors() == {"sde": 2, "nvme0": 1}
    assert m.thin_pools() == [{"name": "pve/data", "data_percent": 16.75, "metadata_percent": 1.01}]



async def test_hwmon_ignores_bogus_limits(tmp_path, monkeypatch):
    """Temperature limits below 40 °C or absurdly high are ignored."""
    chip = tmp_path / "hwmon0"
    chip.mkdir()
    for name, value in {
        "name": "it8655",
        "temp1_input": "34000",
        "temp1_max": "26000",
        "temp2_input": "43000",
        "temp2_max": "70000",
        "temp2_crit": "65261850",
        "fan1_input": "1051",
    }.items():
        (chip / name).write_text(value)
    real_glob = m.glob.glob
    monkeypatch.setattr(
        m.glob, "glob", lambda pattern: real_glob(pattern.replace("/sys/class/hwmon", str(tmp_path)))
    )
    temperatures, fans = m.hwmon()
    assert temperatures == [
        {"chip": "it8655", "label": "temp1", "value": 34.0},
        {"chip": "it8655", "label": "temp2", "value": 43.0, "max": 70.0},
    ]
    assert fans == [{"chip": "it8655", "label": "fan1", "rpm": 1051}]


async def test_self_test_age_from_smartd_log():
    """smartd's log wins when the disk's self-test hours do not match its power-on hours."""
    now = m.time.time()
    calls = []
    m.run = fake({"journalctl": (
        f"{int(now) - 5 * 3600} HeimserverLP smartd[1]: Device: /dev/sdd [SAT], "
        "previous self-test completed without error\n"
        f"{int(now) - 3 * 3600} HeimserverLP smartd[1]: Device /dev/nvme0, "
        "previous short self-test completed without error\n"
    )}, calls)
    completions = m.self_test_completions()
    assert set(completions) == {"sdd", "nvme0"}

    # Old WD disk: self-test log at hour 234, power-on hours 10071.
    test = {"type": {"string": "Short offline"},
            "status": {"string": "Completed without error", "passed": True},
            "lifetime_hours": 234}
    data = _smart(10071, [test])
    m.smartctl = lambda *args: (data, 0)
    disk = m.check_disk({"name": "/dev/sdd", "type": "sat"}, completions)
    assert disk["self_test"]["hours_ago"] == 5
    assert disk["problems"] == []


async def test_pool_free_space_uses_smaller_estimate():
    """A mirror of unequal disks reports the free space statfs sees."""
    calls = []
    m.run = fake({
        "btrfs filesystem show": "Label: 'ssd'  uuid: u\n"
            "\tdevid    1 size 120034123776 used 1 path /dev/sdd\n"
            "\tdevid    2 size 240057409536 used 1 path /dev/sde\n",
        "btrfs filesystem df": "Data, RAID1: total=1, used=40000000000\n",
        "btrfs filesystem usage": "    Free (estimated):\t\t138935959552\t(min: 138935959552)\n"
            "    Free (statfs, df):\t\t78923243520\n",
        "btrfs balance status": "No balance found",
        "btrfs scrub status": "no stats available",
    }, calls)
    pool = m.btrfs_pools({"u": "/mnt/ssd"}, {})[0]
    assert pool["free"] == 78923243520
