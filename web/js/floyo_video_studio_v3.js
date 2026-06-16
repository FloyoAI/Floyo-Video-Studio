/* Floyo Video Studio — frontend UX for the FloyoVideoStudio node.
 * --------------------------------------------------------------------------
 * Upload is handled by the platform's built-in `video_upload` widget (exactly
 * like the core Load Video node), so the file is stored in the input folder and
 * the GPU execution backend can find it. This file only ADDS the extras:
 *   • a dual-handle TIME range slider that shows BOTH the timestamp AND the
 *     frame number live (frame count matters for video models), driving the
 *     start_seconds / end_seconds widgets, and scrubbing the platform's own
 *     preview video to the start / end frame as you drag (no duplicate preview).
 *
 * Network calls go through ComfyUI's `api` helper so they resolve through the
 * hosted proxy. No global observers/timers — per-node setup only, all wrapped in
 * try/catch so it can never break the canvas.
 */

import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

const STYLE_ID = "floyo-video-studio-styles";

function injectStyles() {
    try {
        if (document.getElementById(STYLE_ID)) return;
        const css = `
.fvs-wrap { display:flex; flex-direction:column; gap:8px; padding:6px 8px; box-sizing:border-box; width:100%; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
.fvs-preview { width:100%; border-radius:8px; background:#000; display:none; aspect-ratio:16/9; object-fit:contain; }
.fvs-preview.has-src { display:block; }
.fvs-meta { font-size:11px; color:#9ca3af; min-height:14px; }
.fvs-slider { position:relative; height:30px; }
.fvs-track { position:absolute; left:0; right:0; top:13px; height:4px; border-radius:3px; background:rgba(255,255,255,0.15); }
.fvs-fill  { position:absolute; top:13px; height:4px; border-radius:3px; background:#60A5FA; }
.fvs-range { position:absolute; left:0; right:0; top:6px; width:100%; margin:0; background:none; pointer-events:none; -webkit-appearance:none; appearance:none; height:18px; }
.fvs-range::-webkit-slider-thumb { -webkit-appearance:none; appearance:none; width:14px; height:14px; border-radius:50%; background:#fff; border:2px solid #2563EB; cursor:pointer; pointer-events:all; box-shadow:0 1px 3px rgba(0,0,0,0.5); }
.fvs-range::-moz-range-thumb { width:14px; height:14px; border-radius:50%; background:#fff; border:2px solid #2563EB; cursor:pointer; pointer-events:all; }
.fvs-range::-webkit-slider-runnable-track { background:transparent; }
.fvs-readout { font-size:11px; color:#d1d5db; line-height:1.5; }
.fvs-readout b { color:#fff; font-weight:600; }
.fvs-readout .fvs-frames { color:#60A5FA; }
`;
        const s = document.createElement("style");
        s.id = STYLE_ID;
        s.textContent = css;
        (document.head || document.documentElement).appendChild(s);
    } catch (_) {}
}

function fmtTime(s) {
    s = Math.max(0, Number(s) || 0);
    const m = Math.floor(s / 60);
    const sec = s - m * 60;
    return `${String(m).padStart(2, "0")}:${sec.toFixed(1).padStart(4, "0")}`;
}

function getWidget(node, name) {
    return (node.widgets || []).find((w) => w.name === name);
}

// The platform's video_upload widget renders its own <video> preview as a DOM
// sibling of our wrap. Find it so we can scrub it when the trim handles move.
function frontendVideo(state) {
    try {
        let root = state.wrap && state.wrap.parentElement;
        for (let i = 0; i < 6 && root; i++) {
            const v = root.querySelector && root.querySelector("video");
            if (v) return v;
            root = root.parentElement;
        }
    } catch (_) {}
    return null;
}

