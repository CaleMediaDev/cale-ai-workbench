"""Radio sequencing with real SQLite and fake generation/audio boundaries."""
from pathlib import Path
from unittest.mock import patch

import pytest

import radio_station as radio

REAL_FOREGROUND_CHECK = radio.foreground_music_busy


@pytest.mark.parametrize("receipt", ["null", "[]", '"working"', "42", "true"])
def test_foreground_check_skips_bad_receipt_and_still_detects_live_user_song(tmp_path, receipt):
    import json

    import music_backend

    (tmp_path / "bad.json").write_text(receipt, encoding="utf-8")
    with patch.object(music_backend, "JOBS", tmp_path), patch.object(music_backend, "_worker_running", return_value=True) as running:
        assert not REAL_FOREGROUND_CHECK()
        running.assert_not_called()
        live = {"task_id": "user-song", "status": "working"}
        (tmp_path / "live.json").write_text(json.dumps(live), encoding="utf-8")
        assert REAL_FOREGROUND_CHECK()
        running.assert_called_once_with("live", live)


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


def test_missing_buffered_audio_is_retired_from_published_ready_count(tmp_path):
    _, service, player, jobs, calls, generate = fixture(tmp_path)
    with patch("music_backend.start_music_job", side_effect=generate), patch("music_backend.music_job_status", side_effect=lambda job: jobs[job]):
        service.tick()
        finish(jobs, calls, 0)
        service.tick()
        finish(jobs, calls, 1)
        service.tick()
        assert service.snapshot()["ready"] == 1
        buffered = next(row for row in service._rows() if row["status"] == "ready")
        Path(buffered["path"]).unlink()

        service.tick()

        retired = next(row for row in service._rows() if row["id"] == buffered["id"])
        assert retired["status"] == "error"
        assert service.snapshot()["ready"] == 0
        assert player.active and player.played == [calls[0][1]]
        # The in-flight replacement can still become usable buffered audio.
        finish(jobs, calls, 2)
        service.tick()
        assert service.snapshot()["ready"] == 1


def test_missing_current_audio_at_eof_is_retired_and_replacement_can_play(tmp_path):
    _, service, player, jobs, calls, generate = fixture(tmp_path)
    with patch("music_backend.start_music_job", side_effect=generate), patch("music_backend.music_job_status", side_effect=lambda job: jobs[job]):
        service.tick()
        finish(jobs, calls, 0)
        service.tick()
        current_id = service.current["id"]
        # First exercise the normal cached-repeat fallback.
        player.active = False
        service.tick()
        assert service.repeating
        Path(service.current["path"]).unlink()
        player.active = False

        service.tick()

        assert service.current is None
        assert not service.repeating
        assert not player.active
        assert next(row for row in service._rows() if row["id"] == current_id)["status"] == "error"
        assert service.snapshot()["message"].startswith("Buffering")
        assert len(calls) == 2  # Reattach the in-flight replacement, not a duplicate.
        finish(jobs, calls, 1)
        service.tick()
        assert service.current["path"] == calls[1][1]
        assert player.active and player.played[-1] == calls[1][1]


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


def test_failed_preload_releases_deck_even_when_cleanup_fails(tmp_path):
    active = Deck(lambda: 0)
    failed = Deck(lambda: 0)
    replacement = Deck(lambda: 0)
    decks = iter((active, failed, replacement))
    player = radio.Player(lambda: next(decks), lambda: 0)
    player.play(tmp_path/"first.wav")
    target = tmp_path/"second.wav"

    with patch.object(failed, "open", side_effect=RuntimeError("Open failed")), \
            patch.object(failed, "stop", side_effect=RuntimeError("Close failed")):
        with pytest.raises(RuntimeError):
            player.prepare(target)
        assert player.prepared is None
        assert player.active is active and active.busy() and active.volume == 65
        # Retry while the failed deck still cannot be closed: it must not be reused.
        player.prepare(target)

    assert player.prepared is replacement
    assert replacement.opened and not replacement.busy()
    assert replacement.path == str(target.resolve())
    assert player.blend(target)
    assert player.active is replacement and replacement.busy()
    assert player.outgoing is active and active.busy()


def test_replacing_prepared_deck_recovers_after_close_reports_an_error(tmp_path):
    active = Deck(lambda: 0)
    stale = Deck(lambda: 0)
    replacement = Deck(lambda: 0)
    decks = iter((active, stale, replacement))
    player = radio.Player(lambda: next(decks), lambda: 0)
    player.play(tmp_path/"first.wav")
    player.prepare(tmp_path/"second.wav")
    target = tmp_path/"third.wav"
    original_stop = stale.stop

    def stop_with_error():
        original_stop()
        raise RuntimeError("Close failed")

    with patch.object(stale, "stop", side_effect=stop_with_error) as close:
        with pytest.raises(RuntimeError, match="Close failed"):
            player.prepare(target)
        assert player.prepared is None
        assert player.active is active and active.busy() and active.volume == 65
        player.prepare(target)
        close.assert_called_once_with()

    assert player.prepared is replacement
    assert replacement.path == str(target.resolve())
    assert replacement.opened and not replacement.busy()
    assert player.blend(target)
    assert player.active is replacement and replacement.busy()
    assert player.outgoing is active and active.busy()


@pytest.mark.parametrize("seconds,error", [("invalid", ValueError), (None, TypeError)])
def test_invalid_blend_duration_preserves_decks_and_allows_retry(tmp_path, seconds, error):
    player = radio.Player(lambda: Deck(lambda: 0), lambda: 0)
    first, second = tmp_path/"first.wav", tmp_path/"second.wav"
    player.play(first)
    player.prepare(second)
    current, staged = player.active, player.prepared

    with pytest.raises(error):
        player.blend(second, seconds)

    assert player.active is current and current.busy()
    assert current.volume == player.volume
    assert player.prepared is staged and staged.opened and not staged.running
    assert staged.volume == 0
    assert player.outgoing is None and not player.mixing
    assert player.blend(second, 8)
    assert player.active is staged and staged.busy()
    assert player.outgoing is current and current.busy()


