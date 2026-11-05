"""Opt-in, local webcam visitor greetings. Frames are never saved or uploaded."""

import argparse
import base64
import json
import os
import math
import queue
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent
CONSENT = ROOT / "Media/camera-consent.json"
_EMIT_LOCK = threading.Lock()


def allowed(now=None):
    """Share the desktop app's expiring camera permission with camera tools."""
    try:
        until = float(json.loads(CONSENT.read_text(encoding="utf-8"))["until"])
        return until > (time.time() if now is None else now)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def owner_alive(pid=None, started=None):
    """Stop watching if the desktop window that enabled the camera disappears."""
    if pid is None:
        return True  # Direct bounded tool and smoke-test invocations.
    try:
        owner = psutil.Process(int(pid))
        return owner.is_running() and abs(owner.create_time() - float(started)) < 1
    except (OSError, ValueError, TypeError, psutil.Error):
        return False


def watch_owner(pid, started):
    """Exit promptly even if a network camera's frame read is blocking."""
    while owner_alive(pid, started):
        time.sleep(0.5)
    os._exit(0)



def start_launcher_guard():
    """A Windows venv redirector can be a separate parent of the real worker."""
    try:
        parent=psutil.Process(os.getpid()).parent()
        if parent is None:raise RuntimeError("The camera launcher already closed.")
        threading.Thread(target=watch_owner,args=(parent.pid,parent.create_time()),daemon=True).start()
    except (psutil.Error,OSError) as error:
        raise RuntimeError("The camera launcher could not be verified.") from error


def stop_owned_avatar_process_tree(process, timeout=3, root=None):
    """Reap the exact spawned family, including a venv redirector's real child.

    No name matching or system-wide process scan is used. PID/start identities
    are captured while the Popen launcher is alive, before any termination.
    """
    import subprocess
    children=[];identities=[]
    if process is None:return identities
    if process.poll() is None:
        try:
            owner=psutil.Process(process.pid)
            identities.append((process.pid,owner.create_time()))
            children=owner.children(recursive=True) if hasattr(owner,"children") else []
            identities.extend((child.pid,child.create_time()) for child in children)
        except (psutil.Error,TypeError,ValueError):pass
        for child in reversed(children):
            try:child.terminate()
            except psutil.NoSuchProcess:pass
        try:process.terminate()
        except OSError:
            if process.poll() is None:raise
        try:process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=timeout)
        if children:
            _,remaining=psutil.wait_procs(children,timeout=timeout)
            for child in remaining:
                try:child.kill()
                except psutil.NoSuchProcess:pass
            if remaining:
                _,remaining=psutil.wait_procs(remaining,timeout=timeout)
                if remaining:raise RuntimeError("The owned camera worker did not exit; a replacement was not started.")
    else:process.wait(timeout=timeout)
    for pid,started in identities:remove_avatar_lease_records(root or ROOT,pid,started)
    return identities

class VisitorState:
    """Require a stable face, then an empty room before greeting again."""

    def __init__(self, cooldown=120):
        self.cooldown = cooldown
        self.present_frames = 0
        self.absent_frames = 0
        self.visitor_present = False
        self.last_greeted = float("-inf")

    def observe(self, face_seen, now):
        if face_seen:
            self.present_frames += 1
            self.absent_frames = 0
            if not self.visitor_present and self.present_frames >= 2:
                self.visitor_present = True
                if now - self.last_greeted >= self.cooldown:
                    self.last_greeted = now
                    return True
        else:
            self.absent_frames += 1
            self.present_frames = 0
            if self.absent_frames >= 5:
                self.visitor_present = False
        return False


