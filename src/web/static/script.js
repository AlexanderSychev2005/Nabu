let lastResult = null;
let lastImageDataUrl = null;
let activeMask = 0; // index into result.restorations, for the Restoration card's own inline saliency
let activeSaliencyHead = null; // "period" | "genre" | "language" | "provenience", for the Saliency card
let restoreMode = true; // toggled by the mode buttons
let similarShown = 5; // how many of similar_documents are currently rendered
const SIMILAR_PAGE = 5;
const chartInstances = {}; // task name -> Chart.js instance, destroyed/recreated per analyze()
const leafletMaps = {}; // container id -> Leaflet map instance

const $ = (id) => document.getElementById(id);

const SEAL = "#B23A2E";
const CLAY = "#DCCBA8";

// Approximate site coordinates for the provenience classes with a known
// findspot -- same idea as Aeneas's own geographic-attribution map (circle
// size ~ probability), applicable here because our provenience classes are
// real excavated sites, unlike genre/language/period which aren't
// geographic. Not survey-grade precision, just enough for a demo map.
const PROVENIENCE_COORDS = {
    "Nineveh": [36.3599, 43.1554], "Umma": [31.6178, 45.8683], "Girsu": [31.5978, 46.1489],
    "Nippur": [32.1264, 45.2400], "Puzriš-Dagan": [32.6167, 44.9333], "Kanesh": [38.8667, 35.8167],
    "Assur": [35.4550, 43.2610], "Uruk": [31.3225, 45.6367], "Ur": [30.9625, 46.1031],
    "Ugarit": [35.6019, 35.7833], "Sippar": [33.0500, 44.2833], "Nimrud": [36.0972, 43.3253],
    "Hattusa": [40.0161, 34.6156], "Mari": [34.5489, 40.8917], "Ebla": [35.7997, 36.7972],
    "Susa": [32.1875, 48.2569], "Babylon": [32.5422, 44.4211], "Nuzi": [35.3833, 44.2833],
    "Irisagrig": [32.0, 45.4], "Persepolis": [29.9354, 52.8916], "Kish": [32.5450, 44.6122],
    "Larsa": [31.2333, 45.8583], "Garšana": [31.6, 45.75], "Emar": [35.9308, 38.1550],
    "Isin": [31.9297, 45.2775], "Ešnunna": [33.7178, 44.8253], "Šaduppum": [33.35, 44.55],
    "Nerebtum": [33.36, 44.53], "Šuruppak": [31.7900, 45.2986], "Kisurra": [31.85, 45.35],
    "Adab": [31.9500, 45.5333], "Huzirina": [36.9333, 39.6833], "Pī-Kasî": [33.1, 44.4],
    "Tuttul": [35.8464, 39.5011], "Amarna": [27.6453, 30.9022], "Zabalam": [31.7333, 45.8833],
};

// period is the one head with a real chronological order (unlike genre/
// language/provenience) -- mirrors Aeneas's own date histogram (x-axis =
// time, not probability rank), which reads more informatively for a head
// whose classes are inherently ordered.
const PERIOD_ORDER = [
    "Third Millennium", "Ur III", "Old Assyrian", "Old Babylonian", "Middle Assyrian",
    "Middle Babylonian", "Neo-Assyrian", "Neo-Babylonian", "Late Antiquity",
];

const TASKS = ["period", "genre", "language", "provenience"];

$("image-input").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const dataUrl = await new Promise((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.readAsDataURL(file);
    });
    lastImageDataUrl = dataUrl;
    const canvas = $("image-canvas");
    const ctx = canvas.getContext("2d");
    const img = new Image();
    img.onload = () => ctx.drawImage(img, 0, 0, 224, 224); // same square resize the model itself sees
    img.src = dataUrl;
    $("image-preview-wrap").classList.remove("hidden");
});

$("insert-mask-btn").addEventListener("click", () => {
    const ta = $("input-text");
    const start = ta.selectionStart, end = ta.selectionEnd;
    ta.value = ta.value.slice(0, start) + "[MASK]" + ta.value.slice(end);
    ta.focus();
    ta.selectionStart = ta.selectionEnd = start + "[MASK]".length;
});

$("temp-slider").addEventListener("input", (e) => { $("temp-value").textContent = parseFloat(e.target.value).toFixed(1); });

for (const btn of [$("mode-restore"), $("mode-attribute")]) {
    btn.addEventListener("click", () => {
        $("mode-restore").classList.toggle("active", btn === $("mode-restore"));
        $("mode-attribute").classList.toggle("active", btn === $("mode-attribute"));
        restoreMode = btn.dataset.restore === "true";
    });
}