def test_failed_blend_start_releases_prepared_deck_and_allows_retry(tmp_path):
    player = radio.Player(lambda: Deck(lambda: 0), lambda: 0)
    first, second = tmp_path/"first.wav", tmp_path/"second.wav"
    player.play(first)
    current = player.active
    player.prepare(second)
    failed = player.prepared

    with patch.object(failed, "start", side_effect=RuntimeError("Cannot start track")):
        with pytest.raises(RuntimeError, match="Cannot start track"):
            player.blend(second)

    assert player.active is current and current.busy() and current.volume == 65
    assert not player.mixing and player.prepared is None
    assert not failed.opened and failed.path is None
    assert str(second.resolve()) not in player.protected_paths()
    assert player.blend(second)
    assert player.active is not failed and player.active.busy()
    assert player.outgoing is current and current.busy()


def test_failed_initial_blend_volume_restores_current_deck_and_allows_retry(tmp_path):
    player = radio.Player(lambda: Deck(lambda: 0), lambda: 0)
    first, second = tmp_path/"first.wav", tmp_path/"second.wav"
    player.play(first)
    current = player.active
    player.prepare(second)
    failed = player.prepared

    with patch.object(failed, "set_volume", side_effect=RuntimeError("Cannot set volume")):
        with pytest.raises(RuntimeError, match="Cannot set volume"):
            player.blend(second)

    assert player.active is current and current.busy()
    assert current.volume == player.volume
    assert not player.mixing and player.prepared is None
    assert not failed.opened and not failed.running
    assert str(second.resolve()) not in player.protected_paths()
    assert player.blend(second)
    assert player.active is not failed and player.active.busy()
    assert player.outgoing is current and current.busy()


@pytest.mark.parametrize("level", [0, 40, 100])
def test_failed_initial_blend_preserves_outgoing_deck_level(tmp_path, level):
    player = radio.Player(lambda: Deck(lambda: 0), lambda: 0)
    first, second = tmp_path/"first.wav", tmp_path/"second.wav"
    player.play(first)
    player.set_deck_volume(0, level)
    current = player.active
    expected_gain = player.volume*level/100
    assert current.volume == pytest.approx(expected_gain)
    player.prepare(second)
    failed = player.prepared

    with patch.object(failed, "set_volume", side_effect=RuntimeError("Cannot set volume")):
        with pytest.raises(RuntimeError, match="Cannot set volume"):
            player.blend(second)

    assert player.active is current and current.busy()
    assert current.volume == pytest.approx(expected_gain)
    assert not player.mixing and player.prepared is None
    assert not failed.opened
    assert player.blend(second)
    assert player.outgoing is current and current.busy()
    assert current.volume == pytest.approx(expected_gain)
    assert player.active.busy()


def test_blend_while_paused_preserves_staged_audio_until_resume(tmp_path):
    moment = [0]
    player = radio.Player(lambda: Deck(lambda: moment[0]), lambda: moment[0])
    first, second = tmp_path/"first.wav", tmp_path/"second.wav"
    player.play(first)
    player.prepare(second)
    old, staged = player.active, player.prepared
    moment[0] = 10
    player.pause()
    moment[0] = 50

    assert player.blend(second, 8) is False
    assert player.active is old and old.paused
    assert player.prepared is staged and not staged.running
    assert not player.mixing
    assert old.clock()[0] == 10 and staged.clock()[0] == 0

    player.resume()
    assert player.blend(second, 8) is True
    assert player.active is staged and player.outgoing is old
    assert staged.clock()[0] == 0
    moment[0] = 54
    player.advance_mix()
    assert old.busy() and staged.busy()
    assert staged.clock()[0] == 4
    assert staged.volume == pytest.approx(old.volume)


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


@pytest.mark.parametrize("mixing", [False, True])
def test_failed_resume_keeps_pause_state_retryable(tmp_path, mixing):
    moment = [0]
    player = radio.Player(lambda: Deck(lambda: moment[0]), lambda: moment[0])
    player.play(tmp_path/"first.wav")
    if mixing:
        player.blend(tmp_path/"second.wav", 8)
    moment[0] = 2
    player.advance_mix()
    player.pause()
    fade_start = player.fade_start
    moment[0] = 52

    with patch.object(player.active, "resume", side_effect=RuntimeError("Cannot resume")):
        with pytest.raises(RuntimeError, match="Cannot resume"):
            player.resume()

    assert player.paused_at == 2
    assert player.fade_start == fade_start
    assert player.active.paused
    if mixing:
        assert player.outgoing.paused

    moment[0] = 62
    player.resume()
    assert player.paused_at is None
    assert not player.active.paused
    assert player.active.busy()
    if mixing:
        assert not player.outgoing.paused
        assert player.crossfade_position == pytest.approx(.25)
        moment[0] = 64
        player.advance_mix()
        assert player.active.volume == pytest.approx(player.outgoing.volume)


def test_service_failed_resume_preserves_pause_state_and_retries(tmp_path):
    moment = [0]
    player = radio.Player(lambda: Deck(lambda: moment[0]), lambda: moment[0])
    service = radio.Service(tmp_path, player)
    service.enabled = True
    player.play(tmp_path / "first.wav")
    moment[0] = 2
    service._toggle()
    assert service.paused and player.active.paused

    moment[0] = 52
    with patch.object(player.active, "resume", side_effect=RuntimeError("Cannot resume")):
        with pytest.raises(RuntimeError, match="Cannot resume"):
            service._toggle()

    assert service.paused
    assert player.paused_at == 2 and player.active.paused
    service._publish()
    assert service.snapshot()["paused"]

    moment[0] = 62
    service._toggle()
    assert not service.paused
    assert player.paused_at is None and not player.active.paused
    assert player.busy()
    assert player.clock()[0] == 2


