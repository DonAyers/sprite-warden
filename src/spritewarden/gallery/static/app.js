"use strict";

const state = {
  tab: "assets",
  offset: 0,
  limit: 24,
  total: 0,
  filters: { q: "", game: "", character_id: "", category: "", asset_type: "", perspective: "", provider: "" },
  facets: {},
  projects: [],
  identities: [],
  assetPage: [],
  viewerIndex: 0,
  viewerAssetId: null,
  sequenceAssets: [],
  sequenceIndex: -1,
  sequenceToken: 0,
  sequencePlaying: false,
  sequenceTimer: null,
  framePlayback: null,
  viewSettings: { columns: 4, rows: 6, matchPixels: false, pixelScale: 3 },
  csrfToken: "",
  previewCard: null,
  previewPoint: null,
  loaded: {},
};

const $ = (sel) => document.querySelector(sel);
const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function mutate(path, method, payload) {
  const res = await fetch(path, {
    method,
    headers: { Accept: "application/json", "Content-Type": "application/json", "X-CSRF-Token": state.csrfToken },
    body: JSON.stringify(payload),
  });
  const result = await res.json();
  if (!res.ok) throw new Error(result.error || `${res.status} ${res.statusText}`);
  return result;
}

const slugify = (value) => value.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 64);
const parseTags = (value) => [...new Set(value.split(/[;,]/).map((tag) => tag.trim()).filter(Boolean))];
const option = (value, label, selected) => `<option value="${esc(value)}" ${selected ? "selected" : ""}>${esc(label)}</option>`;

function chips(list) {
  return (list || []).map((c) => `<span class="chip ${c.cls || ""}">${esc(c.label)}</span>`).join("");
}

function gateChips(gate) {
  const out = [];
  if (!gate || gate.has_record === false) {
    out.push({ label: "ungated", cls: "" });
    return out;
  }
  if (gate.rejected) out.push({ label: "rejected", cls: "bad" });
  if (gate.stale) out.push({ label: "stale record", cls: "warn" });
  for (const g of gate.passed || []) out.push({ label: g, cls: "ok" });
  if (gate.review_problem) out.push({ label: "review: " + gate.review_problem, cls: "warn" });
  return out;
}

function applyImageSizing(root = document) {
  const match = state.viewSettings.matchPixels;
  const scale = state.viewSettings.pixelScale;
  document.documentElement.dataset.imageMode = match ? "pixels" : "fit";
  root.querySelectorAll(".card .thumb img").forEach((image) => {
    const sizeImage = () => {
      if (match) {
        const width = Number(image.dataset.renderWidth) || image.naturalWidth;
        const height = Number(image.dataset.renderHeight) || image.naturalHeight;
        image.style.width = `${width * scale}px`;
        image.style.height = `${height * scale}px`;
        image.style.maxWidth = "none";
        image.style.maxHeight = "none";
      } else {
        image.style.removeProperty("width");
        image.style.removeProperty("height");
        image.style.removeProperty("max-width");
        image.style.removeProperty("max-height");
      }
    };
    if (image.complete && image.naturalWidth) sizeImage();
    else image.addEventListener("load", sizeImage, { once: true });
  });
}

function applyViewSettings(reload = false) {
  const columns = Math.max(2, Math.min(8, Number(state.viewSettings.columns) || 4));
  const rows = Math.max(2, Math.min(12, Number(state.viewSettings.rows) || 6));
  const pixelScale = Math.max(1, Math.min(8, Number(state.viewSettings.pixelScale) || 3));
  state.viewSettings = { ...state.viewSettings, columns, rows, pixelScale };
  state.limit = columns * rows;
  document.documentElement.style.setProperty("--grid-columns", columns);
  document.documentElement.style.setProperty("--grid-columns-mobile", Math.min(columns, window.innerWidth < 700 ? 2 : 4));
  $("#grid-columns").value = String(columns);
  $("#grid-rows").value = String(rows);
  $("#match-pixels").checked = state.viewSettings.matchPixels;
  $("#pixel-scale").value = String(pixelScale);
  $("#pixel-scale-value").value = `${pixelScale}×`;
  $("#pixel-scale-setting").classList.toggle("hidden", !state.viewSettings.matchPixels);
  $("#results-per-page").value = String(state.limit);
  $("#settings-summary").textContent = `${columns} columns × ${rows} rows · ${state.limit} per page · ${state.viewSettings.matchPixels ? `${pixelScale}× pixels` : "Fit tile"}`;
  applyImageSizing();
  try { localStorage.setItem("spritewarden-gallery-view", JSON.stringify(state.viewSettings)); } catch (_) {}
  if (reload) {
    state.offset = 0;
    stopSequence();
    loadAssets();
  }
}

