"""Radio sequencing with real SQLite and fake generation/audio boundaries."""
from pathlib import Path
from unittest.mock import patch

import pytest

import radio_station as radio


@pytest.fixture(autouse=True)
def no_live_user_render():
    with patch.object(radio, "foreground_music_busy", return_value=False):
        yield


class Player:
    def __init__(self):
        self.played = []
        self.active = False
    def play(self, path):
        self.played.append(path)
        self.active = True
    def busy(self):
        return self.active
    def stop(self):
        self.active = False
    def pause(self):
        pass
    def resume(self):
        pass
    def set_volume(self, value):
        self.volume = value


def fixture(tmp_path):
    identity = radio.save_station("Night drive", "Electronic dance, no guitars", root=tmp_path)
    player = Player()
    service = radio.Service(tmp_path, player)
    service.suspended = False
    service.choose(identity)
    jobs, calls = {}, []
    def generate(prompt, output, **kwargs):
        job = "job-" + str(len(calls))
        calls.append((prompt, output, kwargs))
        jobs[job] = {"status": "working", "progress": 30}
        return {"job_id": job}
    return identity, service, player, jobs, calls, generate


def finish(jobs, calls, number):
    Path(calls[number][1]).write_bytes(b"verified fake audio boundary")
    jobs["job-"+str(number)] = {"status": "complete"}


def test_station_round_trip_and_validation(tmp_path):
    station = radio.save_station("Dance", "Electronic dance", root=tmp_path)
    radio.save_station("Rap", "Female rap vocals", False, root=tmp_path, station_id=station)
    rows = radio.stations(tmp_path)
    assert len(rows) == 1 and rows[0]["prompt"] == "Female rap vocals" and not rows[0]["instrumental"]
    with pytest.raises(ValueError):
        radio.save_station("", "music", root=tmp_path)
    with pytest.raises(ValueError):
        radio.save_station("Bad", "music", root=tmp_path, station_id="../../else")


def test_default_vocal_stations_and_safe_legacy_migration(tmp_path):
    legacy = radio.save_station("Cale Club", radio.LEGACY_CLUB, root=tmp_path)
    custom = radio.save_station("Cale Country", "My own blues station", root=tmp_path)
    radio.ensure_stations(tmp_path)
    rows = {row["name"]: row for row in radio.stations(tmp_path)}
    assert len(rows) == 6 and rows["Cale Club"]["id"] == legacy
    assert not rows["Cale Club"]["instrumental"] and "Cale Media" in rows["Cale Club"]["prompt"]
    assert rows["Cale Country"]["id"] == custom and rows["Cale Country"]["prompt"] == "My own blues station"
    for name in ("Cale Rap", "Cale Pop", "Cale Rock"):
        assert not rows[name]["instrumental"] and "Cale Media" in rows[name]["prompt"]
    assert rows["Chill"]["instrumental"]
    radio.ensure_stations(tmp_path)
    assert len(radio.stations(tmp_path)) == 6
    radio.save_station("Cale Club", "My custom music", root=tmp_path, station_id=legacy)
    radio.ensure_stations(tmp_path)
    assert next(row for row in radio.stations(tmp_path) if row["id"] == legacy)["prompt"] == "My custom music"


def test_vocal_station_passes_vocal_intent_to_music_worker(tmp_path):
    radio.ensure_stations(tmp_path)
    club = next(row for row in radio.stations(tmp_path) if row["name"] == "Cale Club")
    service = radio.Service(tmp_path, Player())
    service.suspended = False
    service.choose(club["id"])
    with patch("music_backend.start_music_job", return_value={"job_id": "vocal-job"}) as render:
        service.tick()
    assert render.call_args.kwargs["instrumental"] is False
    assert "Cale Media" in render.call_args.args[0]
    assert render.call_args.kwargs["duration_seconds"] == 180


