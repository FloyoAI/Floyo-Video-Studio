"""Floyo Video Studio — one hosted-safe node to load a video and get its parts.

Design goals (why this exists vs VideoHelperSuite):
  * TRIM BY TIME (start/end seconds), shown alongside frame count — not frame-index.
  * 1-CLICK DOWNSCALE presets (Source / 1080p / 720p / 480p).
  * MEMORY-LIGHT: seek by time + scale + fps-decimate INSIDE the decode graph, so we
    only ever materialise the frames we keep, at the target resolution — no OOM on
    long / 4K clips (the #1 VHS failure mode).
  * NO FILE PATHS: input is an upload-widget filename resolved inside ComfyUI's
    managed input dir (with a traversal guard); paths are never returned or echoed.

Outputs: video (VIDEO), frames (IMAGE), audio (AUDIO), fps (FLOAT), frame_count (INT).
The `video` output is a real, trimmed + downscaled video object — it plugs straight
into Floyo's video-to-video AI nodes and Save Video. `frames` is for per-frame work.
Pure-ish utility — only dependency is PyAV (bundles ffmpeg). No ML.
"""

import asyncio
import os
import urllib.error
import urllib.request
from fractions import Fraction

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

# decord (already in the Floyo image) reads EXACT frames by index very fast —
# only the requested frames are decoded, so an exact-N extraction stays quick even
# on a long video. Used for the target_frames path; PyAV is the fallback.
try:
    import decord
    decord.bridge.set_bridge("native")
    _HAS_DECORD = True
except Exception:
    _HAS_DECORD = False

# Native ComfyUI VIDEO type (comfy_api). Lets us hand downstream nodes a real `video`
# object — the trim + downscale + fps the user picked, baked in — that plugs straight
# into Floyo's video-to-video AI nodes (Kling / Krea / Grok / Pixverse / Lightx) and
# Save Video. It's LAZY: the frames are only encoded when a downstream node actually
# saves / uploads the video, so adding this output costs nothing at run time. Guarded
# so the module still imports on a frontend backend that lacks comfy_api (the static
# RETURN_TYPES below still advertises the port; only execution needs the real API).
try:
    from comfy_api.input_impl import VideoFromComponents, VideoFromFile
    from comfy_api.util import VideoComponents
    _HAS_VIDEO_API = True
except Exception:
    try:
        from comfy_api.latest import InputImpl as _ImplNS, Types as _TypesNS
        VideoFromComponents = _ImplNS.VideoFromComponents
        VideoFromFile = _ImplNS.VideoFromFile
        VideoComponents = _TypesNS.VideoComponents
        _HAS_VIDEO_API = True
    except Exception:
        _HAS_VIDEO_API = False

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".mpg", ".mpeg", ".gif")
QUALITY_PRESETS = {"Source": None, "1080p": 1080, "720p": 720, "480p": 480}
# Internal guard for the "all frames" path so a very long clip can't OOM. Users
# who genuinely need more should downscale or set an exact target_frames.
_ALL_FRAMES_SAFETY_CAP = 12000


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
    """Resolve the video-upload widget value to a real file the SAME way the core
    Load Video / Load Image nodes do (folder_paths.get_annotated_filepath), so a
    file the platform stored in the input folder is reliably found on the
    execution backend. Confined to the managed input/temp dirs (defense-in-depth
    against the CVE-2024-21575 traversal class); no path is ever returned to the
    client."""
    if not video_name or not str(video_name).strip():
        raise ValueError("No video selected — upload one first.")
    name = str(video_name).strip()
    try:
        path = folder_paths.get_annotated_filepath(name)
    except Exception:
        path = os.path.join(folder_paths.get_input_directory(), name)
    if not path or not os.path.isfile(path):
        raise ValueError("Video file not found — upload it again.")
    real = os.path.realpath(path)
    ok = False
    for getter in ("get_input_directory", "get_temp_directory"):
        try:
            base = os.path.realpath(getattr(folder_paths, getter)())
            if real == base or real.startswith(base + os.sep):
                ok = True
                break
        except Exception:
            pass
    if not ok:
        raise ValueError("Invalid video reference.")
    return real


def _even(n):
    n = int(round(float(n)))
    return max(2, n - (n % 2))