def test_failed_volume_change_during_paused_fade_still_allows_resume(tmp_path):
    moment = [0]
    player = radio.Player(lambda: Deck(lambda: moment[0]), lambda: moment[0])
    player.play(tmp_path/"first.wav")
    player.blend(tmp_path/"second.wav", 8)
    moment[0] = 2
    player.advance_mix()
    player.pause()
    old, new = player.outgoing, player.active
    moment[0] = 52

    with patch.object(new, "set_volume", side_effect=RuntimeError("Cannot set volume")):
        with pytest.raises(RuntimeError, match="Cannot set volume"):
            player.set_volume(40)

    assert player.paused_at == 52
    assert old.paused and new.paused
    moment[0] = 62
    player.resume()
    assert player.paused_at is None
    assert not old.paused and not new.paused
    moment[0] = 64
    player.advance_mix()
    assert old.busy() and new.busy()
    assert new.volume == pytest.approx(old.volume)
    assert new.volume**2 + old.volume**2 == pytest.approx(40**2)


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


def test_eof_fallback_repeat_blends_into_the_next_ready_track_immediately(tmp_path):
    _, service, _, jobs, calls, generate = fixture(tmp_path)
    moment = [0]
    service.player = radio.Player(lambda: Deck(lambda: moment[0]), lambda: moment[0])
    with patch("music_backend.start_music_job", side_effect=generate), patch("music_backend.music_job_status", side_effect=lambda job: jobs[job]):
        service.tick()
        finish(jobs, calls, 0)
        service.tick()
        original = service.current["id"]
        # Miss the pre-EOF window, forcing playback to restart the cached song.
        moment[0] = 180
        service.tick()
        assert service.current["id"] == original
        assert service.player.busy() and not service.player.mixing
        assert service.repeating
        finish(jobs, calls, 1)
        moment[0] = 181
        service.tick()
        assert service.player.mixing
        assert service.player.outgoing.busy() and service.player.active.busy()
        assert service.current["path"] == calls[1][1]
        assert not service.repeating


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


def test_completed_fade_releases_outgoing_state_when_close_reports_an_error(tmp_path):
    moment = [0]
    player = radio.Player(lambda: Deck(lambda: moment[0]), lambda: moment[0])
    player.play(tmp_path/"one.wav")
    player.blend(tmp_path/"two.wav", 8)
    old, current = player.outgoing, player.active
    original_stop = old.stop

    def stop_with_error():
        original_stop()
        raise RuntimeError("Close failed")

    moment[0] = 8
    with patch.object(old, "stop", side_effect=stop_with_error), pytest.raises(RuntimeError, match="Close failed"):
        player.advance_mix()

    assert not old.opened
    assert player.outgoing is None and not player.mixing
    assert player.active is current and current.busy()
    assert current.volume == pytest.approx(player.volume)
    assert player.blend(tmp_path/"three.wav")
    assert player.outgoing is current and current.busy()
    assert player.active.busy()


def test_stop_attempts_both_channels_even_if_one_close_reports_an_error(tmp_path):
    player = radio.Player(lambda: Deck(lambda: 0), lambda: 0)
    player.play(tmp_path/"one.wav")
    player.blend(tmp_path/"two.wav")
    old = player.outgoing
    with patch.object(player.active, "stop", side_effect=RuntimeError("Close failed")), pytest.raises(RuntimeError):
        player.stop()
    assert not old.opened and player.outgoing is None


def test_service_finalizes_stop_when_audio_cleanup_reports_an_error(tmp_path):
    _, service, player, jobs, calls, generate = fixture(tmp_path)
    with patch("music_backend.start_music_job", side_effect=generate), patch("music_backend.music_job_status", side_effect=lambda job: jobs[job]):
        service.tick()
        finish(jobs, calls, 0)
        service.tick()
        current_id = service.current["id"]

        def stop_with_error():
            player.active = False
            raise RuntimeError("Close failed")

        service.stop()
        with patch.object(player, "stop", side_effect=stop_with_error), pytest.raises(RuntimeError, match="Close failed"):
            service.tick()

        assert not player.active
        assert not service.enabled and not service.paused
        assert service.current is None
        assert next(row for row in service._rows() if row["id"] == current_id)["status"] == "played"
        assert service.snapshot()["message"].startswith("Radio stopped.")
        service.tick()
        assert len(player.played) == 1 and len(calls) == 2


def test_station_change_recovers_when_audio_cleanup_reports_an_error(tmp_path):
    _, service, player, jobs, calls, generate = fixture(tmp_path)
    target = radio.save_station("Piano", "Solo piano", root=tmp_path)
    with patch("music_backend.start_music_job", side_effect=generate), patch("music_backend.music_job_status", side_effect=lambda job: jobs[job]):
        service.tick()
        finish(jobs, calls, 0)
        service.tick()
        old_id = service.current["id"]
        old_path = player.played[-1]

        def stop_with_error():
            player.active = False
            raise RuntimeError("Close failed")

        service.choose(target)
        with patch.object(player, "stop", side_effect=stop_with_error), pytest.raises(RuntimeError, match="Close failed"):
            service.tick()

        assert service.station["id"] == target
        assert service.current is None
        assert service.enabled and not service.paused
        assert next(row for row in service._rows() if row["id"] == old_id)["status"] == "played"
        # The previous station's in-flight render can finish, but must not play.
        finish(jobs, calls, 1)
        service.tick()
        assert player.played == [old_path]
        assert "Solo piano" in calls[2][0]
        finish(jobs, calls, 2)
        service.tick()
        assert player.played == [old_path, calls[2][1]]
        assert service.current["station"] == target