function setViewPanelOpen(open) {
  const panel = $("#results-settings-panel");
  const button = $("#view-settings-toggle");
  panel.classList.toggle("hidden", !open);
  button.setAttribute("aria-expanded", String(open));
}

function initViewSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem("spritewarden-gallery-view") || "{}");
    state.viewSettings = { ...state.viewSettings, ...saved };
  } catch (_) {}
  applyViewSettings();
  $("#view-settings-toggle").addEventListener("click", () => {
    setViewPanelOpen($("#results-settings-panel").classList.contains("hidden"));
  });
  document.addEventListener("click", (event) => {
    if (!$("#view-settings").contains(event.target)) setViewPanelOpen(false);
  });
  $("#grid-columns").addEventListener("change", (event) => {
    state.viewSettings.columns = Number(event.target.value);
    applyViewSettings(true);
  });
  $("#grid-rows").addEventListener("change", (event) => {
    state.viewSettings.rows = Number(event.target.value);
    applyViewSettings(true);
  });
  $("#match-pixels").addEventListener("change", (event) => {
    state.viewSettings.matchPixels = event.target.checked;
    applyViewSettings();
  });
  $("#pixel-scale").addEventListener("input", (event) => {
    state.viewSettings.pixelScale = Number(event.target.value);
    applyViewSettings();
  });
  window.addEventListener("resize", () => {
    document.documentElement.style.setProperty("--grid-columns-mobile", Math.min(state.viewSettings.columns, window.innerWidth < 700 ? 2 : 4));
  });
}

function hideImagePreview() {
  const preview = $("#image-preview");
  preview.classList.remove("visible");
  preview.setAttribute("aria-hidden", "true");
  state.previewCard = null;
  state.previewPoint = null;
}

function positionImagePreview(x, y) {
  const preview = $("#image-preview");
  const bounds = preview.getBoundingClientRect();
  const gap = 18;
  let left = x + gap;
  let top = y + gap;
  if (left + bounds.width > window.innerWidth - 12) left = x - bounds.width - gap;
  if (top + bounds.height > window.innerHeight - 12) top = y - bounds.height - gap;
  preview.style.left = `${Math.max(12, Math.min(left, window.innerWidth - bounds.width - 12))}px`;
  preview.style.top = `${Math.max(12, Math.min(top, window.innerHeight - bounds.height - 12))}px`;
}

function sizePreviewImage(image) {
  const width = image.naturalWidth;
  const height = image.naturalHeight;
  if (!width || !height) return;
  const scale = Math.min(8, 420 / width, Math.min(360, window.innerHeight * 0.6) / height);
  image.style.width = `${Math.max(1, Math.round(width * scale))}px`;
  image.style.height = `${Math.max(1, Math.round(height * scale))}px`;
  if (state.previewPoint) {
    const { x, y } = state.previewPoint;
    requestAnimationFrame(() => positionImagePreview(x, y));
  }
}

function showImagePreview(card, x, y) {
  if (!card || card.dataset.previewable !== "true") return;
  const preview = $("#image-preview");
  const id = card.dataset.id;
  if (!id) return;
  state.previewPoint = { x, y };
  if (state.previewCard !== card) {
    const title = card.querySelector(".name")?.textContent || "Sprite preview";
    preview.innerHTML = `<div class="image-preview-frame"><img src="/thumb/${encodeURIComponent(id)}?size=1024" alt="${esc(title)}"><span>${esc(title)}</span></div>`;
    const image = preview.querySelector("img");
    image.addEventListener("load", () => sizePreviewImage(image), { once: true });
    state.previewCard = card;
    if (image.complete && image.naturalWidth) sizePreviewImage(image);
  }
  preview.classList.add("visible");
  preview.setAttribute("aria-hidden", "false");
  requestAnimationFrame(() => positionImagePreview(x, y));
}

function renderAssetCard(a) {
  const prov = a.provider ? [{ label: a.provider, cls: "provider" }] : [{ label: a.kind, cls: "" }];
  const tags = (a.tag_list || String(a.tags || "").split(/\s+/).filter(Boolean)).slice(0, 3)
    .map((tag) => ({ label: tag, cls: "" }));
  const project = state.projects.find((p) => p.slug === a.game);
  const src = a.exists ? `/thumb/${encodeURIComponent(a.id)}?size=256` : "";
  const img = a.exists
    ? `<img loading="lazy" src="${src}" data-render-width="${Number(a.frame_w) || ""}" data-render-height="${Number(a.frame_h) || ""}" alt="${esc(a.name)}">`
    : `<span class="muted">missing file</span>`;
  return `
    <div class="card" data-id="${esc(a.id)}" data-previewable="${a.exists ? "true" : "false"}" tabindex="0" role="button" aria-label="Open ${esc(a.name)} details">
      <div class="thumb">${img}</div>
      <div class="meta">
        <div class="name" title="${esc(a.name)}">${esc(a.name)}</div>
        <div class="sub">${esc(project ? project.name : a.game || "")}${a.character_id ? " · " + esc(a.character_id) : ""}${a.frame_w ? " · " + a.frame_w + "×" + a.frame_h : ""}</div>
        <div class="chips">${chips(prov.concat(tags, gateChips(a.gate)))}</div>
      </div>
    </div>`;
}

