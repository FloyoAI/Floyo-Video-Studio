"""Floyo Video Studio — one hosted-safe node to load a video and get its parts.

Design goals (why this exists vs VideoHelperSuite):
  * TRIM BY TIME (start/end seconds), shown alongside frame count — not frame-index.
  * 1-CLICK DOWNSCALE presets (Source / 1080p / 720p / 480p).
  * MEMORY-LIGHT: seek by time + scale + fps-decimate INSIDE the decode graph, so we
    only ever materialise the frames we keep, at the target resolution — no OOM on
    long / 4K clips (the #1 VHS failure mode).
  * NO FILE PATHS: input is an upload-widget filename resolved inside ComfyUI's
    managed input dir (with a traversal guard); paths are never returned or echoed.

Outputs: frames (IMAGE), audio (AUDIO), fps, width, height, duration, frame_count, info.
Pure-ish utility — only dependency is PyAV (bundles ffmpeg). No ML.
"""

import os
import numpy as np
import torch

import folder_paths

try:
    import av
    _HAS_AV = True
    _AV_ERR = ""
except Exception as _e:  # pragma: no cover
    _HAS_AV = False
    _AV_ERR = str(_e)

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".mpg", ".mpeg", ".gif")
QUALITY_PRESETS = {"Source": None, "1080p": 1080, "720p": 720, "480p": 480}


# ───────────────────────── helpers ─────────────────────────
def _list_input_videos():
    try:
        d = folder_paths.get_input_directory()
        out = []
        for name in os.listdir(d):
            if name.lower().endswith(VIDEO_EXTENSIONS) and os.path.isfile(os.path.join(d, name)):
                out.append(name)
        return sorted(out)
    except Exception:
        return []


def _safe_input_path(video_name):
    """Resolve an upload-widget filename to a real file INSIDE the input dir only.

    Rejects path traversal / absolute paths (the CVE-2024-21575 class of bug) and
    never lets a caller reach outside ComfyUI's managed input directory. Raises a
    generic message that never contains the resolved server path.
    """
    if not video_name or not str(video_name).strip():
        raise ValueError("No video selected — upload one first.")
    name = str(video_name).strip()
    # Strip any ComfyUI "[input]"/"subfolder" annotation but keep just a relative name.
    if name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":"):
        raise ValueError("Invalid video reference.")
    if ".." in name.replace("\\", "/").split("/"):
        raise ValueError("Invalid video reference.")
    input_dir = os.path.realpath(folder_paths.get_input_directory())
    candidate = os.path.realpath(os.path.join(input_dir, name))
    if candidate != input_dir and not candidate.startswith(input_dir + os.sep):
        raise ValueError("Invalid video reference.")
    if not os.path.isfile(candidate):
        raise ValueError("Video file not found — upload it again.")
    return candidate


def _even(n):
    n = int(round(float(n)))
    return max(2, n - (n % 2))