def test_worker_processes_stop_before_a_failing_crossfade(tmp_path):
    player = radio.Player(lambda: Deck(lambda: 0), lambda: 0)
    service = radio.Service(tmp_path, player)
    player.play(tmp_path/"first.wav")
    player.blend(tmp_path/"second.wav")
    service.enabled = True
    service.stop()

    # End after one worker iteration; final shutdown alone must not count as
    # handling the queued Stop command and updating the service state.
    with patch.object(player.active, "set_volume", side_effect=RuntimeError("Cannot set volume")), \
            patch.object(service.command_ready, "wait", side_effect=lambda timeout: service.closed.set()), \
            patch.object(radio.time, "monotonic", return_value=100):
        service._loop()

    assert not service.enabled
    assert service.commands.empty()
    assert service.snapshot()["message"].startswith("Radio stopped.")
    assert not player.mixing and not player.busy()


def test_controls_wake_playback_worker_between_radio_status_checks(tmp_path):
    import threading

    player = Player()
    service = radio.Service(tmp_path, player)
    sleeping, changed = threading.Event(), threading.Event()
    wait = service.command_ready.wait

    def await_command(timeout):
        sleeping.set()
        return wait(timeout)

    def set_volume(value):
        player.volume = value
        changed.set()

    with patch.object(service, "tick") as tick, \
            patch.object(service.command_ready, "wait", side_effect=await_command), \
            patch.object(player, "set_volume", side_effect=set_volume), \
            patch.object(radio.time, "monotonic", return_value=100):
        service.start()
        try:
            assert sleeping.wait(2)
            service.volume(42)
            assert changed.wait(2)
            assert player.volume == 42
            assert tick.call_count == 1
        finally:
            service.close()
            service.thread.join(2)
        assert not service.thread.is_alive()


def test_worker_survives_failure_to_publish_its_retry_status(tmp_path):
    import sqlite3

    _, service, player, _, _, _ = fixture(tmp_path)
    waits = []

    def wait(timeout):
        waits.append(timeout)
        if len(waits) == 2:
            service.closed.set()

    with patch.object(service, "tick", side_effect=[sqlite3.OperationalError("database is locked"), None]) as tick, \
            patch.object(service, "_publish", side_effect=sqlite3.OperationalError("database is locked")) as publish, \
            patch.object(service.command_ready, "wait", side_effect=wait), \
            patch.object(radio.time, "monotonic", side_effect=range(0, 100, 10)):
        service._loop()

    assert tick.call_count == 2
    publish.assert_called_once_with()
    assert len(waits) == 2
    assert not player.active


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


def test_studio_manual_crossfade_real_gains_hold_and_auto_resumes_without_jump(tmp_path):
    import math
    moment = [0.0]
    player = radio.Player(lambda: Deck(lambda: moment[0]), lambda: moment[0])
    player.play(tmp_path / "a.wav")
    old = player.active
    player.prepare(tmp_path / "b.wav")
    player.blend(tmp_path / "b.wav", 8)
    new = player.active
    assert old.channel == 0 and new.channel == 1
    player.set_deck_volume(0, 80)
    player.set_deck_volume(1, 50)
    player.set_crossfade(.25)
    assert old.volume == pytest.approx(65*.8*math.cos(math.pi/8))
    assert new.volume == pytest.approx(65*.5*math.sin(math.pi/8))
    moment[0] = 90
    player.advance_mix()
    assert player.crossfade_position == .25 and old.opened
    player.set_auto_mix(True)
    assert player.crossfade_position == pytest.approx(.25)
    moment[0] += 6
    player.advance_mix()
    assert not old.opened and new.volume == pytest.approx(65*.5)
    player.prepare(tmp_path / "c.wav")
    player.blend(tmp_path / "c.wav", 8)
    assert player.active.channel == 0 and player.outgoing.channel == 1
    assert player.crossfade_position == pytest.approx(1)
    player.set_crossfade(0)
    assert player.outgoing is None and player.active.channel == 0
    assert player.active.volume == pytest.approx(65*.8)


def test_studio_service_manual_fader_uses_existing_ready_queue_and_real_transition(tmp_path):
    _, service, _, jobs, calls, generate = fixture(tmp_path)
    moment = [0]
    service.player = radio.Player(lambda: Deck(lambda: moment[0]), lambda: moment[0])
    with patch("music_backend.start_music_job", side_effect=generate), patch("music_backend.music_job_status", side_effect=lambda job: jobs[job]):
        service.tick(); finish(jobs, calls, 0); service.tick(); finish(jobs, calls, 1); service.tick()
        old = service.player.active
        service.crossfade(.5)
        assert service.auto_blend and not service.player.mixing  # UI only queued a command.
        service.tick()
        assert not service.auto_blend and service.player.mixing
        assert service.current["path"] == calls[1][1]
        assert old.busy() and service.player.active.busy()
        assert service.player.crossfade_position == .5
        service.auto_mix(True); service.tick()
        assert service.auto_blend and service.player.manual_fraction is None


def _studio_pcm(path):
    import array
    import wave
    values = array.array("h", [0]*4000 + [16384, -16384]*2000)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(4000)
        output.writeframes(values.tobytes())
    return path


def test_studio_waveform_comes_from_pcm_and_invalid_audio_is_blank(tmp_path):
    path = _studio_pcm(tmp_path / "actual-preview-tone.wav")
    peaks = radio.waveform_peaks(path, bins=8)
    assert len(peaks) == 8 and peaks[:4] == [0]*4
    assert peaks[4:] == [.5]*4
    assert len(radio.waveform_peaks(path, bins=9000)) <= 512
    (tmp_path / "bad.wav").write_text("not an audio waveform")
    assert radio.waveform_peaks(tmp_path / "bad.wav") == []
    assert radio.waveform_peaks(tmp_path / "missing.wav") == []