def _fmt_time(s):
    s = max(0.0, float(s))
    m = int(s // 60)
    sec = s - m * 60
    return f"{m:02d}:{sec:04.1f}"


def _meta_from_container(c):
    """Pull duration, fps, dims, frame_count, has_audio from an OPEN av container.
    Reads header only — no decode — so it is cheap regardless of clip length."""
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


def _probe(path):
    """Fast metadata read (no full decode): duration, fps, width, height, frame_count."""
    with av.open(path) as c:
        return _meta_from_container(c)


def _url_is_safe(url):
    """SSRF guard for the probe-by-URL route. Only https, and the host must resolve to a
    PUBLIC ip (reject loopback/private/link-local/reserved) so it can't be aimed at an
    internal service. The url is the platform's own preview-video url the browser loaded."""
    try:
        from urllib.parse import urlparse
        import socket
        import ipaddress
        p = urlparse(url)
        if p.scheme != "https" or not p.hostname:
            return False
        infos = socket.getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM)
        if not infos:
            return False
        for _fam, _stype, _proto, _canon, sockaddr in infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
        return True
    except Exception:
        return False


_PROBE_UA = "Mozilla/5.0 (FloyoVideoStudio)"


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-run the SSRF guard on every redirect hop so a public URL can't 30x-bounce the
    fetch onto an internal address (e.g. cloud metadata)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _url_is_safe(newurl):
            raise urllib.error.HTTPError(newurl, code, "Unsafe redirect target", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _HttpRangeReader:
    """Seekable file-like that range-fetches over HTTPS via urllib. jacob's bundled ffmpeg
    has no network protocols, but Python does — so Python pulls the bytes and ffmpeg seeks
    to + reads ONLY the moov/header through this object. Probing a remote clip then costs a
    handful of range requests + ~100 KB regardless of the clip's length. Requires the
    origin to honour HTTP Range (206); otherwise it bails so we never pull a whole file."""

    def __init__(self, url):
        self.url = url
        self.pos = 0
        self._opener = urllib.request.build_opener(_SafeRedirectHandler())
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": _PROBE_UA})
        with self._opener.open(req, timeout=15) as r:
            if getattr(r, "status", 200) != 206:
                raise ValueError("Origin does not support HTTP Range requests.")
            cr = r.headers.get("Content-Range", "")
            self.size = int(cr.split("/")[-1]) if "/" in cr else int(r.headers.get("Content-Length") or 0)
        if self.size <= 0:
            raise ValueError("Could not determine remote video size.")

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        if n <= 0 or self.pos >= self.size:
            return b""
        end = min(self.pos + n, self.size) - 1
        req = urllib.request.Request(
            self.url, headers={"Range": f"bytes={self.pos}-{end}", "User-Agent": _PROBE_UA})
        with self._opener.open(req, timeout=15) as r:
            data = r.read()
        self.pos += len(data)
        return data

    def seek(self, offset, whence=0):
        if whence == 0:
            self.pos = offset
        elif whence == 1:
            self.pos += offset
        elif whence == 2:
            self.pos = self.size + offset
        return self.pos

    def tell(self):
        return self.pos

    def seekable(self):
        return True


def _probe_url(url):
    """Read fps/frame_count/duration/dims from a remote video's HEADER over HTTPS without
    downloading it. A urllib-backed seekable reader feeds ffmpeg, which seeks to + reads
    only the moov — a few range requests + ~100 KB even for a 20-30 min 4K clip — and runs
    server-side, so the user's device does nothing. Used for freshly-uploaded #inputs
    videos not yet on the backend disk."""
    with av.open(_HttpRangeReader(url)) as c:
        return _meta_from_container(c)


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


def _decord_exact_frames(path, start, end, n, tw, th, do_downscale):
    """Read EXACTLY n frames, evenly spaced over [start, end] seconds, directly by
    index using decord. Only the n frames are decoded (fast even on long videos)
    and they are the real frames (no resampling). Downscales at decode when asked.
    Returns a (n, H, W, 3) uint8 RGB array, or None to fall back to PyAV."""
    try:
        from decord import VideoReader, cpu
        if do_downscale:
            vr = VideoReader(path, ctx=cpu(0), width=int(tw), height=int(th), num_threads=0)
        else:
            vr = VideoReader(path, ctx=cpu(0), num_threads=0)
        total = len(vr)
        if total <= 0:
            return None
        fps = float(vr.get_avg_fps() or 25.0)
        sf = max(0, int(round(start * fps)))
        ef = int(round(end * fps)) if end and end > 0 else total
        ef = min(max(ef, sf + 1), total)
        idx = np.linspace(sf, ef - 1, n)
        idx = np.clip(np.round(idx).astype(np.int64), 0, total - 1).tolist()
        batch = vr.get_batch(idx)
        arr = batch.asnumpy() if hasattr(batch, "asnumpy") else np.asarray(batch)
        if arr is None or arr.ndim != 4 or arr.shape[0] != n:
            return None
        return np.ascontiguousarray(arr)
    except Exception:
        return None


# ───────────────────────── node ─────────────────────────
class FloyoVideoStudio:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                # A video-upload combo — EXACTLY like the core Load Video node, so
                # the platform's standard uploader stores the file in the input
                # folder and the execution backend can find it. The frontend
                # renders the upload control from the `video_upload` flag.
                "video": (sorted(_list_input_videos()), {"video_upload": True,
                          "tooltip": "Upload a video — stored in the input folder like Load Video. No server paths."}),
                "start_seconds": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 86400.0, "step": 0.1,
                                            "tooltip": "Trim start (seconds)."}),
                "end_seconds": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 86400.0, "step": 0.1,
                                          "tooltip": "Trim end (seconds). 0 = until the end."}),
                "quality": (list(QUALITY_PRESETS.keys()), {"tooltip": "Downscale preset. Never upscales."}),
                "target_fps": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 240.0, "step": 1.0,
                                         "tooltip": "Output frames-per-second. 0 = keep the video's own fps. Lower = fewer frames."}),
                "target_frames": ("INT", {"default": 0, "min": 0, "max": 100000,
                                          "tooltip": "Output EXACTLY this many frames, evenly sampled across the trim — for models that need a specific count (e.g. 81). Overrides fps. 0 = all frames."}),
                "include_audio": ("BOOLEAN", {"default": True, "tooltip": "Also output the trimmed audio."}),
            }
        }

    RETURN_TYPES = ("VIDEO", "IMAGE", "AUDIO", "FLOAT", "INT")
    RETURN_NAMES = ("video", "frames", "audio", "fps", "frame_count")
    OUTPUT_TOOLTIPS = (
        "The trimmed + downscaled video — wire it straight into a video-to-video AI node "
        "(Kling / Krea / Grok / Pixverse / Lightx…) or Save Video. Carries the audio too.",
        "Extracted frames (image batch) — for per-frame AI / upscale / Video Combine.",
        "Trimmed audio (silent if none / disabled).",
        "Output frames-per-second — wire to Video Combine's frame_rate so the rebuilt video plays at the right speed.",
        "Output frame count.",
    )
    FUNCTION = "process"
    CATEGORY = "Floyo/Video"

    @classmethod
    def IS_CHANGED(cls, video, start_seconds, end_seconds, quality, target_fps, target_frames, include_audio):
        try:
            path = _safe_input_path(video)
            return f"{path}:{os.path.getmtime(path)}:{start_seconds}:{end_seconds}:{quality}:{target_fps}:{target_frames}:{include_audio}"
        except Exception:
            return float("nan")

    def process(self, video, start_seconds, end_seconds, quality, target_fps, target_frames, include_audio):
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

        out_dur = max(1e-6, end - start)
        n_target = int(target_frames) if target_frames else 0

        if n_target > 0:
            # EXACT N frames, evenly sampled across [start, end] — for video models
            # that need a specific frame count (e.g. 81). fps is derived so the clip
            # keeps its real timespan. Sample the centre of each of N equal segments.
            targets = [start + (i + 0.5) * out_dur / n_target for i in range(n_target)]
            out_fps = round(n_target / out_dur, 4)
        else:
            out_fps = float(target_fps) if target_fps and target_fps > 0 else src_fps
            if out_fps <= 0:
                out_fps = src_fps
            step = 1.0 / out_fps if out_fps else 0.0

        # ── FAST PATH: exact-N via decord — decodes ONLY the n requested frames by
        #    index, so an exact extraction stays quick even on a long video, and the
        #    frames are the real ones (no resampling). Used when the count is sparse
        #    vs the window; PyAV handles all-frames / dense / fallback. ──
        do_downscale = bool(preset_h and src_h and src_h > preset_h)
        decord_batch = None
        if n_target > 0 and _HAS_DECORD:
            window_frames = max(1, int(round(out_dur * src_fps)))
            if n_target <= int(window_frames * 0.7) + 2:
                decord_batch = _decord_exact_frames(path, start, end, n_target, tw, th, do_downscale)

        frames = []
        if decord_batch is None:
            try:
                with av.open(path) as c:
                    v = c.streams.video[0]
                    v.thread_type = "AUTO"
                    if start > 0 and v.time_base:
                        try:
                            c.seek(int(start / v.time_base), stream=v)
                        except Exception:
                            pass
                    if n_target > 0:
                        ti = 0
                        last_nd = None
                        for frame in c.decode(v):
                            t = frame.time
                            if t is None or t < start - 1e-4:
                                continue
                            if ti >= n_target:
                                break
                            nd = None
                            while ti < n_target and targets[ti] <= t + 1e-4:
                                if nd is None:
                                    nd = frame.reformat(width=tw, height=th, format="rgb24").to_ndarray()
                                frames.append(nd)
                                last_nd = nd
                                ti += 1
                            if t >= end:
                                break
                        while ti < n_target and last_nd is not None:
                            frames.append(last_nd)
                            ti += 1
                    else:
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
                            if len(frames) >= _ALL_FRAMES_SAFETY_CAP:
                                break  # internal guard so a huge clip can't OOM
            except Exception as e:
                raise RuntimeError(f"Could not decode the selected range: {e}")
            if not frames:
                raise RuntimeError("No frames found in the selected time range — widen start/end.")

        # Build the (N,H,W,3) float tensor. decord gives one contiguous uint8 array;
        # the PyAV path fills a pre-allocated buffer with one in-place scale (frames
        # freed as we go → low peak memory on weak machines).
        if decord_batch is not None:
            arr = decord_batch.astype(np.float32)
            arr /= 255.0
            images = torch.from_numpy(arr)
        else:
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

        # Assemble the headline `video` output: the exact frames we kept (trim +
        # downscale baked in) at the output fps, with the trimmed audio muxed in. This
        # is a lazy container — nothing is encoded until a downstream node saves or
        # uploads it — so it's free to hand out alongside the raw frames. A node that
        # only wants frames simply ignores this port. Only attach real audio (skip the
        # 1-sample silent placeholder) so a no-audio clip yields a clean silent video.
        video_out = None
        if _HAS_VIDEO_API:
            try:
                vid_audio = audio if (include_audio and meta.get("has_audio")) else None
                comps = VideoComponents(
                    images=images,
                    audio=vid_audio,
                    frame_rate=Fraction(out_fps).limit_denominator(1000000),
                )
                vc = VideoFromComponents(comps)
                # Encode to a real temp .mp4 and hand back a *file-backed* VIDEO. Why:
                # Floyo's partner AI nodes upload the clip via video.get_stream_source()
                # -> upload_file(path), which needs a filesystem PATH. A bare
                # VideoFromComponents only yields an in-memory BytesIO, which their
                # uploader rejects ("expected str/bytes/os.PathLike, not BytesIO"). A
                # VideoFromFile exposes a real path, so it works with the partner nodes
                # AND stays fully compatible with Save Video / Video Combine.
                try:
                    import uuid as _uuid
                    tdir = folder_paths.get_temp_directory()
                    os.makedirs(tdir, exist_ok=True)
                    tpath = os.path.join(tdir, f"floyo_vs_{_uuid.uuid4().hex}.mp4")
                    vc.save_to(tpath)
                    video_out = VideoFromFile(tpath)
                except Exception:
                    video_out = vc  # fall back to in-memory (still fine for Save Video)
            except Exception:
                video_out = None

        # video → for video-to-video AI / Save Video; frames → per-frame work; plus the
        # audio, the fps (Video Combine's frame_rate) and the count. The trim/quality
        # the user picked are already baked in, so width/height/duration aren't outputs.
        return (video_out, images, audio, float(out_fps), frame_count)


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

    @PromptServer.instance.routes.get("/floyo_vs/probe_url")
    async def _floyo_vs_probe_url(request):
        # Header-only metadata read for a JUST-uploaded clip whose file isn't on the backend
        # yet (the #inputs case). The browser passes the platform preview-video URL; ffmpeg
        # range-fetches only the header, so even a 30-min 4K clip resolves fast with no
        # decode and no load on the user's device. GET (not POST) because the platform proxy
        # forwards GET custom-node routes but not POST ones.
        if not _HAS_AV:
            return web.json_response({"error": "PyAV not installed on server."}, status=500)
        url = (request.query.get("url") or "").strip()
        if not url or not _url_is_safe(url):
            return web.json_response({"error": "Unsupported or unsafe video URL."}, status=400)
        try:
            loop = asyncio.get_running_loop()
            meta = await loop.run_in_executor(None, _probe_url, url)
            return web.json_response(meta)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)
except Exception:
    # Not running inside a ComfyUI server context (e.g. import-time tooling) — skip.
    pass


NODE_CLASS_MAPPINGS = {"FloyoVideoStudio": FloyoVideoStudio}
NODE_DISPLAY_NAME_MAPPINGS = {"FloyoVideoStudio": "🎬 Floyo Video Studio"}
