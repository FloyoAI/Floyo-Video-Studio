<div align="center">

# 🎬 Floyo Video Studio

### One node to load a video and get everything — frames, audio, fps, duration & size — with time-based trim and 1-click downscale, for **ComfyUI**.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](#-license)
[![ComfyUI](https://img.shields.io/badge/ComfyUI-custom%20node-7C3AED.svg)](https://github.com/comfyanonymous/ComfyUI)
[![Made by Floyo](https://img.shields.io/badge/made%20by-floyo.ai-2C1852.svg)](https://floyo.ai)

</div>

> **TL;DR** — Upload a video, get its **frames + audio + fps + width + height + duration + frame count** from a single node. **Trim by start/end time** (with the timestamp *and* the frame number shown live), **downscale in one click** (Source / 1080p / 720p / 480p), stays **memory-light** on long/4K clips, and **never exposes a file path** — built for hosted ComfyUI.

---

## Why this exists

The popular video helper (VideoHelperSuite, 1.6k⭐) trims by **frame index**, runs **out of memory** on big videos, and exposes **file paths** — and nothing combines time-trim + quality presets + safe upload in one clean node. There are open, unanswered community requests for exactly this. Floyo Video Studio fixes all four:

- ⏱️ **Trim by time, not frame index** — drag a range slider in **seconds**; it shows **both the timestamp and the frame number** as you go (frame count matters when a video model needs a specific multiple).
- 📉 **1-click downscale** — `Source / 1080p / 720p / 480p`. Never upscales.
- 🪶 **Memory-light** — seeks to the start time, scales and drops frames *inside* the decode, and only ever holds the frames you keep, at the target resolution. No OOM.
- 🔒 **No file paths** — upload-only; the server resolves just a filename inside ComfyUI's input folder (with a traversal guard) and never returns a path.

---

## 📦 Install

**ComfyUI Manager** — search **Floyo Video Studio** and install.

**Git**
```bash
cd /path/to/ComfyUI/custom_nodes
git clone https://github.com/FloyoAI/Floyo-Video-Studio.git
pip install -r Floyo-Video-Studio/requirements.txt   # PyAV
```
Restart ComfyUI, then add it via **Add Node → Floyo → Video → 🎬 Floyo Video Studio**.

> Needs **PyAV** (`av`), which bundles ffmpeg — no separate binary to install.

---

## 🎛️ The node

**Inputs**
| Widget | Does |
| --- | --- |
| **Upload video** | Pick a video — only the filename is used (no paths). Shows an in-node preview. |
| **Time range** | Drag start/end in **seconds**; live timestamp **+ frame number**. |
| **quality** | Downscale preset: Source / 1080p / 720p / 480p. |
| **target_fps** | Output fps (`0` = source). Lower = fewer frames = less memory. |
| **frame_cap** | Max frames to extract (`0` = no cap) — a hard memory guard. |
| **include_audio** | Also output the trimmed audio. |

**Outputs** — `frames` (IMAGE) · `audio` (AUDIO) · `fps` · `width` · `height` · `duration` · `frame_count` · `info` (a readable summary).

---

## 🔒 Security & performance notes

- **No path in, no path out.** The upload widget stores a filename; the server resolves it strictly inside the input directory and rejects `..` / absolute paths. Nothing path-like is ever returned, previewed, or saved into the workflow.
- **Streaming decode.** Trim + downscale + fps happen in the decode graph, so a 3s slice of a long 4K clip never decodes the whole file at full resolution.

---

## 📄 License

MIT — see [`LICENSE`](LICENSE).

<div align="center">

**Built with care by [Floyo](https://floyo.ai) 💜**

</div>
