import * as THREE from 'three';
import ForceGraph3D from '3d-force-graph';
import SpriteText from 'three-spritetext';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';

const $ = (id) => document.getElementById(id);
const api = (p, opt) => fetch(p, opt).then((r) => r.json());
const post = (p, body) => api(p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

const AREA = { residential: '#7aa2ff', knowledge: '#4ff0d0', work: '#ff8f5a' };
const GOLD = '#ffd27a';
let view = 'city';
let WORLD = null, CITY = null;
const pulses = new Map();          // node id -> {mesh, until}
const meshes = new Map();          // node id -> THREE.Object3D
const nodeCache = new Map();       // node id -> node object (keeps positions across refreshes)
let signature = '';

function hue(id) { let h = 0; for (const c of id) h = (h * 31 + c.charCodeAt(0)) >>> 0; return h % 360; }
function cityColor(id) { return `hsl(${hue(id)}, 85%, 66%)`; }

// ---------------------------------------------------------------- the scene
const G = new ForceGraph3D($('graph'), { controlType: 'orbit' })
  .backgroundColor('#04050a')
  .showNavInfo(false)
  .nodeLabel((n) => n.label || n.id)
  .nodeThreeObject(makeNode)
  .linkColor((l) => l.color)
  .linkOpacity(0.32)
  .linkWidth((l) => (l.kind === 'road' ? (l.hub ? 1.4 : 0.7) : 0.2))
  .linkCurvature((l) => (l.kind === 'road' ? 0.22 : 0))
  .linkDirectionalParticles((l) => (l.kind === 'road' ? 3 : l.kind === 'heaven' ? 2 : 0))
  .linkDirectionalParticleSpeed((l) => (l.kind === 'road' ? 0.0035 : 0.007))
  .linkDirectionalParticleWidth((l) => (l.kind === 'road' ? 2.6 : 1.4))
  .linkDirectionalParticleColor((l) => l.color)
  .onNodeClick(select)
  .d3VelocityDecay(0.35);

const bloom = new UnrealBloomPass(new THREE.Vector2(innerWidth, innerHeight), 1.25, 0.55, 0.04);
G.postProcessingComposer().addPass(bloom);
addStars(G.scene());
addEventListener('resize', () => G.width(innerWidth).height(innerHeight));

function addStars(scene) {
  const g = new THREE.BufferGeometry(); const pts = [];
  let s = 7; const rnd = () => ((s = (s * 16807) % 2147483647) / 2147483647);
  for (let i = 0; i < 1800; i++) { const r = 2400 + rnd() * 2400, t = rnd() * Math.PI * 2, p = Math.acos(2 * rnd() - 1);
    pts.push(r * Math.sin(p) * Math.cos(t), r * Math.sin(p) * Math.sin(t), r * Math.cos(p)); }
  g.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
  scene.add(new THREE.Points(g, new THREE.PointsMaterial({ color: 0x8090c0, size: 2.2, sizeAttenuation: false, transparent: true, opacity: 0.55 })));
}

function makeNode(n) {
  const grp = new THREE.Group();
  const r = { core: 16, heaven: 13, portal: 9, area: 6, building: 4.2, citizen: 1.4 }[n.kind] || 3;
  const mat = new THREE.MeshBasicMaterial({ color: n.color, transparent: true, opacity: n.dim ? 0.25 : 0.95 });
  const mesh = new THREE.Mesh(new THREE.SphereGeometry(r, n.kind === 'citizen' ? 6 : 20, n.kind === 'citizen' ? 6 : 16), mat);
  grp.add(mesh);
  if (n.kind === 'core' || n.kind === 'heaven' || n.kind === 'portal') {
    const halo = new THREE.Mesh(new THREE.SphereGeometry(r * 1.9, 20, 16),
      new THREE.MeshBasicMaterial({ color: n.color, transparent: true, opacity: 0.07, depthWrite: false }));
    grp.add(halo);
  }
  if (['core', 'heaven', 'portal', 'building', 'area'].includes(n.kind)) {
    const t = new SpriteText(n.title || n.id, n.kind === 'core' ? 9 : n.kind === 'building' ? 3.2 : 4.5, n.dim ? '#55607a' : '#dfe6ff');
    t.fontFace = 'JetBrains Mono'; t.position.y = r + (n.kind === 'core' ? 12 : 6);
    grp.add(t);
  }
  meshes.set(n.id, mesh);
  return grp;
}

function node(id, props) {
  const n = nodeCache.get(id) || { id };
  Object.assign(n, props);
  nodeCache.set(id, n);
  return n;
}

// ---------------------------------------------------------------- building the two views
function cityView() {
  const me = CITY.city, nodes = [], links = [];
  const core = node(`city:${me}`, { kind: 'core', title: CITY.name, color: cityColor(me), fx: 0, fy: 0, fz: 0, data: CITY });
  nodes.push(core);
  for (const a of ['residential', 'knowledge', 'work']) {
    nodes.push(node(`area:${me}:${a}`, { kind: 'area', title: a, color: AREA[a] }));
    links.push({ source: core.id, target: `area:${me}:${a}`, kind: 'area', color: AREA[a] });
  }
  // the canon: residential and work meet only through the knowledge center
  links.push({ source: `area:${me}:residential`, target: `area:${me}:knowledge`, kind: 'heaven', color: AREA.knowledge });
  links.push({ source: `area:${me}:knowledge`, target: `area:${me}:work`, kind: 'heaven', color: AREA.knowledge });
  for (const b of CITY.buildings) {
    const id = `b:${me}:${b.id}`;
    nodes.push(node(id, { kind: 'building', title: b.id, color: b.paused ? '#555' : AREA[b.area], data: b }));
    links.push({ source: `area:${me}:${b.area}`, target: id, kind: 'b', color: AREA[b.area] });
  }
  for (const c of CITY.citizens_list || []) {
    const id = `a:${c.id}`;
    nodes.push(node(id, { kind: 'citizen', title: c.name, label: `${c.name} · ${c.role} · ${c.wallet}`, color: AREA[c.place] || '#888', data: c }));
    links.push({ source: `b:${me}:${c.building}`, target: id, kind: 'c', color: '#334' });
  }
  if (WORLD.heaven === me) {
    nodes.push(node('heaven', { kind: 'heaven', title: 'heaven', color: GOLD, fx: 0, fy: 230, fz: 0 }));
    links.push({ source: 'heaven', target: core.id, kind: 'heaven', color: GOLD });
  }
  // pipelines leaving the monitor: one beam per other city, anchored far outside the frame
  const peers = Object.keys(WORLD.peers || {});
  peers.forEach((p, i) => {
    const ang = (i / Math.max(1, peers.length)) * Math.PI * 2 + Math.PI / 5;
    const up = WORLD.peers[p].up;
    const id = `portal:${p}`;
    nodes.push(node(id, { kind: 'portal', title: `→ ${p}`, color: cityColor(p), dim: !up, fx: Math.cos(ang) * 1100, fy: Math.sin(ang) * 260, fz: Math.sin(ang) * 500, data: WORLD.peers[p] }));
    links.push({ source: core.id, target: id, kind: 'road', color: up ? cityColor(p) : '#2a3040', hub: true });
    links.push({ source: id, target: core.id, kind: 'road', color: up ? cityColor(me) : '#2a3040', hub: true });
  });
  return { nodes, links };
}

function worldView() {
  const all = { [WORLD.me]: { up: true, summary: WORLD.self } };
  for (const [k, v] of Object.entries(WORLD.peers || {})) all[k] = v;
  for (const d of WORLD.declared || []) if (!all[d.id]) all[d.id] = { up: false, summary: { name: d.name, hub: d.hub, buildings: [] } };
  const ids = Object.keys(all).sort();
  const R = 240 + 60 * ids.length, nodes = [], links = [];
  ids.forEach((c, i) => {
    const ang = (i / ids.length) * Math.PI * 2;
    const s = all[c].summary || {};
    nodes.push(node(`city:${c}`, { kind: 'core', title: s.name || c, color: cityColor(c), dim: !all[c].up,
      fx: Math.cos(ang) * R, fy: 0, fz: Math.sin(ang) * R, data: { city: c, ...all[c] } }));
    for (const b of s.buildings || []) {
      const id = `b:${c}:${b.id}`;
      nodes.push(node(id, { kind: 'building', title: b.id, color: AREA[b.area], dim: !all[c].up, data: b }));
      links.push({ source: `city:${c}`, target: id, kind: 'b', color: AREA[b.area] });
    }
  });
  for (let i = 0; i < ids.length; i++) for (let j = i + 1; j < ids.length; j++) {
    const a = ids[i], b = ids[j], up = all[a].up && all[b].up;
    const hub = (all[a].summary || {}).hub || (all[b].summary || {}).hub;
    links.push({ source: `city:${a}`, target: `city:${b}`, kind: 'road', color: up ? cityColor(a) : '#232838', hub });
    links.push({ source: `city:${b}`, target: `city:${a}`, kind: 'road', color: up ? cityColor(b) : '#232838', hub });
  }
  nodes.push(node('heaven', { kind: 'heaven', title: 'heaven', color: GOLD, fx: 0, fy: 260, fz: 0 }));
  links.push({ source: 'heaven', target: `city:${WORLD.heaven}`, kind: 'heaven', color: GOLD });
  return { nodes, links };
}

function render() {
  if (!WORLD || !CITY) return;
  const data = view === 'city' ? cityView() : worldView();
  const sig = view + '|' + data.nodes.map((n) => n.id + (n.dim ? '-' : '+') + n.color).join(',') + '|' + data.links.map((l) => l.source + l.target + l.color).join(',');
  if (sig !== signature) {
    signature = sig;
    G.graphData(data);
    if (!render.placed?.[view]) { G.cameraPosition(view === 'city' ? { x: 0, y: 120, z: 520 } : { x: 0, y: 420, z: 900 }, { x: 0, y: 40, z: 0 }, 1200); render.placed = { ...(render.placed || {}), [view]: true }; }
  }
}

// ---------------------------------------------------------------- live effects
function linkBetween(a, b) {
  const { links } = G.graphData();
  return links.find((l) => (l.source.id || l.source) === a && (l.target.id || l.target) === b);
}

function flow(from, to) {
  const me = WORLD?.me;
  const pairs = [];
  const targets = to === 'all' ? Object.keys(WORLD?.peers || {}).concat(me).filter((c) => c !== from) : [to];
  for (const t of targets) {
    if (view === 'world') pairs.push([`city:${from}`, `city:${t}`]);
    else if (from === me) pairs.push([`city:${me}`, `portal:${t}`]);
    else if (t === me) pairs.push([`portal:${from}`, `city:${me}`]);
  }
  for (const [a, b] of pairs) { const l = linkBetween(a, b); if (l) { G.emitParticle(l); setTimeout(() => G.emitParticle(l), 120); } }
}

function pulse(id, scale = 2.2, ms = 1400) {
  const m = meshes.get(id); if (!m) return;
  pulses.set(id, { m, t0: performance.now(), ms, scale });
}
(function animate() {
  const now = performance.now();
  for (const [id, p] of pulses) {
    const k = (now - p.t0) / p.ms;
    if (k >= 1) { p.m.scale.setScalar(1); pulses.delete(id); continue; }
    p.m.scale.setScalar(1 + (p.scale - 1) * Math.sin(Math.PI * k));
  }
  requestAnimationFrame(animate);
})();

// ---------------------------------------------------------------- the story column
const STORY = {
  'job.commissioned': (d, e) => ['✶ commissioned: ' + (d.title || e.subject) + ' (by ' + d.by + ')', ''],
  'stage.started': (d, e) => [`${d.building} took up ${short(e.subject)}`, 'work'],
  'stage.done': (d, e) => [`${d.building} ${d.verdict === 'pass' ? 'finished' : d.verdict} ${short(e.subject)}${d.reason ? ' — ' + d.reason.slice(0, 90) : ''}`, d.verdict === 'pass' ? 'work' : 'bad'],
  'job.travels': (d, e) => [`${short(e.subject)} took the road to ${d.to}`, 'road'],
  'job.arrived': (d, e) => [`${short(e.subject)} arrived from ${d.frm}`, 'road'],
  'job.offered': (d, e) => [`${short(e.subject)} offered to heaven`, 'gold'],
  'ambrosia.accepted': (d, e) => [`✦ ambrosia — ${d.title || e.subject}`, 'gold'],
  'heaven.rejected': (d, e) => [`heaven returned ${short(e.subject)}: ${(d.reason || '').slice(0, 100)}`, 'bad'],
  'job.failed': (d, e) => [`${short(e.subject)} failed at ${d.stage}`, 'bad'],
  'city.up': (d, e) => [`${e.subject} came online`, 'road'],
  'city.down': (d, e) => [`${e.subject} went quiet`, 'bad'],
  'provider.parked': (d) => [`${d.provider} parked until ${new Date(d.until * 1000).toLocaleTimeString()}`, 'bad'],
  'model.starved': (d) => [`no model could answer for ${d.building || 'heaven'}`, 'bad'],
  'command': (d, e) => [`command ${e.subject} by ${d.by}`, ''],
  'city.started': (d) => [`${d.city} started (v${d.version})`, 'road'],
};
const short = (s) => (s || '').replace(/^(\w+)-([0-9a-f]{6})[0-9a-f]+$/, '$1·$2');

function tell(e) {
  const f = STORY[e.kind]; if (!f) return;
  const [text, cls] = f(e.data || {}, e);
  const li = document.createElement('li'); li.className = cls;
  li.innerHTML = `<span class="t">${new Date(e.ts * 1000).toLocaleTimeString()} · ${e.city}</span>`;
  li.append(text);
  const ol = $('story'); ol.prepend(li); while (ol.children.length > 80) ol.lastChild.remove();
}

function onEvent(e) {
  const d = e.data || {};
  if (e.kind === 'road.flow') flow(d.frm, d.to);
  else if (e.kind === 'stage.started' || e.kind === 'stage.done') pulse(`b:${e.city}:${d.building}`, 2.6);
  else if (e.kind === 'agent.moved') pulse(`a:${d.agent}`, 3, 900);
  else if (e.kind === 'ambrosia.accepted') { pulse('heaven', 3.2, 2400); for (const b of d.buildings || []) pulse(`b:${e.city}:${b}`, 2.4, 2400); }
  else if (e.kind === 'city.up' || e.kind === 'city.down') refresh();
  tell(e);
}

// ---------------------------------------------------------------- data
async function refresh() {
  try {
    [WORLD, CITY] = await Promise.all([api('/api/world'), api('/api/city')]);
  } catch { $('where').textContent = 'monitor unreachable'; return; }
  $('worldName').textContent = WORLD.world;
  $('where').textContent = `${CITY.name} · ${CITY.mode}${WORLD.heaven === WORLD.me ? ' · heaven is here' : ''}`;
  const up = 1 + Object.values(WORLD.peers || {}).filter((p) => p.up).length;
  $('kCities').textContent = up;
  $('kAmbrosia').textContent = CITY.metrics.ambrosia;
  $('kSupply').textContent = Math.round(CITY.metrics.money_supply);
  $('kTick').textContent = CITY.metrics.tick;
  const sel = $('cPipeline');
  if (!sel.options.length) for (const p of Object.keys(CITY.pipelines || {})) sel.add(new Option(p, p));
  render();
}

async function refreshSlow() {
  const [eco, jobs, join] = await Promise.all([api('/api/economy'), api('/api/jobs?limit=25'), api('/api/join')]).catch(() => [null, null, null]);
  if (eco) {
    $('eHeaven').textContent = Math.round(eco.now.heaven); $('eCompute').textContent = Math.round(eco.now.compute_total);
    $('eDrift').textContent = eco.drift; spark(eco.series.map((m) => m.money_supply));
  }
  if (jobs) $('jobs').innerHTML = jobs.map((j) => `<li><span class="s ${j.status}">${j.status}</span>${esc(j.subject).slice(0, 70)}<br><span class="muted">${j.stage || ''} · ${j.provenance} · ${j.origin}</span></li>`).join('') || '<li class="muted">no jobs yet</li>';
  if (join) $('joinSteps').innerHTML = join.steps.map((s) => `<li>${esc(s)}</li>`).join('');
}
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function spark(vals) {
  if (vals.length < 2) return;
  const lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
  const pts = vals.map((v, i) => `${(i / (vals.length - 1)) * 240},${46 - ((v - lo) / span) * 42}`).join(' ');
  $('spark').innerHTML = `<polyline points="${pts}" fill="none" stroke="#9b8cff" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`;
}

function select(n) {
  const d = n.data || {};
  const rows = Object.entries(d).filter(([, v]) => typeof v !== 'object').map(([k, v]) => `<div class="row"><span class="muted">${esc(k)}</span><b>${esc(v)}</b></div>`).join('');
  $('detailBody').innerHTML = `<div class="row"><b>${esc(n.title || n.id)}</b><span class="muted">${n.kind}</span></div>${rows}`;
  G.cameraPosition({ x: n.x * 1.4 + 60, y: n.y * 1.4 + 60, z: n.z * 1.4 + 160 }, n, 900);
}

// ---------------------------------------------------------------- controls
document.querySelectorAll('.tab').forEach((b) => b.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach((x) => { x.classList.toggle('on', x === b); x.setAttribute('aria-selected', x === b); });
  view = b.dataset.view; $('connectCard').hidden = view !== 'world'; signature = ''; render();
}));