$("analyze-btn").addEventListener("click", analyze);

async function analyze() {
    const text = $("input-text").value.trim();
    if (!text) return;
    $("error-message").classList.add("hidden");
    $("empty-state").classList.add("hidden");
    $("results-container").classList.add("hidden");
    $("loading-spinner").classList.remove("hidden");
    $("analyze-btn").disabled = true;

    try {
        const resp = await fetch("/api/analyze", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                text, image_base64: lastImageDataUrl,
                temperature: parseFloat($("temp-slider").value),
                restore: restoreMode,
            }),
        });
        if (!resp.ok) throw new Error(`Server error: ${resp.status}`);
        lastResult = await resp.json();
        render(lastResult);
    } catch (err) {
        $("error-message").textContent = err.message;
        $("error-message").classList.remove("hidden");
        $("empty-state").classList.remove("hidden");
    } finally {
        $("loading-spinner").classList.add("hidden");
        $("analyze-btn").disabled = false;
    }
}

function render(result) {
    $("results-container").classList.remove("hidden");

    const warn = $("truncation-warning");
    if (result.truncated) {
        warn.textContent = `Input truncated to ${result.max_length} tokens (had ${result.full_length}) -- ` +
            `anything past that point, including any [MASK] you placed there, was dropped before the model saw it.`;
        warn.classList.remove("hidden");
    } else {
        warn.classList.add("hidden");
    }

    activeMask = 0;
    activeSaliencyHead = "provenience";
    similarShown = SIMILAR_PAGE;

    renderRestoration(result);
    renderSaliency(result);
    renderMetadata(result);
    renderVision(result);
    renderSimilar(result);
}

// ---------- Restoration ----------

function renderRestoration(result) {
    const card = $("restoration-card");
    if (!result.restorations.length) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");

    const reading = result.best_reading.map((t) => (t.startsWith("##") ? t.slice(2) : " " + t)).join("").trim();
    $("best-reading").innerHTML = `<span class="section-label" style="margin-bottom:2px;">Most likely reading</span>${reading}`;

    renderMaskPicker(result);
    renderRestorationTokens(result);
    renderTopK(result);
}

function renderMaskPicker(result) {
    const wrap = $("mask-picker");
    wrap.innerHTML = "";
    result.restorations.forEach((r, i) => {
        const btn = document.createElement("button");
        btn.className = "mask-btn" + (activeMask === i ? " active" : "");
        btn.textContent = `mask #${i + 1}`;
        btn.onclick = () => { activeMask = i; renderMaskPicker(result); renderRestorationTokens(result); renderTopK(result); };
        wrap.appendChild(btn);
    });
}

function renderRestorationTokens(result) {
    const saliency = result.restorations[activeMask]?.saliency;
    const activePos = result.restorations[activeMask]?.position;
    const container = $("output-text");
    container.innerHTML = "";
    const maskPositions = new Set(result.masked_positions);
    result.tokens.forEach((tok, i) => {
        const span = document.createElement("span");
        const display = tok.startsWith("##") ? tok.slice(2) : " " + tok;
        if (maskPositions.has(i)) {
            span.className = "tok tok-mask";
            if (activePos === i) span.classList.add("active");
            span.textContent = " [MASK]";
            const idx = result.restorations.findIndex((r) => r.position === i);
            if (idx >= 0) {
                span.onclick = () => { activeMask = idx; renderMaskPicker(result); renderRestorationTokens(result); renderTopK(result); };
            }
        } else {
            span.className = "tok";
            span.textContent = display;
            if (saliency && saliency[i] !== undefined) {
                span.style.backgroundColor = `rgba(178,58,46,${(saliency[i] * 0.7).toFixed(2)})`;
            }
        }
        container.appendChild(span);
    });
}

function renderTopK(result) {
    const panel = $("topk-panel");
    const r = result.restorations[activeMask];
    if (!r) { panel.classList.add("hidden"); return; }
    panel.classList.remove("hidden");
    panel.innerHTML = `<div class="section-label">Top-5 for mask #${activeMask + 1}</div>`;
    r.top_k.forEach((c) => {
        const row = document.createElement("div");
        row.className = "topk-row";
        row.innerHTML = `<span class="tok-name">${c.token}</span>
            <span class="bar-wrap"><span class="bar" style="width:${(c.prob * 100).toFixed(0)}%"></span></span>
            <span class="pct">${(c.prob * 100).toFixed(1)}%</span>`;
        panel.appendChild(row);
    });
}