def _fmt_time(s):
    s = max(0.0, float(s))
    m = int(s // 60)
    sec = s - m * 60
    return f"{m:02d}:{sec:04.1f}"


def _probe(path):
    """Fast metadata read (no full decode): duration, fps, width, height, frame_count."""
    with av.open(path) as c:
        if not c.streams.video:
            raise ValueError("File has no video stream.")
        v = c.streams.video[0]
        width = int(v.codec_context.width or v.width or 0)
        height = int(v.codec_context.height or v.height or 0)
        fps = float(v.average_rate) if v.average_rate else (float(v.base_rate) if v.base_rate else 0.0)
        if c.duration:
            duration = float(c.duration) / float(av.time_base)
        elif v.duration and v.time_base:
            duration = float(v.duration * v.time_base)
        else:
            duration = 0.0
        frame_count = int(v.frames) if v.frames else (int(round(duration * fps)) if fps else 0)
        has_audio = len(c.streams.audio) > 0
    return {
        "width": width, "height": height,
        "fps": round(fps, 4), "duration": round(duration, 4),
        "frame_count": frame_count, "has_audio": has_audio,
    }


def _silent_audio(sample_rate=44100):
    return {"waveform": torch.zeros((1, 2, 1), dtype=torch.float32), "sample_rate": int(sample_rate)}


def _extract_audio(path, start, end):
    """Decode audio in [start, end) seconds into a ComfyUI AUDIO dict. Best-effort:
    any failure degrades to silence rather than crashing the graph."""
    try:
        with av.open(path) as c:
            if not c.streams.audio:
                return _silent_audio()
            a = c.streams.audio[0]
            sr = int(a.codec_context.sample_rate or a.rate or 44100)
            resampler = av.AudioResampler(format="fltp", layout="stereo", rate=sr)
            if start > 0 and a.time_base:
                try:
                    c.seek(int(start / a.time_base), stream=a)
                except Exception:
                    pass
            chunks = []
            for frame in c.decode(a):
                t = frame.time
                if t is None:
                    continue
                if t < start - 0.05:
                    continue
                if t >= end:
                    break
                for rf in resampler.resample(frame):
                    nd = rf.to_ndarray()  # (channels, samples) planar float
                    if nd.ndim == 1:
                        nd = nd[None, :]
                    chunks.append(nd)
            if not chunks:
                return _silent_audio(sr)
            data = np.concatenate(chunks, axis=1)  # (C, T)
            wav = torch.from_numpy(np.ascontiguousarray(data)).float().unsqueeze(0)  # (1, C, T)
            return {"waveform": wav, "sample_rate": sr}
    except Exception:
        return _silent_audio()


# ───────────────────────── node ─────────────────────────
class FloyoVideoStudio:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                # A plain string the JS turns into an upload button + preview. The
                # value is just a filename; the server guards it to the input dir.
                "video": ("STRING", {"default": "", "video_upload": True,
                                     "tooltip": "Upload a video. Only the filename is used — no server paths."}),
                "start_seconds": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 86400.0, "step": 0.1,
                                            "tooltip": "Trim start (seconds)."}),
                "end_seconds": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 86400.0, "step": 0.1,
                                          "tooltip": "Trim end (seconds). 0 = until the end."}),
                "quality": (list(QUALITY_PRESETS.keys()), {"tooltip": "Downscale preset. Never upscales."}),
                "target_fps": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 240.0, "step": 1.0,
                                         "tooltip": "Output fps. 0 = keep source fps. Lower = fewer frames = less memory."}),
                "frame_cap": ("INT", {"default": 0, "min": 0, "max": 100000,
                                      "tooltip": "Max frames to extract (memory safety). 0 = no cap."}),
                "include_audio": ("BOOLEAN", {"default": True, "tooltip": "Also output the trimmed audio."}),
            }
        }

    RETURN_TYPES = ("IMAGE", "AUDIO", "FLOAT", "INT", "INT", "FLOAT", "INT", "STRING")
    RETURN_NAMES = ("frames", "audio", "fps", "width", "height", "duration", "frame_count", "info")
    OUTPUT_TOOLTIPS = (
        "Extracted frames (image batch).",
        "Trimmed audio (silent if none / disabled).",
        "Output frames-per-second.",
        "Output width (px).", "Output height (px).",
        "Output duration (seconds).",
        "Output frame count — matters for video models needing specific multiples.",
        "Human-readable summary of the trim/quality.",
    )
    FUNCTION = "process"
    CATEGORY = "Floyo/Video"

    @classmethod
    def IS_CHANGED(cls, video, start_seconds, end_seconds, quality, target_fps, frame_cap, include_audio):
        try:
            path = _safe_input_path(video)
            return f"{path}:{os.path.getmtime(path)}:{start_seconds}:{end_seconds}:{quality}:{target_fps}:{frame_cap}:{include_audio}"
        except Exception:
            return float("nan")

    def process(self, video, start_seconds, end_seconds, quality, target_fps, frame_cap, include_audio):
        if not _HAS_AV:
            raise RuntimeError(
                "Floyo Video Studio needs PyAV. Install it on the server: pip install av  "
                f"(import error: {_AV_ERR})")

        path = _safe_input_path(video)
        meta = _probe(path)
        src_fps = meta["fps"] or 30.0
        src_w, src_h = meta["width"], meta["height"]
        duration = meta["duration"] or 0.0

        # Resolve trim window (seconds).
        start = max(0.0, float(start_seconds))
        end = float(end_seconds)
        if end <= 0 or (duration and end > duration):
            end = duration if duration else (start + 10.0)
        if end <= start:
            end = duration if (duration and duration > start) else (start + 1.0 / src_fps * 2)

        # Target resolution — DOWNSCALE only (never upscale), even dims.
        preset_h = QUALITY_PRESETS.get(quality)
        if preset_h and src_h and src_h > preset_h:
            scale = preset_h / float(src_h)
            tw, th = _even(src_w * scale), _even(preset_h)
        else:
            tw, th = _even(src_w or 16), _even(src_h or 16)

        out_fps = float(target_fps) if target_fps and target_fps > 0 else src_fps
        if out_fps <= 0:
            out_fps = src_fps
        step = 1.0 / out_fps if out_fps else 0.0

        frames = []
        try:
            with av.open(path) as c:
                v = c.streams.video[0]
                v.thread_type = "AUTO"
                if start > 0 and v.time_base:
                    try:
                        c.seek(int(start / v.time_base), stream=v)
                    except Exception:
                        pass
                next_capture = start
                for frame in c.decode(v):
                    t = frame.time
                    if t is None:
                        continue
                    if t < start - 1e-4:
                        continue
                    if t >= end:
                        break
                    if step and (t + 1e-4) < next_capture:
                        continue
                    nd = frame.reformat(width=tw, height=th, format="rgb24").to_ndarray()
                    frames.append(nd)
                    next_capture += step if step else 0.0
                    if next_capture < t:
                        next_capture = t + step
                    if frame_cap and len(frames) >= frame_cap:
                        break
        except Exception as e:
            raise RuntimeError(f"Could not decode the selected range: {e}")

        if not frames:
            raise RuntimeError("No frames found in the selected time range — widen start/end.")

        # Build the (N,H,W,3) float tensor with a single pre-allocated buffer and
        # one in-place scale — avoids np.stack's extra uint8 copy + a second
        # float32 copy (a big win when the OUTPUT frames are large). Free each
        # source frame as we go to keep peak memory low on low-end machines.
        n = len(frames)
        fh, fw = frames[0].shape[:2]
        arr = np.empty((n, fh, fw, 3), dtype=np.float32)
        for i in range(n):
            arr[i] = frames[i]
            frames[i] = None
        arr /= 255.0
        images = torch.from_numpy(arr)

        audio = _extract_audio(path, start, end) if include_audio else _silent_audio()

        frame_count = int(images.shape[0])
        out_duration = round(frame_count / out_fps, 3) if out_fps else round(end - start, 3)
        info = (f"{_fmt_time(start)}–{_fmt_time(end)}  ·  {frame_count} frames  ·  "
                f"{tw}×{th}  ·  {out_fps:.0f} fps  ·  {out_duration:.1f}s")

        return (images, audio, float(out_fps), int(tw), int(th), float(out_duration), frame_count, info)


