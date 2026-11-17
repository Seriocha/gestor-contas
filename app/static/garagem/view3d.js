// Vista 3D da garagem (carregada só quando é pedida).
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

let current = null;

function label(text) {
  const c = document.createElement('canvas');
  const ctx = c.getContext('2d');
  const font = '600 44px system-ui, "Segoe UI", Roboto, sans-serif';
  ctx.font = font;
  c.width = Math.ceil(ctx.measureText(text).width) + 36;
  c.height = 64;
  ctx.font = font;
  ctx.fillStyle = 'rgba(20, 26, 34, .85)';
  ctx.beginPath(); ctx.roundRect(0, 0, c.width, c.height, 14); ctx.fill();
  ctx.fillStyle = '#fff'; ctx.textBaseline = 'middle'; ctx.textAlign = 'center';
  ctx.fillText(text, c.width / 2, c.height / 2 + 2);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, transparent: true }));
  const hgt = 0.26;
  s.scale.set(hgt * c.width / c.height, hgt, 1);
  s.renderOrder = 10;
  return s;
}

// zones: [{ id, name, x, y, w, d, z, h, levels }] em metros; (x, y) é o canto na planta, z a distância ao chão.
// camStore: objeto guardado pela app que mantém o ponto de vista entre redesenhos (ex.: depois de mover uma zona);
// a app esvazia-o ao mudar de ecrã, por isso cada ecrã abre sempre na vista inicial.
export function mount(el, { W, D, H, zones, highlight, onpick, editable, onmove, ondrag, camStore }) {
  if (current) current.dispose();
  const css = getComputedStyle(document.documentElement);
  const col = name => new THREE.Color(css.getPropertyValue(name).trim() || '#888');

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  el.replaceChildren(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, 0.05, 500);
  const span = Math.max(W, D, H);
  // Câmara do lado da entrada, a olhar para o fundo (o topo da planta).
  camera.position.set(W / 2, H + span * 0.85, D + span * 0.8);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(W / 2, H * 0.3, D / 2);
  controls.maxPolarAngle = Math.PI / 2 - 0.02;
  controls.minDistance = 0.8;
  controls.maxDistance = span * 4;
  controls.update();
  const offset = camera.position.clone().sub(controls.target);
  const homeTarget = controls.target.clone();
  // Vista inicial: sempre a mesma (do lado da entrada, de cima), afastada o suficiente para caber no ecrã.
  const goHome = () => {
    controls.target.copy(homeTarget);
    camera.position.copy(homeTarget).addScaledVector(offset, Math.max(1, 1.1 / camera.aspect));
    controls.update();
  };
  let fitted = false;
  const camKey = [W, D, H].join('|');
  const store = camStore || {};
  if (store.v && store.v.key === camKey) {
    camera.position.copy(store.v.pos);
    controls.target.copy(store.v.target);
    controls.update();
    fitted = true;
  }

  scene.add(new THREE.AmbientLight(0xffffff, 1.7));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6);
  sun.position.set(W * 0.2, H * 3, D * 1.5);
  scene.add(sun);

  // Chão com quadrícula de 1 m.
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(W, D), new THREE.MeshBasicMaterial({ color: col('--bg') }));
  floor.rotation.x = -Math.PI / 2;
  floor.position.set(W / 2, -0.005, D / 2);
  scene.add(floor);
  const pts = [];
  for (let x = 0; x <= W + 1e-6; x++) pts.push(x, 0, 0, x, 0, D);
  for (let z = 0; z <= D + 1e-6; z++) pts.push(0, 0, z, W, 0, z);
  pts.push(W, 0, 0, W, 0, D, 0, 0, D, W, 0, D);
  const grid = new THREE.BufferGeometry();
  grid.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
  scene.add(new THREE.LineSegments(grid, new THREE.LineBasicMaterial({ color: col('--line') })));

  // Paredes do fundo e laterais (a frente fica aberta).
  const wallMat = new THREE.MeshBasicMaterial({ color: col('--text'), transparent: true, opacity: 0.07, side: THREE.DoubleSide, depthWrite: false });
  const edgeMat = new THREE.LineBasicMaterial({ color: col('--muted') });
  const wall = (w, x, z, ry) => {
    const g = new THREE.PlaneGeometry(w, H);
    const m = new THREE.Mesh(g, wallMat);
    m.position.set(x, H / 2, z); m.rotation.y = ry;
    const e = new THREE.LineSegments(new THREE.EdgesGeometry(g), edgeMat);
    e.position.copy(m.position); e.rotation.copy(m.rotation);
    scene.add(m, e);
  };
  wall(W, W / 2, 0, 0);
  wall(D, 0, D / 2, Math.PI / 2);
  wall(D, W, D / 2, Math.PI / 2);

  // Zonas: um grupo por zona (bloco translúcido + o que ela contém), para se poder mover inteiro.
  const boxes = [], things = [];
  const hl = new Set([].concat(highlight ?? []));
  const lineCol = col('--accent'), textCol = col('--text'), hlCol = col('--hl');
  const TYPE_COLOR = { caixa: '#b9854f', gaveta: '#8fa7bf', item: '#00e676', armario: '#7d8fa3', estante: '#7d8fa3', bancada: '#a08a6a' };
  const TYPE_H = { caixa: 0.3, armario: 0.6, estante: 0.8, bancada: 0.3, gaveta: 0.15 };
  const darkLine = new THREE.LineBasicMaterial({ color: 0x1b1f26 });

  // Grelha de gavetas desenhada na face da frente (+z); as células em `hl` ficam a laranja.
  const cellHl = cs => (cs || []).some(c => hl.has(c.id) || (c.things || []).some(t => hl.has(t.id)));
  // Bancadas e mezanines (áreas de armazenamento): a matriz fica sempre no topo do objeto, vista de cima —
  // linha A atrás, colunas da esquerda para a direita; as caixas e artigos das células assentam em cima.
  const TOPO = new Set(['bancada', 'mezanine']);
  const topGrid = (parent, x, yTop, z0, w, d, rows, cols, cells, zone) => {
    const yy = yTop + 0.02, cw = w / cols, cd = d / rows; // por cima das placas de cada célula
    const lp = [];
    for (let i = 0; i <= cols; i++) lp.push(x + cw * i, yy, z0, x + cw * i, yy, z0 + d);
    for (let j = 0; j <= rows; j++) lp.push(x, yy, z0 + cd * j, x + w, yy, z0 + cd * j);
    const lg = new THREE.BufferGeometry();
    lg.setAttribute('position', new THREE.Float32BufferAttribute(lp, 3));
    parent.add(new THREE.LineSegments(lg, darkLine));
    const cx = c => x + cw * (c.col + 0.5), cz = c => z0 + cd * (c.row + 0.5);
    // Cada posição é uma placa baixa e bem visível no topo (mais clara se tem conteúdo, laranja se está destacada).
    const have = new Map((cells || []).map(c => [c.row + ':' + c.col, c]));
    for (let r = 0; r < rows; r++) for (let k = 0; k < cols; k++) {
      const c = have.get(r + ':' + k);
      const full = c && ((c.things || []).length || (c.items || []).length);
      const tile = new THREE.Mesh(new THREE.BoxGeometry(cw * 0.94, 0.016, cd * 0.94), new THREE.MeshLambertMaterial({
        color: c && hl.has(c.id) ? hlCol : full ? '#7ea2cc' : '#3d556f' }));
      tile.position.set(x + cw * (k + 0.5), yTop + 0.008, z0 + cd * (r + 0.5));
      parent.add(tile);
    }
    for (const c of cells || []) {
      if (c.row >= rows || c.col >= cols) continue;
      const flat = (color, opacity, dy) => {
        const m = new THREE.Mesh(new THREE.PlaneGeometry(cw, cd), new THREE.MeshBasicMaterial(
          opacity === 0 ? { transparent: true, opacity: 0, depthWrite: false } : { color }));
        m.rotation.x = -Math.PI / 2;
        m.position.set(cx(c), yy + dy, cz(c));
        parent.add(m);
        return m;
      };
      if (hl.has(c.id)) flat(hlCol, 1, 0.002);
      if (rows * cols <= 400) { // célula escolhível
        const pm = flat(null, 0, 0.001);
        pm.userData.zone = zone;
        pm.userData.thing = { id: c.id, itemId: null, name: c.name || 'Posição' };
        things.push(pm);
      }
      const entries = [...(c.things || []).map(t => ({ t })), ...(c.items || []).slice(0, 3).map(it => ({ it }))];
      const n = entries.length;
      entries.forEach((en, k) => {
        const sw = cw * 0.9 / n, sd = en.it ? cd * 0.4 : cd * 0.82, sh = Math.min(0.16, Math.max(0.05, Math.min(cw, cd) * 0.5));
        const on = hl.has(c.id) || (en.t && hl.has(en.t.id));
        const bg = new THREE.BoxGeometry(sw, sh, sd);
        const m = new THREE.Mesh(bg, en.it ? new THREE.MeshBasicMaterial({ color: TYPE_COLOR.item })
          : new THREE.MeshLambertMaterial({ color: on ? hlCol : (TYPE_COLOR[en.t.type] || '#8fa7bf') }));
        m.position.set(x + cw * c.col + cw * 0.05 + sw * (k + 0.5), yy + sh / 2 + 0.006, cz(c));
        m.userData.zone = zone;
        m.userData.thing = en.t ? { id: en.t.id, itemId: null, name: en.t.name + (en.t.n ? ` · ${en.t.n} artigos` : '') }
          : { id: null, itemId: en.it.id, name: en.it.name };
        const e = new THREE.LineSegments(new THREE.EdgesGeometry(bg), darkLine);
        e.position.copy(m.position);
        parent.add(m, e);
        things.push(m);
      });
    }
  };
  const frontGrid = (parent, x, y, zf, w, h, rows, cols, cells, zone) => {
    const lp = [];
    for (let i = 0; i <= cols; i++) lp.push(x + w * i / cols, y, zf + 0.002, x + w * i / cols, y + h, zf + 0.002);
    for (let j = 0; j <= rows; j++) lp.push(x, y + h * j / rows, zf + 0.002, x + w, y + h * j / rows, zf + 0.002);
    const lg = new THREE.BufferGeometry();
    lg.setAttribute('position', new THREE.Float32BufferAttribute(lp, 3));
    parent.add(new THREE.LineSegments(lg, darkLine));
    for (const c of cells || []) {
      if (!hl.has(c.id) || c.row >= rows || c.col >= cols) continue;
      const p = new THREE.Mesh(new THREE.PlaneGeometry(w / cols, h / rows), new THREE.MeshBasicMaterial({ color: hlCol }));
      p.position.set(x + w * (c.col + 0.5) / cols, y + h * (1 - (c.row + 0.5) / rows), zf + 0.004);
      parent.add(p);
    }
    // Cada célula pode ser escolhida (um plano transparente à frente dela), p.ex. para arrumar um artigo nela.
    if (rows * cols <= 400) {
      for (const c of cells || []) {
        if (c.row >= rows || c.col >= cols) continue;
        const pm = new THREE.Mesh(new THREE.PlaneGeometry(w / cols, h / rows),
          new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false }));
        pm.position.set(x + w * (c.col + 0.5) / cols, y + h * (1 - (c.row + 0.5) / rows), zf + 0.003);
        pm.userData.zone = zone;
        pm.userData.thing = { id: c.id, itemId: null, name: c.name || 'Posição' };
        parent.add(pm);
        things.push(pm);
      }
    }
    // Caixas e artigos que estão numa célula: saem um pouco da face para se verem (e escolherem) no desenho.
    const cw = w / cols, ch = h / rows;
    for (const c of cells || []) {
      if (c.row >= rows || c.col >= cols) continue;
      const entries = [...(c.things || []).map(t => ({ t })), ...(c.items || []).slice(0, 3).map(it => ({ it }))];
      const n = entries.length;
      if (!n) continue;
      const depth = Math.min(0.16, Math.max(0.05, cw * 0.5));
      entries.forEach((en, k) => {
        const sw = cw * 0.9 / n, sh = en.it ? ch * 0.4 : ch * 0.82;
        const on = hl.has(c.id) || (en.t && hl.has(en.t.id));
        const bg = new THREE.BoxGeometry(sw, sh, depth);
        const m = new THREE.Mesh(bg, en.it ? new THREE.MeshBasicMaterial({ color: TYPE_COLOR.item })
          : new THREE.MeshLambertMaterial({ color: on ? hlCol : (TYPE_COLOR[en.t.type] || '#8fa7bf') }));
        m.position.set(x + cw * c.col + cw * 0.05 + sw * (k + 0.5),
          y + ch * (rows - 1 - c.row) + ch / 2 - (en.it ? ch * 0.2 : 0), zf + depth / 2 + 0.006);
        m.userData.zone = zone;
        m.userData.thing = en.t ? { id: en.t.id, itemId: null, name: en.t.name + (en.t.n ? ` · ${en.t.n} artigos` : '') }
          : { id: null, itemId: en.it.id, name: en.it.name };
        const e = new THREE.LineSegments(new THREE.EdgesGeometry(bg), darkLine);
        e.position.copy(m.position);
        parent.add(m, e);
        things.push(m);
      });
    }
  };

  // (x, y) é o canto mínimo da zona no chão. Largura (z.w) corre ao longo da parede e profundidade (z.d) da parede
  // para dentro; por isso o grupo roda conforme a parede onde a zona está encostada, e a frente (+z local) olha
  // sempre para dentro da sala.
  const place = (grp, z, x, y) => {
    const o = z.orient || z.wall; // 'orient': parede a que as costas da zona estão voltadas (a frente olha para o lado oposto)
    if (o === 'esquerda') { grp.rotation.y = Math.PI / 2; grp.position.set(x, z.z, y + z.w); }
    else if (o === 'direita') { grp.rotation.y = -Math.PI / 2; grp.position.set(x + z.d, z.z, y); }
    else if (o === 'frente') { grp.rotation.y = Math.PI; grp.position.set(x + z.w, z.z, y + z.d); }
    else { grp.rotation.y = 0; grp.position.set(x, z.z, y); }
  };

  for (const z of zones) {
    const levels = z.contents || [];
    const L = Math.max(1, levels.length), lh = z.h / L;
    const hasContents = !!z.rows || levels.some(l => l.rows || l.things.length || l.items.length);
    const contentHl = cellHl(z.cells) || levels.some(l => (l.id && hl.has(l.id)) || cellHl(l.cells) ||
      l.things.some(t => hl.has(t.id) || cellHl(t.cells)));
    const subHl = z.kind === 'mezz' && zones.some(q => q.parent === z.id && hl.has(q.id)); // zona da mezanine destacada
    const zoneHl = hl.has(z.id) && !contentHl && !subHl;
    const mezz = z.kind === 'mezz';
    const grp = new THREE.Group();
    place(grp, z, z.x, z.y);
    const g = new THREE.BoxGeometry(z.w, z.h, z.d);
    const mesh = new THREE.Mesh(g, new THREE.MeshLambertMaterial({
      color: zoneHl ? hlCol : mezz ? new THREE.Color('#8d7b68') : col('--zone'), transparent: true, opacity: zoneHl ? 0.95 : mezz ? 0.95 : hasContents ? 0.22 : 0.85,
      depthWrite: !hasContents,
    }));
    mesh.position.set(z.w / 2, z.h / 2, z.d / 2);
    mesh.userData.zone = z;
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(g), new THREE.LineBasicMaterial({ color: hl.has(z.id) ? textCol : lineCol }));
    edges.position.copy(mesh.position);
    grp.add(mesh, edges);
    boxes.push(mesh);
    if (mezz && z.z > 0.15) { // pernas de apoio da mezanine até ao chão
      const leg = new THREE.MeshLambertMaterial({ color: 0x6b6b6b });
      for (const [lx, lz] of [[0.05, 0.05], [z.w - 0.05, 0.05], [0.05, z.d - 0.05], [z.w - 0.05, z.d - 0.05]]) {
        const lm = new THREE.Mesh(new THREE.BoxGeometry(0.06, z.z, 0.06), leg);
        lm.position.set(lx, -z.z / 2, lz);
        grp.add(lm);
      }
    }

    if (L > 1) {
      const lp = [];
      for (let i = 1; i < L; i++) {
        const y = lh * i;
        lp.push(0, y, 0, z.w, y, 0, z.w, y, 0, z.w, y, z.d, z.w, y, z.d, 0, y, z.d, 0, y, z.d, 0, y, 0);
      }
      const lg = new THREE.BufferGeometry();
      lg.setAttribute('position', new THREE.Float32BufferAttribute(lp, 3));
      grp.add(new THREE.LineSegments(lg, new THREE.LineBasicMaterial({ color: lineCol })));
    }
    if (z.rows) {
      if (TOPO.has(z.type)) topGrid(grp, 0, z.h, 0, z.w, z.d, z.rows, z.cols, z.cells, z);
      else frontGrid(grp, 0, 0, z.d, z.w, z.h, z.rows, z.cols, z.cells, z);
    }

    // Conteúdo de cada nível: caixas/outros objetos e artigos soltos, em fila ao longo do lado maior.
    const alongX = z.w >= z.d, len = alongX ? z.w : z.d, across = alongX ? z.d : z.w;
    levels.forEach((lv, i) => {
      const y0 = i * lh;
      if (lv.id) { // a prateleira: tábua que se pode escolher (e fica a laranja se está destacada)
        const on = hl.has(lv.id);
        const slab = new THREE.Mesh(new THREE.BoxGeometry(z.w, 0.04, z.d), new THREE.MeshBasicMaterial(
          on ? { color: hlCol } : { color: lineCol, transparent: true, opacity: 0.18, depthWrite: false }));
        slab.position.set(z.w / 2, y0 + 0.02, z.d / 2);
        slab.userData.zone = z;
        slab.userData.thing = { id: lv.id, itemId: null, name: lv.name || 'Prateleira' };
        grp.add(slab);
        things.push(slab);
      }
      if (lv.rows) frontGrid(grp, 0, y0, z.d, z.w, lh, lv.rows, lv.cols, lv.cells, z); // matriz na prateleira
      const entries = [...lv.things.map(t => ({ t })), ...lv.items.map(it => ({ it }))];
      const slot = len / Math.max(1, entries.length);
      entries.forEach((en, k) => {
        const t = en.t, item = en.it;
        const sAlong = t ? slot * 0.88 : Math.min(slot * 0.8, 0.18);
        const sThick = t ? across * 0.8 : Math.min(across * 0.8, 0.18);
        const sy = t ? Math.min(lh * 0.85, TYPE_H[t.type] || 0.3) : Math.min(lh * 0.8, 0.18);
        const sx = alongX ? sAlong : sThick, sz = alongX ? sThick : sAlong;
        const cx = alongX ? (k + 0.5) * slot : z.w / 2, cz = alongX ? z.d / 2 : (k + 0.5) * slot;
        const by = y0 + (lv.id ? 0.04 : 0.01);
        const on = t && (hl.has(t.id) || cellHl(t.cells));
        const bg = new THREE.BoxGeometry(sx, sy, sz);
        // Os artigos soltos usam um verde vivo sem sombreado (MeshBasic) para se verem bem no meio do resto.
        const m = new THREE.Mesh(bg, item ? new THREE.MeshBasicMaterial({ color: TYPE_COLOR.item })
          : new THREE.MeshLambertMaterial({ color: on ? hlCol : (TYPE_COLOR[t ? t.type : 'item'] || '#8fa7bf') }));
        m.position.set(cx, by + sy / 2, cz);
        m.userData.zone = z;
        m.userData.thing = { id: t ? t.id : null, itemId: item ? item.id : null, name: t ? t.name + (t.n ? ` · ${t.n} artigos` : '') : item.name };
        const e = new THREE.LineSegments(new THREE.EdgesGeometry(bg), darkLine);
        e.position.copy(m.position);
        grp.add(m, e);
        things.push(m);
        if (t && t.rows) {
          if (TOPO.has(t.type)) topGrid(grp, cx - sx / 2, by + sy, cz - sz / 2, sx, sz, t.rows, t.cols, t.cells, z);
          else frontGrid(grp, cx - sx / 2, by, cz + sz / 2, sx, sy, t.rows, t.cols, t.cells, z);
        }
      });
    });

    const tag = label(z.name);
    tag.position.set(z.w / 2, z.h + 0.18, z.d / 2);
    grp.add(tag);
    scene.add(grp);
  }

  const render = () => renderer.render(scene, camera);
  controls.addEventListener('change', render);

  // Toque simples numa zona escolhe-a; no modo de edição, arrastar uma zona move-a no chão
  // e arrastar fora das zonas roda a vista.
  const ray = new THREE.Raycaster();
  const cv = renderer.domElement;
  const aim = e => {
    const r = cv.getBoundingClientRect();
    ray.setFromCamera(new THREE.Vector2((e.clientX - r.left) / r.width * 2 - 1, -(e.clientY - r.top) / r.height * 2 + 1), camera);
  };
  const pick = () => ray.intersectObjects(things)[0] || ray.intersectObjects(boxes)[0];
  cv.addEventListener("pointermove", e => { // nome do objeto debaixo do rato
    if (drag || e.buttons) return;
    aim(e);
    const t = ray.intersectObjects(things)[0];
    cv.title = t ? t.object.userData.thing.name : "";
  });
  const STEP = 0.05;
  const snap = (v, max) => Math.round(Math.min(Math.max(0, max), Math.max(0, v)) / STEP) * STEP;
  let down = null, drag = null;
  cv.addEventListener('pointerdown', e => {
    down = { x: e.clientX, y: e.clientY };
    if (!editable || e.button !== 0 || drag) return;
    aim(e);
    const hit = pick();
    if (!hit) return;
    if (hit.object.userData.zone.fixed) return; // zonas de mezanine: posicionam-se na planta, não se arrastam no 3D
    const grp = hit.object.parent;
    drag = {
      grp, z: hit.object.userData.zone, id: e.pointerId, moved: false,
      plane: new THREE.Plane(new THREE.Vector3(0, 1, 0), -hit.point.y),
      dx: hit.point.x - hit.object.userData.zone.x, dz: hit.point.z - hit.object.userData.zone.y,
    };
    controls.enabled = false; // impede a rotação enquanto se arrasta a zona
    cv.setPointerCapture(e.pointerId);
  }, true);
  cv.addEventListener('pointermove', e => {
    if (!drag || e.pointerId !== drag.id) return;
    if (!drag.moved && Math.hypot(e.clientX - down.x, e.clientY - down.y) <= 6) return;
    aim(e);
    const p = ray.ray.intersectPlane(drag.plane, new THREE.Vector3());
    if (!p) return;
    drag.moved = true;
    const dz = drag.z; // ex/ey: tamanho no chão em x e em y, já considerando a parede
    let nx = snap(p.x - drag.dx, W - dz.ex), ny = snap(p.z - drag.dz, D - dz.ey);
    if (dz.wall === 'esquerda') nx = 0; else if (dz.wall === 'direita') nx = W - dz.ex;
    if (dz.wall === 'fundo') ny = 0; else if (dz.wall === 'frente') ny = D - dz.ey;
    drag.x = +nx.toFixed(2);
    drag.y = +ny.toFixed(2);
    dz.x = drag.x; dz.y = drag.y; // próximo arrasto parte daqui
    place(drag.grp, dz, drag.x, drag.y);
    if (ondrag) ondrag(drag.z.id, drag.x, drag.y);
    render();
  });
  const finish = e => {
    const d = drag;
    if (d) {
      if (e.pointerId !== d.id) return;
      drag = null;
      controls.enabled = true;
      if (d.moved) { if (e.type === 'pointerup' && onmove) onmove(d.z.id, d.x, d.y); return; }
    }
    if (e.type !== 'pointerup' || !down || Math.hypot(e.clientX - down.x, e.clientY - down.y) > 6) return;
    aim(e);
    const hit = pick();
    if (hit && onpick) { const t = hit.object.userData.thing; onpick(hit.object.userData.zone.id, t && t.id, t && t.itemId); }
  };
  cv.addEventListener('pointerup', finish);
  cv.addEventListener('pointercancel', finish);

  const ro = new ResizeObserver(() => {
    if (!el.isConnected) { dispose(); return; }
    const w = el.clientWidth, hgt = el.clientHeight;
    if (!w || !hgt) return;
    renderer.setSize(w, hgt, false);
    camera.aspect = w / hgt;
    if (!fitted) { fitted = true; goHome(); } // em ecrãs estreitos (telemóvel ao alto) afasta a câmara para caber tudo
    camera.updateProjectionMatrix();
    render();
  });
  ro.observe(el);

  let dead = false;
  function dispose() {
    if (dead) return;
    dead = true;
    ro.disconnect();
    store.v = { key: camKey, pos: camera.position.clone(), target: controls.target.clone() };
    controls.dispose();
    scene.traverse(o => {
      if (o.geometry) o.geometry.dispose();
      if (o.material) { if (o.material.map) o.material.map.dispose(); o.material.dispose(); }
    });
    renderer.dispose();
    renderer.forceContextLoss();
    if (current === handle) current = null;
  }
  const handle = {
    dispose, camera,
    reset() { goHome(); render(); },
    // Aproxima a câmara da zona destacada (a que contém o artigo ou a que está escolhida).
    focus() {
      const z = [...zones].reverse().find(q => [].concat(highlight ?? []).includes(q.id)); // a mais interior
      if (!z) return;
      const ex = z.ex ?? z.w, ey = z.ey ?? z.d;
      const c = new THREE.Vector3(z.x + ex / 2, z.z + z.h / 2, z.y + ey / 2);
      controls.target.copy(c);
      // matriz no topo (bancada, mezanine): a câmara sobe para se ver de cima
      const dir = z.rows && ['bancada', 'mezanine'].includes(z.type) ? new THREE.Vector3(0.1, 1.1, 0.7) : new THREE.Vector3(0.25, 0.35, 1);
      camera.position.copy(c).add(dir.normalize().multiplyScalar(Math.max(ex, ey, z.h, 0.8) * 2.4 + 0.8));
      controls.update();
      render();
    },
  };
  current = handle;
  return handle;
}