async function loadSummary() {
  const s = await api("/api/summary");
  const providers = Object.entries(s.by_provider || {})
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `${esc(k)} ${v}`)
    .join("  ·  ");
  $("#summary-line").innerHTML =
    `${s.assets} assets · ${s.identities} identities` + (providers ? ` · ${providers}` : "");
}

async function loadFacets() {
  [state.facets, state.projects, state.identities] = await Promise.all([
    api("/api/facets"), api("/api/projects"), api("/api/identities"),
  ]);
  const projectNames = new Map(state.projects.map((p) => [p.slug, p.name]));
  const map = {
    "f-game": ["game", (v) => projectNames.get(v) || v],
    "f-character": ["character_id", (v) => v],
    "f-category": ["category", (v) => v],
    "f-type": ["asset_type", (v) => v],
    "f-perspective": ["perspective", (v) => v],
    "f-provider": ["providers", (v) => v],
  };
  for (const [id, [key, display]] of Object.entries(map)) {
    const sel = $("#" + id);
    const current = sel.value;
    const label = sel.options[0].textContent;
    sel.innerHTML = `<option value="">${esc(label)}</option>` +
      (state.facets[key] || []).map((v) => option(v, display(v), false)).join("");
    sel.value = current;
  }
}

function filterQuery() {
  const q = new URLSearchParams();
  const f = state.filters;
  if (f.q) q.set("q", f.q);
  for (const k of ["game", "character_id", "category", "asset_type", "perspective", "provider"]) {
    if (f[k]) q.set(k, f[k]);
  }
  return q.toString();
}

function assetQuery() {
  const q = new URLSearchParams(filterQuery());
  q.set("limit", state.limit);
  q.set("offset", state.offset);
  return q.toString();
}

async function loadAssets() {
  hideImagePreview();
  stopSequence();
  const page = await api("/api/assets?" + assetQuery());
  state.total = page.total;
  state.assetPage = page.assets;
  const grid = $("#asset-grid");
  grid.innerHTML = page.assets.length
    ? page.assets.map(renderAssetCard).join("")
    : `<p class="empty">No assets match. Run <code>warden sync</code> to index the project.</p>`;
  applyImageSizing(grid);
  const from = page.offset + 1;
  const to = page.offset + page.assets.length;
  $("#page-label").textContent = page.total ? `${from}–${to} of ${page.total}` : "0 assets";
  $("#prev-page").disabled = page.offset <= 0;
  $("#next-page").disabled = page.offset + page.limit >= page.total;
}

function stopFramePlayback() {
  if (state.framePlayback?.timer) clearInterval(state.framePlayback.timer);
  state.framePlayback = null;
}

function stopSequence() {
  if (state.sequenceTimer) clearTimeout(state.sequenceTimer);
  state.sequenceTimer = null;
  state.sequenceToken += 1;
  state.sequencePlaying = false;
  state.sequenceAssets = [];
  state.sequenceIndex = -1;
  const button = $("#sequence-toggle");
  if (button) {
    button.textContent = "Play sequence";
    button.disabled = state.total < 2;
  }
}

function closeViewer() {
  stopSequence();
  stopFramePlayback();
  $("#modal").classList.add("hidden");
}

function drawAnimationFrame(index) {
  const player = state.framePlayback;
  if (!player) return;
  const { animation, context, image } = player;
  const count = animation.frame_count;
  player.frame = (index + count) % count;
  const column = player.frame % animation.columns;
  const row = Math.floor(player.frame / animation.columns);
  context.imageSmoothingEnabled = false;
  context.clearRect(0, 0, animation.frame_width, animation.frame_height);
  try {
    context.drawImage(image, column * animation.frame_width, row * animation.frame_height,
                      animation.frame_width, animation.frame_height, 0, 0,
                      animation.frame_width, animation.frame_height);
  } catch (_) {}
  const counter = $("#animation-counter");
  if (counter) counter.textContent = `${player.frame + 1} / ${count}`;
}

function setAnimationPlaying(playing) {
  const player = state.framePlayback;
  if (!player) return;
  if (player.timer) clearInterval(player.timer);
  player.timer = null;
  player.playing = playing;
  const button = $("#animation-toggle");
  if (button) button.textContent = playing ? "Pause" : "Play";
  if (playing) {
    player.timer = setInterval(() => drawAnimationFrame(player.frame + 1), 1000 / player.fps);
  }
}

