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