$('commission').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const subject = $('cSubject').value.trim(); if (!subject) return;
  const r = await post('/api/command', { op: 'commission', args: { pipeline: $('cPipeline').value, subject } });
  $('cSubject').value = ''; godSay(r.ok ? `commissioned ${r.job}` : r.error, 'god');
});

$('connect').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const r = await post('/api/command', { op: 'add_peer', args: { city: $('pCity').value.trim(), host: $('pHost').value.trim() } });
  godSay(r.note || r.error, 'god'); refresh();
});

function godSay(text, who) {
  const d = document.createElement('div'); d.className = who; d.textContent = text;
  $('godLog').append(d); $('godLog').scrollTop = 1e9;
}

function showPending(p) {
  const box = $('godPending');
  if (!p) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `<code>${esc(JSON.stringify(p.approve))}</code><button id="yes">Approve</button><button id="no">Deny</button>`;
  const answer = async (yes) => { box.hidden = true; const r = await post('/api/god/approve', { yes }); godReport(r); };
  $('yes').onclick = () => answer(true); $('no').onclick = () => answer(false);
}

function godReport(r) {
  if (r.say) godSay(r.say, 'god');
  for (const x of r.results || []) godSay(`${x.tool}: ${JSON.stringify(x.result).slice(0, 220)}`, 'god');
  showPending(r.pending);
}

$('godForm').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const m = $('godInput').value.trim(); if (!m) return;
  $('godInput').value = ''; godSay(m, 'me');
  godReport(await post('/api/god', { message: m }));
});

// ---------------------------------------------------------------- go
const es = new EventSource('/api/stream');
es.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch { /* malformed event */ } };
api('/api/events?limit=40').then((evs) => evs.forEach(tell)).catch(() => {});
api('/api/god/pending').then((r) => showPending(r.pending)).catch(() => {});
refresh(); refreshSlow();
setInterval(refresh, 5000); setInterval(refreshSlow, 10000);