function startFramePlayback(asset) {
  const animation = asset.animation;
  const canvas = $("#asset-animation");
  if (!animation || !canvas || !asset.exists) return;
  canvas.width = animation.frame_width;
  canvas.height = animation.frame_height;
  const context = canvas.getContext("2d");
  if (!context) return;
  const image = new Image();
  image.onload = () => {
    if (state.viewerAssetId !== asset.id || $("#modal").classList.contains("hidden")) return;
    state.framePlayback = { animation, canvas, context, image, frame: 0, fps: animation.fps, playing: true, timer: null };
    drawAnimationFrame(0);
    setAnimationPlaying(true);
  };
  image.onerror = () => {
    const status = $("#animation-status");
    if (status) status.textContent = "Could not load the sprite sheet.";
  };
  image.src = `/img/${encodeURIComponent(asset.id)}`;
}

function stepAnimationFrame(delta) {
  if (!state.framePlayback) return;
  drawAnimationFrame(state.framePlayback.frame + delta);
}

function sequenceDelay(asset) {
  const animation = asset?.animation;
  if (!animation) return 1400;
  return Math.min(5000, Math.max(1200, (animation.frame_count / animation.fps) * 2000));
}

function scheduleSequence() {
  if (!state.sequencePlaying || state.sequenceAssets.length < 2) return;
  const current = state.sequenceAssets[state.sequenceIndex];
  state.sequenceTimer = setTimeout(() => {
    state.sequenceIndex = (state.sequenceIndex + 1) % state.sequenceAssets.length;
    const asset = state.sequenceAssets[state.sequenceIndex];
    state.sequenceTimer = null;
    openAsset(asset.id, true);
  }, sequenceDelay(current));
}

async function startSequence() {
  const button = $("#sequence-toggle");
  const token = ++state.sequenceToken;
  button.disabled = true;
  button.textContent = "Loading results…";
  try {
    const result = await api("/api/sequence?" + filterQuery());
    if (token !== state.sequenceToken) return;
    state.sequenceAssets = result.assets;
    state.sequenceIndex = state.sequenceAssets.findIndex((item) => item.id === state.viewerAssetId);
    if (state.sequenceAssets.length < 2 || state.sequenceIndex < 0) {
      button.textContent = "Need more than one result";
      state.sequencePlaying = false;
      button.disabled = true;
      return;
    }
    state.sequencePlaying = true;
    button.disabled = false;
    button.textContent = result.truncated ? "Stop sequence (first 5,000)" : "Stop sequence";
    scheduleSequence();
  } catch (error) {
    state.sequencePlaying = false;
    button.disabled = false;
    button.textContent = `Could not load sequence: ${error.message}`;
  }
}

