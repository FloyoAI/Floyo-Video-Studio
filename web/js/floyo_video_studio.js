/* Floyo Video Studio — frontend UX for the FloyoVideoStudio node.
 * --------------------------------------------------------------------------
 *  • Upload-only video widget (the raw string field is hidden so a user can
 *    never type a server path — security for a hosted platform).
 *  • In-node <video> preview.
 *  • A dual-handle TIME range slider that shows BOTH the timestamp AND the
 *    frame number live as you drag (per senior-dev feedback: frame count
 *    matters for video models that need specific multiples), driving the
 *    start_seconds / end_seconds number widgets.
 *
 *  All network calls go through ComfyUI's `api` helper so they resolve through
 *  Floyo's hosted comfyui-proxy base path (a bare "/floyo_vs/..." would 404 on
 *  the hosted frontend). No global observers/timers — per-node setup only,
 *  everything wrapped in try/catch so it can never break the canvas.
 */

import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

const STYLE_ID = "floyo-video-studio-styles";

function injectStyles() {
    try {
        if (document.getElementById(STYLE_ID)) return;
        const css = `
.fvs-wrap { display:flex; flex-direction:column; gap:8px; padding:6px 8px; box-sizing:border-box; width:100%; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
.fvs-upload { display:flex; align-items:center; justify-content:center; gap:6px; height:30px; border:1px dashed rgba(255,255,255,0.35); border-radius:8px; color:#e5e7eb; background:rgba(255,255,255,0.04); cursor:pointer; font-size:12px; user-select:none; transition:background 120ms ease,border-color 120ms ease; }
.fvs-upload:hover { background:rgba(255,255,255,0.09); border-color:rgba(255,255,255,0.6); }
.fvs-upload.is-busy { opacity:0.6; pointer-events:none; }
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

function setup(node) {
    try {
        const videoW = getWidget(node, "video");
        const startW = getWidget(node, "start_seconds");
        const endW = getWidget(node, "end_seconds");
        const targetFpsW = getWidget(node, "target_fps");
        if (!videoW || !startW || !endW) return;

        // Hide the raw string field so no one can type a server path.
        videoW.type = "hidden";
        videoW.computeSize = () => [0, -4];

        const state = { meta: { duration: 0, fps: 0, frame_count: 0 }, node, startW, endW, videoW, targetFpsW };
        node.__fvs = state;

        // ── Upload button (DOM, so we control the look + accept video/*) ──
        const wrap = document.createElement("div");
        wrap.className = "fvs-wrap";

        const upload = document.createElement("div");
        upload.className = "fvs-upload";
        upload.textContent = "📁  Upload video";
        const fileInput = document.createElement("input");
        fileInput.type = "file";
        fileInput.accept = "video/*";
        fileInput.style.display = "none";
        upload.appendChild(fileInput);
        upload.addEventListener("click", () => fileInput.click());
        fileInput.addEventListener("change", () => {
            const f = fileInput.files && fileInput.files[0];
            if (f) doUpload(state, f, upload);
            fileInput.value = "";
        });
        wrap.appendChild(upload);

        // ── Preview ──
        const preview = document.createElement("video");
        preview.className = "fvs-preview";
        preview.muted = true;
        preview.loop = true;
        preview.playsInline = true;
        preview.setAttribute("disablepictureinpicture", "");
        wrap.appendChild(preview);
        state.preview = preview;

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
        meta.textContent = "No video loaded.";
        wrap.appendChild(meta);
        state.metaLabel = meta;

        const onSlide = () => {
            const dur = state.meta.duration || 1;
            let lo = parseFloat(rMin.value) * dur;
            let hi = parseFloat(rMax.value) * dur;
            const minGap = (1 / (state.meta.fps || 30)) * 2;
            if (lo > hi - minGap) {
                // push whichever the user is NOT holding
                if (document.activeElement === rMin) { lo = Math.max(0, hi - minGap); rMin.value = lo / dur; }
                else { hi = Math.min(dur, lo + minGap); rMax.value = hi / dur; }
            }
            startW.value = Math.round(lo * 100) / 100;
            endW.value = Math.round(hi * 100) / 100;
            refresh(state);
            node.setDirtyCanvas?.(true, true);
        };
        rMin.addEventListener("input", onSlide);
        rMax.addEventListener("input", onSlide);

        // Keep the slider in sync if the user types into the number widgets.
        const wrapCb = (w) => {
            const orig = w.callback;
            w.callback = function () { const r = orig?.apply(this, arguments); syncFromWidgets(state); return r; };
        };
        wrapCb(startW); wrapCb(endW);
        // target_fps doesn't move the handles, but it changes the output frame
        // count — refresh the readout live when it changes.
        if (targetFpsW) {
            const orig = targetFpsW.callback;
            targetFpsW.callback = function () { const r = orig?.apply(this, arguments); refresh(state); return r; };
        }

        node.addDOMWidget("fvs_ui", "floyo_video_studio", wrap, { serialize: false });

        // Restore a saved video (workflow reload) once widget values are populated.
        setTimeout(() => {
            const name = videoW.value;
            if (name) loadMeta(state, name);
        }, 80);

        // A roomier default so the preview + slider fit.
        if (!node.size || node.size[0] < 300) node.setSize?.([320, node.size ? node.size[1] : 360]);
    } catch (e) {
        console.error("[Floyo Video Studio] setup failed:", e);
    }
}

async function doUpload(state, file, uploadEl) {
    try {
        uploadEl.classList.add("is-busy");
        uploadEl.textContent = "⏳  Uploading…";
        const fd = new FormData();
        fd.append("video", file, file.name);
        const resp = await api.fetchApi("/floyo_vs/upload", { method: "POST", body: fd });
        const data = await resp.json();
        if (!resp.ok || !data.name) throw new Error(data.error || "upload failed");
        state.videoW.value = data.name;
        await loadMeta(state, data.name);
        uploadEl.textContent = "📁  Change video";
    } catch (e) {
        uploadEl.textContent = "⚠️  Upload failed — retry";
        console.error("[Floyo Video Studio] upload:", e);
    } finally {
        uploadEl.classList.remove("is-busy");
    }
}

async function loadMeta(state, filename) {
    try {
        // Preview through the api base path (hosted-proxy safe).
        state.preview.src = api.apiURL(`/view?filename=${encodeURIComponent(filename)}&type=input&t=${Date.now()}`);
        state.preview.classList.add("has-src");
        state.preview.play?.().catch(() => {});

        const resp = await api.fetchApi(`/floyo_vs/probe?filename=${encodeURIComponent(filename)}`);
        const m = await resp.json();
        if (!resp.ok || m.error) throw new Error(m.error || "probe failed");
        state.meta = m;
        state.metaLabel.textContent = `${m.width}×${m.height} · ${m.fps} fps · ${fmtTime(m.duration)} · ${m.frame_count} frames${m.has_audio ? " · 🔊" : ""}`;

        // Default trim = whole clip if widgets are still at defaults.
        if ((Number(state.endW.value) || 0) <= 0) state.endW.value = Math.round(m.duration * 100) / 100;
        syncFromWidgets(state);
    } catch (e) {
        state.metaLabel.textContent = "Could not read video info.";
        console.error("[Floyo Video Studio] probe:", e);
    }
}

// number widgets -> slider handles + readout
function syncFromWidgets(state) {
    const dur = state.meta.duration || 1;
    const lo = Math.min(Math.max(0, Number(state.startW.value) || 0), dur);
    let hi = Number(state.endW.value) || 0;
    if (hi <= 0 || hi > dur) hi = dur;
    state.rMin.value = lo / dur;
    state.rMax.value = hi / dur;
    refresh(state);
}

// redraw the fill + the timestamp/frame readout
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

        // Per-handle frame index = position in the SOURCE video (source fps).
        const fLo = fps ? Math.round(lo * fps) : 0;
        const fHi = fps ? Math.round(hi * fps) : 0;
        // Total OUTPUT frames respects target_fps (0 = source) — so the count
        // updates the moment you change trim OR target_fps.
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