def test_waveform_read_does_not_hold_up_radio_playback_or_stop(tmp_path):
    import threading
    import time

    moment = [0]
    player = radio.Player(lambda: Deck(lambda: moment[0]), lambda: moment[0])
    service = radio.Service(tmp_path, player)
    first = _studio_pcm(tmp_path / "first.wav")
    second = _studio_pcm(tmp_path / "second.wav")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def read_waveform(path):
        started.set()
        try:
            assert release.wait(3)
            return [.25]
        finally:
            finished.set()

    player.play(first)
    player.prepare(second)
    service.enabled = service.manual_mode = True
    with patch.object(radio, "waveform_peaks", side_effect=read_waveform) as decode:
        try:
            service.tick()
            assert started.wait(1) and not finished.is_set()
            assert {deck["status"] for deck in service.snapshot()["decks"]} == {"Playing", "Cued"}
            assert all(not deck["peaks"] for deck in service.snapshot()["decks"])
            assert player.blend(second, 8)
            moment[0] = 4
            player.advance_mix()
            assert player.active.volume > 0 and player.outgoing.volume > 0
            service.tick()
            assert decode.call_count == 1
            service.stop()
            service.tick()
            assert not service.enabled and not player.busy() and not finished.is_set()
        finally:
            release.set()
            assert finished.wait(2)
        deadline = time.monotonic() + 2
        while service.waveform_pending and time.monotonic() < deadline:
            service.tick()
            time.sleep(.01)
        assert not service.waveform_pending
        assert all(not deck["peaks"] for deck in service.snapshot()["decks"])
        player.play(first)
        service.enabled = True
        service.tick()
        assert any(deck["peaks"] == [.25] for deck in service.snapshot()["decks"])
        assert decode.call_count == 1


def studio_preview_fixture(tmp_path):
    """Screenshot-capable real Tk harness; no generation, audio device or user state."""
    import tkinter as tk
    import shutil
    radio.ensure_stations(tmp_path)
    (tmp_path / "assets").mkdir(exist_ok=True)
    logo = radio.ROOT / "assets/cale-logo.png"
    if logo.exists():
        shutil.copyfile(logo, tmp_path / "assets/cale-logo.png")
    import array, math, wave
    def preview_tone(path, beat):
        samples = array.array("h", (round(27000*(.16+.8*abs(math.sin(number/8000*beat*math.pi)))*
            math.sin(number/8000*440*math.pi)) for number in range(16000)))
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(8000); audio.writeframes(samples.tobytes())
        return radio.waveform_peaks(path)
    peaks = preview_tone(tmp_path / "preview-tone-a.wav", 3.5)
    next_peaks = preview_tone(tmp_path / "preview-tone-b.wav", 5)
    class PreviewService:
        root = tmp_path
        enabled = True
        def __init__(self):
            self.calls = []
            self.station_catalog = radio.stations(self.root)
        def station_prompt(self, *args, **kwargs): self.calls.append(("station_prompt", args, kwargs))
        def snapshot(self):
            return {"message": "Preview tone · real PCM waveform", "playing": True, "paused": False,
                "on_air": True, "ready": 2, "next_loaded": True, "auto_blend": True,
                "crossfade": .3, "tracks_made": 12, "radio_tokens": 12480, "local_tokens": 82360,
                "decks": [{"title": "Cale Club · preview", "status": "Playing", "duration": 2,
                    "elapsed": 1.25, "peaks": peaks, "gain": 65},
                    {"title": "Next track · preview", "status": "Cued", "duration": 2,
                    "elapsed": 0, "peaks": next_peaks, "gain": 0}]}
        def start(self): self.calls.append(("start",))
        def skip(self): self.calls.append(("skip",))
        def stop(self): self.calls.append(("stop",))
        def toggle(self): self.calls.append(("toggle",))
        def choose(self, identity): self.calls.append(("choose", identity))
        def volume(self, value): self.calls.append(("volume", value))
        def crossfade(self, value): self.calls.append(("crossfade", value))
        def auto_mix(self, value): self.calls.append(("auto_mix", value))
        def deck_volume(self, *value): self.calls.append(("deck_volume", *value))
    window = tk.Tk()
    from tkinter import ttk
    ttk.Style(window).theme_use("clam")
    window.title("NIGHTSHIFT — Music Studio preview")
    window.geometry("1360x760+40+40")
    songs = []
    service = PreviewService()
    controls = radio.Controls(window, window, service, on_make_song=songs.append)
    window.update_idletasks(); window.update()
    return window, controls, service, songs


def test_studio_tk_song_callback_radio_controls_and_auto_position_do_not_fake_audio(tmp_path):
    import tkinter as tk
    try:
        window, controls, service, songs = studio_preview_fixture(tmp_path)
    except tk.TclError as error:
        pytest.skip("Tk desktop unavailable: "+str(error))
    window.withdraw()
    try:
        controls.set_mode("song")
        controls.prompt.insert(0, "A clear female vocal electronic song")
        controls.generate_button.invoke()
        assert songs == ["A clear female vocal electronic song"]
        assert not any(call[0] == "choose" for call in service.calls)
        controls.set_mode("radio"); controls.deck_action(1)
        assert service.calls[-1] == ("skip",)
        controls.set_crossfade(.6)
        assert service.calls[-1] == ("crossfade", .6)
        service.calls.clear(); controls.poll(); window.update()
        assert not any(call[0] == "crossfade" for call in service.calls)
        assert controls.waves.find_all()
        assert controls.frame.winfo_reqwidth() <= 1360
        assert controls.frame.winfo_reqheight() <= 760
    finally:
        window.destroy()


