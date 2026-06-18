<div align="center">

# 🎬 Floyo Video Studio

### One node to turn a video into a ready-to-use **VIDEO + frames + audio + fps + frame count** — with time-based trim, 1-click downscale, and exact frame targeting. For **ComfyUI**.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](#-license)
[![ComfyUI](https://img.shields.io/badge/ComfyUI-custom%20node-7C3AED.svg)](https://github.com/comfyanonymous/ComfyUI)
[![Made by Floyo](https://img.shields.io/badge/made%20by-floyo.ai-2C1852.svg)](https://floyo.ai)

</div>

> **TL;DR** — Upload a clip and get a real **`VIDEO`** (plugs straight into video‑to‑video AI nodes) **plus** its `frames`, `audio`, `fps` and `frame_count` from one node. **Trim by seconds** (slider shows timestamp **and** frame number), **downscale in one click**, hit an **exact frame count**, stay **memory‑light** on long/4K clips, and never expose a file path.

---

## ✨ Features

- 🎞️ **Real `VIDEO` output** — hands downstream nodes a native ComfyUI `VIDEO` (file‑backed), so it connects directly to Floyo's video‑to‑video AI nodes, `SaveVideo`, `AudioVideoCombine`, etc. — not just raw frames.
- ⏱️ **Trim by time, not frame index** — drag a range slider in **seconds**; it shows the **timestamp + the frame number** live as you scrub.
- 🎯 **Exact frame targeting** — set `target_frames` to force a precise count (e.g. a model that needs a multiple of 16); fps is derived automatically. Or set `target_fps` instead. *(`target_frames` wins when both are set — industry‑standard: frames = fps × duration.)*
- 📉 **1‑click downscale** — `Source / 1080p / 720p / 480p`. Never upscales.
- 🔎 **Instant source info** — read‑only **FPS / Frames** of the loaded clip, read from the file **header server‑side** (no decoding) — so it stays fast and light **even for 20–30 min 4K uploads on low‑end devices**.
- 🔊 **Audio passthrough** — outputs the trimmed audio (and muxes it into the `VIDEO`); wire it to any audio input.
- 🪶 **Memory‑light** — seeks, scales and drops frames *inside* the decode, so a 3s slice of a long 4K clip never decodes the whole file at full res. No OOM.
- 🔒 **Upload‑only, no file paths** — resolves a filename strictly inside ComfyUI's input dir (traversal‑guarded); nothing path‑like is returned or saved into the workflow. Built for **hosted ComfyUI**.

---

## 📤 Outputs

| Port | Type | What |
| --- | --- | --- |
| `video` | **VIDEO** | The trimmed/scaled clip as a native, file‑backed video — for AI video nodes & `SaveVideo`. |
| `frames` | IMAGE | The trimmed/scaled frame batch. |
| `audio` | AUDIO | The trimmed audio (silent if the source has none or `include_audio` is off). |
| `fps` | FLOAT | Output fps. |
| `frame_count` | INT | Number of output frames. |

## 🎛️ Inputs

| Widget | Type | Does |
| --- | --- | --- |
| `video` | upload | Pick / upload a clip — filename only, no paths. Shows an in‑node preview. |
| `start_seconds` | float | Trim start (seconds). |
| `end_seconds` | float | Trim end (seconds). `0` = end of clip. |
| `quality` | preset | Downscale: `Source / 1080p / 720p / 480p`. Never upscales. |
| `target_fps` | float | Output fps. `0` = keep source. |
| `target_frames` | int | Exact output frame count. `0` = off. **Overrides `target_fps`.** |
| `include_audio` | bool | Also output the trimmed audio. |

> The panel also shows a live **Output** readout (frames · fps · duration) and greys out `target_fps` when `target_frames` is set, so the active mode is always clear.

---

## 📦 Install

**ComfyUI Manager** — search **Floyo Video Studio** → Install.

**Git**
```bash
cd /path/to/ComfyUI/custom_nodes
git clone https://github.com/FloyoAI/Floyo-Video-Studio.git
pip install -r Floyo-Video-Studio/requirements.txt   # PyAV
```
Restart ComfyUI → **Add Node → Floyo → Video → 🎬 Floyo Video Studio**.

> Needs **PyAV** (`av`) — it bundles ffmpeg, so there's no separate binary to install.

---

## 🚀 Quick start

1. Add the node and **upload** a video.
2. Drag the **trim** slider to the range you want (watch the timestamp + frame number).
3. Optionally pick a **quality** preset and/or set `target_fps` **or** `target_frames`.
4. Wire it up:
   - `video` → any **video‑to‑video AI node** or `SaveVideo`
   - `frames` → image nodes · `audio` → audio nodes · `fps` / `frame_count` → wherever a count is needed.

**Example (proven end‑to‑end):** `Floyo Video Studio.video → Seedvr_Upscale_Video → Video URL to Frames → Video Combine`.

---

## 📝 Notes

- **Why a `VIDEO` output:** every Floyo video‑to‑video AI node consumes the native `VIDEO` type. The output is encoded to a temp file so partner uploaders that need a real path work out of the box.
- **`target_frames` vs `target_fps`:** for a fixed trim duration, an exact frame count forces `fps = frames / duration` — matching FFmpeg / NLE retiming / AI video models. (A few fps‑conditioned models, e.g. SVD, treat fps as a motion input — set `target_fps` there instead.)
- **Hosted‑ComfyUI safe:** no path in, no path out; streaming decode keeps memory flat on long/4K clips.

---

## 📄 License

MIT — see [`LICENSE`](LICENSE).

<div align="center">

**Built with care by [Floyo](https://floyo.ai) 💜**

</div>
