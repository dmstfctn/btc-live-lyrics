"""Render every karaoke part defined in index.html to a 1080p MP4 with its audio.

Usage:  python render/render.py [--only 0 3 | "Studio part 2"] [--fps 30] [--out videos]
                                [--preview SECONDS] [--still SECONDS ...] [--jobs N] [--chrome PATH]

The page is the only source of truth: parts, names and audio files are read from its
PARTS array at run time. Time is driven manually (fake audio clock, manual rAF, manual
scroll easing and stepped CSS transitions), so frames are exact however slow the machine.

--------------------------------------------------------------------------------------
PAGE CONTRACT - things in index.html this script relies on. If the page changes and the
render breaks, check these first (preflight() verifies them and fails with a clear error):
  Globals (reachable by name from evaluated code):
    PARTS   top-level const; each entry has at least {name, src}
    load(n) selects part n (do NOT rely on the #partN hash)
    S       let, array of line start times (seconds) for the loaded part
    cur     let, index of the currently highlighted line
    tick()  re-queues itself with requestAnimationFrame every call
  Elements / selectors:
    #ly     scroll container of lyric lines; tick() calls ly.scrollTo({top})
    .line   lyric rows;  .line.on = current;  .line.pre = pre-roll text;  .tag  tag labels
    .leg    speaker legend
    h1, .tabs, .tp, details   app chrome hidden in the video (title, tabs, transport, sync)
  Audio: a single <audio> element; tick() reads its currentTime.
--------------------------------------------------------------------------------------
"""
import argparse
import concurrent.futures as cf
import math
import pathlib
import re
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
W, H, SCALE = 1280, 720, 1.5  # -> 1920x1080

FONT_CSS = """
@font-face{font-family:"InterPinned";src:url("%s") format("woff2");font-weight:100 900;font-style:normal}
@font-face{font-family:"InterPinned";src:url("%s") format("woff2");font-weight:100 900;font-style:italic}
body,button,input{font-family:"InterPinned",system-ui,sans-serif!important}
"""

# Video-mode styling: hide the app controls, let the lyric panel fill the frame.
CSS = """
h1,.tabs,.tp,details{display:none!important}
body{max-width:1000px!important;padding:28px 40px!important;height:100vh;display:flex;flex-direction:column}
.leg{font-size:17px!important;margin-bottom:14px!important}
#ly{flex:1;height:auto!important;padding:42vh 32px!important;scroll-behavior:auto!important;overflow:hidden!important}
.line{font-size:26px!important}.line.pre span{font-size:23px!important}
.line.on{font-size:38px!important}
.tag{font-size:14px!important}
"""

# Installed before page scripts run: fake clock on the <audio>, manual rAF, cleared saved sync.
INIT = """
window.__t = 0; window.__dur = 0; window.__raf = [];
window.requestAnimationFrame = cb => { window.__raf.push(cb); return 1; };
Object.defineProperty(HTMLMediaElement.prototype, 'currentTime', {get(){return window.__t}, set(v){}, configurable:true});
Object.defineProperty(HTMLMediaElement.prototype, 'duration', {get(){return window.__dur}, configurable:true});
Object.defineProperty(HTMLMediaElement.prototype, 'paused', {get(){return false}, configurable:true});
HTMLMediaElement.prototype.load = function(){}; HTMLMediaElement.prototype.play = function(){return Promise.resolve()};
HTMLMediaElement.prototype.pause = function(){};
try { localStorage.clear(); } catch(e) {}
"""

# One frame: set the clock, run the queued tick() once, step CSS transitions, ease the scroll.
STEP = """([t, dt, first]) => {
  window.__t = t;
  const ly = document.getElementById('ly');
  if (!ly.__patched) { ly.__patched = true; ly.__target = ly.scrollTop;
    ly.scrollTo = o => { ly.__target = o.top; }; }
  const cbs = window.__raf; window.__raf = []; cbs.forEach(cb => cb(t * 1000));
  document.getAnimations().forEach(a => { a.pause(); a.currentTime = Math.min((a.currentTime || 0) + dt * 1000, 1e9); });
  const max = ly.scrollHeight - ly.clientHeight;
  const tgt = Math.max(0, Math.min(max, ly.__target));
  if (first) { ly.__pos = tgt; ly.scrollTop = tgt; }
  else { const cur = ly.__pos ?? ly.scrollTop; ly.__pos = cur + (tgt - cur) * (1 - Math.exp(-dt / 0.12)); ly.scrollTop = ly.__pos; }
}"""