async function openAsset(id, fromSequence = false) {
  if (!fromSequence) stopSequence();
  stopFramePlayback();
  state.viewerAssetId = id;
  const foundIndex = state.assetPage.findIndex((item) => item.id === id);
  state.viewerIndex = Math.max(0, foundIndex);
  if (fromSequence) {
    state.sequenceIndex = state.sequenceAssets.findIndex((item) => item.id === id);
  }
  const a = await api("/api/assets/" + encodeURIComponent(id));
  if (state.viewerAssetId !== id) return;
  const projects = state.projects;
  const identities = state.identities;
  const prov = a.provenance || {};
  const gates = a.gate && a.gate.gates ? a.gate.gates : {};
  const gateRows = Object.entries(gates)
    .map(([name, v]) => {
      const ok = v && typeof v === "object" ? v.passed : v;
      return `<tr><td>${esc(name)}</td><td>${ok ? '<span class="chip ok">pass</span>' : '<span class="chip bad">fail</span>'}</td>
        <td class="muted">${esc((v && v.summary) || "")}</td></tr>`;
    })
    .join("");
  const meta = (k, v) => (v === undefined || v === null || v === "" ? "" : `<div class="kv"><b>${esc(k)}</b> ${esc(v)}</div>`);
  const animation = a.animation;
  const img = a.exists
    ? `<img src="/img/${encodeURIComponent(a.id)}" alt="${esc(a.name)}">`
    : `<span class="muted">file missing</span>`;
  const media = animation
    ? `<canvas id="asset-animation" width="${animation.frame_width}" height="${animation.frame_height}" aria-label="Animated preview of ${esc(a.name)}"></canvas>`
    : img;
  const projectOptions = projects.map((p) => option(p.slug, p.name, p.slug === a.game)).join("");
  const identityOptions = `<option value="">Unassigned</option>` + identities
    .map((i) => option(i.id, `${i.name} (${i.id})`, i.id === a.character_id)).join("");
  const tags = a.tag_list || String(a.tags || "").split(/\s+/).filter(Boolean);
  const sequencePosition = state.sequencePlaying && state.sequenceIndex >= 0
    ? state.sequenceIndex + 1
    : Math.max(1, state.offset + foundIndex + 1);
  const previousDisabled = state.sequencePlaying ? state.sequenceAssets.length < 2 : foundIndex <= 0;
  const nextDisabled = state.sequencePlaying ? state.sequenceAssets.length < 2 : foundIndex < 0 || foundIndex >= state.assetPage.length - 1;
  const sequenceControls = `<div class="viewer-toolbar">
    <div class="sequence-navigation">
      <button id="asset-previous" class="btn ghost" ${previousDisabled ? "disabled" : ""}>Previous</button>
      <span class="muted">${sequencePosition} / ${state.sequencePlaying ? state.sequenceAssets.length : state.total} filtered results</span>
      <button id="asset-next" class="btn ghost" ${nextDisabled ? "disabled" : ""}>Next</button>
    </div>
    <button id="sequence-toggle" class="btn" ${state.total < 2 ? "disabled" : ""}>${state.sequencePlaying ? "Stop sequence" : "Play sequence"}</button>
  </div>`;
  const animationControls = animation ? `<div class="animation-controls">
    <button id="animation-previous" class="btn ghost" aria-label="Previous animation frame">Prev frame</button>
    <button id="animation-toggle" class="btn">Pause</button>
    <button id="animation-next" class="btn ghost" aria-label="Next animation frame">Next frame</button>
    <output id="animation-counter" class="frame-counter">1 / ${animation.frame_count}</output>
    <label>FPS <input id="animation-fps" type="range" min="1" max="24" value="${animation.fps}"><output id="animation-fps-value">${animation.fps}</output></label>
  </div>` : "";
  $("#modal-content").innerHTML = `
    <h2>${esc(a.name)}</h2>
    <p class="muted">${esc(a.relative_path)}</p>
    <a class="btn" href="/repair?asset_id=${encodeURIComponent(a.id)}">Repair pixels locally</a>
    ${sequenceControls}
    <div class="detail-grid">
      <div>
        <div class="detail-image">${media}</div>
        ${animationControls}
        <div class="row metadata-readout">
          ${meta("id", a.id)}${meta("project", a.game)}${meta("character", a.character_id)}
          ${meta("category", a.category)}${meta("type", a.asset_type)}${meta("perspective", a.perspective)}
          ${meta("palette", a.palette_id)}${meta("size", a.frame_w ? a.frame_w + "×" + a.frame_h : "")}
          ${animation ? meta("frames", `${animation.frame_count} at ${animation.fps} fps`) : ""}
        </div>
        <form id="asset-edit-form" class="edit-form">
          <h4>Organize asset</h4>
          <label>Tags <span class="muted">comma or semicolon separated</span>
            <input id="asset-tags" type="text" maxlength="1600" value="${esc(tags.join(", "))}" placeholder="robot, blue, concept">
          </label>
          <label>Project
            <select id="asset-project" required>${projectOptions}</select>
          </label>
          <label>Character identity
            <select id="asset-character">${identityOptions}</select>
          </label>
          <label>Category
            <input id="asset-category" type="text" maxlength="80" required value="${esc(a.category || "local")}">
          </label>
          <div class="form-actions"><button type="submit" class="btn">Save organization</button><span id="edit-message" class="muted"></span></div>
        </form>
      </div>
      <div>
        <h4>Gates</h4>
        ${gateRows ? `<table><tr><th>gate</th><th>verdict</th><th>summary</th></tr>${gateRows}</table>`
                   : '<p class="muted">No gate record on disk.</p>'}
        ${a.gate && a.gate.review_problem ? `<p class="chip warn">review: ${esc(a.gate.review_problem)}</p>` : ""}
        <h4>Provenance</h4>
        <pre>${esc(JSON.stringify(prov, null, 2))}</pre>
      </div>
    </div>`;
  $("#modal").classList.remove("hidden");
  if (animation) startFramePlayback(a);
  $("#asset-previous").addEventListener("click", () => {
    const list = state.sequencePlaying ? state.sequenceAssets : state.assetPage;
    const index = state.sequencePlaying
      ? (state.sequenceIndex - 1 + list.length) % list.length
      : state.viewerIndex - 1;
    if (index < 0 || !list[index]) return;
    if (state.sequencePlaying) {
      if (state.sequenceTimer) clearTimeout(state.sequenceTimer);
      state.sequenceTimer = null;
      state.sequenceIndex = index;
      openAsset(list[index].id, true);
    } else openAsset(list[index].id);
  });
  $("#asset-next").addEventListener("click", () => {
    const list = state.sequencePlaying ? state.sequenceAssets : state.assetPage;
    const index = state.sequencePlaying
      ? (state.sequenceIndex + 1) % list.length
      : state.viewerIndex + 1;
    if (!list[index]) return;
    if (state.sequencePlaying) {
      if (state.sequenceTimer) clearTimeout(state.sequenceTimer);
      state.sequenceTimer = null;
      state.sequenceIndex = index;
      openAsset(list[index].id, true);
    } else openAsset(list[index].id);
  });
  $("#sequence-toggle").addEventListener("click", () => {
    if (state.sequencePlaying) stopSequence();
    else startSequence();
  });
  if (animation) {
    $("#animation-toggle").addEventListener("click", () => setAnimationPlaying(!state.framePlayback?.playing));
    $("#animation-previous").addEventListener("click", () => stepAnimationFrame(-1));
    $("#animation-next").addEventListener("click", () => stepAnimationFrame(1));
    $("#animation-fps").addEventListener("input", (event) => {
      if (!state.framePlayback) return;
      state.framePlayback.fps = Number(event.target.value);
      $("#animation-fps-value").value = String(state.framePlayback.fps);
      if (state.framePlayback.playing) setAnimationPlaying(true);
    });
  }
  if (state.sequencePlaying) scheduleSequence();
  $("#asset-edit-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = $("#edit-message");
    message.textContent = "Saving…";
    try {
      await mutate(`/api/assets/${encodeURIComponent(id)}`, "PATCH", {
        tags: parseTags($("#asset-tags").value),
        project: $("#asset-project").value,
        character_id: $("#asset-character").value || null,
        category: $("#asset-category").value.trim(),
      });
      await Promise.all([loadSummary(), loadFacets()]);
      if (state.tab === "assets") await loadAssets();
      await openAsset(id);
    } catch (error) {
      message.textContent = error.message;
    }
  });
}

