"""Sensor math and unavailable-GPU behavior for the header gauges."""

import system_gauges


def test_cpu_percent_uses_elapsed_idle_and_total_time():
    assert system_gauges.cpu_percent((100, 200), (125, 300)) == 75
    assert system_gauges.cpu_percent(None, (125, 300)) is None
    assert system_gauges.cpu_percent((100, 200), (125, 200)) is None


def test_gpu_parser_handles_multiple_cards_and_missing_values():
    assert system_gauges.parse_gpu_utilization("9\n41\n") == 41
    assert system_gauges.parse_gpu_utilization("N/A\n") is None


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
