let lastResult = null;
let lastImageDataUrl = null;
let activeSelection = null; // {kind: 'mask', idx} or {kind: 'provenience'}
const chartInstances = {}; // task name -> Chart.js instance, destroyed/recreated per analyze()
const leafletMaps = {}; // container id -> Leaflet map instance

const $ = (id) => document.getElementById(id);

const SEAL = "#B23A2E";
const CLAY = "#DCCBA8";

// Approximate site coordinates for the 12 provenience classes -- same idea
// as Aeneas's own geographic-attribution map (circle size ~ probability),
// applicable here because our provenience classes are real excavated sites
// with known findspots, unlike genre/language/period which aren't
// geographic. Not survey-grade precision, just enough for a demo map.
const PROVENIENCE_COORDS = {
    "Nineveh": [36.3599, 43.1554], "Umma": [31.6178, 45.8683], "Girsu": [31.5978, 46.1489],
    "Nippur": [32.1264, 45.2400], "Puzriš-Dagan": [32.6167, 44.9333], "Kanesh": [38.8667, 35.8167],
    "Assur": [35.4550, 43.2610], "Uruk": [31.3225, 45.6367], "Ur": [30.9625, 46.1031],
    "Ugarit": [35.6019, 35.7833], "Sippar": [33.0500, 44.2833], "Nimrud": [36.0972, 43.3253],
};

// period is the one head with a real chronological order (unlike genre/
// language/provenience) -- mirrors Aeneas's own date histogram (x-axis =
// time, not probability rank), which reads more informatively for a head
// whose classes are inherently ordered.
const PERIOD_ORDER = [
    "Third Millennium", "Ur III", "Old Assyrian", "Old Babylonian", "Middle Assyrian",
    "Middle Babylonian", "Neo-Assyrian", "Neo-Babylonian", "Late Antiquity",
];

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
            body: JSON.stringify({ text, image_base64: lastImageDataUrl }),
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

    if (result.restorations.length) {
        activeSelection = { kind: "mask", idx: 0 };
    } else if (result.provenience_saliency) {
        activeSelection = { kind: "provenience" };
    } else {
        activeSelection = null;
    }

    renderMaskPicker(result);
    renderTokens(result);
    renderTopK(result);
    renderMetadata(result);
    renderVision(result);
    renderSimilar(result);
}

function currentSaliency(result) {
    if (!activeSelection) return null;
    if (activeSelection.kind === "mask") return result.restorations[activeSelection.idx].saliency;
    return result.provenience_saliency;
}

function renderMaskPicker(result) {
    const wrap = $("mask-picker");
    wrap.innerHTML = "";
    result.restorations.forEach((r, i) => {
        const btn = document.createElement("button");
        btn.className = "mask-btn" + (activeSelection?.kind === "mask" && activeSelection.idx === i ? " active" : "");
        btn.textContent = `mask #${i + 1} (pos. ${r.position})`;
        btn.onclick = () => { activeSelection = { kind: "mask", idx: i }; renderMaskPicker(result); renderTokens(result); renderTopK(result); };
        wrap.appendChild(btn);
    });
    if (result.provenience_saliency) {
        const btn = document.createElement("button");
        btn.className = "mask-btn" + (activeSelection?.kind === "provenience" ? " active" : "");
        btn.textContent = "saliency: provenience";
        btn.onclick = () => { activeSelection = { kind: "provenience" }; renderMaskPicker(result); renderTokens(result); renderTopK(result); };
        wrap.appendChild(btn);
    }
}