async function renderPipelines() {
  const rows = await api("/api/pipelines");
  const el = $("#view-pipelines");
  if (!rows.length) {
    el.innerHTML = `<p class="empty">No pipeline.json found under ${esc(state.projectRoot || "this project")}.</p>`;
    return;
  }
  el.innerHTML = rows
    .map((p) => {
      const st = p.status || {};
      const views = st.views || {};
      const pct = views.total ? Math.round((100 * views.shown_ready) / views.total) : 0;
      const problems = (p.problems || [])
        .map((pr) => `<li><span class="chip ${pr.level === "error" ? "bad" : "warn"}">${esc(pr.level)}</span> ${esc(pr.text)}</li>`)
        .join("");
      const d = p.data || {};
      return `<div class="panel">
        <h3>${esc(p.bot)} <span class="muted">${esc(p.dir)}</span></h3>
        <div class="row">
          <div class="kv"><b>next</b> ${esc(st.next || p.error || "—")}</div>
          <div class="kv"><b>rotate</b> ${esc(d.rotation_provider || "undecided")}</div>
          <div class="kv"><b>animate</b> ${esc(d.animation_provider || "undecided")}</div>
          <div class="kv"><b>anchor</b> ${st.anchor ? '<span class="chip ok">gated</span>' : '<span class="chip bad">no</span>'}</div>
          <div class="kv"><b>views</b> ${views.shown_ready || 0}/${views.total || 0} <progress max="100" value="${pct}"></progress></div>
        </div>
        ${problems ? `<h4>Problems</h4><ul>${problems}</ul>` : ""}
      </div>`;
    })
    .join("");
}

async function renderIdentities() {
  const rows = await api("/api/identities");
  const el = $("#view-identities");
  if (!rows.length) {
    el.innerHTML = `<p class="empty">No identities registered.</p>`;
    return;
  }
  el.innerHTML = rows
    .map((i) => {
      const feats = Object.keys(i.features || {});
      const stills = Object.keys(i.turnaround_stills || {});
      return `<div class="panel">
        <h3>${esc(i.name)} <span class="muted">${esc(i.id)}</span></h3>
        <div class="row">
          <div class="kv"><b>game</b> ${esc(i.game)}</div>
          <div class="kv"><b>palette</b> ${esc(i.palette_id)}</div>
          <div class="kv"><b>perspective</b> ${esc(i.perspective)}</div>
          <div class="kv"><b>cell</b> ${(i.dimensions || []).join("×")}</div>
          <div class="kv"><b>locomotion</b> ${esc(i.locomotion || "—")}</div>
          <div class="kv"><b>body</b> ${esc(i.body_class || "—")}</div>
          <div class="kv"><b>tier</b> ${esc(i.size_tier || "—")}</div>
        </div>
        <p style="margin:8px 0 0">${esc(i.prompt_description || "")}</p>
        <div class="chips" style="margin-top:8px">${chips(feats.map((f) => ({ label: f })))}${chips(stills.map((s) => ({ label: "still:" + s, cls: "ok" })))}</div>
      </div>`;
    })
    .join("");
}