class VisitorTracker:
    """Track individual faces and greet once per arrival, subject to cooldown."""

    def __init__(self, cooldown=120, similarity=0.68):
        self.cooldown = cooldown
        self.similarity = similarity
        self.tracks = []
        self.arrival_continuities = []

    def observe(self, features, now, boxes=None):
        self.arrival_continuities=[]
        boxes=[_visitor_box(box) for box in boxes] if isinstance(boxes,list) and len(boxes)==len(features) else [None]*len(features)
        # Keep tracks until five observed misses establish departure, including
        # a stream stall. Retain recently departed faces for cooldown.
        self.tracks = [track for track in self.tracks
                       if track["misses"] < 5
                       or now - track["last_seen"] <= max(self.cooldown, 30)]
        greetings = []
        features = features[:8]
        matched = set()
        assignments = {}
        # Reserve the strongest matches before an ambiguous detection can
        # consume another visitor's track merely by appearing first.
        candidates = sorted(
            (sum(a * b for a, b in zip(feature, track["feature"])),
             feature_index, track_index)
            for feature_index, feature in enumerate(features)
            for track_index, track in enumerate(self.tracks)
            if len(feature) == len(track["feature"])
        )
        for score, feature_index, track_index in reversed(candidates):
            if (score >= self.similarity and feature_index not in assignments
                    and track_index not in matched):
                assignments[feature_index] = track_index
                matched.add(track_index)
        # Strong embedding matches always win. Geometry merely marks an
        # otherwise new greeting candidate as possibly continuous: it does not
        # merge identities, borrow a name or lower recognition thresholds.
        geometric={i:[j for j,track in enumerate(self.tracks)
                       if j not in matched and track['misses']==0
                       and math.isfinite(track['last_greeted'])
                       and 0<=now-track['last_seen']<=1.5
                       and _visitor_overlap(boxes[i],track.get('box'))>=.55]
                   for i in range(len(features)) if i not in assignments}
        continuous={i for i,targets in geometric.items() if len(targets)==1
                    and sum(targets[0] in other for other in geometric.values())==1}
        # Snapshot before updating any track so detection order cannot hide
        # that the original presentation now belongs to another visible face.
        visible_tracks={id(self.tracks[j]) for j in matched}
        for feature_index, feature in enumerate(features):
            if feature_index in assignments:
                track = self.tracks[assignments[feature_index]]
                if track["misses"] >= 5:
                    # A real departure starts a new visit. Decide eligibility
                    # now, so waiting in the room cannot outlast a cooldown.
                    track["hits"] = 1
                    track["pending_greeting"] = now - track["last_greeted"] >= self.cooldown
                    track['presentation_continuity']=False
                    track.pop('presentation_anchor',None)
                else:
                    track["hits"] = (
                        track["hits"] + 1
                        if track["misses"] == 0 and now - track["last_seen"] <= 1.5
                        else 1
                    )
                track["last_seen"] = now
                track["feature"] = feature
                track["misses"] = 0
            else:
                track = {"feature": feature, "last_seen": now,
                         "hits": 1, "last_greeted": float("-inf"),
                         "misses": 0, "pending_greeting": True,
                         'presentation_continuity':feature_index in continuous}
                if feature_index in continuous:
                    anchor=self.tracks[geometric[feature_index][0]]
                    # Only a RAM token and geometry, never a retained track or
                    # feature vector. Tracks outlive this 1.5-second decision.
                    track['presentation_anchor']=(id(anchor),anchor.get('box'),anchor['last_seen'])
                self.tracks.append(track)
                matched.add(len(self.tracks) - 1)
            track['box']=boxes[feature_index]
            if track["hits"] >= 2 and track["pending_greeting"]:
                anchor_context=track.pop('presentation_anchor',None)
                if track.get('presentation_continuity'):
                    # This is temporary greeting suppression, not an identity
                    # match. Recheck its original evidence at decision time:
                    # an occluded second visitor may now be clearly separate.
                    if anchor_context is None:
                        track['presentation_continuity']=False
                    else:
                        anchor_id,anchor_box,anchor_seen=anchor_context
                        track['presentation_continuity']=(
                            anchor_id not in visible_tracks
                            and 0<=now-anchor_seen<=1.5
                            and _visitor_overlap(boxes[feature_index],anchor_box)>=.55
                            and sum(_visitor_overlap(box,anchor_box)>=.55
                                    for box in boxes[:len(features)])==1)
                track["pending_greeting"] = False
                track["last_greeted"] = now
                greetings.append(feature)
                self.arrival_continuities.append(track.get('presentation_continuity',False))
        for index, track in enumerate(self.tracks):
            if index not in matched:
                track["misses"] += 1
        return greetings