# Everything the video relies on, checked once the page is loaded.
PREFLIGHT = """() => {
  const miss = [];
  for (const s of ['#ly', '.leg', '.line', 'audio']) if (!document.querySelector(s)) miss.push('element ' + s);
  if (typeof PARTS === 'undefined' || !Array.isArray(PARTS)) miss.push('global PARTS');
  if (typeof load !== 'function') miss.push('global load()');
  if (typeof tick !== 'function') miss.push('global tick()');
  if (typeof S === 'undefined') miss.push('global S');
  if (typeof cur === 'undefined') miss.push('global cur');
  return miss;
}"""


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "part"


def ffprobe_duration(p):
    return float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)]).decode())


def launch(pw, chrome):
    return pw.chromium.launch(executable_path=chrome, args=["--force-color-profile=srgb"]) if chrome \
        else pw.chromium.launch(args=["--force-color-profile=srgb"])


def open_part(browser, n, dur):
    """Fresh context -> page with fake clock -> video CSS -> load(n)."""
    ctx = browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=SCALE)
    ctx.add_init_script(INIT)
    page = ctx.new_page()
    page.goto((REPO / "index.html").as_uri())
    page.evaluate(f"window.__dur = {dur}")
    page.evaluate(f"load({n})")
    fonts = (HERE / "fonts" / "InterVariable.woff2").as_uri(), (HERE / "fonts" / "InterVariable-Italic.woff2").as_uri()
    page.add_style_tag(content=FONT_CSS % fonts)
    page.add_style_tag(content=CSS)
    page.evaluate("Promise.all([document.fonts.load('16px InterPinned'),"
                  "document.fonts.load('italic 16px InterPinned'),document.fonts.load('600 16px InterPinned')])"
                  ".then(() => document.fonts.ready)")
    # load() may have been called after the page's own tick chain started; reset frame state.
    page.evaluate("() => { const ly = document.getElementById('ly'); ly.scrollTop = 0; ly.__target = 0; ly.__pos = undefined; cur = -2; }")
    return ctx, page


def discover(chrome):
    with sync_playwright() as pw:
        browser = launch(pw, chrome)
        ctx = browser.new_context(viewport={"width": W, "height": H})
        ctx.add_init_script(INIT)
        page = ctx.new_page()
        page.goto((REPO / "index.html").as_uri())
        miss = page.evaluate(PREFLIGHT)
        if miss:
            sys.exit("index.html no longer matches what the renderer expects (see PAGE CONTRACT in render.py). Missing: "
                     + ", ".join(miss))
        parts = page.evaluate("PARTS.map(p => ({name: p.name, src: p.src}))")
        browser.close()
    return parts


def expected_active(S, t):
    i = -1
    for k, s in enumerate(S):
        if s <= t:
            i = k
    return i