def test_radio_metrics_count_only_reported_local_text_usage(tmp_path):
    import sqlite3
    assert radio.local_token_totals(tmp_path) == (None, None)
    path = tmp_path / "LongTasks/usage.db"
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE usage_events(source TEXT,total_tokens INTEGER,task_id TEXT)")
        conn.executemany("INSERT INTO usage_events VALUES(?,?,?)", [
            ("ollama", 100, "ordinary-task"), ("ollama", 40, "radio-club"),
            ("chatgpt", 500, "radio-club"), ("groq", 200, "other")])
    assert radio.local_token_totals(tmp_path) == (140, 40)


def test_playback_status_does_not_erase_completed_render_evidence(tmp_path):
    _, service, _, jobs, calls, generate = fixture(tmp_path)
    with patch("music_backend.start_music_job", side_effect=generate), patch("music_backend.music_job_status", side_effect=lambda job: jobs[job]):
        service.tick()
        finish(jobs, calls, 0)
        service.tick()
        current = service.current
        row = next(row for row in service._rows() if row["id"] == current["id"])
        assert row["status"] == "playing" and row["finished"] > 0 and row["render_seconds"] > 0
        service.stop()
        service.tick()
        row = next(row for row in service._rows() if row["id"] == current["id"])
        assert row["status"] == "played" and row["finished"] > 0
        service._update(current["id"], "pruned")
        service.stats_at = 0
        service._publish()
        assert service.snapshot()["tracks_made"] == 1  # Cache cleanup never subtracts a made track.


def test_legacy_pruned_track_count_requires_a_success_receipt(tmp_path):
    import json
    import sqlite3
    state = tmp_path / "LongTasks"
    state.mkdir()
    with sqlite3.connect(state / "radio.sqlite3") as conn:
        conn.execute("CREATE TABLE tracks(id TEXT,station TEXT,job TEXT,path TEXT,status TEXT,created REAL,finished REAL,render_seconds REAL,config TEXT)")
        conn.executemany("INSERT INTO tracks VALUES(?,?,?,?,?,?,?,?,?)", [
            ("first", "station", "a"*32, "audio.wav", "pruned", 1, 0, 0, ""),
            ("failed", "station", "b"*32, "audio.wav", "pruned", 2, 0, 0, "")])
    jobs = state / "music-jobs"
    jobs.mkdir()
    (jobs/("a"*32+".json")).write_text(json.dumps({"status": "complete"}))
    (jobs/("b"*32+".json")).write_text(json.dumps({"status": "error"}))
    with radio.database(tmp_path) as conn:
        assert dict(conn.execute("SELECT id,completed FROM tracks")) == {"first": 1, "failed": 0}


def test_continuous_queue_prefetches_and_repeats_instead_of_silence(tmp_path):
    identity, service, player, jobs, calls, generate = fixture(tmp_path)
    with patch("music_backend.start_music_job", side_effect=generate), patch("music_backend.music_job_status", side_effect=lambda job: jobs[job]):
        service.tick()
        assert len(calls) == 1 and calls[0][2]["duration_seconds"] == 180 and calls[0][2]["make_video"] is False
        service.tick()
        assert len(calls) == 1  # One GPU render, not duplicate jobs per UI poll.
        finish(jobs, calls, 0)
        service.tick()
        assert player.played == [calls[0][1]] and len(calls) == 2
        player.active = False
        service.tick()
        assert player.played == [calls[0][1], calls[0][1]]  # Underrun repeats the good track.
        finish(jobs, calls, 1)
        service.tick()
        assert len(calls) == 3
        finish(jobs, calls, 2)
        service.tick()
        assert len(calls) == 3 and service.snapshot()["ready"] == 2
        player.active = False
        service.tick()
        assert player.played[-1] == calls[1][1] and len(calls) == 4
        assert all(call[2]["task_id"] == "radio-"+identity for call in calls)


