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


def test_completed_render_without_output_retries_and_recovers(tmp_path):
    _, service, player, jobs, calls, generate = fixture(tmp_path)
    with patch("music_backend.start_music_job", side_effect=generate), patch("music_backend.music_job_status", side_effect=lambda job: jobs[job]), patch.object(radio.time, "monotonic", return_value=100) as clock:
        service.tick()
        jobs["job-0"] = {"status": "complete"}
        assert not Path(calls[0][1]).exists()
        service.tick()
        assert service._rows()[0]["status"] == "error"
        assert service.retry_at == 130
        service.tick()
        assert len(calls) == 1 and not player.played
        clock.return_value = 131
        service.tick()
        assert len(calls) == 2
        finish(jobs, calls, 1)
        service.tick()
        assert player.played == [calls[1][1]]


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


class Deck:
    """Audio boundary with a clock, so overlap and paused timing are testable."""
    def __init__(self, now):
        self.now = now
        self.path = None
        self.opened = self.running = self.paused = False
        self.volume = self.offset = self.started = 0
        self.length = 180
    def open(self, path):
        self.path = str(Path(path).resolve())
        self.opened, self.running, self.paused = True, False, False
        self.offset = 0
    def start(self):
        self.running = True
        self.started = self.now()
    def play(self, path):
        self.open(path)
        self.start()
    def clock(self):
        return self.offset+(self.now()-self.started if self.running and not self.paused else 0), self.length
    def busy(self):
        return self.opened and self.running and self.clock()[0] < self.length
    def set_volume(self, value):
        self.volume = value
    def pause(self):
        self.offset = self.clock()[0]
        self.paused = True
    def resume(self):
        self.started = self.now()
        self.paused = False
    def stop(self):
        self.path = None
        self.opened = self.running = False


def test_preload_and_equal_power_overlap_start_before_the_first_track_ends(tmp_path):
    import math
    moment = [0]
    player = radio.Player(lambda: Deck(lambda: moment[0]), lambda: moment[0])
    first, second = tmp_path/"first.wav", tmp_path/"second.wav"
    player.play(first)
    old = player.active
    player.prepare(second)
    staged = player.prepared
    assert old.busy() and staged.opened and not staged.busy() and staged.volume == 0
    assert player.blend(second, 8)
    assert player.active is staged and player.outgoing is old
    assert old.busy() and staged.busy() and old.volume == 65 and staged.volume == 0
    moment[0] = 4
    player.advance_mix()
    assert old.busy() and staged.busy()
    assert old.volume == pytest.approx(65/math.sqrt(2))
    assert staged.volume**2+old.volume**2 == pytest.approx(65**2)
    moment[0] = 8
    player.advance_mix()
    assert not old.opened and player.outgoing is None and staged.busy() and staged.volume == pytest.approx(65)


def test_pausing_and_muting_keep_both_decks_and_fade_position_consistent(tmp_path):
    moment = [0]
    player = radio.Player(lambda: Deck(lambda: moment[0]), lambda: moment[0])
    player.play(tmp_path/"first.wav")
    player.blend(tmp_path/"second.wav", 8)
    moment[0] = 2
    player.advance_mix()
    player.pause()
    old, new = player.outgoing, player.active
    assert old.paused and new.paused
    moment[0] = 52
    player.set_volume(0)
    assert old.volume == new.volume == 0
    player.set_volume(40)
    assert new.volume**2+old.volume**2 == pytest.approx(40**2)
    assert new.volume < old.volume  # Still one-quarter through, not 52 seconds later.
    player.resume()
    moment[0] = 54
    player.advance_mix()
    assert new.volume == pytest.approx(old.volume)
    player.stop()
    assert not old.opened and not new.opened and not player.mixing


def test_service_stages_ready_audio_immediately_and_blends_eight_seconds_before_eof(tmp_path):
    _, service, _, jobs, calls, generate = fixture(tmp_path)
    moment = [0]
    service.player = radio.Player(lambda: Deck(lambda: moment[0]), lambda: moment[0])
    with patch("music_backend.start_music_job", side_effect=generate), patch("music_backend.music_job_status", side_effect=lambda job: jobs[job]):
        service.tick()
        finish(jobs, calls, 0)
        service.tick()
        finish(jobs, calls, 1)
        service.tick()
        assert service.player.prepared.path == str(Path(calls[1][1]).resolve())
        assert service.snapshot()["next_loaded"]
        finish(jobs, calls, 2)
        service.tick()
        old = service.player.active
        moment[0] = 164
        service.tick()
        assert service.player.prepared is not None and not service.player.mixing
        moment[0] = 165
        service.tick()
        assert service.player.prepared.path == str(Path(calls[1][1]).resolve())
        assert not service.player.prepared.busy() and old.busy()
        moment[0] = 172
        service.tick()
        assert service.player.mixing and service.snapshot()["blending"]
        assert service.current["path"] == calls[1][1] and len(calls) == 4
        moment[0] = 176
        service.player.advance_mix()
        assert old.busy() and service.player.active.busy()
        moment[0] = 180
        service.player.advance_mix()
        assert not old.opened and service.player.active.busy()