// ---------- Attribution saliency ----------

function renderSaliency(result) {
    const card = $("saliency-card");
    if (!result.attribution_saliency) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");

    const picker = $("saliency-picker");
    picker.innerHTML = "";
    TASKS.forEach((t) => {
        const btn = document.createElement("button");
        btn.className = "mask-btn" + (activeSaliencyHead === t ? " active" : "");
        btn.textContent = t;
        btn.onclick = () => { activeSaliencyHead = t; renderSaliency(result); };
        picker.appendChild(btn);
    });

    const saliency = result.attribution_saliency[activeSaliencyHead];
    const container = $("saliency-text");
    container.innerHTML = "";
    result.tokens.forEach((tok, i) => {
        const span = document.createElement("span");
        span.className = "tok";
        span.textContent = tok.startsWith("##") ? tok.slice(2) : " " + tok;
        if (saliency && saliency[i] !== undefined) {
            span.style.backgroundColor = `rgba(178,58,46,${(saliency[i] * 0.7).toFixed(2)})`;
        }
        container.appendChild(span);
    });
}

// ---------- Attribution ----------

// Same idea as kyivan/src/web's region doughnut / date histogram -- one
// horizontal bar chart per head, predicted class in seal red. Horizontal
// reads better than a vertical layout once a head has more than ~5 classes.
// period follows PERIOD_ORDER (real chronological order, mirrors Aeneas's
// own date histogram); genre/language (<=9 classes) show every class;
// provenience (36 classes) is capped to its own top-10, the same scale
// Aeneas shows for its 62-region head, to keep the chart legible.
function renderBarChart(canvasId, task, allProbs) {
    if (chartInstances[task]) chartInstances[task].destroy();
    const byProb = [...allProbs].sort((a, b) => b.prob - a.prob);
    const probs = task === "provenience" ? byProb.slice(0, 10) : allProbs;
    const sorted = task === "period"
        ? [...probs].sort((a, b) => PERIOD_ORDER.indexOf(a.label) - PERIOD_ORDER.indexOf(b.label))
        : [...probs].sort((a, b) => b.prob - a.prob);
    const topLabel = byProb[0].label;
    const ctx = $(canvasId).getContext("2d");
    chartInstances[task] = new Chart(ctx, {
        type: "bar",
        data: {
            // Percentage baked into the label itself, not just the hover
            // tooltip -- otherwise the smaller bars' own values are
            // impossible to read at a glance (their bar is too short to
            // print a number inside, and nothing shows without hovering).
            labels: sorted.map((c) => `${c.label} (${(c.prob * 100).toFixed(0)}%)`),
            datasets: [{
                data: sorted.map((c) => c.prob),
                backgroundColor: sorted.map((c) => (c.label === topLabel ? SEAL : CLAY)),
                borderRadius: 3,
            }],
        },
        options: {
            indexAxis: "y",
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => `${(c.raw * 100).toFixed(1)}%` } } },
            scales: {
                x: { min: 0, max: 1, ticks: { callback: (v) => `${(v * 100).toFixed(0)}%` } },
                y: { ticks: { font: { size: 11 } } },
            },
        },
    });
}

// Aeneas shows one marker for its top attribution guess, not one per
// candidate region -- clearer at a glance than plotting all 36 provenience
// sites every time. We show the top-3 (not just 1) since several of our
// classes sit close together geographically and a little context helps.
function renderProvMap(containerId, probs) {
    const el = $(containerId);
    if (leafletMaps[containerId]) { leafletMaps[containerId].remove(); delete leafletMaps[containerId]; }
    el.classList.remove("hidden");
    const top = [...probs].sort((a, b) => b.prob - a.prob).slice(0, 3).filter((c) => PROVENIENCE_COORDS[c.label]);
    if (!top.length) { el.classList.add("hidden"); return; }
    const map = L.map(el, { scrollWheelZoom: false }).setView(PROVENIENCE_COORDS[top[0].label], 6);
    leafletMaps[containerId] = map;
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution: "© OpenStreetMap contributors", maxZoom: 10,
    }).addTo(map);
    top.forEach((c, i) => {
        L.circleMarker(PROVENIENCE_COORDS[c.label], {
            radius: i === 0 ? 10 : 6,
            color: SEAL, fillColor: SEAL, fillOpacity: i === 0 ? 0.75 : 0.4, weight: 1,
        }).bindTooltip(`${c.label}: ${(c.prob * 100).toFixed(1)}%`).addTo(map);
    });
}

