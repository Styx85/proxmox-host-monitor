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



async def test_btrfs_errors_per_device_and_filesystem():
    """Device stats are read per counter, device and filesystem."""
    m.run = fake({
        "btrfs device stats /mnt/s": "[/dev/sda].write_io_errs    5\n[/dev/sda].corruption_errs  0\n"
                                     "[/dev/sdb].write_io_errs    0\n",
        "btrfs device stats /mnt/t": "[/dev/sda3].corruption_errs  7\n",
    }, [])
    assert m.btrfs_errors({"u": "/mnt/s", "v": "/mnt/t"}) == {
        "sda": [{"uuid": "u", "mount": "/mnt/s", "part": "", "counters": {"write_io": 5, "corruption": 0}},
                {"uuid": "v", "mount": "/mnt/t", "part": "3", "counters": {"corruption": 7}}],
        "sdb": [{"uuid": "u", "mount": "/mnt/s", "part": "", "counters": {"write_io": 0}}],
    }


def _disk(*codes):
    return {"serial": "S", "status": "ok",
            "problems": [{"code": c, "severity": "warning", "value": 1} for c in codes]}


IO = {"write_io": 50, "read_io": 8, "flush_io": 1, "corruption": 0, "generation": 0}


def _eval(disk, counters, scrub, state, now, mount="/mnt/s"):
    m.evaluate_btrfs_errors(disk, [{"uuid": "u", "mount": mount, "part": "", "counters": counters}],
                            {mount: scrub}, state, now)
    return disk


async def test_btrfs_io_errors_without_media_problem_are_a_connection_warning():
    """Failed requests on a healthy disk point to cable or controller."""
    disk = _eval(_disk(), IO, None, {}, 100)
    assert disk["status"] == "warning"
    assert disk["problems"] == [{"code": "connection_errors", "severity": "warning", "value": 59}]
    assert disk["btrfs_errors"] == 59


async def test_btrfs_errors_with_media_problem_or_corruption_are_critical():
    """Checksum errors, or failed requests plus media problems, make the disk critical."""
    disk = _eval(_disk("pending_sectors"), IO, None, {}, 100)
    assert disk["status"] == "critical" and disk["problems"][-1]["code"] == "btrfs_errors"
    disk = _eval(_disk(), dict(IO, write_io=0, read_io=0, flush_io=0, corruption=2), None, {}, 100)
    assert disk["status"] == "critical" and disk["problems"][-1]["value"] == 2


async def test_errors_of_several_filesystems_add_up():
    """A scrub of one filesystem does not clear the errors of another on the same disk."""
    state = {}
    clean = {"status": "finished", "uncorrectable": 0, "last": 150, "duration": None}
    devices = [{"uuid": "u", "mount": "/mnt/s", "part": "1", "counters": dict(IO)},
               {"uuid": "v", "mount": "/mnt/t", "part": "2", "counters": dict(IO, write_io=0, read_io=0,
                                                                            flush_io=0, corruption=7)}]
    disk = _disk()
    m.evaluate_btrfs_errors(disk, devices, {}, state, 100)
    assert disk["btrfs_errors"] == 66 and disk["status"] == "critical"
    disk = _disk()
    m.evaluate_btrfs_errors(disk, devices, {"/mnt/s": clean}, state, 200)
    assert disk["btrfs_errors"] == 7 and disk["status"] == "critical"
    assert set(state) == {"S:u1", "S:v2"}


async def test_clean_scrub_after_the_errors_repairs_them():
    """Only a finished scrub without uncorrectable errors that started after the last change clears them."""
    state = {}
    _eval(_disk(), IO, None, state, 100)
    assert _eval(_disk(), IO, {"status": "finished", "uncorrectable": 0, "last": 50, "duration": None}, state, 200)["btrfs_errors"] == 59
    for bad in ({"status": "running", "uncorrectable": 0, "last": 150, "duration": None},
                {"status": "finished", "uncorrectable": 1, "last": 150, "duration": None}):
        assert _eval(_disk(), IO, bad, state, 200)["btrfs_errors"] == 59
    clean = {"status": "finished", "uncorrectable": 0, "last": 150, "duration": None}
    assert _eval(_disk(), IO, clean, state, 300) == dict(_disk(), btrfs_errors=0)
    # New errors after the scrub count again, until the next clean scrub.
    disk = _eval(_disk(), dict(IO, write_io=53), clean, state, 400)
    assert disk["btrfs_errors"] == 3 and disk["status"] == "warning"


async def test_errors_found_by_a_running_scrub_belong_to_it():
    """Stale blocks a scrub finds and repairs are cleared when it finishes cleanly."""
    state = {}
    running = {"status": "running", "uncorrectable": 0, "last": 150, "duration": None}
    assert _eval(_disk(), IO, running, state, 200)["status"] == "warning"
    disk = _eval(_disk(), dict(IO, generation=5), running, state, 300)
    assert disk["problems"] == [{"code": "connection_errors", "severity": "warning", "value": 64}]
    disk = _eval(_disk(), dict(IO, generation=5), dict(running, status="finished"), state, 400)
    assert disk["btrfs_errors"] == 0 and disk["status"] == "ok"


async def test_reset_counters_start_from_zero():
    """After btrfs device stats -z new errors count in full, even below the old counters."""
    state = {"S:u": {"baseline": {"write_io": 50}, "last": {"write_io": 100}, "changed": 100}}
    assert _eval(_disk(), {"write_io": 60}, None, state, 200)["btrfs_errors"] == 60
    state = {"S:u": {"baseline": dict(IO), "last": dict(IO), "changed": 100}}
    assert _eval(_disk(), dict(IO, write_io=2, read_io=0, flush_io=0), None, state, 200)["btrfs_errors"] == 2


async def test_errors_of_a_scrub_between_two_reports_belong_to_it():
    """A scrub that ran between two reports clears what changed meanwhile, a later one does not."""
    state = {}
    _eval(_disk(), dict(IO, generation=0), None, state, 100)
    short = {"status": "finished", "uncorrectable": 0, "last": 110, "duration": 60}
    assert _eval(_disk(), dict(IO, generation=3), short, state, 200)["btrfs_errors"] == 0
    old = {"status": "finished", "uncorrectable": 0, "last": 110, "duration": 60}
    assert _eval(_disk(), dict(IO, generation=4), old, state, 300)["btrfs_errors"] == 1


async def test_smart_error_log_alone_is_no_media_problem():
    """Transfer errors in the SMART error log do not make I/O errors critical."""
    disk = _eval(_disk("error_log", "crc_errors"), IO, None, {}, 100)
    assert disk["status"] == "warning" and disk["problems"][-1]["code"] == "connection_errors"