def test_studio_updates_playheads_without_rebuilding_waves_and_skips_hidden_page(tmp_path):
    import tkinter as tk

    try:
        window, controls, service, _ = studio_preview_fixture(tmp_path)
    except tk.TclError as error:
        pytest.skip("Tk desktop unavailable: " + str(error))
    try:
        state = service.snapshot()
        waves = controls.waves
        items = waves.find_all()
        old_head = waves.coords("wave-0-head")
        state["decks"][0]["elapsed"] = 1.75
        with patch.object(service, "snapshot", return_value=state) as snapshot:
            controls.poll()
            assert waves.find_all() == items
            assert waves.coords("wave-0-head")[0] > old_head[0]
            window.withdraw()
            window.update()
            snapshot.reset_mock()
            controls.poll()
            snapshot.assert_not_called()
            state = {**state, "decks": [{}, state["decks"][1]]}
            snapshot.return_value = state
            window.deiconify()
            window.update()
            snapshot.assert_called()
            assert not waves.find_withtag("wave-0-head")
            assert waves.find_withtag("wave-1") == tuple(item for item in items if "wave-1" in waves.gettags(item))
    finally:
        window.destroy()


def test_studio_station_changes_are_queued_without_ui_database_io(tmp_path):
    _, service, _, _, _, _ = fixture(tmp_path)
    with patch.object(radio, "database", side_effect=AssertionError("Tk callback opened SQLite")):
        identity = service.station_prompt("New station", "Clear vocals and synthesizers", False)
        service.choose(identity)
    service._commands()
    assert service.station["id"] == identity and service.enabled
    assert next(row for row in radio.stations(tmp_path) if row["id"] == identity)["instrumental"] == 0


def test_studio_player_gains_reach_windows_audio_commands_not_just_ui_values():
    deck = radio.AudioDeck()
    deck.opened = True
    with patch.object(deck, "command") as command:
        deck.set_volume(23.4)
    command.assert_called_once_with(f"setaudio {deck.alias} volume to 234")


def test_studio_waveform_does_not_open_oversized_audio(tmp_path):
    path = tmp_path / "oversized.wav"
    from types import SimpleNamespace
    with patch.object(Path, "stat", return_value=SimpleNamespace(st_size=129*1024*1024)), patch("wave.open") as opened:
        assert radio.waveform_peaks(path) == []
    opened.assert_not_called()


def test_studio_failed_song_callback_keeps_the_description(tmp_path):
    import tkinter as tk
    try:
        window, controls, service, songs = studio_preview_fixture(tmp_path)
    except tk.TclError as error:
        pytest.skip("Tk desktop unavailable: "+str(error))
    window.withdraw()
    try:
        controls.on_make_song = lambda _prompt: False
        controls.set_mode("song")
        controls.prompt.insert(0, "Keep my exact song description")
        controls.generate()
        assert controls.prompt.get() == "Keep my exact song description"
        assert "did not start" in controls.text.get()
    finally:
        window.destroy()


def test_studio_vocals_is_radio_only_and_missing_song_launcher_keeps_prompt(tmp_path):
    import tkinter as tk
    try:
        window, controls, service, songs = studio_preview_fixture(tmp_path)
    except tk.TclError as error:
        pytest.skip("Tk desktop unavailable: "+str(error))
    window.withdraw()
    try:
        controls.vocals.set(False)
        controls.on_make_song = None
        controls.set_mode("song")
        assert not controls.vocals_toggle.winfo_manager()
        assert str(controls.generate_button.cget("state")) == "disabled"
        controls.prompt.insert(0, "A song with my original vocal instructions")
        controls.generate()  # Return-key path must also give an honest response.
        assert controls.prompt.get() == "A song with my original vocal instructions"
        assert "not connected" in controls.text.get()
        assert not songs
        controls.set_mode("radio")
        assert controls.vocals_toggle.winfo_manager() == "pack"
        assert controls.vocals.get() is False
    finally:
        window.destroy()


def test_studio_song_acknowledgement_and_errors_survive_radio_polling(tmp_path):
    import tkinter as tk
    try:
        window, controls, service, songs = studio_preview_fixture(tmp_path)
    except tk.TclError as error:
        pytest.skip("Tk desktop unavailable: "+str(error))
    window.withdraw()
    try:
        controls.set_mode("song")
        controls.prompt.insert(0, "An upbeat song with clear vocals")
        controls.generate()
        assert songs == ["An upbeat song with clear vocals"]
        assert not controls.prompt.get()
        acknowledgement = controls.song_notice.get()
        assert acknowledgement == "Song started. Follow its progress in Home → My tasks."
        controls.poll(); window.update()
        assert controls.song_notice.get() == acknowledgement
        assert controls.song_notice_label.winfo_manager() == "pack"
        controls.on_make_song = lambda _prompt: False
        controls.prompt.insert(0, "Retain this failed song request")
        controls.generate(); failed = controls.song_notice.get()
        controls.poll(); window.update()
        assert controls.song_notice.get() == failed and "did not start" in failed
        assert controls.prompt.get() == "Retain this failed song request"
        def broken(_prompt):
            raise RuntimeError("Readiness check failed")
        controls.on_make_song = broken
        controls.generate(); errored = controls.song_notice.get()
        controls.poll(); window.update()
        assert controls.song_notice.get() == errored and "Readiness check failed" in errored
        assert controls.prompt.get() == "Retain this failed song request"
        controls.set_mode("radio")
        assert not controls.song_notice_label.winfo_manager()
        controls.set_mode("song"); controls.poll(); window.update()
        assert controls.song_notice.get() == errored
    finally:
        window.destroy()