def test_pause_stop_and_setup_holds_do_not_launch_music(tmp_path):
    _, service, player, jobs, calls, generate = fixture(tmp_path)
    with patch("music_backend.start_music_job", side_effect=generate), patch("music_backend.music_job_status", side_effect=lambda job: jobs[job]), patch("music_backend.pause_music_job") as stop:
        service.suspended = True
        service.tick()
        assert not calls
        service.suspended = False
        service.tick()
        service.toggle()
        service.tick()
        finish(jobs, calls, 0)
        service.tick()
        assert service.snapshot()["paused"] and not player.played and len(calls) == 1
        service.toggle()
        service.tick()
        assert player.played and len(calls) == 2
        service.stop()
        service.tick()
        assert not service.snapshot()["playing"] and not player.active and len(calls) == 2
        stop.assert_not_called()  # Stop playback, never orphan the engine's in-flight render.


def test_restarts_reattach_existing_render_and_station_edits_do_not_play_old_prompt(tmp_path):
    identity, service, player, jobs, calls, generate = fixture(tmp_path)
    with patch("music_backend.start_music_job", side_effect=generate), patch("music_backend.music_job_status", side_effect=lambda job: jobs[job]), patch("music_backend.pause_music_job") as stop:
        service.tick()
        restarted = radio.Service(tmp_path, player)
        restarted.suspended = False
        restarted.choose(identity)
        restarted.tick()
        assert len(calls) == 1
        radio.save_station("Night drive", "Calm piano", root=tmp_path, station_id=identity)
        restarted.choose(identity)
        restarted.tick()
        stop.assert_not_called()
        assert len(calls) == 1
        finish(jobs, calls, 0)
        restarted.tick()
        assert len(calls) == 2 and "Calm piano" in calls[1][0]
        assert not player.played


def test_failed_render_backs_off_and_keeps_cached_playback(tmp_path):
    _, service, player, jobs, calls, generate = fixture(tmp_path)
    with patch("music_backend.start_music_job", side_effect=generate), patch("music_backend.music_job_status", side_effect=lambda job: jobs[job]):
        service.tick()
        finish(jobs, calls, 0)
        service.tick()
        jobs["job-1"] = {"status": "error"}
        service.tick()
        player.active = False
        service.tick()
        assert len(calls) == 2 and player.played[-1] == calls[0][1]


def test_foreground_song_takes_priority_over_new_radio_render(tmp_path):
    _, service, player, jobs, calls, generate = fixture(tmp_path)
    with patch("music_backend.start_music_job", side_effect=generate), patch.object(radio, "foreground_music_busy", return_value=True):
        service.tick()
        assert not calls and "current song" in service.snapshot()["message"]


def test_unplayable_cache_is_retired_so_the_station_can_recover(tmp_path):
    _, service, player, jobs, calls, generate = fixture(tmp_path)
    with patch("music_backend.start_music_job", side_effect=generate), patch("music_backend.music_job_status", side_effect=lambda job: jobs[job]):
        service.tick()
        finish(jobs, calls, 0)
        with patch.object(player, "play", side_effect=RuntimeError("broken audio")), pytest.raises(RuntimeError):
            service.tick()
        assert service.current is None and service._rows()[0]["status"] == "error"
        service.tick()
        finish(jobs, calls, 1)
        service.tick()
        assert player.played == [calls[1][1]]


def test_radio_admission_is_atomic_across_services(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    identity, first, _, jobs, calls, generate = fixture(tmp_path)
    second = radio.Service(tmp_path, Player())
    second.suspended = False
    second.choose(identity)
    barrier = threading.Barrier(2)
    # Both windows see an empty snapshot before either tries the actual claim.
    for service in (first, second):
        original = service._rows
        def rows(original=original):
            result = original()
            barrier.wait(timeout=3)
            return result
        service._rows = rows
    with patch("music_backend.start_music_job", side_effect=generate):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda service: service.tick(), (first, second)))
    assert len(calls) == 1