async function renderProjects() {
  const projects = await api("/api/projects");
  const el = $("#view-projects");
  const cards = projects.map((p) => `
    <div class="panel project-card">
      <div><h3>${esc(p.name)}</h3><div class="muted">${esc(p.slug)}</div></div>
      <div class="project-count">${p.asset_count} assets</div>
      ${p.description ? `<p>${esc(p.description)}</p>` : ""}
      <button type="button" class="btn ghost project-browse" data-project="${esc(p.slug)}">Browse assets</button>
    </div>`).join("");
  el.innerHTML = `
    <div class="panel">
      <h3>Add project</h3>
      <p class="muted">Projects group catalog assets. Creating one does not create or modify folders on disk.</p>
      <form id="project-create-form" class="project-form">
        <label>Project name<input id="project-name" type="text" maxlength="100" required placeholder="My game"></label>
        <label>Slug<input id="project-slug" type="text" maxlength="64" placeholder="my-game"></label>
        <label>Description<input id="project-description" type="text" maxlength="500" placeholder="Optional"></label>
        <div class="form-actions"><button type="submit" class="btn">Create project</button><span id="project-message" class="muted"></span></div>
      </form>
    </div>
    <div class="project-grid">${cards || '<p class="empty">No projects yet.</p>'}</div>`;

  $("#project-name").addEventListener("input", (event) => {
    const slug = $("#project-slug");
    if (!slug.dataset.edited) slug.value = slugify(event.target.value);
  });
  $("#project-slug").addEventListener("input", (event) => { event.target.dataset.edited = "1"; });
  $("#project-create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = $("#project-message");
    const name = $("#project-name").value.trim();
    const slug = $("#project-slug").value.trim() || slugify(name);
    message.textContent = "Creating…";
    try {
      await mutate("/api/projects", "POST", {
        name,
        slug,
        description: $("#project-description").value.trim(),
      });
      await Promise.all([loadSummary(), loadFacets(), renderProjects()]);
    } catch (error) {
      message.textContent = error.message;
    }
  });
  el.querySelectorAll(".project-browse").forEach((button) => button.addEventListener("click", () => {
    state.filters.game = button.dataset.project;
    state.offset = 0;
    $("#f-game").value = state.filters.game;
    showTab("assets");
  }));
}

async function renderRejections() {
  const data = await api("/api/rejections");
  const el = $("#view-rejections");
  const groups = (data.groups || [])
    .map((g) => `<tr><td>${g.count}×</td><td>${esc(g.provider)}</td><td>${esc(g.stage)}</td><td>${esc(g.what)}</td><td class="muted">${esc(g.example)}</td></tr>`)
    .join("");
  const entries = (data.entries || [])
    .slice(0, 60)
    .map((e) => `<tr><td class="muted">${esc((e.time || "").slice(0, 19))}</td><td>${esc(e.by)}</td><td>${esc(e.image)}</td><td>${esc(e.provider || "")}</td><td class="muted">${esc(e.reason || (e.failed || []).map((f) => f.name).join(", "))}</td></tr>`)
    .join("");
  el.innerHTML = `
    <div class="panel">
      <h3>Repeat offenders</h3>
      <p class="muted">${esc(data.log)} ${data.exists ? "" : "(not written yet)"}</p>
      ${groups ? `<table><tr><th>count</th><th>provider</th><th>stage</th><th>what</th><th>example</th></tr>${groups}</table>` : '<p class="empty">No rejections logged.</p>'}
    </div>
    ${entries ? `<div class="panel"><h3>Recent</h3><table><tr><th>time</th><th>by</th><th>image</th><th>provider</th><th>reason</th></tr>${entries}</table></div>` : ""}`;
}

async function renderProviders() {
  const data = await api("/api/providers");
  const rules = (data.pipeline_rules || []).map((r) => `<li>${esc(r)}</li>`).join("");
  const stages = (data.stages || [])
    .map((s) => `<tr><td>${esc(s.stage)}</td><td>${esc(s.what)}</td><td>${esc(s.gate)}</td><td class="muted">${esc(s.command || "")}</td></tr>`)
    .join("");
  const providers = (data.providers || [])
    .map((p) => {
      const ident = p.identity || {};
      return `<div class="panel">
        <h3>${esc(p.label)} <span class="muted">${esc(p.name)}</span></h3>
        ${ident.concept ? `<p>${esc(ident.concept)}</p>` : ""}
        <div class="row">
          <div class="kv"><b>own character</b> ${ident.entity ? "yes" : "no"}</div>
          <div class="kv"><b>ops</b> ${p.operations.length}</div>
          <div class="kv"><b>spends on</b> ${esc((p.spends || []).join(", ") || "—")}</div>
        </div>
        <div class="chips" style="margin-top:8px">${chips(p.operations.slice(0, 18).map((o) => ({ label: o })))}</div>
      </div>`;
    })
    .join("");
  $("#view-providers").innerHTML = `
    <div class="panel"><h3>Standing rules</h3><ol>${rules || "<li class='muted'>none</li>"}</ol></div>
    <div class="panel"><h3>Stages</h3>${stages ? `<table><tr><th>stage</th><th>what</th><th>gate</th><th>command</th></tr>${stages}</table>` : '<p class="muted">none</p>'}</div>
    ${providers}`;
}

