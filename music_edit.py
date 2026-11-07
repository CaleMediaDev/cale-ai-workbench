"""Small, offline audio edits for songs created or supplied by the user.

FFmpeg is already bundled with the Workbench toolbox through imageio-ffmpeg.
These tools write new files, leaving recordings and generated songs intact.
"""

from __future__ import annotations

import math
import subprocess
import tempfile
import wave
from decimal import Decimal
from pathlib import Path

import imageio_ffmpeg

SUPPORTED = {".wav", ".mp3", ".flac", ".m4a", ".ogg"}
OUTPUTS = {".wav", ".mp3", ".flac"}
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _source(value: str) -> Path:
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.lower() not in SUPPORTED:
        raise ValueError("Choose an existing WAV, MP3, FLAC, M4A or OGG audio file.")
    return path


def _output(value: str, sources: tuple[Path, ...]) -> Path:
    path = Path(value).expanduser().resolve()
    if path.suffix.lower() not in OUTPUTS:
        raise ValueError("Save edited audio as a new WAV, MP3 or FLAC file.")
    if path.exists() or path in sources:
        raise ValueError("Choose a new output filename so existing audio is preserved.")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _run(arguments: list[str], output: Path) -> None:
    # Keep partial audio private, including when subprocess.run times out.
    # Staging beside the destination allows atomic, no-overwrite publication.
    with tempfile.TemporaryDirectory(prefix=".music-edit-", dir=output.parent) as directory:
        staged = Path(directory) / output.name
        command = [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", "-n", *arguments, str(staged)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=3600, creationflags=HIDE)
        if result.returncode or not staged.is_file() or not staged.stat().st_size:
            raise RuntimeError((result.stderr or result.stdout)[-800:] or "Local audio edit failed.")
        if output.suffix.lower() == ".wav":
            try:
                with wave.open(str(staged), "rb") as audio:
                    if audio.getframerate() <= 0:
                        raise RuntimeError("Local audio edit produced an invalid WAV sample rate.")
                    if audio.getnframes() == 0:
                        raise RuntimeError("Local audio edit produced no audio frames.")
                    remaining = audio.getnframes()
                    frame_size = audio.getnchannels() * audio.getsampwidth()
                    # wave derives frame size from channels and sample width,
                    # ignoring the PCM block alignment declared in the header.
                    # Walk RIFF chunks rather than assuming a fixed fmt offset.
                    with staged.open("rb") as header:
                        header.seek(12)
                        while True:
                            chunk = header.read(8)
                            if len(chunk) != 8:
                                raise RuntimeError("Local audio edit produced an invalid WAV header.")
                            size = int.from_bytes(chunk[4:8], "little")
                            if chunk[:4] == b"fmt ":
                                fmt = header.read(16)
                                if len(fmt) != 16 or int.from_bytes(fmt[12:14], "little") != frame_size:
                                    raise RuntimeError("Local audio edit produced an invalid WAV block alignment.")
                                break
                            header.seek(size + (size & 1), 1)
                    while remaining:
                        frames = min(remaining, 65536)
                        if len(audio.readframes(frames)) != frames * frame_size:
                            raise RuntimeError("Local audio edit produced a truncated WAV file.")
                        remaining -= frames
                    if audio.readframes(1):
                        raise RuntimeError("Local audio edit produced an incomplete WAV frame.")
            except (wave.Error, EOFError) as exc:
                raise RuntimeError("Local audio edit produced an invalid WAV file.") from exc
        else:
            # A nonempty compressed file may contain only metadata. Decode it
            # privately and reuse the WAV frame checks before publishing it.
            # The recursive call outputs WAV, so validation cannot recurse again.
            _run(["-xerror", "-i", str(staged), "-map", "0:a:0", "-vn",
                  "-c:a", "pcm_s16le"], Path(directory) / "validation.wav")
        # Unlike replace(), a hard link refuses to overwrite a destination
        # created while FFmpeg was running. Cleanup removes only our staging file.
        output.hardlink_to(staged)


def _seconds(value: float) -> str:
    # FFmpeg duration fields require decimal notation, not float exponents.
    return format(Decimal(str(value)), "f")


def edit_music(source_path: str, output_path: str, start_seconds: float = 0,
               duration_seconds: float | None = None, fade_in_seconds: float = 0,
               fade_out_seconds: float = 0, gain_db: float = 0) -> dict:
    """Trim, fade and adjust gain in one pass; duration is the saved clip length."""
    source = _source(source_path)
    output = _output(output_path, (source,))
    timings = (start_seconds, fade_in_seconds, fade_out_seconds)
    if duration_seconds is not None:
        timings += (duration_seconds,)
    if not all(math.isfinite(value) for value in timings):
        raise ValueError("Start, duration and fade lengths must be finite.")
    if start_seconds < 0 or duration_seconds is not None and duration_seconds <= 0:
        raise ValueError("Start must be nonnegative and duration must be positive.")
    if fade_in_seconds < 0 or fade_out_seconds < 0:
        raise ValueError("Fade lengths must be nonnegative.")
    if fade_out_seconds and duration_seconds is None:
        raise ValueError("Choose a clip duration when using fade-out.")
    if duration_seconds is not None and max(fade_in_seconds, fade_out_seconds) > duration_seconds:
        raise ValueError("A fade cannot be longer than the saved clip.")
    if not -60 <= gain_db <= 24:
        raise ValueError("Gain must be between -60 and +24 dB.")
    filters = []
    if gain_db:
        filters.append(f"volume={gain_db:.17g}dB")
    if fade_in_seconds:
        filters.append(f"afade=t=in:st=0:d={_seconds(fade_in_seconds)}")
    if fade_out_seconds:
        filters.append(f"afade=t=out:st={_seconds(duration_seconds - fade_out_seconds)}:d={_seconds(fade_out_seconds)}")
    args = ["-ss", _seconds(start_seconds), "-i", str(source)]
    if duration_seconds is not None:
        args += ["-t", _seconds(duration_seconds)]
    if filters:
        args += ["-af", ",".join(filters)]
    args += ["-vn"]
    _run(args, output)
    return {"source": str(source), "path": str(output), "edit": "trim/fade/gain"}


def mix_music(background_path: str, foreground_path: str, output_path: str,
              background_gain_db: float = -10, foreground_gain_db: float = 0,
              foreground_start_seconds: float = 0) -> dict:
    """Mix voice or another track over a song; preserve both inputs."""
    background = _source(background_path)
    foreground = _source(foreground_path)
    output = _output(output_path, (background, foreground))
    if background == foreground:
        raise ValueError("Choose two different audio files to mix.")
    if not all(-60 <= gain <= 24 for gain in (background_gain_db, foreground_gain_db)):
        raise ValueError("Track gain must be between -60 and +24 dB.")
    if not 0 <= foreground_start_seconds <= 36000:
        raise ValueError("Foreground start must be between 0 and 10 hours.")
    delay = round(foreground_start_seconds * 1000)
    graph = (f"[0:a]volume={background_gain_db:.17g}dB[bg];"
             f"[1:a]volume={foreground_gain_db:.17g}dB,adelay={delay}:all=1[fg];"
             "[bg][fg]amix=inputs=2:duration=longest:normalize=0[mix]")
    _run(["-i", str(background), "-i", str(foreground), "-filter_complex", graph,
          "-map", "[mix]", "-vn"], output)
    return {"background": str(background), "foreground": str(foreground),
            "path": str(output), "edit": "mix"}

# The integrated DJ player uses the unmodified, separately downloaded LibVLC
# runtime. Audio stays inside Cale: no external player, account or cloud route.
DJ_VERSION = "3.0.24"
DJ_ARCHIVE_SHA256 = "fcf30850371ad10c9373cc4f0f4501e7dee49e3e9ae9f20c72fb2661a1ca6323"
DJ_ARCHIVE_BYTES = 82765861
DJ_URL = "https://download.videolan.org/pub/videolan/vlc/3.0.24/win64/vlc-3.0.24-win64.zip"
_DJ_ENGINES = {}


def _finite(value, name, minimum, maximum):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not minimum<=value<=maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return float(value)


def dj_directory(root):
    return Path(root).resolve() / "Media/dj" / ("vlc-" + DJ_VERSION)


def dj_runtime_ready(root, *, full=False):
    import hashlib, json
    folder = dj_directory(root)
    try:
        record = json.loads((folder / ".cale-runtime.json").read_text(encoding="utf-8"))
        if record["version"] != DJ_VERSION or record["archive_sha256"] != DJ_ARCHIVE_SHA256:
            return False
        files=record["files"]
        if not isinstance(files,dict) or not {"libvlc.dll","libvlccore.dll"}<=files.keys():return False
        for name in files if full else ("libvlc.dll", "libvlccore.dll"):
            path=(folder/name).resolve()
            if not path.is_relative_to(folder) or hashlib.sha256(path.read_bytes()).hexdigest() != files[name]:
                return False
        return (folder/"plugins").is_dir()
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _extract_dj_archive(archive, destination):
    """The caller verifies the pinned archive before this bounded extraction."""
    import stat, zipfile
    from pathlib import PurePosixPath
    destination = Path(destination).resolve()
    if destination.name != "vlc-"+DJ_VERSION:raise ValueError("Unexpected DJ extraction directory.")
    with zipfile.ZipFile(archive) as source:
        entries=source.infolist()
        if len(entries)>10000 or sum(e.file_size for e in entries)>1024**3:
            raise ValueError("DJ runtime archive exceeds its file/size limits.")
        seen=set()
        for entry in entries:
            parts=PurePosixPath(entry.filename).parts
            if (not parts or parts[0] != "vlc-"+DJ_VERSION or ".." in parts
                    or "\\" in entry.filename or ":" in entry.filename
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError("Unexpected DJ runtime archive path.")
            relative=Path(*parts[1:])
            target=(destination/relative).resolve()
            if not target.is_relative_to(destination) or str(relative).casefold() in seen:
                raise ValueError("Duplicate or escaping DJ runtime archive path.")
            seen.add(str(relative).casefold())
        source.extractall(destination.parent)
        # extractall uses the verified archive's single versioned root name.


def ensure_dj_runtime(root, progress=lambda percent, message: None):
    """Install one pinned portable audio runtime, without elevation or a console."""
    import hashlib, json, os, shutil, tempfile, time, uuid
    folder=dj_directory(root);base=folder.parent;base.mkdir(parents=True,exist_ok=True)
    with (base/"setup.lock").open("a+b") as lock:
        if lock.tell()==0:lock.write(b"0");lock.flush()
        lock.seek(0)
        if os.name=="nt":
            import msvcrt
            try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:raise RuntimeError("DJ sound setup is already running. It will finish in the background.")
        else:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if dj_runtime_ready(root,full=True):
            progress(100,"DJ sound ready")
            return str(folder)
        if (Path(root)/"LongTasks/offline-network.json").is_file():
            raise RuntimeError("DJ sound will install when Internet is back on. Basic playback is ready.")
        if os.name!="nt":raise RuntimeError("This DJ sound package needs 64-bit Windows.")
        if shutil.disk_usage(base).free<1024**3:raise RuntimeError("DJ sound setup needs 1 GB of free space.")
        staging=Path(tempfile.mkdtemp(prefix=".dj-setup-",dir=base)).resolve()
        archive=staging/"runtime.zip"
        try:
            progress(2,"Downloading DJ sound · no account needed")
            curl=Path(os.environ.get("SystemRoot",r"C:\Windows"))/"System32/curl.exe"
            args=[str(curl),"--disable","--fail","--location","--proto","=https","--proto-redir","=https",
                "--connect-timeout","20","--max-time","600","--max-filesize",str(DJ_ARCHIVE_BYTES),
                "--silent","--show-error","--output",str(archive),DJ_URL]
            with tempfile.TemporaryFile() as errors:
                process=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=errors,creationflags=HIDE)
                try:
                    while process.poll() is None:
                        size=archive.stat().st_size if archive.exists() else 0
                        progress(min(75,2+int(size/DJ_ARCHIVE_BYTES*73)),"Downloading DJ sound")
                        time.sleep(.25)
                    if process.returncode:
                        errors.seek(0)
                        raise RuntimeError("DJ sound download failed. "+errors.read(500).decode(errors="replace"))
                finally:
                    if process.poll() is None:process.kill();process.wait(timeout=10)
            with archive.open("rb") as stream: digest=hashlib.file_digest(stream,"sha256").hexdigest()
            if archive.stat().st_size!=DJ_ARCHIVE_BYTES or digest!=DJ_ARCHIVE_SHA256:
                raise RuntimeError("DJ sound download did not match the verified official release.")
            progress(80,"Installing DJ sound")
            unpack=staging/("vlc-"+DJ_VERSION)
            _extract_dj_archive(archive,unpack)
            files={p.relative_to(unpack).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                for p in unpack.rglob("*.dll")}
            if not {"libvlc.dll","libvlccore.dll"}<=files.keys() or not (unpack/"plugins").is_dir():
                raise RuntimeError("DJ sound runtime is incomplete.")
            (unpack/".cale-runtime.json").write_text(json.dumps({"version":DJ_VERSION,
                "archive_sha256":DJ_ARCHIVE_SHA256,"files":files},indent=2),encoding="utf-8")
            if folder.exists():
                backup=base/(folder.name+"-repair-"+uuid.uuid4().hex[:8])
                if folder.is_symlink() or folder.resolve().parent!=base.resolve() or backup.resolve().parent!=base.resolve():
                    raise RuntimeError("Unexpected DJ installation location; existing files were preserved.")
                folder.replace(backup)
            unpack.replace(folder)
            progress(100,"DJ sound ready")
            return str(folder)
        finally:
            if staging.parent==base.resolve() and staging.name.startswith(".dj-setup-") and not staging.is_symlink():
                shutil.rmtree(staging)


class _DJEngine:
    """Small typed binding to LibVLC3's public C API; no Qt/player window."""
    def __init__(self, root):
        import ctypes as C, hashlib, json, os
        folder=dj_directory(root)
        if not dj_runtime_ready(root):raise RuntimeError("DJ sound is still being installed.")
        record=json.loads((folder/".cale-runtime.json").read_text(encoding="utf-8"))
        for name,digest in record['files'].items():
            path=(folder/name).resolve()
            if not path.is_relative_to(folder) or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
                raise RuntimeError("DJ sound files need repair.")
        self.search=os.add_dll_directory(str(folder))
        os.environ['VLC_PLUGIN_PATH']=str(folder/"plugins")
        self.lib=C.CDLL(str(folder/"libvlc.dll"))
        pointer=C.c_void_p;integer=C.c_int;wide=C.c_int64;number=C.c_float
        specs={
            'libvlc_new':(pointer,[integer,C.POINTER(C.c_char_p)]),
            'libvlc_release':(None,[pointer]),
            'libvlc_media_new_path':(pointer,[pointer,C.c_char_p]),
            'libvlc_media_parse':(None,[pointer]),
            'libvlc_media_get_duration':(wide,[pointer]),
            'libvlc_media_release':(None,[pointer]),
            'libvlc_media_player_new':(pointer,[pointer]),
            'libvlc_media_player_set_media':(None,[pointer,pointer]),
            'libvlc_media_player_play':(integer,[pointer]),
            'libvlc_media_player_stop':(None,[pointer]),
            'libvlc_media_player_release':(None,[pointer]),
            'libvlc_media_player_set_pause':(None,[pointer,integer]),
            'libvlc_media_player_get_state':(integer,[pointer]),
            'libvlc_media_player_get_time':(wide,[pointer]),
            'libvlc_media_player_get_length':(wide,[pointer]),
            'libvlc_media_player_set_time':(None,[pointer,wide]),
            'libvlc_media_player_set_rate':(integer,[pointer,number]),
            'libvlc_audio_set_volume':(integer,[pointer,integer]),
            'libvlc_audio_equalizer_new':(pointer,[]),
            'libvlc_audio_equalizer_release':(None,[pointer]),
            'libvlc_audio_equalizer_set_amp_at_index':(integer,[pointer,number,C.c_uint]),
            'libvlc_audio_equalizer_set_preamp':(integer,[pointer,number]),
            'libvlc_media_player_set_equalizer':(integer,[pointer,pointer]),
        }
        for name,(result,arguments) in specs.items():
            function=getattr(self.lib,name);function.restype=result;function.argtypes=arguments
        options=[b'--no-video',b'--no-osd',b'--no-media-library',b'--no-metadata-network-access',b'--audio-time-stretch']
        argv=(C.c_char_p*len(options))(*options)
        self.instance=self.lib.libvlc_new(len(options),argv)
        if not self.instance:raise RuntimeError("DJ sound could not initialize the audio device.")


def _dj_engine(root):
    key=str(Path(root).resolve())
    if key not in _DJ_ENGINES:_DJ_ENGINES[key]=_DJEngine(root)
    return _DJ_ENGINES[key]


class DJDeck:
    """A local audio deck with live equalization, pitch-preserving rate and cue."""
    def __init__(self, root, *, _engine=None):
        import uuid
        self.engine=_engine or _dj_engine(root);self.api=self.engine.lib
        self.alias='cale_dj_'+uuid.uuid4().hex;self.path=None;self.opened=False
        self.player=None;self.equalizer=None;self.volume=65.;self.speed=1.;self.tone=(0.,0.,0.)
        self.duration=0;self.started_at=None
        self.trim = 0.
        self.color_filter = 0.
        self.effect = (0., 0., 0.)
        self.effect_depth = 0.

    def open(self,path):
        self.stop();source=_source(str(path));self.path=str(source)
        media=self.api.libvlc_media_new_path(self.engine.instance,str(source).encode('utf-8'))
        if not media:raise RuntimeError("DJ sound could not open this song.")
        try:
            self.api.libvlc_media_parse(media)
            self.duration=max(0,self.api.libvlc_media_get_duration(media)/1000)
            self.player=self.api.libvlc_media_player_new(self.engine.instance)
            self.equalizer=self.api.libvlc_audio_equalizer_new()
            if not self.player or not self.equalizer:raise RuntimeError("DJ sound could not create this deck.")
            self.api.libvlc_media_player_set_media(self.player,media)
            self.opened=True
            self.set_volume(self.volume);self.set_tone(*self.tone);self.set_speed(self.speed)
        except Exception:
            self.stop();raise
        finally:self.api.libvlc_media_release(media)

    def start(self):
        import time
        if not self.player or self.api.libvlc_media_player_play(self.player)!=0:
            self.stop()
            raise RuntimeError("DJ sound could not start this song.")
        self.started_at=time.monotonic()
        self.set_speed(self.speed)
        # Native playback starts asynchronously. Keep the outgoing song at
        # its current gain until the new deck has actually started playing.
        deadline=self.started_at+2
        while time.monotonic()<deadline:
            state=self.api.libvlc_media_player_get_state(self.player)
            if state==3:
                self.set_volume(self.volume)
                return
            if state in (5,6,7):break
            time.sleep(.01)
        self.stop()
        raise RuntimeError("DJ sound could not start this song.")

    def play(self,path):self.open(path);self.start()

    def busy(self):
        import time
        if not self.player:return False
        state=self.api.libvlc_media_player_get_state(self.player)
        if state==7:raise RuntimeError("DJ sound could not decode this song.")
        return state in (1,2,3,4) or state==0 and self.started_at is not None and time.monotonic()-self.started_at<2

    def clock(self):
        if not self.player:return 0,0
        return max(0,self.api.libvlc_media_player_get_time(self.player)/1000),max(self.duration,self.api.libvlc_media_player_get_length(self.player)/1000)

    def pause(self):
        if self.player:self.api.libvlc_media_player_set_pause(self.player,1)

    def resume(self):
        if self.player:self.api.libvlc_media_player_set_pause(self.player,0)

    def set_volume(self,value):
        import time
        self.volume=_finite(value,'Volume',0,100)
        if self.player and self.api.libvlc_audio_set_volume(self.player,round(self.volume))<0:
            # The incoming deck already has its zero gain set before playback.
            # VLC may temporarily refuse updates while creating the audio output;
            # the next fade tick retries without interrupting the outgoing deck.
            if self.started_at is not None and time.monotonic()-self.started_at>=1:
                raise RuntimeError("DJ volume could not be changed.")

    def set_tone(self,bass,mid,treble):
        self.tone=tuple(_finite(v,'Equalizer',-12,12) for v in (bass,mid,treble))
        self._apply_equalizer()

    def set_trim(self, value):
        self.trim = _finite(value, "Channel trim", -12, 12)
        self._apply_equalizer()

    def set_filter(self, value):
        self.color_filter = _finite(value, "Color filter", -1, 1)
        self._apply_equalizer()

    def _apply_equalizer(self):
        """Combine tone and color-filter cuts in the native ten-band equalizer."""
        if self.player:
            for band in range(10):
                section = 0 if band < 3 else 1 if band < 7 else 2
                gain = self.tone[section]+self.effect[section]*self.effect_depth
                distance = band if self.color_filter >= 0 else 9-band
                cut = min(1., max(0., abs(self.color_filter)*10-distance))
                gain = max(-20., min(20., gain-20*cut))
                if self.api.libvlc_audio_equalizer_set_amp_at_index(self.equalizer,gain,band)<0:raise RuntimeError("DJ equalizer could not be changed.")
            peak = max(0., *(tone+effect*self.effect_depth for tone, effect in zip(self.tone, self.effect)))
            self.api.libvlc_audio_equalizer_set_preamp(self.equalizer, max(-20., self.trim-peak))
            if self.api.libvlc_media_player_set_equalizer(self.player,self.equalizer)<0:raise RuntimeError("DJ equalizer could not be applied.")

    def set_effect(self, tone, depth):
        """A separate, reversible tone send leaves the channel EQ intact."""
        if not isinstance(tone, (tuple, list)) or len(tone) != 3:
            raise ValueError("Choose a three-band tone effect.")
        self.effect = tuple(_finite(value, "Effect tone", -12, 12) for value in tone)
        self.effect_depth = _finite(depth, "Effect depth", 0, 1)
        self._apply_equalizer()

    def set_speed(self,value):
        self.speed=_finite(value,'Playback speed',.8,1.2)
        if self.player and self.api.libvlc_media_player_set_rate(self.player,self.speed)<0:raise RuntimeError("This song cannot change playback speed.")

    def seek(self,seconds):
        seconds=_finite(seconds,'Cue position',0,max(0,self.clock()[1]))
        if self.player:self.api.libvlc_media_player_set_time(self.player,round(seconds*1000))

    def stop(self):
        try:
            if self.player:
                self.api.libvlc_media_player_stop(self.player)
                self.api.libvlc_media_player_release(self.player)
            if self.equalizer:self.api.libvlc_audio_equalizer_release(self.equalizer)
        finally:self.player=self.equalizer=None;self.path=None;self.opened=False;self.started_at=None