@pytest.mark.parametrize("operation", ["pause", "resume"])
@pytest.mark.parametrize("deck_name,mixing", [("active", False), ("active", True), ("outgoing", True)])
def test_service_pause_resume_retries_each_failed_deck_without_losing_intent(tmp_path, operation, deck_name, mixing):
    moment = [0]
    player = radio.Player(lambda: Deck(lambda: moment[0]), lambda: moment[0])
    service = radio.Service(tmp_path, player)
    service.enabled = True
    player.play(tmp_path / "first.wav")
    if mixing:
        player.blend(tmp_path / "second.wav", 8)
    moment[0] = 2
    if operation == "resume":
        service._toggle()
        assert service.paused
        moment[0] = 52
    original_paused = service.paused
    deck = getattr(player, deck_name)
    with patch.object(deck, operation, side_effect=RuntimeError("Owned fake deck failure")):
        with pytest.raises(RuntimeError, match="Owned fake deck failure"):
            service._toggle()
    assert service.paused is original_paused
    assert (player.paused_at is not None) is original_paused
    moment[0] = 62
    service._toggle()
    expected = operation == "pause"
    assert service.paused is expected
    assert (player.paused_at is not None) is expected
    assert all(item.paused is expected for item in (player.active, player.outgoing) if item is not None)
    service._publish()
    assert service.snapshot()["paused"] is expected


def _saved_song(root, name="A finished song", identity="made-1"):
    import json
    audio=root/(identity+".wav");audio.write_bytes(b"existing generated audio")
    jobs=root/"LongTasks/music-jobs";jobs.mkdir(parents=True,exist_ok=True)
    (jobs/(identity+".json")).write_text(json.dumps({"status":"complete","task_id":"user-song",
        "output_path":str(audio),"prompt":name}),encoding="utf-8")
    return audio


def test_maker_result_plays_in_shared_decks_without_a_new_render(tmp_path):
    audio=_saved_song(tmp_path);before=audio.read_bytes();moment=[0]
    player=radio.Player(lambda:Deck(lambda:moment[0]),lambda:moment[0])
    service=radio.Service(tmp_path,player)
    service.suspended=False
    with patch("music_backend.start_music_job") as create:
        service.tick()
        assert service.snapshot()["songs"]==[{"id":"made-1","path":str(audio),"title":"A finished song"}]
        service.play_song("made-1");service.tick()
        assert service.manual_mode and service.enabled and player.active.path==str(audio)
        assert service.snapshot()["decks"][0]["title"]=="A finished song"
        moment[0]=181;service.tick()
        assert not service.enabled and not player.active.opened
        service.toggle();service.tick()
        create.assert_not_called()
    assert audio.read_bytes()==before


def test_maker_library_ignores_radio_failed_and_missing_outputs(tmp_path):
    import json
    audio=_saved_song(tmp_path)
    jobs=tmp_path/"LongTasks/music-jobs"
    for identity,task,status,path in (("radio","radio-club","complete",audio),
        ("failed","user-song","error",audio),("missing","user-song","complete",tmp_path/"gone.wav")):
        (jobs/(identity+".json")).write_text(json.dumps({"status":status,"task_id":task,"output_path":str(path)}))
    (jobs/"bad.json").write_text("{invalid")
    assert [song["id"] for song in radio.completed_songs(tmp_path)]==["made-1"]


def test_maker_track_blends_into_active_radio_and_radio_resumes(tmp_path):
    import hashlib
    from contextlib import closing
    audio=_saved_song(tmp_path);moment=[0]
    player=radio.Player(lambda:Deck(lambda:moment[0]),lambda:moment[0])
    service=radio.Service(tmp_path,player);service.suspended=False
    station=radio.stations(tmp_path)[0];signature=hashlib.sha256((station["prompt"]+str(station["instrumental"])).encode()).hexdigest()
    cached=[]
    with closing(radio.database(tmp_path)) as conn,conn:
        for number in range(3):
            path=tmp_path/("radio-"+str(number)+".wav");path.write_bytes(b"cached radio fixture");cached.append(path)
            conn.execute("INSERT INTO tracks(id,station,job,path,status,created,config,completed) VALUES(?,?,?,?,?,?,?,1)",
                (str(number),station["id"],"unused",str(path),"ready",number,signature))
    service.choose(station["id"])
    with patch("music_backend.start_music_job") as create,patch.object(radio,"foreground_music_busy",return_value=True):
        service.tick();assert player.active.path==str(cached[0])
        service.play_song("made-1");service.tick()
        assert player.active.path==str(audio) and player.outgoing.path==str(cached[0])
        assert service.station["id"]==station["id"] and not service.manual_mode
        moment[0]=9;player.advance_mix();assert player.outgoing is None
        moment[0]=173;service.tick()
        assert player.active.path==str(cached[1]) and player.outgoing.path==str(audio)
        create.assert_not_called()
    assert audio.read_bytes()==b"existing generated audio"


def test_live_loop_blocks_auto_transition_until_released(tmp_path):
    moment=[0]
    class DJFixture(Deck):
        def seek(self,position):self.offset=position;self.started=self.now()
        def set_tone(self,*values):self.tone=values
        def set_speed(self,value):self.speed=value
    player=radio.Player(lambda:DJFixture(lambda:moment[0]),lambda:moment[0]);first=tmp_path/"first.wav"
    player.play(first);moment[0]=173
    player.dj_control(0,"tone",(8,-3,-6));player.dj_control(0,"speed",1.08);player.dj_control(0,"loop")
    assert player.active.tone==(8,-3,-6) and player.active.speed==1.08
    service=radio.Service(tmp_path,player);service.current={"id":"first","path":str(first)}
    ready=[{"id":"next","path":str(tmp_path/"next.wav")}]
    service._prepare_transition(ready)
    assert not player.mixing and len(ready)==1
    moment[0]=179.95;player.advance_loops();assert player.active.clock()[0]==173
    player.dj_control(0,"loop");assert not player.loops
    player.dj_control(0,"cue");assert player.active.clock()[0]==0