def _visitor_box(value):
    if not isinstance(value,(list,tuple)) or len(value)!=4:return None
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in value):return None
    x,y,w,h=value
    return tuple(value) if 0<=x<1 and 0<=y<1 and w>0 and h>0 and x+w<=1.000001 and y+h<=1.000001 else None


def _visitor_overlap(left,right):
    left,right=_visitor_box(left),_visitor_box(right)
    if left is None or right is None:return 0.
    x,y,w,h=left;a,b,c,d=right
    intersection=max(0.,min(x+w,a+c)-max(x,a))*max(0.,min(y+h,b+d)-max(y,b))
    return intersection/(w*h+c*d-intersection)


class VisitorGreetingPolicy:
    """Deduplicate automatic introductions without assigning face identities.

    Unknown people replacing one another in the same position without an
    observed absence remain ambiguous. Known names are evaluated from CURRENT
    embeddings; geometry never supplies a name or authorizes enrollment.
    """
    def __init__(self,cooldown=120):
        self.cooldown=cooldown;self.known={}

    def select(self,names,continuities,now):
        self.known={name:stamp for name,stamp in self.known.items() if now-stamp<self.cooldown}
        selected=[]
        for index,name in enumerate(names):
            if name:
                if name in self.known:continue
                self.known[name]=now;selected.append(name)
                if len(self.known)>256:self.known.pop(min(self.known,key=self.known.get))
            elif not (index<len(continuities) and continuities[index] is True):selected.append(None)
        return selected


def greeting_line(names):
    """Speak once for a group while keeping unknown visitors invited to answer."""
    known = list(dict.fromkeys(name for name in names if name))
    unknown = len(names) - sum(bool(name) for name in names)
    if len(names) == 1:
        if known:
            return f"Hey {known[0]}! Good to see you. Say Hey Cale if you need anything."
        return ("Hey there! I'm Cale. Who are you? If you'd like me to remember "
                "your name and learn your face on this PC, say Hey Cale, then tell me your name.")
    if unknown:
        hello = ("Hey " + " and ".join(known) + ", and hello to everyone else!") if known else "Hey everyone!"
        return (hello + " I'm Cale. Who are you? If you'd like me to learn your "
                "faces, step in front of the camera one at a time and say Hey Cale, then your name.")
    return "Hey " + " and ".join(known) + "! Good to see you. Say Hey Cale if you need anything."


def emit(kind, message=""):
    with _EMIT_LOCK:
        print(json.dumps({"event": kind, "message": message}), flush=True)


def preview_frame(frame, cv2):
    """Encode a small in-memory frame for the desktop's local beach scene."""
    try:
        width = 320
        height = max(1, min(160, round(frame.shape[0] * width / frame.shape[1])))
        small = cv2.resize(frame, (width, height))
        ok, encoded = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return base64.b64encode(encoded.tobytes()).decode("ascii") if ok else ""
    except Exception:
        return ""  # Preview failure must not stop local visitor detection.



def negotiate_capture(camera, cv2, local=True):
    """Request native UHD transport, report driver values rather than upscale."""
    if local:
        for name, value in (("CAP_PROP_FOURCC", getattr(cv2, "VideoWriter_fourcc", lambda *_: 0)(*"MJPG")),
                            ("CAP_PROP_FRAME_WIDTH", 3840), ("CAP_PROP_FRAME_HEIGHT", 2160),
                            ("CAP_PROP_FPS", 30), ("CAP_PROP_BUFFERSIZE", 1), ("CAP_PROP_READ_TIMEOUT_MSEC", 2000)):
            try:
                camera.set(getattr(cv2, name), value)
            except Exception:
                pass  # Unsupported properties are common; actual values win.
    values = []
    for name in ("CAP_PROP_FRAME_WIDTH", "CAP_PROP_FRAME_HEIGHT", "CAP_PROP_FPS"):
        try:
            value = float(camera.get(getattr(cv2, name)))
            values.append(value if math.isfinite(value) and 0 < value <= 10000 else None)
        except Exception:
            values.append(None)
    return dict(zip(("width", "height", "fps"), values))