function setup(node) {
    try {
        const videoW = getWidget(node, "video");
        const startW = getWidget(node, "start_seconds");
        const endW = getWidget(node, "end_seconds");
        const targetFpsW = getWidget(node, "target_fps");
        if (!videoW || !startW || !endW) return;

        const state = { meta: { duration: 0, fps: 0, frame_count: 0 }, node, startW, endW, videoW, targetFpsW };
        node.__fvs = state;

        const wrap = document.createElement("div");
        wrap.className = "fvs-wrap";
        state.wrap = wrap;
        // No custom preview here — the platform's video_upload widget already
        // renders one (a second one was a duplicate). We scrub THAT video when the
        // trim handles move, if its source supports seeking.

        // ── Time-range slider (two overlaid native ranges) ──
        const slider = document.createElement("div");
        slider.className = "fvs-slider";
        const track = document.createElement("div"); track.className = "fvs-track";
        const fill = document.createElement("div"); fill.className = "fvs-fill";
        const rMin = document.createElement("input"); rMin.type = "range"; rMin.className = "fvs-range";
        const rMax = document.createElement("input"); rMax.type = "range"; rMax.className = "fvs-range";
        for (const r of [rMin, rMax]) { r.min = "0"; r.max = "1"; r.step = "0.001"; }
        rMin.value = "0"; rMax.value = "1";
        slider.append(track, fill, rMin, rMax);
        wrap.appendChild(slider);
        state.rMin = rMin; state.rMax = rMax; state.fill = fill;

        const readout = document.createElement("div");
        readout.className = "fvs-readout";
        wrap.appendChild(readout);
        state.readout = readout;

        const meta = document.createElement("div");
        meta.className = "fvs-meta";
        meta.textContent = "Upload a video to set the trim range.";
        wrap.appendChild(meta);
        state.metaLabel = meta;

        const onSlide = () => {
            const dur = state.meta.duration || 1;
            let lo = parseFloat(rMin.value) * dur;
            let hi = parseFloat(rMax.value) * dur;
            const minGap = (1 / (state.meta.fps || 30)) * 2;
            if (lo > hi - minGap) {
                if (document.activeElement === rMin) { lo = Math.max(0, hi - minGap); rMin.value = lo / dur; }
                else { hi = Math.min(dur, lo + minGap); rMax.value = hi / dur; }
            }
            startW.value = Math.round(lo * 100) / 100;
            endW.value = Math.round(hi * 100) / 100;
            // Scrub the platform's preview video to the handle you're dragging, if
            // its source is seekable (some serve without HTTP range support).
            try {
                const fv = frontendVideo(state);
                if (fv && fv.seekable && fv.seekable.length && fv.seekable.end(0) > 0.1) {
                    fv.pause();
                    fv.currentTime = (document.activeElement === rMax) ? hi : lo;
                }
            } catch (_) {}
            refresh(state);
            node.setDirtyCanvas?.(true, true);
        };
        rMin.addEventListener("input", onSlide);
        rMax.addEventListener("input", onSlide);

        const wrapCb = (w, fn) => {
            const orig = w.callback;
            w.callback = function () { const r = orig?.apply(this, arguments); try { fn(); } catch (_) {} return r; };
        };
        wrapCb(startW, () => syncFromWidgets(state));
        wrapCb(endW, () => syncFromWidgets(state));
        if (targetFpsW) wrapCb(targetFpsW, () => refresh(state));
        // When the platform's upload/combo sets a new video, (re)load its metadata.
        wrapCb(videoW, () => loadMeta(state, videoW.value));

        node.addDOMWidget("fvs_ui", "floyo_video_studio", wrap, { serialize: false });

        // Pick up a value that's already set (workflow reload / post-upload).
        [120, 600, 1500].forEach((d) => setTimeout(() => {
            if (videoW.value && videoW.value !== state._loaded) loadMeta(state, videoW.value);
        }, d));

        if (!node.size || node.size[0] < 300) node.setSize?.([320, node.size ? node.size[1] : 360]);
    } catch (e) {
        console.error("[Floyo Video Studio] setup failed:", e);
    }
}

async function loadMeta(state, value) {
    if (!value) return;
    try {
        state._loaded = value;
        const resp = await api.fetchApi(`/floyo_vs/probe?filename=${encodeURIComponent(value)}`);
        const m = await resp.json();
        if (!resp.ok || m.error) throw new Error(m.error || "probe failed");
        state.meta = m;
        state.metaLabel.textContent = `${m.width}×${m.height} · ${m.fps} fps · ${fmtTime(m.duration)} · ${m.frame_count} frames${m.has_audio ? " · 🔊" : ""}`;
        if ((Number(state.endW.value) || 0) <= 0) state.endW.value = Math.round(m.duration * 100) / 100;
        syncFromWidgets(state);
    } catch (e) {
        state.metaLabel.textContent = "Could not read video info (the trim still works by seconds).";
        console.error("[Floyo Video Studio] probe:", e);
    }
}

function syncFromWidgets(state) {
    const dur = state.meta.duration || 1;
    const lo = Math.min(Math.max(0, Number(state.startW.value) || 0), dur);
    let hi = Number(state.endW.value) || 0;
    if (hi <= 0 || hi > dur) hi = dur;
    state.rMin.value = lo / dur;
    state.rMax.value = hi / dur;
    refresh(state);
}

function refresh(state) {
    try {
        const dur = state.meta.duration || 1;
        const fps = state.meta.fps || 0;
        const lo = parseFloat(state.rMin.value) * dur;
        const hi = parseFloat(state.rMax.value) * dur;
        const a = Math.min(parseFloat(state.rMin.value), parseFloat(state.rMax.value)) * 100;
        const b = Math.max(parseFloat(state.rMin.value), parseFloat(state.rMax.value)) * 100;
        state.fill.style.left = a + "%";
        state.fill.style.width = (b - a) + "%";

        const fLo = fps ? Math.round(lo * fps) : 0;
        const fHi = fps ? Math.round(hi * fps) : 0;
        const tfps = state.targetFpsW ? (Number(state.targetFpsW.value) || 0) : 0;
        const efps = tfps > 0 ? tfps : fps;
        const nFrames = efps ? Math.max(0, Math.round((hi - lo) * efps)) : Math.max(0, fHi - fLo);
        state.readout.innerHTML =
            `Start <b>${fmtTime(lo)}</b> <span class="fvs-frames">(frame ${fLo})</span> → ` +
            `End <b>${fmtTime(hi)}</b> <span class="fvs-frames">(frame ${fHi})</span><br>` +
            `<b>${(hi - lo).toFixed(1)}s</b> · <span class="fvs-frames">${nFrames} frames</span>${efps ? ` @ ${efps} fps` : ""}` +
            `${tfps > 0 ? ` <span style="opacity:.6">(target)</span>` : ""}`;
    } catch (_) {}
}

app.registerExtension({
    name: "Floyo.VideoStudio",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== "FloyoVideoStudio") return;
        injectStyles();
        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onCreated?.apply(this, arguments);
            setup(this);
            return r;
        };
    },
});