# ───────────────────────── server route (metadata for the JS slider) ─────────────────────────
# Lets the frontend show live timestamp + frame count without exposing any path.
try:
    from server import PromptServer
    from aiohttp import web

    @PromptServer.instance.routes.get("/floyo_vs/probe")
    async def _floyo_vs_probe(request):
        name = request.query.get("filename", "")
        if not _HAS_AV:
            return web.json_response({"error": "PyAV not installed on server."}, status=500)
        try:
            path = _safe_input_path(name)
            return web.json_response(_probe(path))
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    @PromptServer.instance.routes.post("/floyo_vs/upload")
    async def _floyo_vs_upload(request):
        """Save an uploaded video into ComfyUI's input dir and return ONLY its
        filename (never a path). Sanitises the name and avoids overwrites."""
        try:
            reader = await request.multipart()
            field = await reader.next()
            if field is None or field.name != "video":
                return web.json_response({"error": "No video file in request."}, status=400)
            raw = os.path.basename(field.filename or "upload.mp4")
            clean = "".join(ch for ch in raw if ch.isalnum() or ch in "._- ").strip() or "upload.mp4"
            if not clean.lower().endswith(VIDEO_EXTENSIONS):
                clean += ".mp4"
            input_dir = folder_paths.get_input_directory()
            os.makedirs(input_dir, exist_ok=True)
            base, ext = os.path.splitext(clean)
            name = clean
            dest = os.path.join(input_dir, name)
            i = 1
            while os.path.exists(dest):
                name = f"{base}_{i}{ext}"
                dest = os.path.join(input_dir, name)
                i += 1
            with open(dest, "wb") as f:
                while True:
                    chunk = await field.read_chunk()
                    if not chunk:
                        break
                    f.write(chunk)
            return web.json_response({"name": name})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
except Exception:
    # Not running inside a ComfyUI server context (e.g. import-time tooling) — skip.
    pass


NODE_CLASS_MAPPINGS = {"FloyoVideoStudio": FloyoVideoStudio}
NODE_DISPLAY_NAME_MAPPINGS = {"FloyoVideoStudio": "🎬 Floyo Video Studio"}