const loaders = { pipelines: renderPipelines, identities: renderIdentities, projects: renderProjects, rejections: renderRejections, providers: renderProviders };

function showTab(tab) {
  hideImagePreview();
  setViewPanelOpen(false);
  state.tab = tab;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  $("#view-" + tab).classList.remove("hidden");
  $("#filters").classList.toggle("hidden", tab !== "assets");
  $("#view-settings").classList.toggle("hidden", tab !== "assets");
  if (tab === "assets") {
    loadAssets();
  } else {
    loaders[tab]();
  }
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function init() {
  initViewSettings();

  Promise.all([api("/api/session"), loadSummary(), loadFacets()]).then(([session]) => {
    state.csrfToken = session.csrf_token;
    return loadAssets();
  }).catch((e) => {
    $("#summary-line").textContent = "error: " + e.message;
  });

  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

  $("#search").addEventListener("input", debounce((e) => {
    state.filters.q = e.target.value.trim();
    state.offset = 0;
    if (state.tab === "assets") loadAssets();
  }, 220));

  const bindings = {
    "f-game": "game", "f-character": "character_id", "f-category": "category",
    "f-type": "asset_type", "f-perspective": "perspective", "f-provider": "provider",
  };
  for (const [id, key] of Object.entries(bindings)) {
    $("#" + id).addEventListener("change", (e) => {
      state.filters[key] = e.target.value;
      state.offset = 0;
      if (state.tab === "assets") loadAssets();
    });
  }

  $("#clear-filters").addEventListener("click", () => {
    for (const k of Object.keys(state.filters)) state.filters[k] = "";
    $("#search").value = "";
    for (const id of Object.keys(bindings)) $("#" + id).value = "";
    state.offset = 0;
    if (state.tab === "assets") loadAssets();
  });

  $("#prev-page").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - state.limit);
    loadAssets();
  });
  $("#next-page").addEventListener("click", () => {
    if (state.offset + state.limit < state.total) { state.offset += state.limit; loadAssets(); }
  });

  $("#refresh").addEventListener("click", () => {
    loadSummary(); loadFacets();
    if (state.tab === "assets") loadAssets(); else loaders[state.tab]();
  });

  // Image errors do not bubble; capture handles dynamically rendered cards.
  $("#asset-grid").addEventListener("error", (event) => {
    const image = event.target;
    if (!(image instanceof HTMLImageElement) || !image.matches(".card .thumb img")) return;
    const missing = document.createElement("span");
    missing.className = "muted";
    missing.textContent = "no file";
    image.replaceWith(missing);
  }, true);
  $("#asset-grid").addEventListener("pointerover", (event) => {
    if (event.pointerType === "touch") return;
    const card = event.target.closest(".card");
    if (card) showImagePreview(card, event.clientX, event.clientY);
  });
  $("#asset-grid").addEventListener("pointermove", (event) => {
    const card = event.target.closest(".card");
    if (card && card === state.previewCard) {
      state.previewPoint = { x: event.clientX, y: event.clientY };
      positionImagePreview(event.clientX, event.clientY);
    }
  });
  $("#asset-grid").addEventListener("pointerout", (event) => {
    const card = event.target.closest(".card");
    if (card && !card.contains(event.relatedTarget)) hideImagePreview();
  });
  $("#asset-grid").addEventListener("focusin", (event) => {
    const card = event.target.closest(".card");
    if (card) {
      const rect = card.getBoundingClientRect();
      showImagePreview(card, rect.right, rect.top);
    }
  });
  $("#asset-grid").addEventListener("focusout", (event) => {
    const card = event.target.closest(".card");
    if (card && !card.contains(event.relatedTarget)) hideImagePreview();
  });
  $("#asset-grid").addEventListener("click", (event) => {
    const card = event.target.closest(".card");
    if (card) {
      hideImagePreview();
      openAsset(card.dataset.id);
    }
  });
  $("#asset-grid").addEventListener("keydown", (event) => {
    const card = event.target.closest(".card");
    if (card && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      hideImagePreview();
      openAsset(card.dataset.id);
    }
  });
  window.addEventListener("scroll", hideImagePreview, true);
  window.addEventListener("resize", hideImagePreview);

  $("#modal").addEventListener("click", (e) => {
    if (e.target.dataset.close) closeViewer();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      setViewPanelOpen(false);
      closeViewer();
      hideImagePreview();
    }
  });
}

document.addEventListener("DOMContentLoaded", init);