def test_dj_initialization_failure_leaves_basic_song_playback_available(tmp_path):
    _saved_song(tmp_path);player=radio.Player(lambda:Deck(lambda:0),lambda:0)
    service=radio.Service(tmp_path,player);service.dj_setup["status"]="ready"
    with patch.object(player,"enable_dj",side_effect=OSError("fixture missing audio DLL")):
        service.play_song("made-1");service.tick()
    assert service.dj_setup["status"]=="error" and not service.dj_enabled
    assert player.active.opened and service.manual_mode


def test_music_page_song_picker_and_dj_controls_share_service(tmp_path):
    import tkinter as tk
    try:window,controls,service,_=studio_preview_fixture(tmp_path)
    except tk.TclError as error:pytest.skip("Tk display unavailable: "+str(error))
    try:
        base=service.snapshot()
        service.snapshot=lambda:{**base,"songs":[{"id":"finished","title":"My new song"}],
            "dj_setup":{"status":"ready","message":"DJ mixer ready"},"dj_controls_ready":[True,True]}
        service.play_song=lambda job:service.calls.append(("play_song",job))
        service.dj_control=lambda *args:service.calls.append(("dj_control",*args))
        controls.poll();controls.play_finished_song()
        controls.tone_values[1][0].set(8);controls.apply_dj_slider(1,False)
        controls.speed_values[0].set(110);controls.apply_dj_slider(0,True)
        assert ("play_song","finished") in service.calls
        assert ("dj_control",1,"tone",[8.,0.,0.]) in service.calls
        assert ("dj_control",0,"speed",1.1) in service.calls
        assert all(str(w.cget("state"))=="normal" for w in controls.dj_widgets[0])
    finally:window.destroy()


class ManualDeck(Deck):
    def seek(self, seconds):
        self.offset, self.started = seconds, self.now()

    def start(self):
        self.start_gain = self.volume
        super().start()


def test_four_decks_mix_independently_and_keep_paused_decks_paused(tmp_path):
    moment = [0.]
    player = radio.Player(lambda: ManualDeck(lambda: moment[0]), lambda: moment[0])
    for channel in range(4):
        path = tmp_path/f"deck-{channel}.wav"
        path.touch()
        player.load_deck(channel, path)
        player.dj_control(channel, "assign", (-1, 1, 0, 0)[channel])
    for channel in (0, 1, 2):
        player.toggle_deck(channel)
    player.set_crossfade(0)
    assert [deck.volume for deck in player.loaded_decks()] == [65, 0, 65, 65]
    player.set_crossfade(1)
    assert [deck.volume for deck in player.loaded_decks()] == pytest.approx([0, 65, 65, 65])
    player.dj_control(2, "mute")
    player.set_deck_volume(1, 50)
    assert player.studio_decks[2].volume == 0 and player.studio_decks[1].volume == 32.5
    player.pause()
    assert player.studio_paused == {0, 1, 2, 3}
    player.resume()
    assert player.studio_paused == {3}

    song = tmp_path/"finished.wav"
    song.touch()
    incoming = player.mix_studio_song(song, 4)
    assert incoming == 3 and player.studio_decks[3].start_gain == 0
    moment[0] = 2
    player.advance_mix()
    assert player.studio_decks[3].volume > 0 and player.studio_decks[1].volume > 0
    player.pause()
    levels = player.deck_levels.copy()
    moment[0] = 9
    player.advance_mix()
    assert player.deck_levels == levels
    player.resume()
    moment[0] = 11
    player.advance_mix()
    assert not player.mixing and 1 in player.studio_paused and player.studio_decks[3].volume == 65
    player.stop()
    assert not player.loaded_decks()[0].opened and not player.studio_decks


def test_performance_pads_recall_playheads_and_roll_returns_to_unlooped_time(tmp_path):
    moment = [0.]
    player = radio.Player(lambda: ManualDeck(lambda: moment[0]), lambda: moment[0])
    path = tmp_path/"cues.wav"
    path.touch()
    player.load_deck(2, path)
    player.dj_control(2, "seek", 5)
    player.dj_control(2, "cue_set")
    player.dj_control(2, "hotcue", {"slot": 3})
    player.dj_control(2, "beat_jump", 4)
    assert player.deck_clock(player.studio_decks[2])[0] == 7
    player.dj_control(2, "hotcue", {"slot": 3})
    deck = player.studio_decks[2]
    assert deck.clock()[0] == 5 and deck.running and not deck.paused
    player.dj_control(2, "roll_on", 1)
    moment[0] = .6
    player.advance_mix()
    assert deck.clock()[0] == 5
    moment[0] = .8
    player.dj_control(2, "roll_off")
    assert deck.clock()[0] == pytest.approx(5.8) and 2 not in player.loops
    player.dj_control(2, "loop_in")
    moment[0] = 1.8
    player.dj_control(2, "loop_out")
    assert player.loops[2][1:] == pytest.approx((5.8, 6.8))
    player.dj_control(2, "cue")
    assert deck.paused and deck.clock()[0] == 5
    player.dj_control(2, "hotcue", {"slot": 3, "clear": True})
    assert player.hot_cues[2][3] is None
    player.dj_control(2, "eject")
    assert not deck.opened and 2 not in player.studio_decks


def test_sampler_voices_follow_their_channel_and_are_released(tmp_path):
    moment = [0.]
    player = radio.Player(lambda: ManualDeck(lambda: moment[0]), lambda: moment[0])
    path = radio.sampler_sound(tmp_path, 1)
    assert max(radio.waveform_peaks(path)) > .1
    player.play_sample(2, 1, path)
    voice = player.samples[2, 1]
    assert voice.running and voice.start_gain == 52
    player.dj_control(2, "mute")
    assert voice.volume == 0
    player.play_sample(2, 1, path)
    assert not voice.opened and len(player.samples) == 1
    moment[0] = 181
    player.advance_mix()
    assert not player.samples