function renderTokens(result) {
    const saliency = currentSaliency(result);
    const container = $("output-text");
    container.innerHTML = "";
    const maskPositions = new Set(result.masked_positions);
    result.tokens.forEach((tok, i) => {
        const span = document.createElement("span");
        const display = tok.startsWith("##") ? tok.slice(2) : " " + tok;
        if (maskPositions.has(i)) {
            span.className = "tok tok-mask";
            if (activeSelection?.kind === "mask") {
                const r = result.restorations[activeSelection.idx];
                if (r.position === i) span.classList.add("active");
            }
            span.textContent = " [MASK]";
            const idx = result.restorations.findIndex((r) => r.position === i);
            if (idx >= 0) {
                span.onclick = () => { activeSelection = { kind: "mask", idx }; renderMaskPicker(result); renderTokens(result); renderTopK(result); };
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
    if (!activeSelection || activeSelection.kind !== "mask") { panel.classList.add("hidden"); return; }
    const r = result.restorations[activeSelection.idx];
    panel.classList.remove("hidden");
    panel.innerHTML = `<div class="section-label">Top-5 for mask #${activeSelection.idx + 1}</div>`;
    r.top_k.forEach((c) => {
        const row = document.createElement("div");
        row.className = "topk-row";
        row.innerHTML = `<span class="tok-name">${c.token}</span>
            <span class="bar-wrap"><span class="bar" style="width:${(c.prob * 100).toFixed(0)}%"></span></span>
            <span class="pct">${(c.prob * 100).toFixed(1)}%</span>`;
        panel.appendChild(row);
    });
}

// Same idea as kyivan/src/web's region doughnut / date histogram -- one
// horizontal bar chart per head, predicted class in seal red. Horizontal
// reads better than Kyivan's vertical bars once a head has more than ~5
// classes (period has 9, provenience 12). Every head except period is
// sorted by probability (highest first); period instead follows
// PERIOD_ORDER (real chronological order) -- mirrors Aeneas's own date
// histogram (x-axis = time, not probability rank), which is more legible
// for a head whose classes are inherently ordered.
function renderBarChart(canvasId, task, probs) {
    if (chartInstances[task]) chartInstances[task].destroy();
    const sorted = task === "period"
        ? [...probs].sort((a, b) => PERIOD_ORDER.indexOf(a.label) - PERIOD_ORDER.indexOf(b.label))
        : [...probs].sort((a, b) => b.prob - a.prob);
    const topLabel = [...probs].sort((a, b) => b.prob - a.prob)[0].label;
    const ctx = $(canvasId).getContext("2d");
    chartInstances[task] = new Chart(ctx, {
        type: "bar",
        data: {
            labels: sorted.map((c) => c.label),
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

// Same idea as Aeneas's geographic-attribution map: one circle marker per
// candidate class, sized by predicted probability, on real OSM tiles.
// Leaflet requires a fresh container each re-init (or .remove() the old
// map instance first) since it attaches its own DOM/state to the div.
function renderProvMap(containerId, probs) {
    const el = $(containerId);
    if (leafletMaps[containerId]) { leafletMaps[containerId].remove(); delete leafletMaps[containerId]; }
    el.classList.remove("hidden");
    const map = L.map(el, { scrollWheelZoom: false }).setView([33.5, 44], 5);
    leafletMaps[containerId] = map;
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution: "© OpenStreetMap contributors", maxZoom: 10,
    }).addTo(map);
    const maxProb = Math.max(...probs.map((c) => c.prob));
    probs.forEach((c) => {
        const coord = PROVENIENCE_COORDS[c.label];
        if (!coord) return;
        L.circleMarker(coord, {
            radius: 4 + 22 * (c.prob / maxProb),
            color: SEAL, fillColor: SEAL, fillOpacity: 0.5, weight: 1,
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
    const card = $("vision-card");
    if (!result.provenience_vision) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");
    const pred = result.provenience_vision;
    const textPred = result.metadata.provenience;
    const diff = pred.label !== textPred.label ? " (differs from the text-only prediction)" : "";
    $("vision-result").innerHTML =
        `<div class="meta-head-title">provenience: <b>${pred.label}</b> (${(pred.confidence * 100).toFixed(0)}%)${diff}</div>`;
    renderBarChart("vision-chart", "provenience_vision", pred.probs);
    renderProvMap("vision-map", pred.probs);

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

function renderSimilar(result) {
    const card = $("similar-card");
    if (!result.similar_documents.length) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");
    const list = $("similar-list");
    list.innerHTML = "";
    result.similar_documents.forEach((d) => {
        const row = document.createElement("div");
        row.className = "similar-row";
        const tags = [d.period, d.genre, d.provenience].filter(Boolean).join(" · ");
        row.innerHTML = `<span>${d.tablet_id} <span class="similar-tags">${tags}</span></span>
            <span class="similar-score">${d.score.toFixed(3)}</span>`;
        list.appendChild(row);
    });
}
