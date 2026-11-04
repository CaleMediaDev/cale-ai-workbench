"""Sensor math and unavailable-GPU behavior for the header gauges."""

import system_gauges


def test_cpu_percent_uses_elapsed_idle_and_total_time():
    assert system_gauges.cpu_percent((100, 200), (125, 300)) == 75
    assert system_gauges.cpu_percent(None, (125, 300)) is None
    assert system_gauges.cpu_percent((100, 200), (125, 200)) is None


def test_cpu_percent_rejects_idle_counter_rollback_and_recovers():
    previous = (100, 200)
    current = (50, 300)
    assert system_gauges.cpu_percent(previous, current) is None
    assert system_gauges.cpu_percent(current, (75, 400)) == 75


def test_cpu_percent_rejects_idle_delta_exceeding_total_and_recovers():
    previous = (100, 200)
    current = (250, 300)
    assert system_gauges.cpu_percent(previous, current) is None
    assert system_gauges.cpu_percent(current, (275, 400)) == 75
    assert system_gauges.cpu_percent(current, (350, 400)) == 0


def test_cpu_sampler_clears_failed_snapshot_and_reprimes(monkeypatch):
    import sys
    from types import SimpleNamespace

    monkeypatch.setattr(system_gauges, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(system_gauges, "_ram_percent", lambda: None)
    monkeypatch.setattr(system_gauges, "_gpu_percent", lambda: None)
    monkeypatch.setattr(system_gauges, "_drive_space", lambda: (None, None))
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        disk_io_counters=lambda: None,
        net_io_counters=lambda: None,
        net_if_stats=lambda: {},
    ))

    for error_type in (OSError, AttributeError):
        snapshots = iter([(100, 200), error_type("Sensor unavailable"),
                          (150, 400), (175, 500)])

        def cpu_times():
            snapshot = next(snapshots)
            if isinstance(snapshot, Exception):
                raise snapshot
            return snapshot

        monkeypatch.setattr(system_gauges, "_cpu_times", cpu_times)
        sampler = system_gauges.HardwareSampler()
        assert sampler.sample()["cpu"] is None
        assert sampler.sample()["cpu"] is None
        assert sampler.previous_cpu is None
        assert sampler.sample()["cpu"] is None
        assert sampler.sample()["cpu"] == 75


def test_gpu_parser_handles_multiple_cards_and_missing_values():
    assert system_gauges.parse_gpu_utilization("9\n41\n") == 41
    assert system_gauges.parse_gpu_utilization("N/A\n") is None


def test_gpu_parser_rejects_out_of_range_readings():
    assert system_gauges.parse_gpu_utilization("-1\n") is None
    assert system_gauges.parse_gpu_utilization("101\n") is None
    assert system_gauges.parse_gpu_utilization("-1\n41\n101\nN/A\n") == 41
    assert system_gauges.parse_gpu_utilization("0\n") == 0
    assert system_gauges.parse_gpu_utilization("100\n") == 100


def test_rpm_scale_lights_green_to_red_and_handles_missing_sensor():
    assert system_gauges.rpm_color(0) == "#32d390"
    assert system_gauges.rpm_color(0.5) == "#ffc64b"
    assert system_gauges.rpm_color(1) == "#ff4f61"
    assert system_gauges.dim_rpm_color(0) != system_gauges.dim_rpm_color(1)
    assert system_gauges.active_segments(None) == 0
    assert system_gauges.active_segments(1) == 1
    assert system_gauges.active_segments(50) == 12
    assert system_gauges.active_segments(100) == 24


def test_drive_gauge_tracks_volume_with_models(monkeypatch):
    monkeypatch.setattr(system_gauges.shutil, "disk_usage", lambda path: (100, 75, 25))
    assert system_gauges._drive_space()[0] == 75


def test_disk_transfer_speed_uses_counter_delta_and_elapsed_time():
    assert system_gauges.disk_transfer_speed((0, 0), (8_000_000, 2_000_000), 2) == (4, 1)
    assert system_gauges.disk_transfer_speed(None, (10, 20), 2) == (None, None)


def test_disk_transfer_speed_rejects_counter_rollback_and_recovers():
    previous = (8_000_000, 4_000_000)
    for current in ((1_000_000, 5_000_000), (9_000_000, 1_000_000)):
        assert system_gauges.disk_transfer_speed(previous, current, 2) == (None, None)
        following = (current[0] + 2_000_000, current[1] + 1_000_000)
        assert system_gauges.disk_transfer_speed(current, following, 2) == (1, 0.5)


