# Karaoke video renderer

Turns every part in the `PARTS` array of `index.html` into a 1920x1080, 30 fps MP4 (H.264 + AAC) with its MP3.
Parts, names and audio files are read from the page at run time, so adding, renaming or re-timing parts needs no change here.

## Setup

```
pip install -r render/requirements.txt
playwright install chromium        # or: python -m playwright install chromium
```
ffmpeg and ffprobe must be on your PATH.

## Run

```
python render/render.py                      # all parts -> videos/NN-slug.mp4
python render/render.py --only 0 3           # by index...
python render/render.py --only "Studio part 2"   # ...or exact name
python render/render.py --preview 20         # first 20 s of each part -> NN-slug.preview.mp4
python render/render.py --still 5 30 60      # PNG frames at those times -> videos/stills/
python render/render.py --jobs 2             # render 2 parts in parallel
python render/render.py --fps 30 --out some/dir --chrome "C:\path\to\chrome.exe"
```

- Output names are `NN-slug.mp4` (index + slugified part name, e.g. `00-studio-part-1.mp4`).
- A full run (no `--only`/`--preview`/`--still`) deletes `NN-*.mp4` files in the output folder that no longer match a part.
- `--still` prints the highlighted line index and the index expected from `S`, flagging any mismatch.
- Time is driven by a fake clock, not real playback, so frames are exact however slow the machine. Expect a render to take several times the video's length.

## Timings

Videos use the **default timings in `index.html`** (`bpm`, `off`, `split`, `bpm2`, `gapSec`), never tap-sync settings saved in a browser: every render starts with empty `localStorage`. To fix timing, edit the HTML.

## Font

Videos use Inter (`render/fonts/`, SIL OFL licence) so they look the same on every machine.

## If the page changes and the render breaks

The top of `render.py` lists the globals and selectors the renderer depends on; the script checks them and reports what is missing.