def recognition_frame(frame, cv2):
    """Bound face-detector work independently of the native capture resolution."""
    height, width = frame.shape[:2]
    return (cv2.resize(frame, (960, max(1, round(height * 960 / width))))
            if width > 960 else frame)


class LatestFrameWorker:
    """One recognition worker and one pending frame; previews never wait on it."""

    def __init__(self, process):
        self.process = process
        self.pending = queue.Queue(maxsize=1)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, frame):
        if self.stopped.is_set():
            return
        try:
            self.pending.get_nowait()
        except queue.Empty:
            pass
        try:
            self.pending.put_nowait(frame)
        except queue.Full:
            pass

    def _run(self):
        while not self.stopped.is_set():
            try:
                frame = self.pending.get(timeout=0.1)
            except queue.Empty:
                continue
            if not self.stopped.is_set():
                try:
                    self.process(frame)
                except Exception:
                    emit("notice", "Local face recognition could not process this frame.")
                self.stopped.wait(0.35)

    def close(self):
        self.stopped.set()
        try:
            self.pending.get_nowait()
        except queue.Empty:
            pass
        self.thread.join(timeout=2)





def _avatar_read_lease(path):
    try:
        with path.open("rb") as stream:raw=stream.read(4097)
        value=json.loads(raw) if len(raw)<=4096 else None
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}


def _avatar_atomic_lease(path,value):
    temporary=path.with_name(path.name+"-"+uuid.uuid4().hex+".tmp")
    try:
        temporary.write_text(json.dumps(value),encoding="utf-8")
        for attempt in range(5):
            try:os.replace(temporary,path);break
            except PermissionError:
                if attempt==4:raise
                time.sleep(.01)  # A bounded reader can briefly hold the Windows target.
    finally:temporary.unlink(missing_ok=True)


@contextmanager
def _avatar_canonical_lock(directory):
    """Serialize canonical replace/compare/delete across probe/camera owners."""
    with (directory/"avatar-gpu-current.lock").open("a+b") as stream:
        stream.seek(0,os.SEEK_END)
        if stream.tell()==0:stream.write(b"0");stream.flush()
        deadline=time.monotonic()+.5
        while True:
            stream.seek(0)
            try:
                if os.name=="nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic()>=deadline:raise
                time.sleep(.01)
        try:yield
        finally:
            stream.seek(0)
            if os.name=="nt":msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def remove_avatar_lease_records(root,pid,started):
    """Remove only one reaped owner's accounting; never another live session."""
    directory=Path(root)/"LongTasks"
    if not directory.is_dir() or started is None:return
    path=directory/f"avatar-gpu-active-{pid}.json"
    try:
        value=_avatar_read_lease(path)
        if value.get("pid")==pid and value.get("started")==started:path.unlink()
        with _avatar_canonical_lock(directory):
            canonical=directory/"avatar-gpu-current.json"
            value=_avatar_read_lease(canonical)
            if value.get("pid")==pid and value.get("started")==started:canonical.unlink()
    except OSError:pass

