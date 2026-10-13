'use strict';
const $ = id => document.getElementById(id);
let info, original, candidate, history = [], selection = null, start = null, token, backdropURL;
const status = text => { $('status').textContent = text; };
async function api(url, options) { const r = await fetch(url, options); const data = await r.json(); if (!r.ok) throw Error(data.error || r.statusText); return data; }
function rgb(hex) { return [1,3,5].map(i=>parseInt(hex.slice(i,i+2),16)); }
function patch() {
  const changes=[];
  for(let i=0;i<original.data.length;i+=4) if(original.data.slice(i,i+4).some((v,j)=>v!==candidate.data[i+j])) changes.push([(i/4)%info.width,Math.floor(i/4/info.width),Array.from(original.data.slice(i,i+4)),Array.from(candidate.data.slice(i,i+4))]);
  return {version:1,source_digest:info.source_digest,size:[info.width,info.height],changes};
}
function render() {
  if(!info)return;
  const diff=new ImageData(info.width,info.height), changes=patch().changes;
  for(const [x,y] of changes) diff.data.set([255,0,150,255],(y*info.width+x)*4);
  for(const [name,data] of [['original',original],['candidate',candidate],['difference',diff]]) {
    const c=$(name); c.width=info.width;c.height=info.height;c.style.width=info.width*Number($('zoom').value)+'px';c.style.height=info.height*Number($('zoom').value)+'px';
    c.style.backgroundColor=$('background').value==='checker'?'#bbb':$('background').value;
    c.style.backgroundImage=backdropURL?`url(${backdropURL})`:($('background').value==='checker'?'conic-gradient(#aaa 25%,#ddd 0 50%,#aaa 0 75%,#ddd 0)':'none');c.style.backgroundSize=backdropURL?'cover':'16px 16px';
    const ctx=c.getContext('2d');ctx.imageSmoothingEnabled=false;ctx.putImageData(data,0,0);
    if(name==='original'&&selection){ctx.strokeStyle='#ff008c';ctx.lineWidth=1;ctx.strokeRect(selection.x+.5,selection.y+.5,selection.w-1,selection.h-1);}
  }
  status(`${selection?`Selected ${selection.w} × ${selection.h} region. `:''}${changes.length} changed pixels. Original and existing approvals untouched.`);
}
async function load() {
  info=await api('/api/repair?'+new URLSearchParams({asset_id:$('asset').value}));
  const im=new Image(); await new Promise((yes,no)=>{im.onload=yes;im.onerror=()=>no(Error('Cannot load original'));im.src='/img/'+encodeURIComponent(info.asset_id);});
  if(im.width!==info.width||im.height!==info.height)throw Error('Source changed; reload');
  const c=document.createElement('canvas');c.width=im.width;c.height=im.height;c.getContext('2d').drawImage(im,0,0);
  original=c.getContext('2d').getImageData(0,0,c.width,c.height);candidate=new ImageData(new Uint8ClampedArray(original.data),c.width,c.height);history=[];selection=null;
  $('palette').replaceChildren(...[...new Set(info.palette)].slice(0,256).map(color=>{const b=document.createElement('button');b.style.background=color;b.title=color+' · click source, Shift-click replacement';b.onclick=e=>{$(e.shiftKey?'target-color':'source-color').value=color;};return b;}));
  $('checks').replaceChildren();
  const gates=info.gate?.gates||{};
  for(const [name,g] of Object.entries(gates)){const d=document.createElement('details'), s=document.createElement('summary'),p=document.createElement('pre');s.textContent=`${name}: ${g.status==='unavailable'?'unavailable':g.passed?'PASS':'FAIL'} — ${g.summary||''}`;p.textContent=JSON.stringify(g,null,2);d.append(s,p);$('checks').append(d);}
  if(!Object.keys(gates).length)$('checks').textContent='No gate evidence. Saving a repair does not create approval.';
  render();
}
function point(e){const r=$('original').getBoundingClientRect();return [Math.max(0,Math.min(info.width-1,Math.floor((e.clientX-r.left)*info.width/r.width))),Math.max(0,Math.min(info.height-1,Math.floor((e.clientY-r.top)*info.height/r.height)))];}
$('original').onpointerdown=e=>{if(!info)return;start=point(e);selection={x:start[0],y:start[1],w:1,h:1};render();$('original').setPointerCapture(e.pointerId);};
$('original').onpointermove=e=>{if(!start)return;const p=point(e);selection={x:Math.min(start[0],p[0]),y:Math.min(start[1],p[1]),w:Math.abs(p[0]-start[0])+1,h:Math.abs(p[1]-start[1])+1};render();};
$('original').onpointerup=e=>{if(start&&!selection)selection={x:start[0],y:start[1],w:1,h:1};start=null;render();};
$('apply').onclick=()=>{if(!selection)return status('Select a region first.');history.push(new Uint8ClampedArray(candidate.data));const from=rgb($('source-color').value),to=rgb($('target-color').value);for(let y=selection.y;y<selection.y+selection.h;y++)for(let x=selection.x;x<selection.x+selection.w;x++){const i=(y*info.width+x)*4;if(from.every((v,j)=>candidate.data[i+j]===v))candidate.data.set(to,i);}render();};
$('undo').onclick=()=>{if(history.length)candidate.data.set(history.pop());render();};
$('reset').onclick=()=>{if(!info)return;history.push(new Uint8ClampedArray(candidate.data));candidate.data.set(original.data);selection=null;render();};
$('example').onchange=async e=>{try{if(!info)throw Error('Choose an asset first');const file=e.target.files[0];if(!file)return;if(file.size>4*1024*1024)throw Error('Example must be smaller than 4 MiB');const im=await createImageBitmap(file);if(im.width!==info.width||im.height!==info.height){im.close();throw Error('Example dimensions must match original');}const c=document.createElement('canvas');c.width=im.width;c.height=im.height;c.getContext('2d').drawImage(im,0,0);history.push(new Uint8ClampedArray(candidate.data));candidate=c.getContext('2d').getImageData(0,0,c.width,c.height);im.close();e.target.value='';render();}catch(err){status(err.message);}};
$('backdrop').onchange=e=>{const file=e.target.files[0];if(!file){backdropURL=null;render();return;}if(file.size>4*1024*1024)return status('Background must be smaller than 4 MiB');const reader=new FileReader();reader.onload=()=>{backdropURL=reader.result;render();};reader.readAsDataURL(file);};
$('patch-download').onclick=()=>{if(!info)return;const u=URL.createObjectURL(new Blob([JSON.stringify(patch(),null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=u;a.download='pixel-repair.patch.json';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);};
$('save').onclick=async()=>{const b=$('save');b.disabled=true;try{if(!info)throw Error('Choose asset first');const body=JSON.stringify({asset_id:info.asset_id,source_digest:info.source_digest,patch:patch(),actor:$('actor').value,reason:$('reason').value});if(new TextEncoder().encode(body).length>32768)throw Error('Patch exceeds server limit; save a smaller repair');const saved=await api('/api/repair',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body});$('saved').textContent=`Saved ${saved.changed_pixels} changes as a NEW candidate:\n${saved.path}\nApproval: ${saved.approval}. Rejudge before using.`;}catch(e){status(e.message);}finally{b.disabled=false;}};
$('asset').onchange=()=>load().catch(e=>status(e.message));$('background').onchange=()=>{if(backdropURL)URL.revokeObjectURL(backdropURL);backdropURL=null;render();};$('zoom').onchange=render;
(async()=>{token=(await api('/api/session')).csrf_token;const q=new URLSearchParams(location.search);const data=await api('/api/assets?limit=500');const list=Array.isArray(data)?data:data.assets||data.items||[];$('asset').replaceChildren(...list.map(a=>new Option(a.relative_path||a.name,a.id)));if(q.get('asset_id')){const id=q.get('asset_id');if(!list.some(a=>a.id===id)){$('asset').append(new Option(id,id));}$('asset').value=id;}if($('asset').value)await load();else status('No catalogued assets. Index project assets first.');})().catch(e=>status(e.message));
