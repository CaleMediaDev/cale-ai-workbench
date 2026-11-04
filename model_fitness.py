"""Keep local agent models within hardware and observed speed limits."""

import os
import re
import subprocess
from functools import lru_cache


@lru_cache(maxsize=1)
def gpu_vram_gb():
    """Return usable NVIDIA VRAM; unknown GPUs get a conservative CPU path."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=8,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        values = [
            int(line.strip())
            for line in result.stdout.splitlines()
            if re.fullmatch(r"\s*\d+\s*", line)
        ]
        return sum(values) / 1024 if values else 0.0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def hardware_allows(weights_gb, ram_gb, vram_gb):
    """Avoid severe CPU offload; leave headroom for context and the desktop."""
    if weights_gb <= 0 or ram_gb < weights_gb + 8:
        return False
    return weights_gb <= max(8.0, vram_gb * 2.0)


def profile_allows(item, ram_gb, vram_gb):
    """Check profile-specific GPU headroom before downloading or routing."""
    return (
        ram_gb + 1 >= item.get("ram_gb", 16)
        and vram_gb >= item.get("min_vram_gb", 0)
        and (
            not item.get("download_gb")
            or hardware_allows(item["download_gb"], ram_gb, vram_gb)
        )
    )


GAME_PROBE_VERSION = 3
GAME_BRIEF = """Return JSON with a single key code containing plain JavaScript.
Implement these four pure functions, with no imports, DOM, network, or explanation:
movePlayer(x, direction, seconds, width, speed): return x + direction*speed*seconds,
clamped to [20,width-20]. Do not mutate arguments or assume a frame rate.
overlaps(a,b): centered axis-aligned rectangles {x,y,w,h}; return true only for
positive-area overlap on both axes. Merely touching edges is false.
advanceWave(wave, remaining, spawned): return wave+1 only if spawned is true and
remaining is zero; otherwise return wave. Score must not affect wave progression.
tickCooldown(remaining, seconds): return max(0, remaining-seconds).
All inputs are finite numbers, nonnegative times, width>=40 and direction -1..1.
"""
GAME_CHECKS = r"""
const checks = {};
checks.movement = movePlayer(300,1,0.25,600,240) === 360;
checks.left = movePlayer(30,-1,1,600,240) === 20;
checks.right = movePlayer(570,1,1,600,240) === 580;
checks.still = movePlayer(300,0,1,600,240) === 300;
let a=100,b=100;
for(let i=0;i<60;i++) a=movePlayer(a,1,1/60,600,120);
for(let i=0;i<120;i++) b=movePlayer(b,1,1/120,600,120);
checks.frame_independence = Math.abs(a-b)<1e-7 && Math.abs(a-220)<1e-7;
const r={x:0,y:0,w:20,h:20};
checks.hit = overlaps(r,{x:5,y:5,w:20,h:20}) === true;
checks.edge = overlaps(r,{x:20,y:0,w:20,h:20}) === false;
checks.miss = overlaps(r,{x:0,y:30,w:20,h:20}) === false;
checks.complete = advanceWave(2,0,true) === 3;
checks.incomplete = advanceWave(2,1,true) === 2;
checks.not_spawned = advanceWave(2,0,false) === 2;
checks.cooldown = Math.abs(tickCooldown(0.5,0.2)-0.3)<1e-8;
checks.cooldown_clamp = tickCooldown(0.1,0.5) === 0;
return JSON.stringify(checks);
"""

GAME_BUILD_BRIEF = """Return JSON {"html":"complete offline space shooter HTML"}.
Build a real canvas game with stars, player ship, enemies, bullets, score, lives,
waves, game over and Enter restart. Fit small screens. Arrow keys/A-D move,
Space shoots. No external assets. Use seconds-based timing at 120 pixels/second.
Canvas logical size is 600x800, player center starts x=300, half-width=20.
Expose the SAME live state and functions used for gameplay as window.game:
{player:{x,y,w,h,invincible},bullets:[],enemies:[],score,lives,wave,playing,
waveSpawned,update(dt,keys),reset()}. keys contains ArrowLeft/ArrowRight/KeyA/KeyD/Space
booleans. Enemy/bullet objects use centered x,y,w,h and speed, enemy hp defaults1.
update(0,{}) resolves existing collisions. Each killed enemy scores10.
A player hit removes one life with at least1 second invulnerability. At zero
lives playing=false. reset() starts play with score0/lives3/wave1/player.x300
and no bullets. An empty wave advances after waveSpawned=true regardless of
score. Only positive-area rectangle overlap counts. Clear keys on blur.
Return a complete playable game, not just an API or a picture.
"""
GAME_DOM = r"""(()=>{
let pendingFrame=null,drawCalls=0;const nodes={},handlers={};
const gradient={addColorStop(){}};
const drawing=new Proxy({}, {get:(_,k)=>k==='createLinearGradient'||k==='createRadialGradient'?()=>gradient:()=>drawCalls++});
function element(id){return nodes[id]??={style:{},textContent:'',innerHTML:'',width:600,height:800,
 getContext:()=>drawing,addEventListener(){},getBoundingClientRect:()=>({width:600,height:800}),classList:{add(){},remove(){},toggle(){}}};}
Object.assign(globalThis,{window:globalThis,document:{getElementById:element,querySelector:element,addEventListener:(k,f)=>handlers[k]=f},
 innerWidth:1280,innerHeight:900,devicePixelRatio:1,performance:{now:()=>0},
 requestAnimationFrame:f=>{pendingFrame=f;return 1;},cancelAnimationFrame:()=>{},
 addEventListener:(k,f)=>handlers[k]=f,
 __cale_probe:{handlers,draws:()=>drawCalls,frame:()=>pendingFrame}});
})();
"""
GAME_BUILD_CHECKS = r"""
if(typeof window.onload==='function')window.onload();
if(__cale_probe.frame())__cale_probe.frame()(0);
const checks={},g=window.game;
checks.live_api=!!g&&typeof g.update==='function'&&typeof g.reset==='function';
g.reset();checks.reset=g.lives===3&&g.score===0&&g.wave===1&&g.player.x===300&&g.bullets.length===0;
g.player.x=100;for(let i=0;i<60;i++)g.update(1/60,{ArrowRight:true});const a=g.player.x;
g.reset();g.player.x=100;for(let i=0;i<120;i++)g.update(1/120,{ArrowRight:true});const b=g.player.x;
checks.frame_independence=Math.abs(a-b)<1e-6&&Math.abs(a-220)<1e-6;
g.reset();g.player.x=22;g.update(1,{ArrowLeft:true});checks.left=g.player.x===20;
g.reset();g.player.x=578;g.update(1,{ArrowRight:true});checks.right=g.player.x===580;
g.reset();g.update(0,{Space:true});checks.shooting=g.bullets.length>0;
g.reset();g.waveSpawned=false;g.enemies=[{x:300,y:400,w:30,h:30,speed:0,hp:1}];g.bullets=[{x:300,y:400,w:4,h:16,speed:0}];g.update(0,{});
checks.score=g.score===10;
g.reset();g.waveSpawned=false;g.enemies=[{x:g.player.x,y:g.player.y,w:30,h:30,speed:0,hp:1}];g.update(0,{});checks.damage=g.lives===2;
g.enemies=[{x:g.player.x,y:g.player.y,w:30,h:30,speed:0,hp:1}];g.update(0,{});checks.invulnerability=g.lives===2;
g.reset();g.lives=1;g.player.invincible=0;g.waveSpawned=false;g.enemies=[{x:g.player.x,y:g.player.y,w:30,h:30,speed:0,hp:1}];g.update(0,{});checks.gameover=g.playing===false;
g.reset();g.waveSpawned=true;g.enemies=[];g.score=60;g.update(0,{});checks.wave=g.wave===2;
checks.drawing=__cale_probe.draws()>5;checks.blur=typeof __cale_probe.handlers.blur==='function';
return JSON.stringify(checks);
"""


def grade_game_code(code, root, checks=GAME_CHECKS):
    """Check generated pure game functions in a time-bounded Node VM.

    The generated program receives no filesystem, browser, credentials or tools.
    Node's permission mode also denies child processes and filesystem writes.
    """
    import json
    import shutil
    from pathlib import Path

    node = Path(root) / "Toolbox/node-v26.13.0-win-x64/node.exe"
    executable = str(node) if node.is_file() else shutil.which("node")
    if not executable:
        raise RuntimeError("Node runtime is not ready for the local coding check.")
    if not isinstance(code, str) or len(code) > 100000:
        return {"valid_code": False}
    driver = r"""const vm=require('node:vm'),fs=require('node:fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const context=vm.createContext(Object.create(null),{codeGeneration:{strings:false,wasm:false}});
try { console.log(vm.runInContext(input.code+'\n;(()=>{\n'+input.checks+'\n})()',context,{timeout:1000})); }
catch (_) { console.log(JSON.stringify({valid_code:false})); }
"""
    try:
        result = subprocess.run(
            [executable, "--permission", "-e", driver],
            input=json.dumps({"code": code, "checks": checks}),
            capture_output=True,
            text=True,
            timeout=8,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        # Asynchronous work can outlive the VM deadline or terminate Node
        # outside the synchronous driver catch (e.g. an unhandled rejection).
        return {"valid_code": False}
    checks = json.loads(result.stdout)
    return (
        checks
        if isinstance(checks, dict) and len(checks) <= 20
        else {"valid_code": False}
    )


def grade_game_html(html, root):
    """Exercise a generated game with a fixed live-state contract, without a browser.

    DOM/canvas stubs let us check logic and contract compliance only. They do
    not establish appearance, accessibility, viewport fit or real input behavior.
    """
    if not isinstance(html, str) or len(html) > 100000:
        return {"valid_html": False}
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.I | re.S)
    return grade_game_code(GAME_DOM + "\n".join(scripts), root, GAME_BUILD_CHECKS)


def probe_game_coding(source, root):
    """Measure two real local completions against independent game-logic cases.

    Persist public-fixture results, bound to model weights and this PC. This is
    a small routing signal, not a visual game review or a general coding score.
    """
    import json
    import time
    from pathlib import Path

    import model_library

    info = model_library.post("/api/show", {"model": source}, timeout=15)
    if info.get("remote_host") or info.get("remote_model"):
        raise ValueError("Game probes require physically installed local weights.")
    digest = model_library.weight_blob(info)
    samples = []
    for seed in (41, 79):
        started = time.monotonic()
        result = model_library.post(
            "/api/chat",
            {
                "model": source,
                "stream": False,
                "think": False,
                "format": "json",
                "messages": [{"role": "user", "content": GAME_BRIEF}],
                "options": {
                    "num_ctx": 8192,
                    "num_predict": 2048,
                    "temperature": 0.2,
                    "seed": seed,
                },
            },
            timeout=150,
        )
        elapsed = time.monotonic() - started
        try:
            document = json.loads(result.get("message", {}).get("content", ""))
            checks = grade_game_code(document.get("code"), root)
        except (ValueError, AttributeError):
            checks = {"valid_code": False}
        samples.append(
            {
                "seconds": round(elapsed, 3),
                "checks": checks,
                "tokens": int(result.get("eval_count") or 0),
                "load_seconds": round((result.get("load_duration") or 0) / 1e9, 3),
            }
        )
    # Small functions alone cannot establish that a model follows a whole-game
    # request. Keep the full-build result separate so speed cannot hide failure.
    started = time.monotonic()
    built = model_library.post(
        "/api/chat",
        {
            "model": source,
            "stream": False,
            "think": False,
            "format": "json",
            "messages": [{"role": "user", "content": GAME_BUILD_BRIEF}],
            "options": {
                "num_ctx": 16384,
                "num_predict": 6000,
                "temperature": 0.2,
                "seed": 41,
            },
        },
        timeout=180,
    )
    build_seconds = time.monotonic() - started
    try:
        html = json.loads(built.get("message", {}).get("content", "")).get("html")
        build_checks = grade_game_html(html, root)
    except (ValueError, AttributeError):
        build_checks = {"valid_html": False}
    build_passed = len(build_checks) == 13 and all(
        v is True for v in build_checks.values()
    )
    report = {
        "version": GAME_PROBE_VERSION,
        "source": source,
        "weights": digest,
        "vram_gb": round(gpu_vram_gb(), 1),
        "ram_gb": round(model_library.total_ram_gb()),
        "updated": time.time(),
        "passed": all(
            len(s["checks"]) == 13 and all(v is True for v in s["checks"].values())
            for s in samples
        ),
        "seconds": round(sum(s["seconds"] for s in samples) / len(samples), 3),
        "build": {
            "passed": build_passed,
            "seconds": round(build_seconds, 3),
            "checks": build_checks,
            "tokens": int(built.get("eval_count") or 0),
        },
        "samples": samples,
    }
    path = Path(root) / "LongTasks/game-model-probes.json"
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(records, dict):
            records = {}
    except (OSError, ValueError):
        records = {}
    records[source] = report
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(records, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return report


def rank_game_models(ordered, catalog, root, weights=None):
    """Prefer the fastest fully passing local coding probe on this PC.

    Preserve the normal order for untested requests and stale hardware results.
    Context, tool support and hardware gates run in the router before this step.
    """
    import json
    import time
    from pathlib import Path

    import model_library

    fallback_order = [
        alias
        for alias in ordered
        if not any(
            item.get("coding_probe") and item.get("alias") == alias
            for item in catalog.values()
        )
    ]
    try:
        records = json.loads(
            (Path(root) / "LongTasks/game-model-probes.json").read_text()
        )
    except (OSError, ValueError):
        return fallback_order
    if not isinstance(records, dict):
        return fallback_order
    scores = {}
    for alias in ordered:
        item = next((x for x in catalog.values() if x.get("alias") == alias), {})
        record = records.get(item.get("source", ""), {})
        if not isinstance(record, dict):
            continue
        try:
            build = record.get("build", {})
            valid = (
                record.get("version") == GAME_PROBE_VERSION
                and record.get("passed") is True
                and build.get("passed") is True
                and 0 < build.get("seconds", 0) < 300
                and bool(record.get("weights"))
                and record["weights"] == (weights or {}).get(alias)
                and record.get("vram_gb") == round(gpu_vram_gb(), 1)
                and record.get("ram_gb") == round(model_library.total_ram_gb())
                and 0 <= time.time() - record["updated"] < 30 * 86400
                and 0 < record["seconds"] < 300
            )
        except (KeyError, TypeError, AttributeError):
            valid = False
        if valid:
            scores[alias] = build.get("seconds", 0) + 2 * record["seconds"]
    # A newly added coding model is not an automatic game-building choice
    # until it also passes the larger fixture. Existing workers stay available.
    verified_order = [
        alias
        for alias in ordered
        if alias in scores
        or not any(
            item.get("coding_probe") and item.get("alias") == alias
            for item in catalog.values()
        )
    ]
    if not scores:
        return verified_order
    return sorted(
        verified_order, key=lambda alias: (alias not in scores, scores.get(alias, 0))
    )


def game_probe_current(source, root, info=None):
    """Check expiry and weight/hardware identity without starting inference."""
    import json
    import time
    from pathlib import Path

    import model_library

    try:
        records = json.loads(
            (Path(root) / "LongTasks/game-model-probes.json").read_text()
        )
        report = records[source]
        info = info or model_library.post("/api/show", {"model": source}, timeout=15)
        return (
            report.get("version") == GAME_PROBE_VERSION
            and bool(report.get("weights"))
            and report["weights"] == model_library.weight_blob(info)
            and report.get("vram_gb") == round(gpu_vram_gb(), 1)
            and report.get("ram_gb") == round(model_library.total_ram_gb())
            and 0 <= time.time() - report["updated"] < 30 * 86400
            and isinstance(report.get("build", {}).get("passed"), bool)
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def game_build_approved(item, root):
    """Keep an unproven coding candidate from replacing the general main alias."""
    import json
    from pathlib import Path

    try:
        source = item["source"]
        if not game_probe_current(source, root):
            return False
        report = json.loads(
            (Path(root) / "LongTasks/game-model-probes.json").read_text()
        )[source]
        return (
            report.get("passed") is True
            and report.get("build", {}).get("passed") is True
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def measured_speed(alias):
    """Return an established median, or None until three real calls exist."""
    import usage_history

    return usage_history.recent_model_speed(alias)


def suitable(alias, weights_gb, ram_gb, vram_gb, min_tokens_per_second=5.0):
    if not hardware_allows(weights_gb, ram_gb, vram_gb):
        return False
    speed = measured_speed(alias)
    return speed is None or speed >= min_tokens_per_second