class AvatarGpuLease:
    """Account for this camera process's measured GPU allocation, not a lock."""
    def __init__(self):
        self.started=psutil.Process(os.getpid()).create_time()
        self.path=ROOT/"LongTasks"/f"avatar-gpu-active-{os.getpid()}.json"
        self.canonical=self.path.with_name("avatar-gpu-current.json")
        self.phase="starting";self.reserved=2*1024**3
        self.stopped=threading.Event();self.lock=threading.Lock()
        self._write()
        self.thread=threading.Thread(target=self._heartbeat,daemon=True);self.thread.start()

    def _write(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.lock:
            value={"pid":os.getpid(),"started":self.started,"reserved_bytes":self.reserved,"updated":time.time(),"phase":self.phase}
            _avatar_atomic_lease(self.path,value)
            with _avatar_canonical_lock(self.path.parent):
                current=_avatar_read_lease(self.canonical)
                try:
                    newer=current.get("started",0)>self.started and owner_alive(current.get("pid"),current.get("started"))
                except TypeError:newer=False
                # A delayed old heartbeat must not displace a newer live owner.
                if not newer:_avatar_atomic_lease(self.canonical,value)

    def _heartbeat(self):
        while not self.stopped.wait(.5):
            try:self._write()
            except OSError:pass

    def measured(self,probe):
        size=probe.get("gpu_allocation_bytes")
        if probe.get("ok") is not True or isinstance(size,bool) or not isinstance(size,int) or size<0:
            raise RuntimeError("Avatar GPU allocation probe did not pass.")
        with self.lock:self.phase="active";self.reserved=size+512*1024**2
        self._write()

    def close(self):
        self.stopped.set();self.thread.join(timeout=2)
        remove_avatar_lease_records(ROOT,os.getpid(),self.started)


def avatar_heavy_work_active():
    from readiness_checklist import _heavy_gpu_active
    return _heavy_gpu_active(ROOT)


AVATAR_MAX_FRAME_AGE = 0.75


def avatar_frame_fresh(captured_at):
    """Bound age since the capture owner submitted it, not driver-buffer age."""
    return (type(captured_at) in (int,float) and math.isfinite(captured_at)
            and 0 <= time.monotonic()-captured_at <= AVATAR_MAX_FRAME_AGE)


class AvatarFrameWorker:
    """Latest-only neural consumer. Its public output never contains raw frames.

    The camera owner keeps reading while this worker loads/renders. Permission
    is checked again after inference so a late frame cannot outlive consent.
    """
    def __init__(self, profile, parent_pid=None, parent_started=None, virtual_output=False):
        self.profile=Path(profile);self.parent_pid=parent_pid;self.parent_started=parent_started
        self.pending=queue.Queue(maxsize=1);self.stopped=threading.Event()
        self.virtual_output=virtual_output;self.sender=None;self.sender_worker=None
        self.thread=threading.Thread(target=self._run,daemon=True);self.thread.start()

    def authorized(self):
        return not self.stopped.is_set() and allowed() and owner_alive(self.parent_pid,self.parent_started)

    def submit(self,frame):
        if not self.authorized():return
        try:self.pending.get_nowait()
        except queue.Empty:pass
        try:self.pending.put_nowait((frame,time.monotonic()))
        except queue.Full:pass

    def _publish(self, rgb):
        if not self.authorized():return
        import cv2
        # Restrict output to a genuine rendered/neutral RGB array. Never use the
        # input camera object as a fallback when setup or rendering fails.
        if rgb is None or getattr(rgb,"ndim",0)!=3 or rgb.shape[2]!=3:return
        height,width=rgb.shape[:2]
        if rgb.dtype.name!="uint8" or not (1<=width<=1920 and 1<=height<=1080):return
        # Keep the illustrated scene's native aspect and pixels. Only the Tk
        # preview is fitted to its small widget; the bridge receives this same
        # complete scene, rather than a distorted square proxy.
        ok,encoded=cv2.imencode(".jpg",rgb[:,:,::-1],[cv2.IMWRITE_JPEG_QUALITY,92])
        if not ok:return
        payload=encoded.tobytes()
        if len(payload)>2*1024*1024:return
        if self.authorized():emit("frame",base64.b64encode(payload).decode("ascii"))
        if self.sender_worker and self.authorized():self.sender_worker.submit(payload)

    def _run(self):
        engine=None;lease=None;sequence=0;last_neutral=0;last_error_notice=0;paused=False
        last_frame_at=None;last_output_live=False
        try:
            import avatar_video,video_generation
            neutral=avatar_video.neutral_camera_avatar()
            self._publish(neutral)
            if not self.authorized():return
            emit("setup","Loading the local animated Cale camera. Your real camera image is not displayed.")
            if self.virtual_output and self.authorized():
                self.sender_worker=AvatarVirtualOutput()
            while self.authorized():
                if avatar_heavy_work_active():
                    if engine is not None:engine.close();engine=None
                    if lease is not None:lease.close();lease=None
                    if not paused:emit("notice","Animated Cale paused for local music/image GPU work. Your real camera stays hidden.")
                    paused=True
                    if time.monotonic()-last_neutral>=.5:
                        self._publish(neutral);last_neutral=time.monotonic();last_output_live=False
                    self.stopped.wait(.1);continue
                if engine is None:
                    lease=AvatarGpuLease()
                    facts=video_generation._avatar_runtime_facts(self.profile,ROOT)
                    if not self.authorized():break
                    engine=avatar_video.CaleAvatar(self.profile)
                    if not self.authorized():break
                    probe=engine.probe()
                    lease.measured(probe)
                    video_generation._avatar_finish_deferred_probe(ROOT,self.profile,facts,probe,self.authorized)
                    if not self.authorized():break
                    paused=False
                try:frame,captured_at=self.pending.get(timeout=.1)
                except queue.Empty:
                    # A blocked camera read must not leave an old animated face
                    # looking live indefinitely. Publish neutral once per lapse.
                    if last_output_live and not avatar_frame_fresh(last_frame_at):
                        self._publish(neutral);last_neutral=time.monotonic();last_output_live=False
                    continue
                last_frame_at=captured_at
                if not avatar_frame_fresh(captured_at):
                    if time.monotonic()-last_neutral>=.5:
                        self._publish(neutral);last_neutral=time.monotonic();last_output_live=False
                    continue  # Do not spend GPU work on an already old input.
                sequence+=1
                try:rendered=engine.render(frame,sequence=sequence,captured_at=captured_at)
                except Exception:
                    rendered=None
                    if time.monotonic()-last_error_notice>=5:
                        emit("notice","Animated Cale could not process this frame. Your real camera stays hidden.")
                        last_error_notice=time.monotonic()
                if not self.authorized():break
                if not avatar_frame_fresh(captured_at):
                    self._publish(neutral);last_neutral=time.monotonic();last_output_live=False
                elif rendered is not None:
                    self._publish(rendered);last_output_live=True
                elif time.monotonic()-last_neutral>=.5:
                    self._publish(neutral);last_neutral=time.monotonic();last_output_live=False
        except Exception as error:
            try:
                import video_generation
                video_generation._avatar_fail_deferred_probe(ROOT,error,self.authorized)
            except (OSError,ValueError):pass
            emit("error","Animated Cale is unavailable. Your real camera stays hidden; switch to Raw camera only if you want to show it.")
        finally:
            if self.sender_worker:self.sender_worker.close()
            try:
                if engine is not None:engine.close()
            finally:
                if lease is not None:lease.close()

    def close(self):
        self.stopped.set()
        try:self.pending.get_nowait()
        except queue.Empty:pass
        self.thread.join(timeout=2)
        # The process owner reaps this process on stop if inference is blocked;
        # never close a CUDA session concurrently with an outstanding render.


class AvatarVirtualOutput:
    """A separate optional GPL bridge receives only already-rendered JPEGs."""
    def __init__(self):
        self.pending=queue.Queue(maxsize=1);self.stopped=threading.Event();self.process=None
        self.thread=threading.Thread(target=self._run,daemon=True);self.thread.start()

    def submit(self,payload):
        try:self.pending.get_nowait()
        except queue.Empty:pass
        try:self.pending.put_nowait(payload)
        except queue.Full:pass

    def _run(self):
        import subprocess,struct
        try:
            self.process=subprocess.Popen([sys.executable,"-I","-B","-u",str(ROOT/"video_generation.py"),"avatar-virtual-sender",
                "--parent-pid",str(os.getpid()),"--parent-started",str(psutil.Process(os.getpid()).create_time())],stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            while not self.stopped.is_set():
                try:payload=self.pending.get(timeout=.1)
                except queue.Empty:continue
                if self.stopped.is_set():break
                self.process.stdin.write(struct.pack("!I",len(payload))+payload);self.process.stdin.flush()
        except (OSError,ValueError):
            emit("notice","Sharing animated Cale stopped. Install/close the existing OBS virtual camera; your real camera was not shared.")
        finally:self._stop_process()

    def _stop_process(self):
        process=self.process
        if process is not None:
            try:stop_owned_avatar_process_tree(process,timeout=2)
            except Exception:
                try:process.kill();process.wait(timeout=2)
                except Exception:pass

    def close(self):
        self.stopped.set();self._stop_process();self.thread.join(timeout=2)

def watch(device=0, parent_pid=None, parent_started=None, avatar=False, virtual_output=False):
    """Detect faces on-device; release the webcam immediately when permission ends."""
    if not allowed() or not owner_alive(parent_pid, parent_started):
        emit("error", "Camera greeting is off. Turn it on in the app first.")
        return 1
    if parent_pid is not None:
        threading.Thread(target=watch_owner,
                         args=(parent_pid, parent_started), daemon=True).start()
    import cv2

    from camera_network import camera_source, greeting_camera
    from face_identity import ENROLL_REQUEST, FaceModel, enroll

    cascade = cv2.CascadeClassifier(
        str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
    )
    if cascade.empty():
        emit("error", "The local face detector is unavailable.")
        return 1
    source = camera_source(greeting_camera())
    camera = (cv2.VideoCapture(source, cv2.CAP_DSHOW)
              if isinstance(source, int) else cv2.VideoCapture(source))
    if not camera.isOpened():
        camera.release()
        emit("error", "Webcam unavailable. Check Windows camera access or close another camera app.")
        return 1
    if not owner_alive(parent_pid, parent_started):
        camera.release()
        emit("stopped", "Camera greeting stopped because the app closed.")
        return 0
    state = VisitorState()
    tracker = VisitorTracker()
    greeting_policy = VisitorGreetingPolicy()
    enrolling_name = None
    enrolling_features = []
    enrolling_started = 0.0
    face_model = None
    model_initialized = False
    recognition_stopped = threading.Event()

    def recognize(frame):
        nonlocal face_model, model_initialized, enrolling_name, enrolling_features, enrolling_started
        if recognition_stopped.is_set() or not allowed() or not owner_alive(parent_pid, parent_started):
            return
        if not model_initialized:
            model_initialized = True
            emit("setup", "Preparing local face recognition.")
            try:
                face_model = FaceModel()
            except Exception:
                face_model = None  # Face detection and generic greetings still work offline.
        if recognition_stopped.is_set():
            return
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        height, width = gray.shape[:2]
        if width > 640:
            gray = cv2.resize(gray, (640, max(1, round(height * 640 / width))))
        try:
            # Capture stays native; recognition has a separate bounded CPU cost.
            face_frame = recognition_frame(frame, cv2) if face_model else frame
            boxes=None
            observe=getattr(face_model,'observations',None) if face_model else None
            observations=observe(face_frame) if callable(observe) else None
            if isinstance(observations,list) and all(isinstance(item,dict) and 'feature' in item for item in observations):
                features=[item['feature'] for item in observations]
                boxes=[item.get('box') for item in observations]
            else:features = face_model.features(face_frame) if face_model else []
        except Exception:
            face_model = None
            features = []
            emit("notice", "Face recognition is unavailable; generic greetings still work.")
        if face_model:
            seen = bool(features)
        else:
            faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                             minSize=(45, 45))
            seen = len(faces) > 0
        if recognition_stopped.is_set() or not allowed() or not owner_alive(parent_pid, parent_started):
            return
        feature = features[0] if len(features) == 1 else None
        if face_model and ENROLL_REQUEST.is_file():
            try:
                enrolling_name = str(json.loads(ENROLL_REQUEST.read_text(encoding="utf-8"))["name"])
                enrolling_features = []
                enrolling_started = time.monotonic()
            except (OSError, ValueError, KeyError, TypeError):
                pass
            try:
                ENROLL_REQUEST.unlink(missing_ok=True)
            except OSError:
                emit("notice", "The face enrollment request could not be removed.")
        if enrolling_name and time.monotonic() - enrolling_started > 12:
            emit("notice", "I couldn't learn that face. Try introducing yourself while looking at the camera.")
            enrolling_name = None
        if enrolling_name and feature:
            enrolling_features.append(feature)
            if len(enrolling_features) >= 5:
                if enroll(enrolling_name, enrolling_features):
                    emit("enrolled", f"I learned {enrolling_name}'s face locally. I might still make mistakes, so please correct me if I do.")
                enrolling_name = None
        now = time.monotonic()
        if face_model:
            arrivals = tracker.observe(features, now, boxes=boxes)
            if arrivals:
                names=greeting_policy.select([face_model.identify(item) for item in arrivals],tracker.arrival_continuities,now)
                if names:emit("visitor", greeting_line(names))
        elif state.observe(seen, now):
            emit("visitor", greeting_line([None]))
    negotiated = negotiate_capture(camera, cv2, isinstance(source, int))
    worker = LatestFrameWorker(recognize)
    avatar_worker = AvatarFrameWorker(ROOT/"Media/avatar/v1",parent_pid,parent_started,virtual_output) if avatar else None
    dimensions = (f"{int(negotiated['width'])}×{int(negotiated['height'])}" if negotiated['width'] and negotiated['height'] else "resolution unreported")
    fps = f"{negotiated['fps']:g} driver-reported fps" if negotiated['fps'] else "fps unreported"
    mode = "Native 4K requested; device fallback is shown" if isinstance(source, int) else "Network camera native format"
    emit("ready", f"Camera on · {dimensions} · {fps}. {mode}. Local only; no video saved.")
    try:
        failed_reads = 0
        last_preview = 0.0
        first_frame = True
        capture_started = time.monotonic()
        captured_frames = 0
        measured_reported = False
        while allowed() and owner_alive(parent_pid, parent_started):
            try:
                ok, frame = camera.read()
            except cv2.error:
                ok, frame = False, None
            if not ok or frame is None or frame.size == 0:
                failed_reads += 1
                if failed_reads >= 5:
                    emit("error", "The webcam stopped sending frames.")
                    return 1
                time.sleep(0.5)
                continue
            if not allowed() or not owner_alive(parent_pid, parent_started):
                break  # Permission can expire during a device read.
            failed_reads = 0
            if first_frame:
                first_frame = False
                emit("notice", f"Camera frames: {frame.shape[1]}×{frame.shape[0]} native pixels; preview is reduced for responsiveness.")
            captured_frames += 1
            preview_now = time.monotonic()
            elapsed = preview_now - capture_started
            if not measured_reported and elapsed >= 2:
                measured_reported = True
                emit("notice", f"Startup camera sample: {captured_frames / elapsed:.1f} frames/s at {frame.shape[1]}×{frame.shape[0]}; no video saved.")
            if avatar_worker is not None:
                avatar_worker.submit(frame)
            if not avatar and preview_now - last_preview >= 1 / 15:
                payload = preview_frame(frame, cv2)
                if payload:
                    emit("frame", payload)
                last_preview = preview_now
            worker.submit(frame)
            # Drivers normally pace read(); cap a nonblocking backend too.
            time.sleep(0.005)
    finally:
        recognition_stopped.set()
        camera.release()
        worker.close()
        if avatar_worker is not None:avatar_worker.close()
    emit("stopped", "Camera greeting is off.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--avatar",action="store_true")
    parser.add_argument("--virtual-output",action="store_true")
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--parent-started", type=float)
    options = parser.parse_args()
    start_launcher_guard()
    sys.exit(watch(parent_pid=options.parent_pid,
                   parent_started=options.parent_started,avatar=options.avatar,
                   virtual_output=options.virtual_output and options.avatar))