def test_disk_counter_runtime_failure_is_isolated_and_recovers(monkeypatch):
    import sys
    from types import SimpleNamespace

    readings = iter([
        SimpleNamespace(read_bytes=1_000_000, write_bytes=2_000_000),
        RuntimeError("couldn't find any physical disk"),
        SimpleNamespace(read_bytes=9_000_000, write_bytes=6_000_000),
        SimpleNamespace(read_bytes=13_000_000, write_bytes=8_000_000),
    ])
    times = iter([10.0, 12.0, 14.0, 16.0])

    def disk_counters():
        reading = next(readings)
        if isinstance(reading, Exception):
            raise reading
        return reading

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        disk_io_counters=disk_counters,
        net_io_counters=lambda: SimpleNamespace(bytes_recv=0, bytes_sent=0),
        net_if_stats=lambda: {},
    ))
    monkeypatch.setattr(system_gauges, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(system_gauges, "_cpu_times", lambda: None)
    monkeypatch.setattr(system_gauges, "_ram_percent", lambda: 37)
    monkeypatch.setattr(system_gauges, "_gpu_percent", lambda: 41)
    monkeypatch.setattr(system_gauges, "_drive_space", lambda: (25, 100))
    monkeypatch.setattr(system_gauges.time, "monotonic", lambda: next(times))

    sampler = system_gauges.HardwareSampler()
    first = sampler.sample()
    assert first["drive_read_mbps"] is None
    failed = sampler.sample()
    assert failed["drive_read_mbps"] is None
    assert failed["drive_write_mbps"] is None
    assert failed["ram"] == 37
    assert failed["gpu"] == 41
    assert failed["drive"] == 25
    assert failed["net_down_mbps"] == 0
    assert sampler.previous_disk is None
    primed = sampler.sample()
    assert primed["drive_read_mbps"] is None
    assert primed["drive_write_mbps"] is None
    recovered = sampler.sample()
    assert recovered["drive_read_mbps"] == 2
    assert recovered["drive_write_mbps"] == 1


def test_network_bandwidth_includes_link_load():
    assert system_gauges.network_bandwidth(
        (0, 0), (12_500_000, 2_500_000), 1, 200
    ) == (100, 20, 60)


def test_network_bandwidth_rejects_counter_rollback_and_recovers():
    previous = (8_000_000, 4_000_000)
    for current in ((1_000_000, 5_000_000), (9_000_000, 1_000_000)):
        assert system_gauges.network_bandwidth(
            previous, current, 2, 100
        ) == (None, None, None)
        following = (current[0] + 2_000_000, current[1] + 1_000_000)
        assert system_gauges.network_bandwidth(
            current, following, 2, 100
        ) == (8, 4, 12)


def test_network_throughput_survives_unavailable_interface_stats(monkeypatch):
    import sys
    from types import SimpleNamespace

    counters = iter([
        SimpleNamespace(bytes_recv=1_000_000, bytes_sent=2_000_000),
        SimpleNamespace(bytes_recv=3_000_000, bytes_sent=3_000_000),
    ])
    times = iter([10.0, 12.0])

    def unavailable_stats():
        raise OSError("Interface statistics unavailable")

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        disk_io_counters=lambda: None,
        net_io_counters=lambda: next(counters),
        net_if_stats=unavailable_stats,
    ))
    monkeypatch.setattr(system_gauges, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(system_gauges, "_cpu_times", lambda: None)
    monkeypatch.setattr(system_gauges, "_ram_percent", lambda: None)
    monkeypatch.setattr(system_gauges, "_gpu_percent", lambda: None)
    monkeypatch.setattr(system_gauges, "_drive_space", lambda: (None, None))
    monkeypatch.setattr(system_gauges.time, "monotonic", lambda: next(times))

    sampler = system_gauges.HardwareSampler()
    first = sampler.sample()
    assert first["net_down_mbps"] is None
    assert first["net_up_mbps"] is None
    second = sampler.sample()
    assert second["net_down_mbps"] == 8
    assert second["net_up_mbps"] == 4
    assert second["net"] is None


def test_gpu_decode_failure_is_isolated_and_recovers(monkeypatch):
    import sys
    from types import SimpleNamespace

    calls = []

    def run_gpu(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return SimpleNamespace(returncode=0, stdout="41\n")

    monkeypatch.setattr(system_gauges, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(system_gauges.subprocess, "run", run_gpu)
    monkeypatch.setattr(system_gauges.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(system_gauges, "_cpu_times", lambda: None)
    monkeypatch.setattr(system_gauges, "_ram_percent", lambda: 37)
    monkeypatch.setattr(system_gauges, "_drive_space", lambda: (25, 100))
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        disk_io_counters=lambda: None,
        net_io_counters=lambda: None,
        net_if_stats=lambda: {},
    ))

    sampler = system_gauges.HardwareSampler()
    first = sampler.sample()
    assert first["gpu"] is None
    assert first["ram"] == 37
    assert first["drive"] == 25
    assert sampler.sample()["gpu"] == 41
    assert len(calls) == 2
    assert all(call["text"] is True for call in calls)


def test_non_windows_sample_returns_all_sensor_fields_as_unavailable(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(system_gauges, "os", SimpleNamespace(name="posix"))
    assert system_gauges.HardwareSampler().sample() == {
        "cpu": None,
        "ram": None,
        "gpu": None,
        "drive": None,
        "drive_free_gb": None,
        "drive_read_mbps": None,
        "drive_write_mbps": None,
        "net": None,
        "net_down_mbps": None,
        "net_up_mbps": None,
    }