function renderMetadata(result) {
    const wrap = $("metadata-cards");
    wrap.innerHTML = "";
    for (const [task, pred] of Object.entries(result.metadata)) {
        const block = document.createElement("div");
        block.className = "meta-head";
        block.innerHTML = `<div class="meta-head-title">${task}: <b>${pred.label}</b> (${(pred.confidence * 100).toFixed(0)}%)</div>
            <div class="chart-wrap"><canvas id="chart-${task}"></canvas></div>`;
        wrap.appendChild(block);
        renderBarChart(`chart-${task}`, task, pred.probs);
    }
    renderProvMap("prov-map", result.metadata.provenience.probs);
}

function renderVision(result) {
    // Grad-CAM is only computed server-side when a photo was actually
    // uploaded (see app.py) -- its presence is what gates this card, not a
    // second prediction (there is only ever one provenience answer now,
    // already shown in the Attribution card above; this card just shows
    // which part of the photo it drew on).
    const card = $("vision-card");
    if (!result.gradcam) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");

    const canvas = $("gradcam-canvas");
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, 224, 224);
    const img = new Image();
    img.onload = () => {
        ctx.drawImage(img, 0, 0, 224, 224);
        if (result.gradcam && $("gradcam-toggle").checked) {
            drawGradcam(ctx, result.gradcam);
        }
    };
    img.src = lastImageDataUrl;
}

function drawGradcam(ctx, cam) {
    const grid = cam.length; // 7
    const cell = 224 / grid;
    for (let y = 0; y < grid; y++) {
        for (let x = 0; x < grid; x++) {
            const v = cam[y][x];
            ctx.fillStyle = `rgba(220,20,20,${(v * 0.55).toFixed(2)})`;
            ctx.fillRect(x * cell, y * cell, cell, cell);
        }
    }
}

$("gradcam-toggle").addEventListener("change", () => { if (lastResult) renderVision(lastResult); });

// ---------- Similar documents ----------

function renderSimilar(result) {
    const card = $("similar-card");
    if (!result.similar_documents.length) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");
    const grid = $("similar-grid");
    grid.innerHTML = "";
    const docs = result.similar_documents.slice(0, similarShown);
    docs.forEach((d) => grid.appendChild(similarCard(d)));

    const more = $("similar-more-btn");
    if (similarShown < result.similar_documents.length) {
        more.classList.remove("hidden");
        more.onclick = () => { similarShown += SIMILAR_PAGE; renderSimilar(result); };
    } else {
        more.classList.add("hidden");
    }
}

function similarCard(d) {
    const el = document.createElement("div");
    el.className = "similar-card";
    const preview = (d.text || "").slice(0, 140) + ((d.text || "").length > 140 ? "…" : "");
    const tags = [d.period, d.genre, d.provenience].filter(Boolean).join(" · ");
    const pct = Math.round(d.score * 100);
    el.innerHTML = `
        <div class="similar-card-id">${d.tablet_id}</div>
        <div class="similar-card-preview">${preview || "(no transliteration on file)"}</div>
        <div class="similar-card-tags">${tags}</div>
        <div class="similar-card-score-row">
            <span class="similar-score-bar-wrap"><span class="similar-score-bar" style="width:${pct}%"></span></span>
            <span class="similar-score-pct">${pct}%</span>
        </div>`;
    el.onclick = () => openDocModal(d);
    return el;
}

function openDocModal(d) {
    const body = $("doc-modal-body");
    const tags = [
        d.period && `period: ${d.period}`, d.genre && `genre: ${d.genre}`,
        d.language && `language: ${d.language}`, d.provenience && `provenience: ${d.provenience}`,
    ].filter(Boolean).join(" · ");
    body.innerHTML = `
        <div class="modal-title">${d.tablet_id}</div>
        <div class="modal-tags">${tags || "(no metadata on file)"}</div>
        ${d.signs ? `<div class="modal-section-label">Cuneiform</div><div class="modal-signs">${d.signs}</div>` : ""}
        <div class="modal-section-label">Transliteration</div>
        <div class="modal-text">${d.text || "(no transliteration on file)"}</div>
        <div class="modal-section-label">Similarity to your query</div>
        <div class="modal-text">${Math.round(d.score * 100)}% (cosine similarity of document embeddings)</div>`;
    $("doc-modal").classList.remove("hidden");
}

$("doc-modal-close").addEventListener("click", () => $("doc-modal").classList.add("hidden"));
$("doc-modal").addEventListener("click", (e) => { if (e.target.id === "doc-modal") $("doc-modal").classList.add("hidden"); });
