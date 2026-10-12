"use strict";
const el = id => document.getElementById(id);
let pipelines = [], current, revision, token, frame = 0, beforeImage, afterImage;
async function json(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}
const showError = error => { el('message').textContent = error.message; };
function options(id, entries) {
  el(id).replaceChildren(...entries.map(([value, label]) => new Option(label, value)));
}
async function refresh(keep) {
  pipelines = (await json('/api/review')).pipelines;
  options('pipeline', pipelines.map(p => [p.id, p.folder]));
  if (keep) el('pipeline').value = keep;
  await selectPipeline();
}
async function selectPipeline() {
  current = pipelines.find(p => p.id === el('pipeline').value);
  if (!current) { el('message').textContent = 'No revisioned pipelines yet. Register candidates with the workflow service; legacy notes are not approvals.'; return; }
  const entries = Object.entries(current.state.revisions);
  options('revision', entries.map(([id, rev]) => [id, `${rev.stage} · ${rev.provider} · ${id.slice(0, 12)}${current.state.current[rev.stage] === id ? ' · current' : ''}`]));
  await selectRevision();
}
async function selectRevision() {
  revision = current.state.revisions[el('revision').value];
  if (!revision) { el('message').textContent = 'Legacy pipeline: no registered revisions. Migration requires explicit review.'; return; }
  options('facing', Object.keys(revision.files).map(k => [k,k]));
  options('before', [['','No comparison'], ...Object.entries(current.state.revisions).filter(([id]) => id !== revision.revision_id).map(([id,r]) => [id, `${r.stage} · ${r.provider} · ${id.slice(0,12)}`])]);
  const dependency = Object.values(revision.dependencies)[0];
  if (dependency) el('before').value = dependency;
  el('readiness').textContent = JSON.stringify(current.state.readiness[revision.revision_id], null, 2);
  el('lineage').textContent = JSON.stringify({revision, decisions: current.state.decisions.filter(d => d.revision_id === revision.revision_id)}, null, 2);
  el('trace').textContent = JSON.stringify(await json('/api/trace?' + new URLSearchParams(revision.run_id ? {run_id:revision.run_id} : {})), null, 2);
  frame = 0; await loadImages();
}
function imageURL(id, facing) {
  return '/api/revision-image?' + new URLSearchParams({pipeline_id:current.id, revision_id:id, facing});
}
function image(url) {
  return new Promise((resolve,reject) => { const result = new Image(); result.onload = () => resolve(result); result.onerror = () => reject(new Error('Archived image unavailable')); result.src = url; });
}
async function loadImages() {
  if (!revision) return;
  afterImage = await image(imageURL(revision.revision_id, el('facing').value));
  const before = current.state.revisions[el('before').value];
  beforeImage = before ? await image(imageURL(before.revision_id, before.files[el('facing').value] ? el('facing').value : Object.keys(before.files)[0])) : null;
  render();
}
function draw(id, img) {
  const canvas = el(id), scale = Number(el('scale').value);
  let w = img ? img.naturalWidth : afterImage.naturalWidth, h = img ? img.naturalHeight : afterImage.naturalHeight;
  let x = 0, y = 0;
  if (el('sheet').checked) {
    w = Math.min(w, Math.max(1, Number(el('frame-width').value) || 64));
    h = Math.min(h, Math.max(1, Number(el('frame-height').value) || 64));
    const columns = Math.max(1, Math.floor((img || afterImage).naturalWidth / w));
    const rows = Math.max(1, Math.floor((img || afterImage).naturalHeight / h));
    const index = ((frame % (columns * rows)) + columns * rows) % (columns * rows);
    x = index % columns * w; y = Math.floor(index / columns) * h;
  }
  canvas.width = w; canvas.height = h; canvas.style.width = w * scale + 'px'; canvas.style.height = h * scale + 'px';
  canvas.style.background = el('background').value;
  const ctx = canvas.getContext('2d'); ctx.imageSmoothingEnabled = false;
  if (img) ctx.drawImage(img, x,y,w,h,0,0,w,h);
}
function render() {
  if (!afterImage) return;
  draw('after-canvas', afterImage); draw('before-canvas', beforeImage); draw('diff-canvas', null);
  el('frame-step').textContent = 'Frame step ' + (frame + 1);
  const a = el('before-canvas'), b = el('after-canvas'), d = el('diff-canvas');
  const w = Math.max(a.width,b.width), h = Math.max(a.height,b.height);
  const left = a.getContext('2d').getImageData(0,0,w,h).data, right = b.getContext('2d').getImageData(0,0,w,h).data;
  d.width = w; d.height = h; d.style.width = w * Number(el('scale').value) + 'px'; d.style.height = h * Number(el('scale').value) + 'px';
  const ctx = d.getContext('2d'), diff = ctx.createImageData(w,h); let changed = 0;
  for (let i=0;i<left.length;i+=4) if (left[i]!==right[i] || left[i+1]!==right[i+1] || left[i+2]!==right[i+2] || left[i+3]!==right[i+3]) { diff.data.set([255,0,160,255],i); changed++; }
  ctx.putImageData(diff,0,0);
  el('comparison').textContent = beforeImage ? `${changed} changed pixels in this preview; before ${beforeImage.naturalWidth}×${beforeImage.naturalHeight}, candidate ${afterImage.naturalWidth}×${afterImage.naturalHeight}. No scaling is used for the pixel comparison.` : 'Choose a source or earlier revision to compare.';
}
el('pipeline').onchange = () => selectPipeline().catch(showError);
el('revision').onchange = () => selectRevision().catch(showError);
for (const id of ['facing','before']) el(id).onchange = () => loadImages().catch(showError);
for (const id of ['scale','background','frame-width','frame-height','sheet']) el(id).onchange = render;
el('previous').onclick = () => { frame = Math.max(0, frame-1); render(); };
el('next').onclick = () => { frame++; render(); };
el('decision').onsubmit = async event => {
  event.preventDefault(); if (!revision) return;
  const button = event.submitter; button.disabled = true;
  try {
    const payload = {pipeline_id:current.id, revision_id:revision.revision_id, expected_version:current.state.version,
      decision:el('choice').value, actor:el('actor').value, reason:el('reason').value, operation:revision.operation,
      gate:el('gate').value || null, run_id:revision.run_id};
    await json('/api/decisions', {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify(payload)});
    el('message').textContent = 'Decision recorded. Machine evidence remains unchanged. Promotion is a separate workflow action.';
    await refresh(current.id);
  } catch (error) { showError(error); } finally { button.disabled = false; }
};
(async () => { token = (await json('/api/session')).csrf_token; await refresh(); })().catch(showError);