def test_generation_underrun_blends_into_a_cached_repeat_then_the_next_ready_track(tmp_path):
    _, service, _, jobs, calls, generate = fixture(tmp_path)
    moment = [0]
    service.player = radio.Player(lambda: Deck(lambda: moment[0]), lambda: moment[0])
    with patch("music_backend.start_music_job", side_effect=generate), patch("music_backend.music_job_status", side_effect=lambda job: jobs[job]):
        service.tick()
        finish(jobs, calls, 0)
        service.tick()
        identity = service.current["id"]
        moment[0] = 172
        service.tick()
        assert service.player.mixing and service.repeating and service.current["id"] == identity
        finish(jobs, calls, 1)
        moment[0] = 180
        service.player.advance_mix()
        service.tick()
        assert service.player.mixing and not service.repeating
        assert service.current["path"] == calls[1][1]


def test_mix_next_button_crossfades_early_instead_of_stopping_playback(tmp_path):
    _, service, _, jobs, calls, generate = fixture(tmp_path)
    moment = [0]
    service.player = radio.Player(lambda: Deck(lambda: moment[0]), lambda: moment[0])
    with patch("music_backend.start_music_job", side_effect=generate), patch("music_backend.music_job_status", side_effect=lambda job: jobs[job]):
        service.tick()
        finish(jobs, calls, 0)
        service.tick()
        finish(jobs, calls, 1)
        service.tick()
        old = service.player.active
        moment[0] = 20
        service.skip()
        service.tick()
        assert service.player.mixing and old.busy() and service.player.active.busy()
        assert service.current["path"] == calls[1][1] and not service.skip_requested
        service.skip()  # Presses during a transition cannot queue another unwanted skip.
        service.tick()
        assert not service.skip_requested


def test_failed_preload_keeps_the_current_deck_audible(tmp_path):
    _, service, _, jobs, calls, generate = fixture(tmp_path)
    moment = [0]
    service.player = radio.Player(lambda: Deck(lambda: moment[0]), lambda: moment[0])
    with patch("music_backend.start_music_job", side_effect=generate), patch("music_backend.music_job_status", side_effect=lambda job: jobs[job]):
        service.tick()
        finish(jobs, calls, 0)
        service.tick()
        finish(jobs, calls, 1)
        old = service.player.active
        moment[0] = 165
        with patch.object(Deck, "open", side_effect=RuntimeError("Cannot decode next file")):
            service.tick()
        assert old.busy() and old.volume == 65 and service.player.active is old
        assert any(row["status"] == "error" for row in service._rows())


def test_cache_cleanup_preserves_the_outgoing_deck_until_fade_finishes(tmp_path):
    import time
    import uuid
    from contextlib import closing
    _, service, _, jobs, calls, generate = fixture(tmp_path)
    moment = [0]
    service.player = radio.Player(lambda: Deck(lambda: moment[0]), lambda: moment[0])
    with patch("music_backend.start_music_job", side_effect=generate), patch("music_backend.music_job_status", side_effect=lambda job: jobs[job]):
        service.tick()
        finish(jobs, calls, 0)
        service.tick()
        finish(jobs, calls, 1)
        service.tick()
        moment[0] = 172
        service.tick()
        outgoing = Path(calls[0][1])
        # More disposable cache rows make the old track eligible for pruning.
        with closing(radio.database(tmp_path)) as conn, conn:
            for _ in range(6):
                identity = uuid.uuid4().hex
                path = outgoing.parent/(identity+".wav")
                path.write_bytes(b"failed cache")
                conn.execute("INSERT INTO tracks(id,station,job,path,status,created) VALUES(?,?,?,?,?,?)",
                    (identity, service.station["id"], "unused", str(path), "error", time.time()))
        moment[0] = 173
        service.player.advance_mix()
        service.tick()
        assert outgoing.is_file() and service.player.outgoing.busy()
        moment[0] = 180
        service.player.advance_mix()
        service.tick()
        assert not outgoing.exists() and service.player.active.busy()


def test_stop_attempts_both_channels_even_if_one_close_reports_an_error(tmp_path):
    player = radio.Player(lambda: Deck(lambda: 0), lambda: 0)
    player.play(tmp_path/"one.wav")
    player.blend(tmp_path/"two.wav")
    old = player.outgoing
    with patch.object(player.active, "stop", side_effect=RuntimeError("Close failed")), pytest.raises(RuntimeError):
        player.stop()
    assert not old.opened and player.outgoing is None


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
