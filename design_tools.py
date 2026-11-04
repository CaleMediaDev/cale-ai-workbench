"""Design checks for project files; no account or online inference is needed."""

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from mcp.server import MCPServer

ROOT = Path(__file__).resolve().parent
NODE = ROOT / "Toolbox/node-v26.13.0-win-x64/node.exe"
SCANNER = ROOT / "vendor/avoid-ai-design/detect.mjs"
AI_UI_KIT = ROOT / "vendor/ai-ui"

server = MCPServer(
    "Local design tools",
    instructions=(
        "Use scan_frontend_design on a website or app source folder, inspect the "
        "findings, then render and visually review the result. Check text colors "
        "with check_color_contrast. Scanner findings are suggestions, not proof "
        "of poor design; preserve deliberate product choices. For Astro AI "
        "interfaces, add_astro_ai_ui installs a bundled, local component kit "
        "without accounts or network calls. Use it only when the project uses Astro. "
        "create_space_shooter creates a new standalone offline HTML game starter. "
        "It never overwrites files; customize and test the actual page against the request."
    ),
)


def luminance(value: str) -> float:
    """Convert a six-digit sRGB color to relative luminance."""
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("Use a six-digit hex color such as #15233A.")
    channels = [int(value[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return sum(a * b for a, b in zip(linear, (0.2126, 0.7152, 0.0722)))


@server.tool()
def check_color_contrast(foreground: str, background: str) -> dict:
    """Measure text contrast and report WCAG AA/AAA results for normal and large text."""
    values = sorted((luminance(foreground), luminance(background)))
    ratio = (values[1] + 0.05) / (values[0] + 0.05)
    return {
        "ratio": round(ratio, 2),
        "normal_text_AA": ratio >= 4.5,
        "large_text_AA": ratio >= 3,
        "normal_text_AAA": ratio >= 7,
        "large_text_AAA": ratio >= 4.5,
    }


@server.tool()
def scan_frontend_design(path: str) -> dict:
    """Scan local HTML/CSS/JS/React for generic design patterns; return file and line findings."""
    target = Path(path).resolve()
    if not target.exists() or not (target.is_file() or target.is_dir()):
        raise ValueError("Choose an existing source file or project folder.")
    if not NODE.is_file() or not SCANNER.is_file():
        raise RuntimeError("The local design scanner is not installed yet.")
    result = subprocess.run(
        [str(NODE), str(SCANNER), "--json", str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=90, cwd=target if target.is_dir() else target.parent,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode not in (0, 2):
        raise RuntimeError(result.stderr[-2000:] or "Design scan failed.")
    report = json.loads(result.stdout)
    report["findings"] = report.get("findings", [])[:100]
    report["note"] = "Code signals only. Render the design to check hierarchy, spacing and overall fit."
    return report


@server.tool()
def add_astro_ai_ui(project_path: str) -> dict:
    """Add local AI chat and agent UI components to an existing Astro project.

    Files go under src/vendor/ai-ui so the project's own components are untouched.
    Repeated calls are safe; changed files at that destination are never overwritten.
    """
    project = Path(project_path).resolve()
    package_file = project / "package.json"
    if not package_file.is_file():
        raise ValueError("Choose an existing Astro project folder with package.json.")
    try:
        package = json.loads(package_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("The project's package.json is not valid JSON.") from exc
    dependencies = {
        **package.get("dependencies", {}),
        **package.get("devDependencies", {}),
        **package.get("peerDependencies", {}),
    }
    if "astro" not in dependencies:
        raise ValueError("This component kit requires an Astro project.")
    source = AI_UI_KIT / "src"
    if not source.is_dir() or not (AI_UI_KIT / "LICENSE").is_file():
        raise RuntimeError("The local AI UI component kit is not installed.")
    destination = project / "src" / "vendor" / "ai-ui"
    source_files = sorted(p for p in source.rglob("*") if p.is_file())
    files = [(p, destination / p.relative_to(source)) for p in source_files]
    files.append((AI_UI_KIT / "LICENSE", destination / "LICENSE"))
    conflicts = [str(target) for original, target in files
                 if any(parent.is_symlink() or (parent.exists() and not parent.is_dir()) for parent in target.parents)
                 or target.is_symlink()
                 or (target.exists() and (not target.is_file() or target.read_bytes() != original.read_bytes()))]
    if conflicts:
        raise FileExistsError("Existing AI UI files differ; no files were changed: " + ", ".join(conflicts[:5]))
    added = []
    for original, target in files:
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
            added.append(str(target))
    return {
        "kit": "michaelvereb/ai-ui 1.2.0 (MIT)",
        "folder": str(destination),
        "components": len(list((destination / "components" / "ai-ui").glob("*.astro"))),
        "files_added": len(added),
        "example": "In an Astro page, import Message from '../vendor/ai-ui/components/ai-ui/Message.astro'; import '../vendor/ai-ui/styles/theme.css';",
        "note": "Review component copy and styles for the project. No account, package install, or online service is required by this kit.",
    }


SPACE_SHOOTER_HTML = r'''<!doctype html>
<!-- Cale local starter v1. Theme: CSS variables below. Gameplay: CONFIG and
window.game. Rendering and controls use that same live state. No external assets. -->
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title><style>
:root{color-scheme:dark;--accent:__ACCENT__;--ink:#edf6ff;--muted:#95abc4;--panel:#101e32}
*{box-sizing:border-box}body{display:flow-root;margin:0;min-height:100svh;color:var(--ink);font:15px/1.5 system-ui,sans-serif;background:radial-gradient(ellipse at 20% 0,#17324e,transparent 58%),#080f1d}
button{font:inherit;cursor:pointer;color:var(--ink);background:#172a42;border:1px solid #36506d;border-radius:12px;padding:9px 16px;touch-action:none}
button:hover{border-color:var(--accent)}button:focus-visible{outline:3px solid var(--accent);outline-offset:3px}
main{width:min(100% - 28px,950px);margin:22px auto}.top{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:18px}
.eyebrow{font-size:11px;letter-spacing:.2em;color:var(--accent);font-weight:750}h1{font-size:clamp(24px,4vw,38px);letter-spacing:-.04em;margin:3px 0}p{color:var(--muted);margin:0}
.live{font-size:11px;white-space:nowrap;color:var(--accent);border:1px solid #345064;border-radius:30px;padding:7px 12px}.layout{display:grid;grid-template-columns:max-content minmax(180px,340px);gap:24px;align-items:start;justify-content:center}
.board{width:min(440px,max(190px,calc((100svh - 280px)*.75)));border:1px solid #30445f;border-radius:18px;overflow:hidden;background:#080f1d;box-shadow:0 24px 80px #0007}.hud{display:grid;grid-template-columns:repeat(3,1fr);padding:12px 8px;background:#132137;border-bottom:1px solid #30445f}
.metric{text-align:center;font-size:10px;color:var(--muted);letter-spacing:.13em}.metric span{display:block;font-size:22px;line-height:1.2;letter-spacing:0;font-weight:750;color:var(--ink)}
canvas{width:100%;height:auto;display:block;aspect-ratio:3/4;background:#080f1d}.controls{display:flex;gap:8px;padding:10px;justify-content:center;background:#111e31}.controls button{min-width:62px;font-weight:700}.controls #fire{flex:1;max-width:160px;color:#06121b;background:var(--accent);border-color:transparent}
aside{padding:18px 4px}.tag{display:inline-block;font-size:11px;letter-spacing:.1em;color:var(--accent);margin-bottom:12px}.card{background:#132137a8;border:1px solid #2d425c;border-radius:16px;padding:18px;margin-bottom:14px}
h2{font-size:17px;margin:0 0 8px}.card p{font-size:13px}.key{display:inline-block;min-width:24px;text-align:center;border:1px solid #4b6079;border-radius:5px;margin:2px;padding:1px 5px;color:var(--ink)}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}#status{margin-top:12px;font-size:13px;color:var(--accent)}.note{font-size:11px;margin-top:16px;color:var(--muted)}
@media(max-width:650px){main{max-width:440px;margin:12px auto}.top{margin-bottom:12px}.live,.tag,.note{display:none}.layout{display:flex;flex-direction:column;gap:0;align-items:center}.board{width:min(100%,max(190px,calc((100svh - 320px)*.75)))}aside{width:100%;padding:10px 0 0}.card{display:none}.controls button{min-width:48px;padding:8px 12px}.actions{margin:0}.hud{padding:9px 6px}#status{margin-top:8px}h1{font-size:26px}}
@media(max-height:700px){main{margin:10px auto}.top{margin-bottom:10px}h1{font-size:26px}.note{display:none}aside{padding-top:8px}.card{padding:14px;margin-bottom:10px}}
</style></head><body><main>
<header class="top"><div><div class="eyebrow">CALE / ARCADE</div><h1>__TITLE__</h1><p>Hold the line. Clear the sector.</p></div><div class="live">● FLIGHT SYSTEMS ONLINE</div></header>
<div class="layout"><section class="board" aria-label="Space shooter">
<div class="hud"><div class="metric">SCORE<span id="score">0</span></div><div class="metric">HULL<span id="lives">3</span></div><div class="metric">SECTOR<span id="wave">1</span></div></div>
<canvas id="canvas" width="600" height="800" aria-label="Space shooter playfield">This game needs a browser with Canvas support.</canvas>
<div class="controls"><button id="left" aria-label="Move left">←</button><button id="fire" aria-label="Fire">FIRE</button><button id="right" aria-label="Move right">→</button></div></section>
<aside><span class="tag">LOCAL · OFFLINE · READY</span><div class="card"><h2>Your sector. Your rules.</h2><p>Intercept the incoming fleet. Protect your hull and push through each wave.</p></div>
<div class="card"><h2>Flight controls</h2><p><span class="key">←</span><span class="key">→</span> or <span class="key">A</span><span class="key">D</span> move<br><span class="key">Space</span> fire<br><span class="key">Enter</span> restart</p></div>
<div class="actions"><button id="restart">Restart flight</button><button id="pause" aria-pressed="false">Pause</button></div><p id="status" role="status">Flight active</p><p class="note">Touch controls work too. No downloads or account needed to play.</p></aside></div></main>
<script>
'use strict';
// One live state drives rendering, controls and the public game API.
const CONFIG=__CONFIG__;
const canvas=document.getElementById('canvas'),ctx=canvas.getContext('2d');
const WIDTH=600,HEIGHT=800,held=Object.create(null),touch=Object.create(null);
let paused=false;
function clearKeys(){for(const map of [held,touch])for(const key of Object.keys(map))delete map[key];}
// Rectangles use centered coordinates. Touching edges are not a collision.
function overlaps(a,b){return a.w>0&&a.h>0&&b.w>0&&b.h>0&&Math.abs(a.x-b.x)*2<a.w+b.w&&Math.abs(a.y-b.y)*2<a.h+b.h;}
function setText(id,value){const el=document.getElementById(id),text=String(value);if(el.textContent!==text)el.textContent=text;}
const game=window.game={
  player:null,bullets:[],enemies:[],particles:[],score:0,lives:3,wave:1,playing:true,waveSpawned:false,time:0,cooldown:0,
  reset(){
    this.player={x:300,y:730,w:40,h:42,invincible:0};this.bullets=[];this.enemies=[];this.particles=[];
    this.score=0;this.lives=3;this.wave=1;this.playing=true;this.waveSpawned=false;this.time=0;this.cooldown=0;
    paused=false;clearKeys();setText('pause','Pause');document.getElementById('pause').setAttribute?.('aria-pressed','false');this.hud();
  },
  spawnWave(){
    const columns=Math.min(9,4+this.wave),rows=Math.min(3,1+Math.floor(this.wave/3));
    for(let row=0;row<rows;row++)for(let col=0;col<columns;col++)this.enemies.push({
      x:(col+1)*WIDTH/(columns+1),y:50-row*56,w:34,h:28,speed:28+this.wave*4,hp:1,phase:col+row});
    this.waveSpawned=true;
  },
  burst(x,y,color){for(let i=0;i<10;i++){const angle=i*Math.PI/5;this.particles.push({x,y,vx:Math.cos(angle)*90,vy:Math.sin(angle)*90,life:.4,color});}},
  damage(){if(this.player.invincible>0||!this.playing)return;this.lives=Math.max(0,this.lives-1);this.player.invincible=1.2;
    this.burst(this.player.x,this.player.y,'#ff6e80');if(this.lives===0)this.playing=false;},
  update(dt,keys={}){
    // Simulation time is seconds. Only the animation loop clamps wall-clock gaps.
    if(!Number.isFinite(dt)||dt<0||!this.playing)return;
    this.time+=dt;this.cooldown=Math.max(0,this.cooldown-dt);this.player.invincible=Math.max(0,this.player.invincible-dt);
    const direction=Number(Boolean(keys.ArrowRight||keys.KeyD))-Number(Boolean(keys.ArrowLeft||keys.KeyA));
    this.player.x=Math.max(20,Math.min(WIDTH-20,this.player.x+direction*CONFIG.playerSpeed*dt));
    if(keys.Space&&this.cooldown===0){this.bullets.push({x:this.player.x,y:this.player.y-30,w:4,h:16,speed:460});this.cooldown=.18;}
    for(const bullet of this.bullets)bullet.y-=bullet.speed*dt;
    for(const enemy of this.enemies)enemy.y+=(enemy.speed||0)*dt;
    const spent=new Set(),destroyed=new Set();
    // Resolve actual live objects even at dt=0. A bullet damages one enemy.
    for(const bullet of this.bullets)for(const enemy of this.enemies){
      if(destroyed.has(enemy)||!overlaps(bullet,enemy))continue;
      spent.add(bullet);enemy.hp=(Number.isFinite(enemy.hp)?enemy.hp:1)-1;
      if(enemy.hp<=0){destroyed.add(enemy);this.score+=10;this.burst(enemy.x,enemy.y,CONFIG.accent);}break;
    }
    for(const enemy of this.enemies){
      if(destroyed.has(enemy))continue;
      if(overlaps(enemy,this.player)){this.damage();destroyed.add(enemy);}
      else if(enemy.y-enemy.h/2>HEIGHT){this.damage();destroyed.add(enemy);}
    }
    this.bullets=this.bullets.filter(b=>!spent.has(b)&&b.y+b.h/2>=0);
    this.enemies=this.enemies.filter(e=>!destroyed.has(e));
    for(const p of this.particles){p.x+=p.vx*dt;p.y+=p.vy*dt;p.life-=dt;}
    this.particles=this.particles.filter(p=>p.life>0);
    if(this.playing&&this.enemies.length===0){if(this.waveSpawned){this.wave++;this.waveSpawned=false;}this.spawnWave();}
    this.hud();
  },
  hud(){setText('score',this.score);setText('lives',this.lives);setText('wave',this.wave);
    setText('status',!this.playing?'Flight ended · press Enter to restart':paused?'Paused':'Flight active · sector '+this.wave);}
};
function drawShip(x,y,color){
  ctx.fillStyle=color;ctx.beginPath();ctx.moveTo(x,y-23);ctx.lineTo(x+20,y+18);ctx.lineTo(x,y+9);ctx.lineTo(x-20,y+18);ctx.closePath();ctx.fill();
  ctx.fillStyle='#e7fbff';ctx.beginPath();ctx.moveTo(x,y-12);ctx.lineTo(x+5,y+4);ctx.lineTo(x-5,y+4);ctx.closePath();ctx.fill();
  ctx.fillStyle='#ffb56b';ctx.beginPath();ctx.moveTo(x-6,y+15);ctx.lineTo(x,y+28+Math.sin(game.time*18)*4);ctx.lineTo(x+6,y+15);ctx.fill();
}
function render(){
  // Drawing reads state; it never spawns enemies, moves ships or awards score.
  ctx.fillStyle='#080f1d';ctx.fillRect(0,0,WIDTH,HEIGHT);
  const nebula=ctx.createRadialGradient(400,240,20,300,300,520);nebula.addColorStop(0,'#143654');nebula.addColorStop(1,'#080f1d');ctx.fillStyle=nebula;ctx.fillRect(0,0,WIDTH,HEIGHT);
  for(let i=0;i<100;i++){const size=i%4===0?2:1;ctx.fillStyle=i%3===0?'#a2d7f8':'#536e8a';ctx.fillRect((i*137.13)%WIDTH,(i*81.7+game.time*(18+i%30))%HEIGHT,size,size);}
  ctx.strokeStyle='#18324a';ctx.lineWidth=1;for(let i=0;i<7;i++){ctx.beginPath();ctx.moveTo(300,300);ctx.lineTo(i*120-60,800);ctx.stroke();}
  for(const b of game.bullets){ctx.fillStyle=CONFIG.accent;ctx.shadowColor=CONFIG.accent;ctx.shadowBlur=12;ctx.fillRect(b.x-b.w/2,b.y-b.h/2,b.w,b.h);}ctx.shadowBlur=0;
  for(const e of game.enemies){ctx.fillStyle='#f77d91';ctx.beginPath();ctx.moveTo(e.x-e.w/2,e.y-e.h/2);ctx.lineTo(e.x+e.w/2,e.y-e.h/2);ctx.lineTo(e.x+e.w*.3,e.y+e.h/2);ctx.lineTo(e.x,e.y+e.h*.2);ctx.lineTo(e.x-e.w*.3,e.y+e.h/2);ctx.closePath();ctx.fill();ctx.fillStyle='#ffe6c7';ctx.fillRect(e.x-5,e.y-5,10,5);}
  if(game.player.invincible>0){ctx.strokeStyle=CONFIG.accent;ctx.lineWidth=2;ctx.beginPath();ctx.arc(game.player.x,game.player.y,30,0,Math.PI*2);ctx.stroke();}
  drawShip(game.player.x,game.player.y,CONFIG.accent);
  for(const p of game.particles){ctx.globalAlpha=Math.max(0,p.life/.4);ctx.fillStyle=p.color;ctx.fillRect(p.x-2,p.y-2,4,4);}ctx.globalAlpha=1;
  if(!game.playing||paused){ctx.fillStyle='#080f1dcc';ctx.fillRect(0,0,WIDTH,HEIGHT);ctx.textAlign='center';ctx.fillStyle='#edf6ff';ctx.font='bold 40px system-ui';ctx.fillText(paused?'FLIGHT PAUSED':'SECTOR LOST',300,355);ctx.font='20px system-ui';ctx.fillStyle='#b8ccdf';ctx.fillText(paused?'Press Resume to continue':'Press Enter or Restart flight',300,398);}
}
const allowedKeys=new Set(['ArrowLeft','ArrowRight','KeyA','KeyD','Space','Enter']);
// Keyboard and pointer maps are separate so their simultaneous inputs combine.
addEventListener('keydown',event=>{const key=event.code||event.key;if(!allowedKeys.has(key))return;event.preventDefault();if(key==='Enter'){game.reset();return;}held[key]=true;});
addEventListener('keyup',event=>{delete held[event.code||event.key];});
addEventListener('blur',clearKeys);document.addEventListener('visibilitychange',()=>{if(document.hidden)clearKeys();});
document.getElementById('restart').addEventListener('click',()=>game.reset());
document.getElementById('pause').addEventListener('click',()=>{if(!game.playing)return;paused=!paused;clearKeys();setText('pause',paused?'Resume':'Pause');document.getElementById('pause').setAttribute('aria-pressed',String(paused));game.hud();});
for(const [id,key] of [['left','ArrowLeft'],['right','ArrowRight'],['fire','Space']]){
  const button=document.getElementById(id);button.addEventListener('pointerdown',e=>{e.preventDefault();touch[key]=true;button.setPointerCapture?.(e.pointerId);});
  for(const type of ['pointerup','pointercancel','lostpointercapture'])button.addEventListener(type,()=>{delete touch[key];});
}
game.reset();render();let previous=performance.now();
function frame(now){const dt=Math.min(.05,Math.max(0,(now-previous)/1000));previous=now;const keys={};for(const key of allowedKeys)keys[key]=Boolean(held[key]||touch[key]);if(!paused)game.update(dt,keys);render();requestAnimationFrame(frame);}
requestAnimationFrame(frame);
</script></body></html>'''


@server.tool()
def create_space_shooter(path: str, title: str = "Nebula Guard", accent: str = "#57E6C2") -> dict:
    """Create a new offline HTML space-shooter starter; never overwrite a file.

    This is reusable project code, not model-generated physics or proof that a
    customized game satisfies its entire brief. Render and test the actual file.
    """
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80 or any(ord(c) < 32 for c in title):
        raise ValueError("Use a short, printable game title of 1 to 80 characters.")
    luminance(accent)  # Reuse the strict six-digit color validator.
    chosen = Path(path).absolute()
    if chosen.suffix.lower() != ".html":
        raise ValueError("Choose a new .html file for the offline game.")
    if chosen.exists() or chosen.is_symlink():
        raise FileExistsError("The game file already exists; customize it with normal edits instead of replacing it.")
    target = chosen.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    config = json.dumps({"accent": accent, "playerSpeed": 120}, ensure_ascii=True).replace('<', r'\u003c')
    values = {"__TITLE__": html.escape(title.strip()), "__ACCENT__": accent, "__CONFIG__": config}
    source = re.sub(r"__TITLE__|__ACCENT__|__CONFIG__", lambda match: values[match[0]], SPACE_SHOOTER_HTML)
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(source)
    return {"path": str(target), "starter": "cale-space-shooter-v1", "bytes": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "tests_run": False,
            "public_api": "window.game: live state, update(seconds,keys), reset(); logical size 600x800",
            "next": "Render the actual page and run check_webapp for movement, fire and restart. Use visible for canvas presence, not expect='exists'. Customize the user's requested appearance/features in small edits; retain and rerun checks.",
            "suggested_cases": space_shooter_cases()}


def space_shooter_cases(starting_lives: int = 3) -> list[dict]:
    """Physical keyboard checks on the same live state used by the renderer."""
    return [
        {"keys": [{"key": "ArrowRight", "hold_ms": 200}], "state": {"game.player.x": {"gt": 305}}},
        {"keys": [{"key": "Space", "hold_ms": 60}], "state": {"game.bullets.length": {"gte": 1}}},
        {"keys": [{"key": "Enter"}], "state": {"game.score": 0, "game.lives": starting_lives, "game.wave": 1}}]


def verify_space_shooter(path: str, starting_lives: int = 3) -> dict:
    """Check the default starter contract on the actual page, offline.

    Deterministic simulation checks freeze animation scheduling in one browser
    context; physical input checks use a separate normally animated page. This
    is not a verifier for arbitrary genres or intentionally changed physics.
    """
    from playwright.sync_api import sync_playwright

    import webapp_check

    if type(starting_lives) is not int or not 1 <= starting_lives <= 99:
        raise ValueError("Starting lives must be an integer from 1 to 99.")
    target = Path(path).resolve(strict=True)
    if target.suffix.lower() != ".html" or target.stat().st_size > 100_000:
        raise ValueError("Use an HTML starter file under 100 KB.")
    before = target.read_bytes()
    errors, logic = [], {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(offline=True, viewport={"width": 375, "height": 812})
            context.route("**/*", webapp_check.project_only_route(target.parent))
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)[:240]))
            page.add_init_script("window.requestAnimationFrame=()=>0;window.cancelAnimationFrame=()=>{};")
            page.goto(target.as_uri(), wait_until="load", timeout=15_000)
            logic = page.evaluate(r'''startingLives=>{
const g=window.game,c={};c.live_api=!!g&&typeof g.update==='function'&&typeof g.reset==='function';
if(!c.live_api)return c;
g.reset();c.reset=g.playing&&g.lives===startingLives&&g.score===0&&g.wave===1&&g.player.x===300&&g.bullets.length===0;
g.player.x=140;for(let i=0;i<24;i++)g.update(1/24,{ArrowRight:true});const slow=g.player.x;
g.reset();g.player.x=140;for(let i=0;i<144;i++)g.update(1/144,{ArrowRight:true});
c.elapsed_movement=Math.abs(slow-260)<1e-5&&Math.abs(slow-g.player.x)<1e-5;
g.reset();g.player.x=25;g.update(1,{ArrowLeft:true});c.left=g.player.x===20;
g.reset();g.player.x=575;g.update(1,{ArrowRight:true});c.right=g.player.x===580;
g.reset();g.update(.5,{ArrowLeft:true,ArrowRight:true});c.opposite_keys=g.player.x===300;
g.reset();g.update(0,{Space:true});c.shooting=g.bullets.length>0;
const enemy=(hp=1)=>({x:100,y:200,w:30,h:30,speed:0,hp});
const bullet=(x=100)=>({x,y:200,w:4,h:16,speed:0});
g.reset();g.waveSpawned=false;const noHp=enemy();delete noHp.hp;g.enemies=[noHp];g.bullets=[bullet()];g.update(0,{});c.default_hp_score=g.score===10;
g.reset();g.waveSpawned=false;const armored=enemy(2);g.enemies=[armored];g.bullets=[bullet()];g.update(0,{});c.armored_hp=g.score===0&&armored.hp===1;
g.reset();g.waveSpawned=false;const edge=enemy();g.enemies=[edge];g.bullets=[bullet(117)];g.update(0,{});c.touching_edges=g.score===0&&edge.hp===1;
const hit=()=>{g.enemies=[{x:g.player.x,y:g.player.y,w:30,h:30,speed:0,hp:1}];g.update(0,{});};
g.reset();g.waveSpawned=false;hit();c.damage=g.lives===Math.max(0,startingLives-1)&&g.player.invincible>=1;
hit();c.invulnerability=g.lives===Math.max(0,startingLives-1);g.update(1.3,{});hit();c.shield_expires=g.lives===Math.max(0,startingLives-2);
g.reset();g.lives=1;g.waveSpawned=false;hit();c.gameover=g.lives===0&&!g.playing;
g.reset();g.score=7;g.waveSpawned=true;g.enemies=[];g.update(0,{});c.wave_after_clear=g.wave===2;
g.reset();g.waveSpawned=false;g.enemies=[];g.update(0,{});c.initial_wave=g.wave===1;
g.reset();g.waveSpawned=false;g.enemies=[enemy()];g.bullets=[bullet()];g.update(0,{});
c.hud_matches_state=['score','lives','wave'].every(id=>document.getElementById(id)?.textContent.trim()===String(g[id]));
g.score=100;g.lives=0;g.playing=false;g.reset();c.restart_after_changes=g.playing&&g.score===0&&g.lives===startingLives&&g.bullets.length===0;
return c;}''', starting_lives)
        except Exception as error:
            errors.append(str(error)[:400])
        finally:
            browser.close()
    browser_checks = webapp_check.check(target.parent, {"path": target.name, "mobile_fit": True, "cases": space_shooter_cases(starting_lives)})
    if target.read_bytes() != before:
        errors.append("Game source changed while checking; rerun read-only verification.")
    failed = [name for name, passed in logic.items() if passed is not True]
    if failed:
        errors.append("Default starter behavior failed: " + ", ".join(failed))
    return {"passed": len(logic) == 18 and not failed and not errors and browser_checks["passed"],
            "contract": "cale-space-shooter-v2-requested-lives", "starting_lives": starting_lives, "logic": logic, "browser": browser_checks,
            "source_sha256": hashlib.sha256(before).hexdigest(), "errors": errors[:8]}


def load_space_shooter_spec(folder: Path, spec_path: Path, digest: str) -> dict:
    """Load a checksum/request-bound component contract without executing it."""
    import task_context

    folder = folder.resolve()
    spec_path = spec_path.resolve(strict=True)
    if (not spec_path.is_relative_to(folder) or not spec_path.is_relative_to((folder / ".local-ai").resolve())
            or spec_path.stat().st_size > 8000):
        raise ValueError("Use a small starter check spec in the project's .local-ai folder.")
    raw = spec_path.read_bytes()
    if not re.fullmatch(r"[a-f0-9]{64}", digest) or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Starter acceptance spec changed; regenerate it through check_game_starter.")
    spec = json.loads(raw)
    if (not isinstance(spec, dict) or type(spec.get("version")) is not int
            or not ((spec["version"] == 1 and set(spec) == {"version", "path", "request_hash"})
                    or (spec["version"] == 2 and set(spec) == {"version", "path", "request_hash", "starting_lives"}
                        and type(spec["starting_lives"]) is int and 1 <= spec["starting_lives"] <= 99))
            or not isinstance(spec["path"], str) or not 1 <= len(spec["path"]) <= 512):
        raise ValueError("Invalid starter acceptance contract.")
    if spec["request_hash"] != task_context.request_identity(folder):
        raise ValueError("User instructions changed; refresh starter verification.")
    target = (folder / spec["path"]).resolve(strict=True)
    if not target.is_relative_to(folder) or target.is_relative_to((folder / ".local-ai").resolve()):
        raise ValueError("Starter output must be in the project, outside internal metadata.")
    return spec


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 5 and sys.argv[1] == "--check-space-shooter":
        try:
            folder = Path(sys.argv[2]).resolve()
            spec_path = Path(sys.argv[3]).resolve()
            spec = load_space_shooter_spec(folder, spec_path, sys.argv[4])
            result = verify_space_shooter(str(folder / spec["path"]), spec.get("starting_lives", 3))
            load_space_shooter_spec(folder, spec_path, sys.argv[4])
        except Exception as error:
            result = {"passed": False, "errors": [f"{type(error).__name__}: {str(error)[:400]}"]}
        print(json.dumps(result))
        raise SystemExit(0 if result["passed"] else 1)
    server.run(transport="stdio")