def render_part(job):
    n, name, mp3, dst, fps, preview, stills, chrome = job
    tag = f"[{n:02d} {name}]"
    dur = ffprobe_duration(mp3)
    total = math.ceil(dur * fps)
    if preview:
        total = min(total, math.ceil(preview * fps))
    with sync_playwright() as pw:
        browser = launch(pw, chrome)
        ctx, page = open_part(browser, n, dur)
        bad = page.evaluate(PREFLIGHT)
        if bad:
            raise RuntimeError(f"{tag} page contract broken: {bad}")
        dt = 1 / fps
        if stills:
            S = page.evaluate("S")
            targets = sorted(stills)
            ti, outs = 0, []
            last = max(min(math.floor(t * fps), total_frames(dur, fps) - 1) for t in targets)
            for f in range(last + 1):
                page.evaluate(STEP, [f / fps, dt, f == 0])
                while ti < len(targets) and min(math.floor(targets[ti] * fps), total_frames(dur, fps) - 1) == f:
                    t = f / fps
                    png = dst / f"{n:02d}-{slug(name)}@{t:.2f}s.png"
                    png.write_bytes(page.screenshot(type="png"))
                    got = page.evaluate("cur")
                    exp = expected_active(S, t)
                    print(f"{tag} still t={t:.2f}s -> {png.name}  active line {got} (expected {exp}"
                          f"{'' if got == exp else ' MISMATCH'})", flush=True)
                    ti += 1
            ctx.close(); browser.close()
            return n, None
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps),
               "-c:v", "mjpeg", "-i", "-", "-i", str(mp3)]
        if preview:
            cmd += ["-t", str(total / fps)]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-vf", "scale=1920:1080:flags=lanczos", "-r", str(fps),
                "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(dst)]
        ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        t0, step = time.time(), max(1, total // 10)
        try:
            for f in range(total):
                page.evaluate(STEP, [f / fps, dt, f == 0])
                ff.stdin.write(page.screenshot(type="jpeg", quality=92))
                if f % step == 0 or f == total - 1:
                    el = time.time() - t0
                    print(f"{tag} {f + 1}/{total} frames ({100 * (f + 1) // total}%), {el:.0f}s elapsed", flush=True)
            ff.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        if ff.wait() != 0:
            raise RuntimeError(f"{tag} ffmpeg failed")
        ctx.close(); browser.close()
    print(f"{tag} done -> {dst}", flush=True)
    return n, dst


def total_frames(dur, fps):
    return math.ceil(dur * fps)


def select(parts, tokens):
    if not tokens:
        return list(range(len(parts)))
    chosen = []
    for tok in tokens:
        if tok.isdigit():
            i = int(tok)
            if not 0 <= i < len(parts):
                sys.exit(f"--only {tok}: no such part (0..{len(parts) - 1})")
        else:
            hits = [k for k, p in enumerate(parts) if tok.lower() in (p["name"].lower(), slug(p["name"]))]
            if not hits:
                sys.exit(f'--only "{tok}": no part with that name. Parts: ' + ", ".join(p["name"] for p in parts))
            i = hits[0]
        if i not in chosen:
            chosen.append(i)
    return sorted(chosen)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", nargs="+", metavar="PART", help="part indexes or exact names")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--out", default=str(REPO / "videos"), help="output folder (default videos/)")
    ap.add_argument("--preview", type=float, metavar="SECONDS", help="render only the first N seconds of each part")
    ap.add_argument("--still", type=float, nargs="+", metavar="SECONDS", help="save PNG frames at these times instead of video")
    ap.add_argument("--jobs", type=int, default=1, help="parts to render in parallel")
    ap.add_argument("--chrome", help="path to a Chrome/Chromium executable (default: Playwright's Chromium)")
    a = ap.parse_args()

    out = pathlib.Path(a.out).resolve()
    parts = discover(a.chrome)
    chosen = select(parts, a.only)
    full_run = not (a.only or a.preview or a.still)

    jobs = []
    for n in chosen:
        p = parts[n]
        mp3 = REPO / p["src"]
        if not mp3.is_file():
            sys.exit(f'Part {n} "{p["name"]}": audio file missing: {mp3}')
        base = f"{n:02d}-{slug(p['name'])}"
        if a.still:
            dst = out / "stills"
        else:
            dst = out / (base + (".preview" if a.preview else "") + ".mp4")
        jobs.append((n, p["name"], mp3, dst, a.fps, a.preview, a.still, a.chrome))
    (out / "stills" if a.still else out).mkdir(parents=True, exist_ok=True)

    if full_run:  # remove videos that no longer match a part (renamed / removed / reordered)
        keep = {f"{n:02d}-{slug(p['name'])}.mp4" for n, p in enumerate(parts)}
        for f in out.glob("[0-9][0-9]-*.mp4"):
            if not f.name.endswith(".preview.mp4") and f.name not in keep:
                print(f"removing stale {f.name}")
                f.unlink()

    print(f"{len(jobs)} part(s): " + ", ".join(f"{j[0]}={j[1]!r}" for j in jobs), flush=True)
    if a.jobs > 1 and len(jobs) > 1:
        with cf.ProcessPoolExecutor(max_workers=a.jobs) as ex:
            for fut in cf.as_completed([ex.submit(render_part, j) for j in jobs]):
                fut.result()
    else:
        for j in jobs:
            render_part(j)


if __name__ == "__main__":
    main()
