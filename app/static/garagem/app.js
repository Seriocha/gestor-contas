'use strict';
// Gestão de Garagem — web app (telemóvel e PC). Sem passo de compilação.

const SVGNS = 'http://www.w3.org/2000/svg';
const SVG_TAGS = new Set(['svg', 'rect', 'line', 'text', 'g', 'circle', 'polygon']);
const TYPES = {
  zona: ['Zona', '📍'], estante: ['Estante', '🗄️'], armario: ['Armário', '🚪'], bancada: ['Bancada', '🛠️'],
  prateleira: ['Prateleira', '🪜'], caixa: ['Caixa', '📦'], gaveta: ['Gaveta', '🗃️'], mezanine: ['Mezanine', '🏗️'], outro: ['Outro', '📌'],
};
const TYPE_OPTIONS = Object.entries(TYPES).map(([value, [label, icon]]) => ({ value, label: `${icon} ${label}` }));
const SNAP = 0.25; // metros
// Prefixo sob o qual a app está servida (o servidor põe-no no <base>): '' ou, por exemplo, '/garagem'.
const BASE = new URL('.', document.baseURI).pathname.replace(/\/$/, '');
// Versão com que o servidor serviu este ficheiro (?v=…): vai também no endereço da vista 3D, para o browser não a guardar velha.
const VER = (() => { try { return new URL(document.currentScript.src).searchParams.get('v') || ''; } catch { return ''; } })();
const PHOTOS = BASE + '/photos/';

let S = { spaces: [], locations: [], items: [], drafts: [] };
// Espaço escolhido (garagem, arrecadação…): os mapas, a planta do início e as zonas novas pertencem-lhe.
let spaceId = null;
try { spaceId = Number(localStorage.getItem('spaceId')) || null; } catch { /* sem armazenamento */ }
const spaceById = id => S.spaces.find(s => s.id === id) || S.spaces[0] || { id: 0, name: 'Garagem', w: 6, d: 5, h: 2.5 };
const curSpace = () => spaceById(spaceId);
const setSpace = id => { spaceId = id; try { localStorage.setItem('spaceId', id || ''); } catch { /* ignora */ } };
const rootsOf = sid => childrenOf(0).filter(l => (l.space_id ?? (S.spaces[0] || {}).id) === sid);
// Mezanine: plataforma elevada (zona de topo do tipo "mezanine") dividida em zonas próprias. Essas zonas posicionam-se
// e orientam-se na planta (Mapa 2D), com X/Y relativos ao canto da mezanine, e no 3D ficam por cima do piso dela.
const mezzOf = l => { const p = byId.get(l.parent_id); return p && p.type === 'mezanine' ? p : null; };
const absXY = l => { const m = mezzOf(l); return m ? { x: m.plan_x + l.plan_x, y: m.plan_y + l.plan_y } : { x: l.plan_x, y: l.plan_y }; };
const baseZ = l => { const m = mezzOf(l); return (l.plan_z || 0) + (m ? (m.plan_z || 0) + (m.plan_alt || defaultAlt(m)) : 0); };
const subZones = m => childrenOf(m.id).filter(c => c.grid_row == null);
const zoneChoices = sid => rootsOf(sid).flatMap(r => (r.type === 'mezanine' ? [r, ...subZones(r)] : [r]));
const onMap = l => l.plan_w > 0 && (!mezzOf(l) || mezzOf(l).plan_w > 0);
// Vista 3D: guarda o ponto de vista entre redesenhos do mesmo ecrã (esvazia-se ao mudar de ecrã).
const cam3d = { v: null };
let byId = new Map(), kids = new Map(), itemsAt = new Map();
let query = '';
let planSel = null;

// ---------- utilitários ----------
// Estado vazio com ícone, texto e (opcional) uma ação; e indicador de carregamento.
const emptyState = (icon, title, text, action) => h('div', { class: 'empty-state', role: 'note' },
  h('div', { class: 'es-icon', 'aria-hidden': 'true' }, icon), h('b', {}, title),
  text ? h('p', { class: 'muted' }, text) : null, action || null);
const loadingBox = text => h('div', { class: 'loading', role: 'status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), text);
function h(tag, attrs, ...children) {
  const svg = SVG_TAGS.has(tag);
  const e = svg ? document.createElementNS(SVGNS, tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (!svg && (k === 'value' || k === 'checked' || k === 'selected' || k === 'disabled')) e[k] = v;
    else e.setAttribute(k, v === true ? '' : v);
  }
  e.append(...children.flat(Infinity).filter(c => c != null && c !== false));
  return e;
}
const $ = sel => document.querySelector(sel);
const fill = (el, ...c) => el.append(...c.flat(Infinity).filter(x => x != null && x !== false));

// Cabeçalho anti-CSRF: só existe quando a app é servida pelo Gestor de Contas (que põe o token no <meta name="csrf">).
const CSRF = (document.querySelector('meta[name="csrf"]') || {}).content;
const csrfHeaders = () => (CSRF ? { 'X-CSRF': CSRF } : {});
async function api(method, url, body) {
  const r = await fetch(BASE + url, {
    method,
    headers: { ...csrfHeaders(), ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Erro ${r.status}`);
  return data;
}

let toastTimer;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 3500);
}
// Executa uma ação, mostra erros e redesenha o ecrã.
async function act(fn) {
  try {
    const r = await fn();
    if (r === false) return;
    await reload();
    render();
  } catch (e) { toast(e.message); }
}

async function reload() {
  S = await api('GET', '/api/state');
  byId = new Map(S.locations.map(l => [l.id, l]));
  kids = new Map(); itemsAt = new Map();
  const push = (m, k, v) => { if (!m.has(k)) m.set(k, []); m.get(k).push(v); };
  for (const l of S.locations) push(kids, l.parent_id ?? 0, l);
  for (const i of S.items) push(itemsAt, i.location_id ?? 0, i);
  const cmp = (a, b) => (a.grid_row ?? 99) - (b.grid_row ?? 99) || (a.grid_col ?? 99) - (b.grid_col ?? 99) ||
    a.name.localeCompare(b.name, 'pt', { numeric: true });
  for (const a of kids.values()) a.sort(cmp);
  S.drafts = S.drafts || [];
  const hist = S.history || {};
  for (const [id, n, label, text] of [['#btn-undo', hist.undo, hist.undoLabel, 'Desfazer'], ['#btn-redo', hist.redo, hist.redoLabel, 'Refazer']]) {
    $(id).disabled = !n;
    $(id).title = n ? `${text}: ${label} (${n} ${n === 1 ? 'passo' : 'passos'})` : `Nada para ${text.toLowerCase()}`;
  }
}

// Desfazer / refazer: o servidor guarda os passos; aqui só se pede e se redesenha.
async function stepHistory(kind) {
  try {
    const r = await api('POST', '/api/' + kind);
    await reload();
    render();
    toast((kind === 'undo' ? 'Desfeito: ' : 'Refeito: ') + r.label);
  } catch (e) { toast(e.message); }
}
document.addEventListener('keydown', e => {
  if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k !== 'z' && k !== 'y') return;
  if (e.target.closest && e.target.closest('input, textarea, select, dialog')) return; // deixa o undo normal do texto
  e.preventDefault();
  stepHistory(k === 'z' && !e.shiftKey ? 'undo' : 'redo');
});

const childrenOf = id => kids.get(id ?? 0) || [];
const itemsIn = id => itemsAt.get(id ?? 0) || [];
function pathOf(id) {
  const p = [];
  for (let l = byId.get(id); l; l = byId.get(l.parent_id)) p.unshift(l);
  return p;
}
const pathText = id => {
  const p = pathOf(id);
  if (!p.length) return 'Área temporária';
  return [...(S.spaces.length > 1 ? [spaceById(p[0].space_id).name] : []), ...p.map(l => l.name)].join(' › ');
};
function countItems(id) {
  return itemsIn(id).length + childrenOf(id).reduce((n, c) => n + countItems(c.id), 0);
}
function descendants(id, out = []) {
  for (const c of childrenOf(id)) { out.push(c); descendants(c.id, out); }
  return out;
}
const icon = l => (TYPES[l.type] || TYPES.outro)[1];
const norm = s => (s || '').toString().toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');

function locationOptions(excludeId) {
  const out = [];
  (function walk(parent, depth) {
    for (const l of childrenOf(parent)) {
      if (l.id === excludeId) continue;
      out.push({ value: l.id, label: '  '.repeat(depth * 2) + `${icon(l)} ${l.name}` + (depth === 0 && S.spaces.length > 1 ? ` (${spaceById(l.space_id).name})` : '') });
      walk(l.id, depth + 1);
    }
  })(0, 0);
  return out;
}

// ---------- fotos e QR ----------
function pickFile(capture) {
  return new Promise(resolve => {
    const i = h('input', { type: 'file', accept: 'image/*', capture: capture ? 'environment' : null });
    i.addEventListener('change', () => resolve(i.files[0] || null));
    i.addEventListener('cancel', () => resolve(null));
    i.click();
  });
}
async function toCanvas(file, max) {
  let bmp;
  try { bmp = await createImageBitmap(file, { imageOrientation: 'from-image' }); }
  catch { throw new Error('Não consegui abrir essa imagem.'); }
  const k = Math.min(1, max / Math.max(bmp.width, bmp.height));
  const c = h('canvas', { width: Math.round(bmp.width * k), height: Math.round(bmp.height * k) });
  c.getContext('2d', { willReadFrequently: true }).drawImage(bmp, 0, 0, c.width, c.height);
  return c;
}
async function uploadPhoto(capture) {
  const file = await pickFile(capture);
  if (!file) return null;
  const c = await toCanvas(file, 1600);
  const blob = await new Promise(r => c.toBlob(r, 'image/jpeg', 0.85));
  const r = await fetch(BASE + '/api/photos', { method: 'POST', headers: { ...csrfHeaders(), 'Content-Type': 'image/jpeg' }, body: blob });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || 'Falha ao enviar a foto.');
  return data.file;
}
// Lê um QR a partir de uma foto tirada no momento (funciona sem HTTPS).
async function scanQR() {
  const file = await pickFile(true);
  if (!file) return null;
  let found = null;
  for (const max of [1000, 1800]) {
    const c = await toCanvas(file, max);
    const d = c.getContext('2d').getImageData(0, 0, c.width, c.height);
    found = window.jsQR(d.data, d.width, d.height);
    if (found) break;
  }
  if (!found) { toast('Não encontrei nenhum código QR na foto. Aproxime-se e tente de novo.'); return null; }
  const m = found.data.match(/\/l\/([A-Za-z0-9]+)/);
  const code = (m ? m[1] : found.data).trim().toUpperCase();
  const loc = S.locations.find(l => l.code === code);
  if (!loc) toast(`Código desconhecido: ${code.slice(0, 30)}`);
  return loc || null;
}

// ---------- formulários ----------
let pick3dUsed = false; // o seletor 3D de um formulário foi aberto (só há uma vista 3D de cada vez)
function fieldEl(f, data) {
  const set = v => { data[f.key] = v; };
  let input;
  if (f.type === 'photo') {
    const img = h('img', { alt: 'Foto' });
    const show = () => { img.hidden = !data[f.key]; if (data[f.key]) img.src = PHOTOS + data[f.key]; };
    const take = capture => async () => {
      try { const file = await uploadPhoto(capture); if (file) { set(file); show(); } }
      catch (e) { toast(e.message); }
    };
    show();
    // Sugestões da internet (Wikimedia Commons) a partir do nome do artigo; a escolhida é guardada como foto do artigo.
    const sugg = h('div', { class: 'sugg', hidden: true }), suggNote = h('small', { class: 'muted' }, '');
    const suggQ = h('input', { type: 'search', placeholder: 'Pesquisar imagens (pode mudar o termo)', autocomplete: 'off', 'aria-label': 'Pesquisar imagens' });
    const suggGrid = h('div', { class: 'sugg-grid' });
    sugg.append(h('div', { class: 'row' }, suggQ, h('button', { type: 'button', onclick: () => suggestSearch() }, 'Procurar')), suggNote, suggGrid);
    suggQ.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); suggestSearch(); } });
    const suggestSearch = async () => {
      const q = suggQ.value.trim();
      if (!q) { suggNote.textContent = 'Escreva o que procurar.'; return; }
      suggGrid.replaceChildren();
      suggNote.textContent = `A procurar imagens de "${q}"…`;
      try {
        const url = 'https://commons.wikimedia.org/w/api.php?action=query&generator=search&gsrnamespace=6&gsrlimit=12&prop=imageinfo' +
          `&iiprop=url|mime&iiurlwidth=480&format=json&origin=*&gsrsearch=${encodeURIComponent(q)}`;
        const r = await (await fetch(url)).json();
        const pics = Object.values((r.query || {}).pages || {}).map(p => (p.imageinfo || [])[0])
          .filter(i => i && /^image\/(jpeg|png|webp)$/.test(i.mime) && i.thumburl);
        suggNote.textContent = pics.length ? 'Toque numa imagem para a usar (fonte: Wikimedia Commons).' : `Nada encontrado para "${q}". Tente outro termo.`;
        suggGrid.append(...pics.map(i => h('button', {
          type: 'button', class: 'sugg-pic', title: 'Usar esta imagem',
          onclick: async () => {
            suggNote.textContent = 'A guardar a imagem…';
            try {
              const c = await toCanvas(await (await fetch(i.thumburl)).blob(), 1600);
              const blob = await new Promise(res => c.toBlob(res, 'image/jpeg', 0.85));
              const up = await fetch(BASE + '/api/photos', { method: 'POST', headers: { ...csrfHeaders(), 'Content-Type': 'image/jpeg' }, body: blob });
              const d = await up.json().catch(() => ({}));
              if (!up.ok) throw new Error(d.error || 'Falha ao guardar a imagem.');
              set(d.file); show(); sugg.hidden = true;
            } catch (e) { suggNote.textContent = e.message; }
          },
        }, h('img', { src: i.thumburl, alt: '', loading: 'lazy', referrerpolicy: 'no-referrer' }))));
      } catch (e) { suggNote.textContent = 'Não consegui procurar imagens na internet.'; }
    };
    const suggest = () => {
      sugg.hidden = false;
      if (!suggQ.value) suggQ.value = (data.name || '').trim();
      suggestSearch();
    };
    input = h('div', { class: 'photo-field' }, img, h('div', { class: 'row' },
      h('button', { type: 'button', onclick: take(true) }, '📷 Tirar foto'),
      h('button', { type: 'button', onclick: take(false) }, 'Escolher imagem'),
      f.suggest ? h('button', { type: 'button', onclick: suggest }, '🌐 Sugerir da internet') : null,
      h('button', { type: 'button', class: 'ghost', onclick: () => { set(null); show(); } }, 'Remover')), sugg);
  } else if (f.type === 'select' || f.type === 'location') {
    const opts = f.type === 'location'
      ? [{ value: '', label: f.emptyLabel || '(Área temporária)' }, ...locationOptions(f.exclude)]
      : f.options;
    const conv = v => (f.type === 'location' ? (v === '' ? null : Number(v)) : v);
    const draw = (list, keep) => list.map(o => h('option', { value: o.value, selected: String(o.value) === String(keep ?? '') }, o.label));
    input = h('select', { onchange: e => set(conv(e.target.value)) }, draw(opts, data[f.key]));
    if (f.type === 'location') {
      const sel = input;
      // Pesquisa: filtra a lista pelo nome, pelo caminho (estante › prateleira › caixa) ou pelo código da etiqueta.
      const search = h('input', {
        type: 'search', placeholder: 'Pesquisar localização…', autocomplete: 'off', 'aria-label': 'Pesquisar localização',
        oninput: e => {
          const terms = norm(e.target.value).split(/\s+/).filter(Boolean);
          if (!terms.length) { sel.replaceChildren(...draw(opts, data[f.key])); return; }
          const hits = locationOptions(f.exclude).filter(o => o.value !== '' && terms.every(t =>
            norm(pathText(o.value) + ' ' + (byId.get(o.value) || {}).code).includes(t)));
          sel.replaceChildren(...hits.map((o, i) => h('option', { value: o.value, selected: i === 0 },
            `${icon(byId.get(o.value))} ${pathText(o.value)}`)));
          if (hits.length) set(hits[0].value);
          note.textContent = hits.length ? `${hits.length} ${hits.length === 1 ? 'resultado' : 'resultados'}` : 'Nada encontrado';
        },
        onkeydown: e => { if (e.key === 'Enter') e.preventDefault(); },
      });
      const note = h('small', { class: 'muted' }, '');
      // Escolha opcional no 3D: toca-se na prateleira, caixa, posição de matriz ou zona onde fica.
      const pickBox = h('div', { class: 'pick3d', hidden: true });
      const choose = id => { search.value = ''; sel.replaceChildren(...draw(opts, id)); sel.value = id; set(id); };
      const drawPick = () => {
        const cur = byId.get(data[f.key]);
        const root = cur ? pathOf(cur.id)[0] : null;
        const sp = root && onMap(root) ? spaceById(root.space_id) : curSpace();
        const picked = h('div', { class: 'muted' }, cur ? '📍 ' + pathText(cur.id) : 'Toque numa prateleira, caixa, posição da matriz ou zona.');
        const spaces = S.spaces.length > 1 ? h('select', { onchange: e => { setSpace(Number(e.target.value)); drawPick(); } },
          S.spaces.map(s => h('option', { value: s.id, selected: s.id === sp.id }, s.name))) : null;
        const view = box3d(cur ? pathOf(cur.id).map(l => l.id) : null, (zid, tid, iid) => {
          if (iid) { toast('Escolha um sítio (prateleira, caixa…), não um artigo.'); return; }
          const id = tid || zid;
          if (id === f.exclude) return;
          choose(id); drawPick();
        }, null, sp);
        pickBox.replaceChildren(spaces, view, picked);
      };
      input = h('div', {},
        h('div', { class: 'row' }, search, note),
        h('div', { class: 'row' }, sel, h('button', {
          type: 'button', onclick: async () => {
            try { const loc = await scanQR(); if (loc) { choose(loc.id); toast('Localização: ' + pathText(loc.id)); } }
            catch (e) { toast(e.message); }
          },
        }, 'Ler QR da caixa/gaveta'), h('button', {
          type: 'button', onclick: () => {
            pickBox.hidden = !pickBox.hidden;
            if (pickBox.hidden) pickBox.replaceChildren(); else { pick3dUsed = true; drawPick(); }
          },
        }, '🧊 Escolher no 3D')),
        pickBox);
      search.style.flex = '1 1 200px'; sel.style.flex = '1 1 200px';
    }
  } else if (f.type === 'textarea') {
    input = h('textarea', { oninput: e => set(e.target.value) }, data[f.key] || '');
  } else {
    const num = f.type === 'number';
    input = h('input', {
      type: f.type || 'text', value: data[f.key] ?? '', required: f.required, list: f.list ? 'dl-' + f.key : null,
      min: f.min, max: f.max, step: f.step, placeholder: f.placeholder, autocomplete: 'off',
      oninput: e => set(num ? (e.target.value === '' ? null : Number(e.target.value)) : e.target.value),
    });
    if (f.list) input = [input, h('datalist', { id: 'dl-' + f.key }, f.list.map(v => h('option', { value: v })))];
  }
  return h(f.type === 'photo' || f.type === 'location' ? 'div' : 'label', { class: 'field' }, h('span', {}, f.label), input);
}
function formDialog(title, fields, values = {}, okLabel = 'Guardar') {
  return new Promise(resolve => {
    const data = { ...values };
    const dlg = h('dialog', {},
      h('form', { onsubmit: e => { e.preventDefault(); dlg.close('ok'); } },
        h('h2', {}, title),
        fields.map(f => fieldEl(f, data)),
        h('div', { class: 'row end' },
          h('button', { type: 'button', class: 'ghost', onclick: () => dlg.close('') }, 'Cancelar'),
          h('button', { type: 'submit', class: 'primary' }, okLabel))));
    dlg.addEventListener('close', () => {
      dlg.remove(); resolve(dlg.returnValue === 'ok' ? data : null);
      if (pick3dUsed) { pick3dUsed = false; render(); } // o seletor 3D substituiu a vista 3D do ecrã: redesenha-a
    });
    document.body.append(dlg);
    dlg.showModal();
  });
}

// ---------- ações ----------
function itemFields() {
  const cats = [...new Set(S.items.map(i => i.category).filter(Boolean))].sort();
  return [
    { key: 'photo', label: 'Foto', type: 'photo', suggest: true },
    { key: 'name', label: 'Nome', required: true, placeholder: 'ex.: Parafuso M4x20 sextavado' },
    { key: 'quantity', label: 'Quantidade', placeholder: 'ex.: 50, ~200, 1 caixa' },
    { key: 'category', label: 'Categoria', list: cats, placeholder: 'ex.: Parafusos' },
    { key: 'location_id', label: 'Localização', type: 'location', emptyLabel: '(Área temporária — arrumar depois)' },
    { key: 'notes', label: 'Notas', type: 'textarea' },
  ];
}
const newItem = locId => act(async () => {
  const d = await formDialog('Novo artigo', itemFields(), { location_id: locId ?? null });
  if (!d) return false;
  const { id } = await api('POST', '/api/items', d);
  location.hash = '#/item/' + id;
});
// «+ Artigo aqui»: pesquisar no inventário um artigo que já existe e pô-lo nesta localização, ou criar um novo.
const addItem = locId => {
  if (locId == null) return newItem(null);
  const loc = byId.get(locId);
  return act(() => new Promise(resolve => {
    const list = h('div', { class: 'cards' });
    const note = h('small', { class: 'muted' }, '');
    const search = h('input', { type: 'search', placeholder: 'Pesquisar no inventário (nome, categoria, notas, localização)…', autocomplete: 'off', 'aria-label': 'Pesquisar no inventário' });
    const dlg = h('dialog', {}, h('div', {},
      h('h2', {}, `Artigo em "${loc ? loc.name : 'esta localização'}"`),
      h('p', { class: 'muted' }, 'Procure um artigo que já existe para o pôr aqui, ou crie um novo.'),
      search, note, list,
      h('div', { class: 'row end' },
        h('button', { type: 'button', class: 'ghost', onclick: () => dlg.close('') }, 'Cancelar'),
        h('button', { type: 'button', class: 'primary', onclick: () => dlg.close('novo') }, '+ Novo artigo'))));
    let picked = null;
    const draw = () => {
      const terms = norm(search.value).split(/\s+/).filter(Boolean);
      const hits = S.items.filter(i => i.location_id !== locId && terms.every(t =>
        norm([i.name, i.category, i.notes, i.quantity, pathText(i.location_id)].join(' ')).includes(t)));
      note.textContent = terms.length ? `${hits.length} ${hits.length === 1 ? 'resultado' : 'resultados'}${hits.length > 30 ? ' (primeiros 30)' : ''}` : `${hits.length} artigos no inventário`;
      list.replaceChildren(...hits.slice(0, 30).map(it => h('button', {
        type: 'button', class: 'card pick', onclick: () => { picked = it; dlg.close('mover'); },
      }, it.photo ? h('img', { class: 'thumb', src: PHOTOS + it.photo, loading: 'lazy', alt: '' }) : h('div', { class: 'thumb' }, '🔩'),
      h('div', { class: 'body' }, h('div', { class: 'title' }, it.name),
        h('div', { class: 'sub' }, [it.quantity && `Qtd: ${it.quantity}`, it.category].filter(Boolean).join(' · ')),
        h('div', { class: 'sub' }, '📍 ' + pathText(it.location_id))))));
    };
    search.addEventListener('input', draw);
    search.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); const f = list.querySelector('button'); if (f) f.click(); } });
    dlg.addEventListener('close', async () => {
      dlg.remove();
      try {
        if (dlg.returnValue === 'mover' && picked) {
          await api('PUT', '/api/items/' + picked.id, { location_id: locId });
          toast(`"${picked.name}" passou para ${pathText(locId)}.`);
          resolve(true);
        } else if (dlg.returnValue === 'novo') {
          const d = await formDialog('Novo artigo', itemFields(), { location_id: locId });
          if (!d) return resolve(false);
          const { id } = await api('POST', '/api/items', d);
          location.hash = '#/item/' + id;
          resolve(true);
        } else resolve(false);
      } catch (e) { toast(e.message); resolve(false); }
    });
    document.body.append(dlg);
    dlg.showModal();
    draw();
    search.focus();
  }));
};
const editItem = it => act(async () => {
  const d = await formDialog('Editar artigo', itemFields(), it);
  if (!d) return false;
  await api('PUT', '/api/items/' + it.id, d);
});
const moveItem = it => act(async () => {
  const d = await formDialog('Mover artigo', [{ key: 'location_id', label: 'Nova localização', type: 'location' }],
    { location_id: it.location_id }, 'Mover');
  if (!d) return false;
  await api('PUT', '/api/items/' + it.id, { location_id: d.location_id });
});
const deleteItem = it => act(async () => {
  if (!confirm(`Apagar o artigo "${it.name}"?`)) return false;
  await api('DELETE', '/api/items/' + it.id);
  location.hash = it.location_id ? '#/loc/' + it.location_id : '#/';
});

function locFields(loc) {
  return [
    { key: 'name', label: 'Nome', required: true, placeholder: 'ex.: Estante A, Caixa de arquivo 1' },
    { key: 'type', label: 'Tipo', type: 'select', options: TYPE_OPTIONS },
    loc && { key: 'parent_id', label: 'Dentro de', type: 'location', exclude: loc.id, emptyLabel: '(diretamente no espaço)' },
    loc && !loc.parent_id && S.spaces.length > 1 && { key: 'space_id', label: 'Espaço', type: 'select', options: S.spaces.map(s => ({ value: s.id, label: s.name })) },
    { key: 'photo', label: 'Foto', type: 'photo' },
    { key: 'notes', label: 'Notas', type: 'textarea' },
  ].filter(Boolean);
}
const newLocation = parentId => act(async () => {
  const parent = byId.get(parentId);
  const d = await formDialog(parent ? `Nova localização em "${parent.name}"` : `Nova zona em "${curSpace().name}"`, locFields(null),
    { type: parent ? (parent.type === 'mezanine' ? 'estante' : 'caixa') : 'estante' });
  if (!d) return false;
  const { id } = await api('POST', '/api/locations', { ...d, parent_id: parentId ?? null, space_id: parentId == null ? curSpace().id : undefined });
  if (!parent || parent.type === 'mezanine') planSel = id;
  return id;
});
const editLocation = loc => act(async () => {
  const d = await formDialog('Editar localização', locFields(loc), loc);
  if (!d) return false;
  const { name, type, parent_id, photo, notes, space_id } = d;
  await api('PUT', '/api/locations/' + loc.id, { name, type, parent_id, photo, notes, space_id });
});
const makeGrid = loc => act(async () => {
  if (inMatrix(loc.id)) { toast('Uma matriz não pode conter outra matriz.'); return false; }
  // Numa zona do mapa sem parede, a frente (onde ficam as gavetas) escolhe-se aqui.
  const onTop = ['bancada', 'mezanine'].includes(loc.type); // nestas a matriz fica sempre no topo: não há «frente»
  const faceable = !onTop && !loc.parent_id && loc.plan_w > 0 && !loc.plan_wall;
  const d = await formDialog(`Matriz de "${loc.name}"${onTop ? ' (no topo)' : ''}`, [
    { key: 'rows', label: 'Linhas (A, B, C…)', type: 'number', min: 1, max: 26, step: 1, required: true },
    { key: 'cols', label: 'Colunas (1, 2, 3…)', type: 'number', min: 1, max: 40, step: 1, required: true },
    { key: 'type', label: 'Tipo de cada posição', type: 'select', options: TYPE_OPTIONS },
    { key: 'prefix', label: 'Prefixo do nome', placeholder: 'ex.: Gaveta → "Gaveta B3"' },
    faceable && { key: 'face', label: 'Frente (lado das gavetas) voltada para', type: 'select', options: FACE_OPTIONS },
  ].filter(Boolean), { rows: loc.grid_rows || 4, cols: loc.grid_cols || 5, type: 'gaveta', prefix: 'Gaveta', face: loc.plan_face || 'entrada' }, loc.grid_rows ? 'Aplicar' : 'Criar matriz');
  if (!d) return false;
  const { face, ...grid } = d;
  await api('POST', `/api/locations/${loc.id}/grid`, grid);
  if (faceable && face && face !== (loc.plan_face || 'entrada')) await api('PUT', '/api/locations/' + loc.id, faceBody(loc, face));
});
// Duplica uma localização (ex.: uma prateleira): uma ou várias cópias, com o nome seguinte e, se estiver no mapa,
// lado a lado para não ficarem sobrepostas.
const suggestName = loc => {
  const taken = new Set(childrenOf(loc.parent_id ?? 0).map(c => c.name));
  const m = loc.name.match(/^(.*?)(\d+)(\s*)$/);
  let n = m ? Number(m[2]) : 1, out;
  do { n++; out = m ? `${m[1]}${n}${m[3]}` : `${loc.name} ${n}`; } while (taken.has(out));
  return out;
};
const duplicateLocation = loc => act(async () => {
  const d = await formDialog(`Duplicar "${loc.name}"`, [
    { key: 'name', label: 'Nome da cópia (as seguintes continuam a numeração)', required: true },
    { key: 'count', label: 'Quantas cópias', type: 'number', min: 1, max: 50, step: 1, required: true },
    { key: 'mode', label: 'O que copiar', type: 'select', options: [
      { value: 'structure', label: 'A localização e o que tem dentro (gavetas, caixas…)' },
      { value: 'full', label: 'Tudo, incluindo os artigos' },
      { value: 'self', label: 'Só a localização, vazia' },
    ] },
  ], { name: suggestName(loc), count: 1, mode: 'structure' }, 'Duplicar');
  if (!d) return false;
  const count = Number(d.count) || 1, body = { name: d.name, count, mode: d.mode };
  if (loc.plan_w > 0) { // no mapa: coloca as cópias ao lado, ao longo da parede se estiver encostada
    const mz = mezzOf(loc), sp = spaceById((pathOf(loc.id)[0] || {}).space_id);
    const bound = mz ? ext(mz) : { ex: Number(sp.w), ey: Number(sp.d) };
    const { ex, ey } = ext(loc), gap = 0.1;
    const fitsX = loc.plan_x + (count + 1) * ex + count * gap <= bound.ex + 1e-6;
    const fitsY = loc.plan_y + (count + 1) * ey + count * gap <= bound.ey + 1e-6;
    const alongY = isSide(orientOf(loc)) && loc.plan_wall;
    if (alongY ? fitsY : !fitsX && fitsY) body.dy = ey + gap;
    else if (fitsX) body.dx = ex + gap;
    else if (fitsY) body.dy = ey + gap;
    else { body.dx = 0.2; body.dy = 0.2; }
  }
  const r = await api('POST', `/api/locations/${loc.id}/duplicate`, body);
  if (loc.plan_w > 0) planSel = r.ids[0];
  else if (location.hash.startsWith('#/loc/') && count === 1) location.hash = '#/loc/' + r.ids[0];
  toast(count === 1 ? `Criada "${d.name}".` : `${count} cópias criadas a partir de "${loc.name}".`);
});
// Apaga a matriz; os artigos que estavam nas gavetas passam para a própria localização.
const deleteGrid = loc => act(async () => {
  const cells = childrenOf(loc.id).filter(c => c.grid_row != null);
  const n = cells.reduce((sum, c) => sum + countItems(c.id), 0);
  if (!confirm(`Apagar a matriz de "${loc.name}" (${cells.length} posições)?` +
    (n ? `\n\nOs ${n} artigos que estão nas gavetas passam para "${loc.name}".` : '') + '\n\nPode desfazer com Ctrl+Z.')) return false;
  await api('DELETE', `/api/locations/${loc.id}/grid`, { moveItems: true });
  if (location.hash.startsWith('#/matrix/')) location.hash = '#/loc/' + loc.id;
  toast('Matriz apagada.');
});
const deleteLocation = loc => act(async () => {
  if (!confirm(`Apagar a localização "${loc.name}"?`)) return false;
  await api('DELETE', '/api/locations/' + loc.id);
  location.hash = loc.parent_id ? '#/loc/' + loc.parent_id : '#/';
});

const resetAll = () => act(async () => {
  const d = await formDialog('Apagar tudo e recomeçar', [{
    key: 'confirm', required: true, placeholder: 'APAGAR',
    label: `Vai apagar ${S.locations.length} localizações, ${S.items.length} artigos e todas as fotos. Não pode ser desfeito. Escreva APAGAR para confirmar.`,
  }], {}, 'Apagar tudo');
  if (!d) return false;
  if ((d.confirm || '').trim().toUpperCase() !== 'APAGAR') { toast('Nada foi apagado: a confirmação não corresponde.'); return false; }
  await api('POST', '/api/reset', { confirm: 'APAGAR' });
  planSel = null; query = '';
  toast('Ambiente reposto a zero.');
});

// ---------- componentes ----------
function itemCard(it) {
  return h('a', { class: 'card', href: '#/item/' + it.id },
    it.photo ? h('img', { class: 'thumb', src: PHOTOS + it.photo, loading: 'lazy', alt: '' }) : h('div', { class: 'thumb' }, '🔩'),
    h('div', { class: 'body' },
      h('div', { class: 'title' }, it.name),
      h('div', { class: 'sub' }, [it.quantity && `Qtd: ${it.quantity}`, it.category].filter(Boolean).join(' · ')),
      h('div', { class: 'sub' }, '📍 ' + pathText(it.location_id))));
}
function locCard(l) {
  const n = countItems(l.id);
  return h('a', { class: 'card', href: '#/loc/' + l.id },
    l.photo ? h('img', { class: 'thumb', src: PHOTOS + l.photo, loading: 'lazy', alt: '' }) : h('div', { class: 'thumb' }, icon(l)),
    h('div', { class: 'body' },
      h('div', { class: 'title' }, l.name),
      h('div', { class: 'sub' }, (TYPES[l.type] || TYPES.outro)[0] + (childrenOf(l.id).length ? ` · ${childrenOf(l.id).length} sub-localizações` : ''))),
    h('span', { class: 'badge' }, n === 1 ? '1 artigo' : `${n} artigos`));
}
function crumbs(id, includeSelf) {
  const p = pathOf(id);
  if (!includeSelf) p.pop();
  return h('nav', { class: 'crumbs', 'aria-label': 'Localização' }, h('a', { href: '#/' }, spaceById((pathOf(id)[0] || {}).space_id).name),
    p.map(l => h('a', { href: '#/loc/' + l.id }, l.name)));
}
// ---------- sobreposição ----------
// Duas zonas sobrepõem-se se ocupam o mesmo espaço no chão E ao mesmo nível (intervalos de altura que se cruzam).
// Uma zona por cima de outra (ex.: sobre a mezanine) ou encostada a ela não conta.
const EPS = 1e-6;
const bbox = l => {
  const { ex, ey } = ext(l), { x, y } = absXY(l), z = baseZ(l);
  return { x, y, ex, ey, z0: z, z1: z + (Number(l.plan_alt) || defaultAlt(l)) };
};
const boxesClash = (a, b) => a.x < b.x + b.ex - EPS && b.x < a.x + a.ex - EPS && a.y < b.y + b.ey - EPS && b.y < a.y + a.ey - EPS &&
  a.z0 < b.z1 - EPS && b.z0 < a.z1 - EPS;
// Zona do mesmo espaço que choca com `l` depois de lhe aplicar `body` (ou undefined).
const clashWith = (l, body = {}) => {
  const t = { ...l, ...body }, b = bbox(t);
  const root = mezzOf(t) ? mezzOf(t) : t;
  return zoneChoices(root.space_id ?? curSpace().id).find(o => o.id !== l.id && onMap(o) && boxesClash(b, bbox(o)));
};
// Primeiro sítio livre (de 25 em 25 cm) para uma zona com estas medidas; null se não houver.
function freeSpot(l, body) {
  const t = { ...l, ...body }, { ex, ey } = ext(t), m = mezzOf(t), sp = curSpace();
  const bw = m ? ext(m).ex : Number(sp.w) || 6, bd = m ? ext(m).ey : Number(sp.d) || 5;
  for (let y = 0; y <= bd - ey + EPS; y += SNAP) for (let x = 0; x <= bw - ex + EPS; x += SNAP) {
    const pos = t.plan_wall ? snapWall(t.plan_wall, x, y, ex, ey) : { plan_x: x, plan_y: y };
    if (!clashWith(l, { ...body, ...pos })) return pos;
  }
  return null;
}

// ---------- projeções (geometria descritiva) ----------
// Ponto de vista no centro da garagem (marcado na planta): quatro alçados, um para cada parede, vistos de dentro e
// em projeção ortogonal, na ordem em que se rodaria no sítio (fundo › direita › frente › esquerda). Em cada um, a
// direita do observador é a direita do desenho; só se veem as zonas que passam para lá do centro e as mais perto
// do observador tapam as mais afastadas. A planta fica por baixo e as alturas estão ligadas por linhas de chamada.
function elevationsSvg({ highlight, onpick, space } = {}) {
  const sp = space || curSpace();
  const W = Number(sp.w) || 6, D = Number(sp.d) || 5, H = Number(sp.h) || 2.5, G = 0.4;
  const cx = W / 2, cy = D / 2;
  const hl = new Set([].concat(highlight ?? []));
  const zones = zoneChoices(sp.id).filter(onMap).map(l => ({ l, b: bbox(l) }));
  const cls = l => 'zone' + (l.type === 'mezanine' ? ' mezz' : '') + (mezzOf(l) ? ' sub' : '') + (hl.has(l.id) ? ' hl' : '');
  // u: posição na horizontal do desenho; len: largura da vista; far/near: distância do observador à face de trás/da frente
  const VIEWS = [
    { title: '① Para o fundo', len: W, u: b => b.x, w: b => b.ex, far: b => cy - b.y, near: b => cy - (b.y + b.ey) },
    { title: '② Para a direita', len: D, u: b => b.y, w: b => b.ey, far: b => b.x + b.ex - cx, near: b => b.x - cx },
    { title: '③ Para a frente (entrada)', len: W, u: b => W - b.x - b.ex, w: b => b.ex, far: b => b.y + b.ey - cy, near: b => b.y - cy },
    { title: '④ Para a esquerda', len: D, u: b => D - b.y - b.ey, w: b => b.ey, far: b => cx - b.x, near: b => cx - (b.x + b.ex) },
  ];
  let off = 0;
  for (const v of VIEWS) { v.off = off; off += v.len + G; }
  const totW = off - G, planX = (totW - W) / 2, planY = H + G + 0.3, totH = planY + D;
  const svg = h('svg', { viewBox: `-0.1 -0.4 ${totW + 0.2} ${totH + 1.1}`, class: 'plan monge' });
  const frame = (x, y, w, hh, title) => svg.append(
    h('rect', { x, y, width: w, height: hh, class: 'floor' }), h('text', { x: x + w / 2, y: y - 0.08, class: 'cap', 'font-size': 0.2 }, title));
  const rect = (l, x, y, w, hh) => {
    const fs = Math.max(0.09, Math.min(0.22, w / (l.name.length * 0.58), hh * 0.6));
    return h('g', { class: cls(l), 'data-id': l.id, onclick: () => onpick && onpick(l) },
      h('rect', { x, y, width: w, height: hh, rx: 0.03 }), h('text', { x: x + w / 2, y: y + hh / 2, 'font-size': fs }, l.name));
  };
  const guide = (x1, y1, x2, y2) => svg.append(h('line', { x1, y1, x2, y2, class: 'guide' }));
  const zs = new Set();
  for (const { b } of zones) { zs.add(Math.min(H, b.z1)); zs.add(Math.max(0, b.z0)); }
  for (const v of VIEWS) frame(v.off, 0, v.len, H, v.title);
  for (let i = 0; i < VIEWS.length - 1; i++) for (const z of zs) guide(VIEWS[i].off + VIEWS[i].len, H - z, VIEWS[i + 1].off, H - z);
  for (const v of VIEWS) {
    const seen = zones.filter(({ b }) => v.far(b) > EPS).sort((p, q) => v.near(q.b) - v.near(p.b));
    for (const { l, b } of seen) {
      const top = H - Math.min(H, b.z1), hh = Math.max(0.02, Math.min(H, b.z1) - Math.max(0, b.z0));
      svg.append(rect(l, v.off + v.u(b), top, v.w(b), hh));
    }
  }
  // planta com o ponto de vista e as quatro direções
  frame(planX, planY, W, D, '');
  svg.append(h('text', { x: planX + W / 2, y: planY + D + 0.55, class: 'cap', 'font-size': 0.2 }, 'Planta · ponto de vista no centro (●)'));
  const rank = l => (l.type === 'mezanine' ? 0 : mezzOf(l) ? 2 : 1);
  for (const { l, b } of zones.slice().sort((p, q) => rank(p.l) - rank(q.l))) svg.append(rect(l, planX + b.x, planY + b.y, b.ex, b.ey));
  const px = planX + cx, py = planY + cy;
  [[px, planY, '①'], [planX + W, py, '②'], [px, planY + D, '③'], [planX, py, '④']].forEach(([x, y, n]) => {
    guide(px, py, x, y);
    svg.append(h('text', { x: x + (n === '②' ? 0.15 : n === '④' ? -0.15 : 0), y: y + (n === '①' ? -0.08 : n === '③' ? 0.22 : 0.07), class: 'cap', 'font-size': 0.2 }, n));
  });
  svg.append(h('circle', { cx: px, cy: py, r: 0.09, class: 'eye' }));
  return h('div', { class: 'monge-wrap' }, svg);
}
// Planta com as medidas numa coluna à esquerda e o desenho encostado à direita, ao seu espaço interno.
// Mostra as medidas do espaço e, havendo uma zona destacada (a mais interior que está no mapa), as dela.
function planFrame(drawing, space, hlIds) {
  const sp = space || curSpace();
  const m = n => (Math.round(Number(n || 0) * 100) / 100).toString().replace('.', ',') + ' m';
  const row = (k, v) => h('div', { class: 'meas-row' }, h('span', {}, k), h('b', {}, v));
  const z = [].concat(hlIds ?? []).map(id => byId.get(id)).filter(l => l && onMap(l)).pop();
  return h('div', { class: 'plan-row' },
    h('div', { class: 'plan-meas' },
      h('h4', {}, sp.name), row('Largura', m(sp.w)), row('Profundidade', m(sp.d)), row('Altura', m(sp.h)),
      z ? [h('h4', {}, z.name), row('Largura', m(z.plan_w)), row('Profundidade', m(z.plan_h)), row('Altura', m(z.plan_alt || defaultAlt(z))),
        row('X', m(absXY(z).x)), row('Y', m(absXY(z).y)), row('Z', m(baseZ(z)))] : null),
    h('div', { class: 'plan-draw' }, drawing));
}
function planSvg({ highlight, onpick, edit, space } = {}) {
  const sp = space || curSpace();
  const W = Number(sp.w) || 6, D = Number(sp.d) || 5;
  const svg = h('svg', { viewBox: `-0.1 -0.1 ${W + 0.2} ${D + 0.2}`, preserveAspectRatio: 'xMaxYMid meet', class: 'plan' + (edit ? ' edit' : '') },
    h('rect', { x: 0, y: 0, width: W, height: D, class: 'floor' }));
  for (let x = 1; x < W; x++) svg.append(h('line', { x1: x, y1: 0, x2: x, y2: D, class: 'grid' }));
  for (let y = 1; y < D; y++) svg.append(h('line', { x1: 0, y1: y, x2: W, y2: y, class: 'grid' }));
  // Desenha primeiro as mezanines (por baixo), depois as zonas normais e por fim as zonas das mezanines.
  const hlSet = new Set([].concat(highlight ?? []));
  const order = l => (l.type === 'mezanine' ? 0 : mezzOf(l) ? 2 : 1);
  const placed = zoneChoices(sp.id).filter(onMap).sort((a, b) => order(a) - order(b));
  for (const l of placed) {
    const { ex, ey } = ext(l), { x, y } = absXY(l);
    const lit = hlSet.has(l.id) && !(l.type === 'mezanine' && subZones(l).some(c => hlSet.has(c.id)));
    const fs = Math.max(0.12, Math.min(0.3, ex / (l.name.length * 0.58), ey * 0.6));
    svg.append(h('g', { class: 'zone' + (l.type === 'mezanine' ? ' mezz' : '') + (mezzOf(l) ? ' sub' : '') + (lit ? ' hl' : ''), 'data-id': l.id, onclick: () => onpick && onpick(l) },
      h('rect', { x, y, width: ex, height: ey, rx: 0.05 }),
      h('text', { x: x + ex / 2, y: y + ey / 2, 'font-size': fs }, l.name),
      // marca verde com o nº de artigos que a zona (e o que tem dentro) já contém
      countItems(l.id) ? [h('circle', { cx: x + ex - 0.02, cy: y + 0.02, r: 0.13, class: 'badge' }),
        h('text', { x: x + ex - 0.02, y: y + 0.02, 'font-size': 0.15, class: 'badge-n' }, String(Math.min(99, countItems(l.id))))] : null));
  }
  return svg;
}
// ---------- parede de encosto ----------
// Largura corre ao longo da parede e profundidade da parede para dentro da sala. ex/ey são os tamanhos
// equivalentes no chão (em x e em y). Sem parede = posição livre (largura em x, profundidade em y).
const WALL_OPTIONS = [
  { value: '', label: 'Livre (sem parede)' }, { value: 'fundo', label: 'Fundo (topo da planta)' },
  { value: 'esquerda', label: 'Esquerda' }, { value: 'direita', label: 'Direita' }, { value: 'frente', label: 'Frente (entrada)' },
];
const isSide = wall => wall === 'esquerda' || wall === 'direita';
// Orientação: parede a que as costas da zona estão voltadas. Encostada a uma parede é essa; senão deduz-se de
// "Frente voltada para" (padrão: para a entrada, ou seja, costas para o fundo).
const FACE_OPTIONS = [
  { value: 'entrada', label: 'Para a entrada (padrão)' }, { value: 'fundo', label: 'Para o fundo' },
  { value: 'esquerda', label: 'Para a esquerda' }, { value: 'direita', label: 'Para a direita' },
];
const FACE_TO_WALL = { entrada: 'fundo', fundo: 'frente', esquerda: 'direita', direita: 'esquerda' };
const WALL_TO_FACE = { fundo: 'entrada', frente: 'fundo', direita: 'esquerda', esquerda: 'direita' };
const orientOf = l => l.plan_wall || FACE_TO_WALL[l.plan_face] || 'fundo';
const ext = l => (isSide(orientOf(l)) ? { ex: l.plan_h, ey: l.plan_w } : { ex: l.plan_w, ey: l.plan_h });
// Muda a frente de uma zona mantendo o mesmo espaço ocupado no chão (troca largura e profundidade se for preciso).
function faceBody(l, face) {
  const swap = isSide(orientOf(l)) !== isSide(FACE_TO_WALL[face]);
  return { plan_face: face, ...(swap ? { plan_w: l.plan_h, plan_h: l.plan_w } : {}) };
}
const toDims = (wall, ex, ey) => (isSide(wall) ? { plan_w: ey, plan_h: ex } : { plan_w: ex, plan_h: ey });
// Coloca o canto (x, y) de modo a que a zona fique encostada à parede.
function snapWall(wall, x, y, ex, ey, space) {
  const sp = space || curSpace();
  const W = Number(sp.w) || 6, D = Number(sp.d) || 5, r = n => Math.round(n * 100) / 100;
  if (wall === 'esquerda') x = 0; else if (wall === 'direita') x = r(W - ex);
  if (wall === 'fundo') y = 0; else if (wall === 'frente') y = r(D - ey);
  return { plan_x: Math.max(0, x), plan_y: Math.max(0, y) };
}

// ---------- vista 3D ----------
const DEFAULT_ALT = { estante: 2, armario: 2, bancada: 0.9, prateleira: 0.4, caixa: 0.4, gaveta: 0.2, mezanine: 0.15 };
const defaultAlt = l => DEFAULT_ALT[l.type] || 1;
// O que cada zona contém (para desenhar): níveis = prateleiras (ou um só), com caixas/objetos e artigos soltos.
function contentsOf(l) {
  if (l.type === 'mezanine') return []; // os filhos da mezanine são zonas na planta, não conteúdo do piso
  const kidsAll = childrenOf(l.id);
  const real = kidsAll.filter(c => c.grid_row == null); // as células da matriz desenham-se como grelha
  const shelves = real.filter(c => c.type === 'prateleira');
  return (shelves.length ? shelves : [null]).map((s, i) => {
    let ts = s ? childrenOf(s.id).filter(c => c.grid_row == null).map(mkThing) : [], its = s ? itemsIn(s.id) : [];
    if (i === 0) { ts = ts.concat(real.filter(c => c.type !== 'prateleira').map(mkThing)); its = its.concat(itemsIn(l.id)); }
    return { id: s ? s.id : null, name: s ? s.name : null, things: ts, items: its.slice(0, 12).map(mkItem),
      rows: s ? s.grid_rows || 0 : 0, cols: s ? s.grid_cols || 0 : 0, cells: s ? cellsOf(s) : [] };
  });
}
const mkItem = i => ({ id: i.id, name: i.name });
const mkThing = c => ({
  id: c.id, name: c.name, type: c.type, rows: c.grid_rows || 0, cols: c.grid_cols || 0, n: countItems(c.id), cells: cellsOf(c),
});
// Células de uma matriz com o que têm dentro (caixas e artigos), para se verem no desenho.
const cellsOf = p => childrenOf(p.id).filter(c => c.grid_row != null).map(c => ({
  id: c.id, name: c.name, row: c.grid_row, col: c.grid_col,
  things: childrenOf(c.id).filter(x => x.grid_row == null).map(mkThing), items: itemsIn(c.id).slice(0, 6).map(mkItem),
}));
// Uma matriz não pode conter outra: dentro de uma posição de matriz (ou do que ela tem) já não se cria matriz.
const inMatrix = id => pathOf(id).some(l => l.grid_row != null);
function scene3d(highlight, onpick, edit, space) {
  const sp = space || curSpace();
  return {
    editable: !!edit, ...edit, camStore: cam3d,
    W: Number(sp.w) || 6, D: Number(sp.d) || 5, H: Number(sp.h) || 2.5,
    highlight, onpick,
    zones: zoneChoices(sp.id).filter(onMap).map(l => ({
      id: l.id, name: l.name, type: l.type, ...absXY(l), w: l.plan_w, d: l.plan_h, wall: l.plan_wall || null, orient: orientOf(l), ...ext(l),
      z: baseZ(l), h: l.plan_alt || defaultAlt(l),
      kind: l.type === 'mezanine' ? 'mezz' : mezzOf(l) ? 'sub' : null, fixed: !!mezzOf(l), parent: l.parent_id,
      contents: contentsOf(l), rows: l.grid_rows || 0, cols: l.grid_cols || 0,
      cells: cellsOf(l),
    })),
  };
}
function box3d(highlight, onpick, edit, space) {
  const box = h('div', { class: 'view3d' }, loadingBox('A carregar vista 3D…'));
  requestAnimationFrame(async () => {
    if (!box.isConnected) return;
    try {
      const m = await import('./view3d.js' + (VER ? '?v=' + VER : ''));
      if (box.isConnected) box.handle = m.mount(box, scene3d(highlight, onpick, edit, space));
    } catch (e) {
      console.error(e);
      box.replaceChildren(emptyState('🧊', 'Vista 3D indisponível', 'Este dispositivo não consegue mostrar a vista 3D. A planta 2D continua disponível.'));
    }
  });
  return box;
}
let planMode = '2d';
try { planMode = localStorage.getItem('planMode') || '2d'; } catch { /* sem armazenamento */ }
// Planta com alternância 2D / 3D.
// Botões sob a vista 3D: voltar à vista inicial (sempre a mesma) e aproximar da zona destacada.
function bar3d(box, canFocus) {
  return h('div', { class: 'row actions' },
    h('button', { onclick: () => box.handle && box.handle.reset() }, '⟲ Vista inicial'),
    canFocus ? h('button', { onclick: () => box.handle && box.handle.focus() }, '🔍 Aproximar da zona') : null);
}
function planView(highlight, path3d, space, compact) {
  const open = (zid, tid, iid) => { location.hash = iid ? '#/item/' + iid : '#/loc/' + (tid || zid); };
  const tabs = h('div', { class: 'row tabs' }), body = h('div', {});
  const draw = () => {
    tabs.replaceChildren(...[['2d', 'Planta 2D'], ['3d', 'Vista 3D']].map(([m, text]) => h('button', {
      class: 'chip' + (m === planMode ? ' sel' : ''),
      onclick: () => { planMode = m; try { localStorage.setItem('planMode', m); } catch { /* ignora */ } draw(); },
    }, text)));
    if (planMode === '3d') {
      const box = box3d(path3d || highlight, open, null, space);
      body.replaceChildren(box, bar3d(box, !!(path3d || highlight)), h('div', { class: 'muted' }, 'Arraste para rodar, belisque ou use a roda para aproximar, toque numa zona para a abrir.'));
    } else {
      const hl = path3d || highlight;
      body.replaceChildren(...[planFrame(planSvg({ highlight: hl, space, onpick: l => open(l.id) }), space, hl),
        compact ? null : [h('h3', {}, 'Alçados vistos do centro'), elevationsSvg({ highlight: hl, space, onpick: l => open(l.id) })]]
        .flat(Infinity).filter(Boolean));
    }
  };
  draw();
  return h('div', { class: 'plan-wrap' }, tabs, body);
}
// Coluna da direita (em todas as páginas, menos as que têm o seu próprio mapa): a planta do espaço atual, com a
// localização/artigo que se está a ver destacado.
function sidePlan(placeId) {
  const root = placeId ? pathOf(placeId)[0] : null;
  const sp = root ? spaceById(root.space_id) : curSpace();
  const hl = placeId ? pathOf(placeId).map(l => l.id) : null;
  const placed = zoneChoices(sp.id).some(onMap);
  return [h('h3', {}, `🗺️ ${sp.name}`),
    placed ? planView(null, hl, sp, true)
      : emptyState('🗺️', 'Planta vazia', 'Ainda não há zonas posicionadas neste espaço.', h('a', { class: 'btn primary', href: '#/mapa2d' }, 'Posicionar no Mapa 2D'))];
}

// ---------- ecrãs ----------
function viewHome() {
  const results = h('div', {});
  const draw = () => {
    const terms = norm(query).split(/\s+/).filter(Boolean);
    results.replaceChildren();
    if (terms.length) {
      const match = text => { const t = norm(text); return terms.every(q => t.includes(q)); };
      const items = S.items.filter(i => match([i.name, i.category, i.notes, i.quantity, pathText(i.location_id)].join(' ')));
      const locs = S.locations.filter(l => match([l.name, l.code, l.notes].join(' ')));
      fill(results,
        h('h2', {}, `Artigos (${items.length})`),
        h('div', { class: 'list' }, items.slice(0, 100).map(itemCard)),
        items.length ? null : emptyState('🔍', 'Nenhum artigo encontrado', 'Experimente outro nome, categoria ou parte do caminho.'),
        locs.length ? [h('h2', {}, `Localizações (${locs.length})`), h('div', { class: 'list' }, locs.slice(0, 50).map(locCard))] : null);
      return;
    }
    const zones = rootsOf(curSpace().id);
    fill(results,
      h('h2', {}, 'Zonas'),
      zones.length ? h('div', { class: 'list' }, zones.map(locCard))
        : emptyState('🗄️', 'Ainda não há zonas', 'Crie a primeira estante, armário ou bancada deste espaço.',
          h('button', { class: 'primary', onclick: () => newLocation(null) }, '+ Criar zona')),
      zones.length ? h('div', { class: 'actions' }, h('button', { onclick: () => newLocation(null) }, '+ Zona')) : null,
      itemsIn(0).length ? [h('h2', {}, 'Área temporária (' + itemsIn(0).length + ')'), h('p', { class: 'muted' }, 'Artigos ainda por arrumar. ', h('a', { href: '#/temporaria' }, 'Arrumar agora')), h('div', { class: 'list' }, itemsIn(0).slice(0, 10).map(itemCard))] : null,
      h('h2', {}, `Artigos recentes (${S.items.length} no total)`),
      h('div', { class: 'list' }, S.items.slice(0, 30).map(itemCard)));
  };
  draw();
  return [
    h('input', {
      class: 'search', type: 'search', value: query, placeholder: 'Procurar artigo, categoria, caixa…',
      oninput: e => { query = e.target.value; draw(); },
    }),
    results,
  ];
}

// Conteúdo de uma localização em árvore: sub-localizações (com o que têm dentro, a todos os níveis) e artigos.
// Das matrizes só se mostram as posições com conteúdo (as vazias contam-se).
function treeParts(l) {
  const kidsAll = childrenOf(l.id), cells = kidsAll.filter(c => c.grid_row != null);
  const used = cells.filter(c => countItems(c.id) > 0);
  return { subs: kidsAll.filter(c => c.grid_row == null).concat(used), empty: cells.length - used.length };
}
function treeNode(l, depth) {
  const its = itemsIn(l.id), { subs, empty } = treeParts(l), n = countItems(l.id);
  const title = h('span', { class: 'tn-title' }, `${icon(l)} `, h('a', { href: '#/loc/' + l.id }, l.name),
    h('span', { class: 'muted' }, ` ${(TYPES[l.type] || TYPES.outro)[0]}`),
    h('span', { class: 'badge' }, n === 1 ? '1 artigo' : `${n} artigos`));
  if (!its.length && !subs.length && !empty) return h('div', { class: 'tree-leaf' }, title);
  return h('details', { class: 'tree-node', open: depth < 2 }, h('summary', {}, title),
    h('div', { class: 'tree-body' },
      its.map(i => h('a', { class: 'tree-item', href: '#/item/' + i.id }, '🔩 ' + i.name, i.quantity ? h('span', { class: 'muted' }, ` · ${i.quantity}`) : null)),
      subs.map(c => treeNode(c, depth + 1)),
      empty ? h('div', { class: 'muted tree-empty' }, `${empty} posições vazias na matriz`) : null));
}
function contentTree(loc) {
  const { subs, empty } = treeParts(loc);
  if (!subs.length && !empty) return null;
  const all = open => wrap.querySelectorAll('details').forEach(d => { d.open = open; });
  const wrap = h('div', { class: 'tree' },
    h('div', { class: 'row tree-tools' }, h('button', { class: 'ghost', onclick: () => all(true) }, 'Expandir tudo'),
      h('button', { class: 'ghost', onclick: () => all(false) }, 'Recolher tudo')),
    subs.map(c => treeNode(c, 0)),
    empty ? h('div', { class: 'muted tree-empty' }, `${empty} posições vazias na matriz`) : null);
  return wrap;
}
function viewLocation(id) {
  const loc = byId.get(id);
  if (!loc) return [h('p', {}, 'Localização não encontrada.'), h('a', { href: '#/' }, 'Voltar ao início')];
  const all = childrenOf(id);
  const inGrid = c => loc.grid_rows && c.grid_row != null && c.grid_row < loc.grid_rows && c.grid_col < loc.grid_cols;
  const cells = all.filter(inGrid), others = all.filter(c => !inGrid(c));
  const items = itemsIn(id);
  let matrix = null;
  if (loc.grid_rows) {
    const at = new Map(cells.map(c => [c.grid_row + ':' + c.grid_col, c]));
    const out = [];
    for (let r = 0; r < loc.grid_rows; r++) for (let c = 0; c < loc.grid_cols; c++) {
      const cell = at.get(r + ':' + c);
      const label = String.fromCharCode(65 + r) + (c + 1);
      if (!cell) { out.push(h('div', { class: 'cell empty' }, h('b', {}, label))); continue; }
      const its = itemsIn(cell.id), total = countItems(cell.id);
      out.push(h('a', { class: 'cell' + (total ? '' : ' empty'), href: '#/loc/' + cell.id, title: cell.name },
        h('b', {}, label),
        its.slice(0, 2).map(i => h('span', {}, i.name)),
        total > 2 ? h('span', { class: 'muted' }, `+${total - 2}`) : null));
    }
    matrix = [h('h2', {}, `Matriz ${loc.grid_rows} × ${loc.grid_cols}`),
      h('div', { class: 'matrix-wrap' }, h('div', {
        class: 'matrix', style: `grid-template-columns: repeat(${loc.grid_cols}, minmax(64px, 1fr))`,
      }, out))];
  }
  return [
    crumbs(id, false),
    h('h1', {}, `${icon(loc)} ${loc.name}`),
    h('div', { class: 'muted' }, `${(TYPES[loc.type] || TYPES.outro)[0]} · código ${loc.code}`),
    loc.notes ? h('p', {}, loc.notes) : null,
    h('div', { class: 'row actions' },
      h('button', { class: 'primary', onclick: () => addItem(id) }, '+ Artigo aqui'),
      h('button', { onclick: () => newLocation(id) }, '+ Sub-localização'),
      loc.grid_rows ? [
        h('a', { class: 'btn', href: '#/matrix/' + id }, 'Rever matriz'),
        h('button', { onclick: () => makeGrid(loc) }, 'Alterar matriz'),
        h('button', { class: 'danger', onclick: () => deleteGrid(loc) }, 'Apagar matriz'),
      ] : inMatrix(id) ? null : h('button', { onclick: () => makeGrid(loc) }, 'Criar matriz'),
      h('a', { class: 'btn', href: '#/labels/' + id }, 'Etiquetas QR'),
      h('button', { onclick: () => editLocation(loc) }, 'Editar'),
      h('button', { onclick: () => duplicateLocation(loc) }, 'Duplicar'),
      h('button', { class: 'danger', onclick: () => deleteLocation(loc) }, 'Apagar')),
    loc.photo ? h('img', { class: 'photo', src: PHOTOS + loc.photo, alt: 'Foto de ' + loc.name }) : null,
    matrix,
    (() => { const t = contentTree(loc); return t ? [h('h2', {}, `Conteúdo por hierarquia (${countItems(id)} artigos)`), t] : null; })(),
    h('h2', {}, `Artigos aqui (${items.length})`),
    items.length ? h('div', { class: 'list' }, items.map(itemCard))
      : emptyState('📦', 'Nada guardado diretamente aqui', 'Adicione um artigo a esta localização.',
        h('button', { class: 'primary', onclick: () => addItem(id) }, '+ Artigo aqui')),
  ];
}

// Revisão da matriz: todas as posições com o que contêm, para conferir, redimensionar ou apagar.
function viewMatrix(id) {
  const loc = byId.get(id);
  if (!loc) return [h('p', {}, 'Localização não encontrada.'), h('a', { href: '#/' }, 'Voltar ao início')];
  if (!loc.grid_rows) return [h('p', {}, 'Esta localização não tem matriz.'), h('a', { href: '#/loc/' + id }, 'Voltar')];
  const all = childrenOf(id).filter(c => c.grid_row != null);
  const inside = c => c.grid_row < loc.grid_rows && c.grid_col < loc.grid_cols;
  const cells = all.filter(inside), outside = all.filter(c => !inside(c));
  const at = new Map(cells.map(c => [c.grid_row + ':' + c.grid_col, c]));
  const total = loc.grid_rows * loc.grid_cols;
  const occupied = cells.filter(c => countItems(c.id)).length;
  const nItems = cells.reduce((n, c) => n + countItems(c.id), 0);
  const out = [];
  for (let r = 0; r < loc.grid_rows; r++) for (let c = 0; c < loc.grid_cols; c++) {
    const cell = at.get(r + ':' + c), label = String.fromCharCode(65 + r) + (c + 1);
    if (!cell) { out.push(h('div', { class: 'cell empty missing' }, h('b', {}, label), h('span', { class: 'muted' }, 'em falta'))); continue; }
    const its = itemsIn(cell.id), n = countItems(cell.id);
    out.push(h('a', { class: 'cell' + (n ? '' : ' empty'), href: '#/loc/' + cell.id, title: cell.name },
      h('b', {}, label), cell.name !== label ? h('span', { class: 'muted' }, cell.name) : null,
      its.slice(0, 4).map(i => h('span', {}, i.name)),
      n > its.slice(0, 4).length ? h('span', { class: 'muted' }, `+${n - its.slice(0, 4).length}`) : null));
  }
  const stat = (n, text) => h('div', { class: 'stat' }, h('b', {}, String(n)), h('span', {}, text));
  return [
    crumbs(id, true),
    h('h1', {}, `Matriz de ${loc.name}`),
    h('div', { class: 'summary' }, stat(`${loc.grid_rows} × ${loc.grid_cols}`, 'tamanho'), stat(cells.length, `posições (de ${total})`),
      stat(occupied, 'com artigos'), stat(cells.length - occupied, 'vazias'), stat(nItems, 'artigos')),
    h('div', { class: 'row actions' },
      h('button', { class: 'primary', onclick: () => makeGrid(loc) }, 'Alterar tamanho'),
      h('a', { class: 'btn', href: '#/labels/' + id }, 'Etiquetas QR'),
      h('button', { class: 'danger', onclick: () => deleteGrid(loc) }, 'Apagar matriz'),
      h('a', { class: 'btn', href: '#/loc/' + id }, 'Voltar')),
    cells.length < total ? h('p', { class: 'muted' }, `Faltam ${total - cells.length} posições. "Alterar tamanho" e "Aplicar" recriam-nas.`) : null,
    h('div', { class: 'matrix-wrap' }, h('div', {
      class: 'matrix review', style: `grid-template-columns: repeat(${loc.grid_cols}, minmax(110px, 1fr))`,
    }, out)),
    outside.length ? [h('h2', {}, 'Posições fora da matriz'),
      h('p', { class: 'muted' }, 'Sobraram de uma versão maior da matriz. Esvazie-as e apague-as, ou aumente a matriz para as incluir.'),
      h('div', { class: 'list' }, outside.map(locCard))] : null,
  ];
}

function viewItem(id) {
  const it = S.items.find(i => i.id === id);
  if (!it) return [h('p', {}, 'Artigo não encontrado.'), h('a', { href: '#/' }, 'Voltar ao início')];
  return [
    it.location_id ? crumbs(it.location_id, true) : h('div', { class: 'crumbs' }, h('a', { href: '#/temporaria' }, 'Área temporária')),
    h('h1', {}, it.name),
    h('div', { class: 'muted' }, [it.quantity && `Quantidade: ${it.quantity}`, it.category].filter(Boolean).join(' · ')),
    h('div', { class: 'row actions' },
      h('button', { class: 'primary', onclick: () => moveItem(it) }, 'Mover'),
      h('button', { onclick: () => editItem(it) }, 'Editar'),
      h('button', { class: 'danger', onclick: () => deleteItem(it) }, 'Apagar')),
    it.photo ? h('img', { class: 'photo', src: PHOTOS + it.photo, alt: 'Foto de ' + it.name }) : null,
    it.notes ? h('p', {}, it.notes) : null,
    h('h2', {}, 'Onde está'),
    h('p', {}, '📍 ' + pathText(it.location_id)),
  ];
}

function viewMap2d() {
  const zones = zoneChoices(curSpace().id);
  if (!zones.some(z => z.id === planSel)) planSel = null;
  const sel = byId.get(planSel);
  const svg = planSvg({ highlight: planSel, edit: true, space: curSpace(), onpick: l => { planSel = l.id; render(); } });
  const W = Number(curSpace().w) || 6, D = Number(curSpace().d) || 5;
  // Limites onde se pode desenhar: o espaço todo ou, para zonas de uma mezanine, o piso dela.
  const bounds = () => {
    const m = sel && mezzOf(sel);
    if (!m || !(m.plan_w > 0)) return { x0: 0, x1: W, y0: 0, y1: D };
    const { ex, ey } = ext(m);
    return { x0: m.plan_x, x1: m.plan_x + ex, y0: m.plan_y, y1: m.plan_y + ey };
  };
  const point = e => {
    const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(svg.getScreenCTM().inverse());
    const b = bounds(), snap = (v, lo, hi) => Math.min(hi, Math.max(lo, Math.round(v / SNAP) * SNAP));
    return { x: snap(p.x, b.x0, b.x1), y: snap(p.y, b.y0, b.y1) };
  };
  // Dois gestos: arrastar sobre a zona escolhida move-a; arrastar no vazio desenha a posição/medidas dela.
  // Em ambos a zona não pode sobrepor-se a outra ao mesmo nível (o rascunho fica vermelho e o gesto é recusado).
  let start = null, draft = null, box = null, moving = null;
  const r2 = n => Math.round(n * 100) / 100;
  // Corpo a gravar para a caixa (x, y em coordenadas absolutas do mapa) com as medidas ex × ey.
  const bodyFor = (b, dims) => {
    const m = mezzOf(sel);
    const pos = m ? { plan_x: r2(b.x - m.plan_x), plan_y: r2(b.y - m.plan_y) } : snapWall(sel.plan_wall, b.x, b.y, b.ex, b.ey);
    return { ...pos, ...(dims ? toDims(orientOf(sel), b.ex, b.ey) : {}) };
  };
  const mark = (rect, b, dims) => {
    rect.setAttribute('x', b.x); rect.setAttribute('y', b.y); rect.setAttribute('width', b.ex); rect.setAttribute('height', b.ey);
    rect.classList.toggle('bad', !!clashWith(sel, bodyFor(b, dims)));
  };
  svg.addEventListener('pointerdown', e => {
    const zone = e.target.closest('.zone');
    if (!sel) { if (!zone) toast('Escolha primeiro uma zona na lista abaixo.'); return; }
    if (mezzOf(sel) && !(mezzOf(sel).plan_w > 0)) { toast('Posicione primeiro a mezanine.'); return; }
    if (zone && Number(zone.dataset.id) !== sel.id) return; // clique noutra zona: escolhe-a (onclick)
    start = point(e);
    if (zone && sel.plan_w > 0) { // mover a zona escolhida
      const { ex, ey } = ext(sel), a = absXY(sel);
      moving = { dx: start.x - a.x, dy: start.y - a.y, ex, ey, moved: false };
    }
    draft = h('rect', { class: 'draft' });
    svg.append(draft);
    svg.setPointerCapture(e.pointerId);
  });
  svg.addEventListener('pointermove', e => {
    if (!start) return;
    const p = point(e);
    if (moving) {
      const b = bounds();
      const x = Math.min(b.x1 - moving.ex, Math.max(b.x0, p.x - moving.dx)), y = Math.min(b.y1 - moving.ey, Math.max(b.y0, p.y - moving.dy));
      box = { x, y, ex: moving.ex, ey: moving.ey };
      moving.moved = true;
      mark(draft, box, false);
      return;
    }
    box = { x: Math.min(start.x, p.x), y: Math.min(start.y, p.y), ex: Math.abs(p.x - start.x), ey: Math.abs(p.y - start.y) };
    mark(draft, box, true);
  });
  const end = () => {
    if (!start) return;
    const b = box, mv = moving;
    start = null; box = null; moving = null; draft.remove();
    if (!b || (mv && !mv.moved)) return;
    if (!mv && !(b.ex >= SNAP && b.ey >= SNAP)) return;
    const body = bodyFor(b, !mv);
    const other = clashWith(sel, body);
    if (other) { toast(`Sobrepõe-se a "${other.name}" ao mesmo nível. Mude a altura (Z) ou escolha outro sítio.`); return; }
    act(() => api('PUT', '/api/locations/' + sel.id, body));
  };
  svg.addEventListener('pointerup', end);
  svg.addEventListener('pointercancel', end);

  return h('div', { class: 'split' }, h('div', { class: 'split-left' },
    h('h1', {}, 'Edição de Mapas · 2D'),
    dimsForm(),
    h('p', { class: 'muted' }, sel
      ? `Arraste "${sel.name}" para a mover, ou arraste no vazio para desenhar a posição e as medidas${mezzOf(sel) ? ' dentro da mezanine' : ''}. Cada quadrícula é 1 m; encaixa de ${SNAP * 100} em ${SNAP * 100} cm. Não pode sobrepor outra zona ao mesmo nível.`
      : 'Escolha uma zona abaixo e depois arraste sobre a planta para a posicionar.'),
    zoneChips(zones),
    sel ? coordsPanel(sel) : null,
    resetSection()),
  // o desenho ocupa 60 % (como a planta das outras páginas): planta e alçados, ajustados ao ecrã (sem scroll)
  h('div', { class: 'split-right' }, h('div', { class: 'stage' },
    h('div', { class: 'plan-wrap' }, planFrame(svg, curSpace(), planSel)),
    h('div', {}, h('h3', { class: 'stage-title' }, 'Alçados vistos do centro'),
      elevationsSvg({ highlight: planSel, space: curSpace(), onpick: l => { planSel = l.id; render(); } })))));
}

// Imóvel do espaço: no Gestor de Contas escolhe-se um dos imóveis (Bens) da entidade; sem lista, é texto livre.
const propertyFields = () => (S.bens && S.bens.length
  ? [{ key: 'property_id', label: 'Imóvel (Gestor de Contas)', type: 'select',
      options: [{ value: '', label: '(nenhum)' }, ...S.bens.map(b => ({ value: String(b.id), label: b.nome }))] }]
  : [{ key: 'property_name', label: 'Imóvel (nome, opcional)', placeholder: 'ex.: Casa da Aldeia' },
     { key: 'property_id', label: 'Imóvel · ID no Gestor de Contas (opcional)' }]);
const withPropertyName = d => {
  const b = (S.bens || []).find(x => String(x.id) === String(d.property_id));
  return b ? { ...d, property_name: b.nome } : d;
};
// Cria um espaço novo (ex.: Arrecadação) e passa a trabalhar nele; também chamado pelo botão do menu lateral.
const newSpace = () => act(async () => {
  const d = await formDialog('Novo espaço', [
    { key: 'name', label: 'Nome', required: true, placeholder: 'ex.: Arrecadação' },
    ...propertyFields(),
    { key: 'w', label: 'Largura (m)', type: 'number', min: 1, max: 100, step: 0.05, required: true },
    { key: 'd', label: 'Profundidade (m)', type: 'number', min: 1, max: 100, step: 0.05, required: true },
    { key: 'h', label: 'Altura (m)', type: 'number', min: 1, max: 20, step: 0.05, required: true },
  ], { w: 3, d: 2.5, h: 2.5 }, 'Criar espaço');
  if (!d) return false;
  const { id } = await api('POST', '/api/spaces', withPropertyName(d));
  setSpace(id); planSel = null; cam3d.v = null;
  location.hash = '#/mapa3d';
  toast(`Espaço "${d.name}" criado. Já pode posicionar as zonas.`);
});

// --- partes comuns aos dois editores de mapa ---
function dimsForm() {
  const sp = curSpace();
  const dims = { name: sp.name, w: sp.w, d: sp.d, h: sp.h, property_name: sp.property_name, property_id: sp.property_id };
  const f = (key, text, attrs, flex) => h('label', { class: 'field', style: `flex:${flex}` }, h('span', {}, text),
    h('input', { ...attrs, value: dims[key] ?? '', oninput: e => { dims[key] = e.target.value; } }));
  const delSpace = () => act(async () => {
    if (!confirm(`Apagar o espaço "${sp.name}"? Só é possível se não tiver zonas.`)) return false;
    await api('DELETE', '/api/spaces/' + sp.id);
    setSpace(null); planSel = null; cam3d.v = null;
  });
  const m = n => String(Math.round(Number(n || 0) * 100) / 100).replace('.', ',');
  return h('details', { class: 'dims-box' },
    h('summary', {}, `⚙️ Espaço «${sp.name}» · ${m(sp.w)} × ${m(sp.d)} × ${m(sp.h)} m` + (sp.property_name ? ` · ${sp.property_name}` : ''),
      h('span', { class: 'muted' }, ' (largura × profundidade × altura) — tocar para alterar')),
    h('form', { class: 'row', onsubmit: e => { e.preventDefault(); act(() => api('PUT', '/api/spaces/' + sp.id, withPropertyName(dims))); } },
      f('name', 'Espaço (nome)', {}, '2 1 160px'),
      S.bens && S.bens.length
        ? h('label', { class: 'field', style: 'flex:2 1 160px' }, h('span', {}, 'Imóvel (Gestor de Contas)'),
          h('select', { onchange: e => { dims.property_id = e.target.value || null; } },
            h('option', { value: '' }, '(nenhum)'),
            S.bens.map(b => h('option', { value: String(b.id), selected: String(b.id) === String(dims.property_id ?? '') }, b.nome))))
        : [f('property_name', 'Imóvel (Gestor de Contas)', { placeholder: 'opcional' }, '2 1 160px'),
           f('property_id', 'ID do imóvel', { placeholder: 'opcional' }, '1 1 90px')],
      f('w', 'Largura (m)', { type: 'number', min: 1, max: 100, step: 0.05 }, '1 1 90px'),
      f('d', 'Profundidade (m)', { type: 'number', min: 1, max: 100, step: 0.05 }, '1 1 90px'),
      f('h', 'Altura (m)', { type: 'number', min: 1, max: 20, step: 0.05 }, '1 1 90px'),
      h('button', { type: 'submit' }, 'Guardar')),
    h('div', { class: 'row actions' },
      h('button', { onclick: newSpace }, '+ Novo espaço'),
      S.spaces.length > 1 ? h('button', { class: 'danger', onclick: delSpace }, `Apagar espaço "${sp.name}"`) : null));
}
// Escolha da zona: uma lista (com «‹ ›» para passar à anterior/seguinte) em vez de dezenas de botões.
function zoneChips(zones) {
  const cur = byId.get(planSel), mz = cur && (cur.type === 'mezanine' ? cur : mezzOf(cur));
  const label = z => `${mezzOf(z) ? '   ↳ ' : ''}${icon(z)} ${z.name}${z.plan_w > 0 ? '' : ' (por posicionar)'}`;
  const go = id => { planSel = id; render(); };
  const i = zones.findIndex(z => z.id === planSel);
  const step = d => { if (zones.length) go(zones[(Math.max(i, 0) + d + zones.length) % zones.length].id); };
  const pick = h('select', { 'aria-label': 'Zona a editar', onchange: e => go(e.target.value ? Number(e.target.value) : null) },
    h('option', { value: '' }, zones.length ? `Escolher zona (${zones.length})…` : 'Ainda não há zonas'),
    zones.map(z => h('option', { value: z.id, selected: z.id === planSel }, label(z))));
  return h('div', { class: 'zone-picker' },
    h('div', { class: 'row' },
      h('button', { type: 'button', title: 'Zona anterior', onclick: () => step(-1), disabled: zones.length < 2 }, '‹'), pick,
      h('button', { type: 'button', title: 'Zona seguinte', onclick: () => step(1), disabled: zones.length < 2 }, '›'),
      h('button', { class: 'primary', onclick: () => newLocation(null) }, '+ Zona'),
      mz ? h('button', { onclick: () => newLocation(mz.id) }, `+ Zona em "${mz.name}"`) : null));
}
// Objeto de referência: uma estante num canto da sala, com cada medida marcada na cor do campo correspondente.
const COORD_COLORS = { plan_x: '#e5484d', plan_y: '#30a46c', plan_z: '#3e63dd', plan_w: '#f76b15', plan_h: '#8e4ec6', plan_alt: '#12a594' };
function refObject() {
  const s = 100, O = { x: 72, y: 112 };
  const P = (x, y, z) => [O.x + x * s - y * 0.45 * s, O.y + y * 0.45 * s - z * s];
  const pts = (...a) => a.map(q => q.join(',')).join(' ');
  const X = 0.25, Y = 0.12, Z = 0.14, L = 0.5, D = 0.4, A = 0.45, RW = 1.4, RD = 0.9, RH = 0.9;
  const line = (p, q, c) => h('line', { x1: p[0], y1: p[1], x2: q[0], y2: q[1], stroke: c, 'stroke-width': 2.2, 'stroke-linecap': 'round' });
  const tag = (p, t, c, dx = 0, dy = 0) => h('text', { x: p[0] + dx, y: p[1] + dy, fill: c, 'font-size': 11, 'font-weight': 700, 'text-anchor': 'middle' }, t);
  const mid = (p, q) => [(p[0] + q[0]) / 2, (p[1] + q[1]) / 2];
  const C = COORD_COLORS;
  const x0 = X, x1 = X + L, y0 = Y, y1 = Y + D, z0 = Z, z1 = Z + A;
  return h('svg', { viewBox: '0 0 230 170', class: 'ref-object', role: 'img', 'aria-label': 'Objeto de referência com as medidas X, Y, Z, largura, profundidade e altura' },
    h('polygon', { points: pts(P(0, 0, 0), P(RW, 0, 0), P(RW, RD, 0), P(0, RD, 0)), class: 'ro-floor' }),
    h('polygon', { points: pts(P(0, 0, 0), P(RW, 0, 0), P(RW, 0, RH), P(0, 0, RH)), class: 'ro-wall' }),
    h('polygon', { points: pts(P(x1, y1, z0), P(x1, y0, z0), P(x1, y0, z1), P(x1, y1, z1)), class: 'ro-side' }),
    h('polygon', { points: pts(P(x0, y0, z1), P(x1, y0, z1), P(x1, y1, z1), P(x0, y1, z1)), class: 'ro-top' }),
    h('polygon', { points: pts(P(x0, y1, z0), P(x1, y1, z0), P(x1, y1, z1), P(x0, y1, z1)), class: 'ro-front' }),
    line(P(0, 0, 0), P(x0, 0, 0), C.plan_x), tag(mid(P(0, 0, 0), P(x0, 0, 0)), 'X', C.plan_x, 0, -4),
    line(P(x0, 0, 0), P(x0, y0, 0), C.plan_y), tag(mid(P(x0, 0, 0), P(x0, y0, 0)), 'Y', C.plan_y, -9, 4),
    line(P(x0, y1, 0), P(x0, y1, z0), C.plan_z), tag(mid(P(x0, y1, 0), P(x0, y1, z0)), 'Z', C.plan_z, -8, 4),
    line(P(x0, y1, z0), P(x1, y1, z0), C.plan_w), tag(mid(P(x0, y1, z0), P(x1, y1, z0)), 'Larg.', C.plan_w, 0, 13),
    line(P(x1, y1, z0), P(x1, y0, z0), C.plan_h), tag(mid(P(x1, y1, z0), P(x1, y0, z0)), 'Prof.', C.plan_h, 16, 8),
    line(P(x1, y0, z0), P(x1, y0, z1), C.plan_alt), tag(mid(P(x1, y0, z0), P(x1, y0, z1)), 'Alt.', C.plan_alt, 14, 0));
}
// Coordenadas e medidas da zona escolhida; cada campo grava ao sair dele (ou com Enter).
function coordsPanel(sel) {
  const put = body => act(async () => {
    const other = clashWith(sel, body);
    if (other) { toast(`Sobrepõe-se a "${other.name}" ao mesmo nível. Mude a altura (Z) ou a posição.`); return false; }
    await api('PUT', '/api/locations/' + sel.id, body);
  });
  const open = h('a', { class: 'btn', href: '#/loc/' + sel.id }, `Abrir "${sel.name}"`);
  const mz = mezzOf(sel), isMezz = sel.type === 'mezanine';
  if (!(sel.plan_w > 0)) {
    if (mz && !(mz.plan_w > 0)) return h('p', { class: 'muted' }, `Posicione primeiro a mezanine "${mz.name}" no mapa.`);
    const first = isMezz
      ? { plan_x: 0, plan_y: 0, plan_w: 2, plan_h: 1.5, plan_z: Math.min(2.2, (Number(curSpace().h) || 2.5) * 0.7), plan_alt: defaultAlt(sel) }
      : { plan_x: 0, plan_y: 0, plan_w: 1, plan_h: 0.5, plan_z: 0, plan_alt: defaultAlt(sel) };
    const place = () => {
      const pos = freeSpot(sel, first);
      if (!pos) return toast('Não há espaço livre ao mesmo nível para esta zona. Reduza as medidas ou mude a altura (Z).');
      put({ ...first, ...pos });
    };
    return h('div', { class: 'row' },
      h('button', { class: 'primary', onclick: place }, mz ? `Colocar "${sel.name}" na mezanine` : `Colocar "${sel.name}" no mapa`), open);
  }
  // Mantém a zona encostada à parede ou, numa mezanine, dentro do piso dela.
  const fit = body => {
    const t = { ...sel, ...body }, { ex, ey } = ext(t);
    if (sel.plan_wall) Object.assign(body, snapWall(t.plan_wall, t.plan_x, t.plan_y, ex, ey));
    else if (mz) {
      const me = ext(mz);
      body.plan_x = Math.max(0, Math.min(t.plan_x, me.ex - ex));
      body.plan_y = Math.max(0, Math.min(t.plan_y, me.ey - ey));
    }
    return body;
  };
  const f = (key, text, optional, placeholder) => h('label', { class: 'field coord', title: text }, h('span', {},
    h('i', { class: 'dot', style: `background:${COORD_COLORS[key]}` }), text.split(' · ')[0].replace(/ \(m\)$/, ''), h('small', {}, ' m')),
    h('input', {
      type: 'number', step: 0.05, min: 0, max: 100, value: sel[key] ?? (optional ? placeholder : ''), placeholder, 'data-coord': key,
      onchange: e => {
        const v = e.target.value === '' ? null : Number(e.target.value);
        if (v == null && !optional) { e.target.value = sel[key]; return; }
        const body = { [key]: v };
        put(['plan_x', 'plan_y', 'plan_w', 'plan_h'].includes(key) ? fit(body) : body);
      },
    }));
  const setWall = wall => {
    const t = { ...sel, plan_wall: wall }, { ex, ey } = ext(t);
    put({ plan_wall: wall, ...(wall ? snapWall(wall, sel.plan_x, sel.plan_y, ex, ey) : { plan_face: WALL_TO_FACE[orientOf(sel)] }) });
  };
  const wallField = h('label', { class: 'field coord' }, h('span', {}, 'Parede'),
    h('select', { onchange: e => setWall(e.target.value || null) },
      WALL_OPTIONS.map(o => h('option', { value: o.value, selected: o.value === (sel.plan_wall || '') }, o.label))));
  const faceField = h('label', { class: 'field coord' }, h('span', {}, 'Frente para'),
    h('select', {
      disabled: !!sel.plan_wall, title: sel.plan_wall ? 'Encostada a uma parede: a frente olha para dentro da sala.' : '',
      onchange: e => put(fit(faceBody(sel, e.target.value))),
    }, FACE_OPTIONS.map(o => h('option', { value: o.value, selected: o.value === (sel.plan_wall ? WALL_TO_FACE[sel.plan_wall] : (sel.plan_face || 'entrada')) }, o.label))));
  return h('div', {},
    h('h2', {}, `Medidas de "${sel.name}"`),
    h('div', { class: 'coords-wrap' }, h('div', { class: 'coords-fields' },
    h('div', { class: 'coords sel2' }, mz ? null : wallField, isMezz ? null : faceField),
    h('div', { class: 'coords compact' },
      f('plan_x', mz ? 'X · do bordo esquerdo da mezanine (m)' : 'X · à parede esquerda (m)'),
      f('plan_y', mz ? 'Y · do bordo do fundo da mezanine (m)' : 'Y · à parede do fundo (m)'),
      f('plan_z', isMezz ? 'Z · elevação do piso ao chão (m)' : mz ? 'Z · acima do piso da mezanine (m)' : 'Z · ao chão (m)', true, '0'),
      f('plan_w', sel.plan_wall ? 'Largura · ao longo da parede (m)' : 'Largura (m)'),
      f('plan_h', sel.plan_wall ? 'Profundidade · da parede para dentro (m)' : 'Profundidade (m)'),
      f('plan_alt', isMezz ? 'Espessura do piso (m)' : 'Altura (m)', true, String(defaultAlt(sel)))),
    h('p', { class: 'muted legend-note' }, 'Passe o rato (ou toque) num campo para ver o que mede. ',
      h('b', {}, 'X, Y, Z'), ': posição · ', h('b', {}, 'Larg., Prof., Alt.'), ': medidas, em metros.')),
    h('figure', { class: 'ref-fig' }, refObject(), h('figcaption', { class: 'muted' }, 'Objeto de referência'))),
    (() => { const t = contentTree(sel); return t ? [h('h2', {}, `Conteúdo de "${sel.name}"`), t] : null; })(),
    mz ? h('p', { class: 'muted' }, 'Esta zona posiciona-se e orienta-se na planta (Mapa 2D); aqui acerta-se por números.') : null,
    isMezz ? h('p', { class: 'muted' }, 'Mezanine: use "+ Zona em ..." para a dividir em zonas e desenhe-as no Mapa 2D.') : null,
    h('div', { class: 'row' }, open, h('button', { onclick: () => duplicateLocation(sel) }, 'Duplicar'), mz ? h('a', { class: 'btn', href: '#/mapa2d' }, 'Desenhar no Mapa 2D') : null,
      h('button', { onclick: () => put({ plan_x: null, plan_y: null, plan_w: null, plan_h: null, plan_wall: null }) }, 'Retirar do mapa')));
}
function resetSection() {
  return [
    h('h2', {}, 'Recomeçar do zero'),
    h('p', { class: 'muted' }, `Apaga tudo: ${S.locations.length} localizações, ${S.items.length} artigos, todas as fotos e as dimensões da garagem. Não pode ser desfeito.`),
    h('button', { class: 'danger', onclick: resetAll }, 'Apagar tudo e recomeçar'),
  ];
}

function viewMap3d() {
  const zones = zoneChoices(curSpace().id);
  if (!zones.some(z => z.id === planSel)) planSel = null;
  const sel = byId.get(planSel);
  const setField = (key, v) => { const i = document.querySelector(`[data-coord="${key}"]`); if (i) i.value = v; };
  const view = box3d(planSel, id => { planSel = id; render(); }, {
    ondrag: (id, x, y) => { if (id === planSel) { setField('plan_x', x); setField('plan_y', y); } },
    onmove: (id, x, y) => { planSel = id; act(() => api('PUT', '/api/locations/' + id, { plan_x: x, plan_y: y })); },
  });
  return h('div', { class: 'split' }, h('div', { class: 'split-left' },
    h('h1', {}, 'Edição de Mapas · 3D'),
    dimsForm(),
    h('details', { class: 'help' }, h('summary', {}, 'ℹ️ Como usar'),
      h('p', { class: 'muted' }, 'Arraste uma zona para a mover no chão (encaixa de 5 em 5 cm); arraste fora das zonas para rodar a vista. Um toque escolhe a zona e mostra as medidas para acerto fino. As zonas de uma mezanine posicionam-se na planta (Mapa 2D).')),
    zoneChips(zones),
    sel ? coordsPanel(sel) : null,
    resetSection()),
  h('div', { class: 'split-right' }, h('div', { class: 'plan-wrap' }, view), bar3d(view, !!planSel)));
}

function viewLabels(id) {
  const loc = byId.get(id);
  if (!loc) return [h('p', {}, 'Localização não encontrada.')];
  let base = location.origin + BASE, sub = true;
  try { base = localStorage.getItem('labelBase') || base; } catch { /* sem armazenamento */ }
  const grid = h('div', { class: 'labels' });
  const draw = () => {
    const list = [loc, ...(sub ? descendants(id) : [])];
    grid.replaceChildren(...list.map(l => h('div', { class: 'label' },
      h('img', { src: BASE + '/api/qr.svg?text=' + encodeURIComponent(`${base.replace(/\/+$/, '')}/l/${l.code}`), alt: l.code }),
      h('div', {},
        h('div', { class: 'n' }, l.name),
        h('div', { class: 'p' }, pathOf(l.parent_id).map(p => p.name).join(' › ')),
        h('div', { class: 'c' }, l.code)))));
  };
  draw();
  return [
    h('div', { class: 'no-print' },
      crumbs(id, true),
      h('h1', {}, 'Etiquetas QR'),
      h('label', { class: 'field' }, h('span', {}, 'Endereço do NAS gravado no QR (tem de ser o que o telemóvel usa)'),
        h('input', {
          value: base, oninput: e => { base = e.target.value.trim(); try { localStorage.setItem('labelBase', base); } catch { /* ignora */ } draw(); },
        })),
      h('label', { class: 'row' },
        h('input', { type: 'checkbox', checked: sub, style: 'width:auto', onchange: e => { sub = e.target.checked; draw(); } }),
        'Incluir tudo o que está dentro (caixas, gavetas…)'),
      h('div', { class: 'row actions' }, h('button', { class: 'primary', onclick: () => print() }, 'Imprimir'))),
    grid,
  ];
}

// ---------- inventário completo ----------
const inv = { q: '', cat: '', sort: 'name', dir: 1, sel: null };
function viewInventory() {
  const cats = [...new Set(S.items.map(i => i.category).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'pt'));
  const rows = S.items.map(i => ({
    it: i, name: i.name, quantity: i.quantity || '', category: i.category || '',
    loc: i.location_id ? pathText(i.location_id) : '', updated: (i.updated || '').slice(0, 10),
  }));
  const cols = [['name', 'Artigo'], ['quantity', 'Qtd'], ['category', 'Categoria'], ['loc', 'Localização'], ['updated', 'Alterado']];
  const head = h('tr', {}), tbody = h('tbody', {}), count = h('span', { class: 'muted' });
  let shown = [];
  // Painel com a planta 3D do artigo escolhido (clicado na tabela ou o primeiro resultado da pesquisa).
  const mapPanel = h('div', { class: 'inv-map' });
  let mapFor; // artigo já desenhado (evita redesenhar o 3D a cada tecla)
  const showMap = () => {
    if (mapFor === inv.sel) return;
    mapFor = inv.sel;
    const it = S.items.find(i => i.id === inv.sel);
    if (!it) { mapPanel.replaceChildren(h('p', { class: 'muted' }, 'Pesquise ou clique num artigo para ver onde está na planta 3D.')); return; }
    if (!it.location_id) {
      mapPanel.replaceChildren(h('p', { class: 'muted' }, `"${it.name}" está na área temporária: ainda não tem posição definitiva na planta. `, h('a', { href: '#/temporaria' }, 'Arrumar'), ' ',
        h('button', { class: 'primary', onclick: () => moveItem(it) }, 'Mover')));
      return;
    }
    const path = pathOf(it.location_id), root = path[0];
    if (!root || !onMap(root)) {
      mapPanel.replaceChildren(h('p', { class: 'muted' }, `📍 ${pathText(it.location_id)} — a zona "${root ? root.name : ''}" ainda não está posicionada no mapa. `, h('a', { href: '#/mapa3d' }, 'Posicionar')));
      return;
    }
    const box = box3d(path.map(l => l.id), (zid, tid, iid) => { location.hash = iid ? '#/item/' + iid : '#/loc/' + (tid || zid); }, null, spaceById(root.space_id));
    mapPanel.replaceChildren(
      h('div', { class: 'inv-map-head' }, h('b', {}, it.name), ' · 📍 ' + pathText(it.location_id) + ' ', h('a', { href: '#/item/' + it.id }, 'Abrir artigo'), ' ',
        h('button', { class: 'primary', onclick: () => moveItem(it) }, 'Mover')),
      box, bar3d(box, true));
  };
  const draw = () => {
    const terms = norm(inv.q).split(/\s+/).filter(Boolean);
    shown = rows.filter(r => (!inv.cat || r.category === inv.cat) &&
      terms.every(t => norm([r.name, r.quantity, r.category, r.loc, r.it.notes].join(' ')).includes(t)));
    shown.sort((a, b) => inv.dir * a[inv.sort].localeCompare(b[inv.sort], 'pt', { numeric: true }));
    if (!shown.some(r => r.it.id === inv.sel)) { // ao pesquisar, mostra logo o primeiro resultado já arrumado
      const first = inv.q.trim() || inv.cat ? shown.find(r => r.it.location_id) : null;
      inv.sel = first ? first.it.id : null;
    }
    head.replaceChildren(...cols.map(([key, text]) => h('th', {
      onclick: () => { inv.dir = inv.sort === key ? -inv.dir : 1; inv.sort = key; draw(); },
    }, text, inv.sort === key ? h('span', { class: 'arrow' }, inv.dir > 0 ? ' ▲' : ' ▼') : null)));
    tbody.replaceChildren(...shown.map(r => h('tr', {
      class: r.it.id === inv.sel ? 'sel' : '',
      onclick: () => { inv.sel = r.it.id; draw(); mapPanel.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); },
    },
      h('td', { class: 'name' },
        r.it.photo ? h('img', { src: PHOTOS + r.it.photo, loading: 'lazy', alt: '' }) : h('span', { class: 'ph' }, '🔩'),
        h('a', { href: '#/item/' + r.it.id, title: 'Abrir artigo', onclick: e => e.stopPropagation() }, r.name)),
      h('td', {}, r.quantity), h('td', {}, r.category),
      h('td', {}, r.loc || h('span', { class: 'muted' }, 'Área temporária')),
      h('td', {}, r.updated))));
    if (!shown.length) {
      tbody.append(h('tr', { class: 'empty-row' }, h('td', { colspan: cols.length },
        rows.length ? emptyState('🔍', 'Nenhum artigo corresponde ao filtro', 'Limpe a pesquisa ou escolha outra categoria.')
          : emptyState('📦', 'Ainda não há artigos registados', 'Comece por registar o primeiro artigo.',
            h('button', { class: 'primary', onclick: () => newItem(null) }, '+ Artigo')))));
    }
    count.textContent = shown.length === rows.length ? `${rows.length} artigos` : `${shown.length} de ${rows.length} artigos`;
    showMap();
  };
  const exportCsv = () => {
    const esc = v => '"' + String(v ?? '').replace(/"/g, '""') + '"';
    const lines = [['Artigo', 'Quantidade', 'Categoria', 'Localização', 'Notas', 'Alterado'].map(esc).join(';'),
      ...shown.map(r => [r.name, r.quantity, r.category, r.loc, r.it.notes, r.updated].map(esc).join(';'))];
    const a = h('a', { href: URL.createObjectURL(new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv' })), download: 'inventario-garagem.csv' });
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
  const stat = (n, text) => h('div', { class: 'stat' }, h('b', {}, String(n)), h('span', {}, text));
  draw();
  return [
    h('h1', {}, 'Inventário completo'),
    h('div', { class: 'summary' },
      stat(S.items.length, 'artigos'), stat(cats.length, 'categorias'), stat(S.locations.length, 'localizações'),
      stat(S.items.filter(i => i.photo).length, 'com foto'), stat(itemsIn(0).length, 'na área temporária')),
    h('div', { class: 'row filters actions' },
      h('input', { type: 'search', value: inv.q, placeholder: 'Filtrar por nome, localização, notas…', oninput: e => { inv.q = e.target.value; draw(); } }),
      h('select', { onchange: e => { inv.cat = e.target.value; draw(); } },
        h('option', { value: '' }, 'Todas as categorias'),
        cats.map(c => h('option', { value: c, selected: c === inv.cat }, c))),
      h('button', { onclick: exportCsv }, 'Exportar CSV')),
    mapPanel,
    h('div', { class: 'inv-section' }, count, h('table', {}, h('thead', {}, head), tbody)),
  ];
}

// ---------- pré-registo ----------
// Tabela de rascunho do que existe na garagem. Nada entra no inventário até "Confirmar"; os artigos sem localização
// vão para a área temporária.
function viewPre() {
  const rows = S.drafts;
  const pending = new Set(); // gravações em curso (para a confirmação esperar por elas)
  const cats = [...new Set(S.items.map(i => i.category).filter(Boolean))].sort();
  const locOpts = [{ value: '', label: '(Área temporária)' }, ...locationOptions()];
  const save = (r, key, v) => {
    const val = v === '' ? null : v;
    const p = api('PUT', '/api/drafts/' + r.id, { [key]: val }).then(() => { r[key] = val; }, e => toast(e.message));
    pending.add(p);
    p.finally(() => pending.delete(p));
  };
  const text = (r, key, attrs) => h('input', { value: r[key] ?? '', autocomplete: 'off', ...attrs, onchange: e => save(r, key, e.target.value) });
  const body = rows.map((r, i) => h('tr', {},
    h('td', { class: 'muted' }, String(i + 1)),
    h('td', {}, text(r, 'name', { placeholder: 'Nome do artigo' })),
    h('td', {}, text(r, 'quantity', { style: 'width:6rem' })),
    h('td', {}, text(r, 'category', { list: 'pre-cats' })),
    h('td', {}, h('select', { onchange: e => save(r, 'location_id', e.target.value === '' ? null : Number(e.target.value)) },
      locOpts.map(o => h('option', { value: o.value, selected: String(o.value) === String(r.location_id ?? '') }, o.label)))),
    h('td', {}, text(r, 'notes')),
    h('td', {}, h('button', { class: 'ghost danger', title: 'Remover linha', onclick: () => act(() => api('DELETE', '/api/drafts/' + r.id)) }, '✕'))));

  const addRows = n => act(() => api('POST', '/api/drafts', { rows: Array.from({ length: n }, () => ({})) }));
  // Reconhece a localização escrita (nome, caminho "Armário › Prateleira 1" ou código da etiqueta).
  const findLoc = txt => {
    const t = norm(txt).trim();
    if (!t) return null;
    const hits = S.locations.filter(l => norm(l.name) === t || norm(pathText(l.id)) === t || norm(l.code) === t);
    return hits.length === 1 ? hits[0].id : null;
  };
  const pasteList = () => act(async () => {
    const d = await formDialog('Colar lista de artigos', [{
      key: 'text', type: 'textarea', required: true,
      label: 'Uma linha por artigo. Colunas: Nome, Quantidade, Categoria, Localização, Notas — separadas por TAB (copiado do Excel) ou por ponto e vírgula.',
    }], {}, 'Adicionar à tabela');
    if (!d) return false;
    const lines = d.text.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
    const parsed = lines.map(l => l.split(l.includes('\t') ? '\t' : ';').map(c => c.trim()));
    if (parsed.length && ['artigo', 'nome'].includes(norm(parsed[0][0]))) parsed.shift(); // cabeçalho
    let unknown = 0;
    const out = parsed.filter(c => c[0]).map(c => {
      const loc = findLoc(c[3]);
      if (c[3] && loc == null) unknown++;
      return { name: c[0], quantity: c[1], category: c[2], location_id: loc, notes: c[4] };
    });
    if (!out.length) { toast('Não encontrei nenhuma linha com nome.'); return false; }
    await api('POST', '/api/drafts', { rows: out });
    toast(`${out.length} linhas adicionadas.` + (unknown ? ` ${unknown} localizações não reconhecidas ficaram na área temporária.` : ''));
  });
  const confirmAll = () => act(async () => {
    await Promise.allSettled([...pending]);
    const fresh = (await api('GET', '/api/state')).drafts;
    const blank = fresh.map((r, i) => (r.name ? 0 : i + 1)).filter(Boolean);
    if (blank.length) { toast(`Preencha o nome (ou remova) as linhas: ${blank.slice(0, 8).join(', ')}.`); return false; }
    const temp = fresh.filter(r => r.location_id == null).length;
    if (!confirm(`Confirmar ${fresh.length} artigos?\n\n${fresh.length - temp} vão para a localização escolhida e ${temp} ficam na área temporária.`)) return false;
    const r = await api('POST', '/api/drafts/confirm', {});
    toast(`${r.created} artigos criados.` + (r.temporary ? ` ${r.temporary} estão na área temporária.` : ''));
    if (r.temporary) location.hash = '#/temporaria';
  });
  const clearAll = () => act(async () => {
    if (!confirm(`Apagar as ${rows.length} linhas do pré-registo? Nada foi criado no inventário.`)) return false;
    await api('DELETE', '/api/drafts');
  });
  return [
    h('h1', {}, 'Pré-registo'),
    h('p', { class: 'muted' }, 'Liste aqui o que existe na garagem, à vontade e sem pressa (também pode colar do Excel). Nada entra no inventário até confirmar; ' +
      'os artigos sem localização ficam na área temporária, até os arrumar.'),
    h('div', { class: 'row actions' },
      h('button', { class: 'primary', disabled: !rows.length, onclick: confirmAll }, rows.length ? `Confirmar ${rows.length} artigos` : 'Confirmar'),
      h('button', { onclick: () => addRows(1) }, '+ Linha'),
      h('button', { onclick: () => addRows(5) }, '+ 5 linhas'),
      h('button', { onclick: pasteList }, 'Colar lista'),
      h('button', { class: 'danger', disabled: !rows.length, onclick: clearAll }, 'Limpar tabela')),
    h('datalist', { id: 'pre-cats' }, cats.map(c => h('option', { value: c }))),
    rows.length ? h('div', { class: 'inv-section' }, h('table', { class: 'pre-table' },
      h('thead', {}, h('tr', {}, ['#', 'Artigo', 'Qtd', 'Categoria', 'Localização', 'Notas', ''].map(t => h('th', {}, t)))),
      h('tbody', {}, body)))
      : h('p', { class: 'muted' }, 'A tabela está vazia. Use "+ 5 linhas" para começar ou "Colar lista".'),
  ];
}

// ---------- área temporária ----------
// Artigos ainda sem localização definitiva: escolhem-se vários e movem-se de uma vez.
function viewTemp() {
  const items = itemsIn(0), sel = new Set();
  const count = h('span', { class: 'muted' });
  const moveBtn = h('button', { class: 'primary', disabled: true, onclick: () => moveSelected() }, 'Mover selecionados…');
  const boxes = [];
  const update = () => {
    count.textContent = sel.size ? `${sel.size} selecionados` : `${items.length} artigos por arrumar`;
    moveBtn.disabled = !sel.size;
  };
  const moveSelected = () => act(async () => {
    const d = await formDialog(`Mover ${sel.size} artigos`, [{ key: 'location_id', label: 'Nova localização', type: 'location', emptyLabel: '(Área temporária)' }],
      { location_id: null }, 'Mover');
    if (!d) return false;
    if (d.location_id == null) { toast('Escolha uma localização.'); return false; }
    const r = await api('POST', '/api/items/move', { ids: [...sel], location_id: d.location_id });
    toast(`${r.moved} artigos movidos para ${pathText(d.location_id)}.`);
  });
  const selectAll = () => {
    const all = sel.size < items.length;
    sel.clear();
    if (all) items.forEach(i => sel.add(i.id));
    boxes.forEach(b => { b.checked = all; });
    update();
  };
  update();
  return [
    h('h1', {}, '📥 Área temporária'),
    h('p', { class: 'muted' }, 'Artigos criados sem localização definitiva. Escolha os que vai arrumar e mova-os para a caixa, gaveta ou prateleira certa (pode ler o QR da etiqueta).'),
    h('div', { class: 'row actions' }, moveBtn, h('button', { onclick: selectAll, disabled: !items.length }, 'Selecionar todos'),
      h('button', { onclick: () => newItem(null) }, '+ Artigo'), h('a', { class: 'btn', href: '#/pre' }, 'Pré-registo'), count),
    items.length ? h('div', { class: 'list' }, items.map(it => {
      const cb = h('input', { type: 'checkbox', onchange: e => { if (e.target.checked) sel.add(it.id); else sel.delete(it.id); update(); } });
      boxes.push(cb);
      return h('div', { class: 'sel-row' }, cb, itemCard(it));
    })) : emptyState('✅', 'Tudo arrumado', 'A área temporária está vazia.', h('a', { class: 'btn', href: '#/inventario' }, 'Ver inventário')),
  ];
}

// ---------- dados: exportar / importar ----------
// Formato "garagem-export" v1: o mesmo ficheiro sai da app autónoma e entra no módulo do Gestor de Contas (e vice-versa).
function viewData() {
  const opt = { photos: '1', mode: 'merge' };
  let file = null;
  const out = h('p', { class: 'muted' });
  const doExport = async () => {
    try {
      const r = await fetch(BASE + '/api/export?photos=' + opt.photos, { headers: csrfHeaders() });
      if (!r.ok) throw new Error('Erro ' + r.status);
      const a = h('a', { href: URL.createObjectURL(await r.blob()), download: 'garagem-export-' + new Date().toISOString().slice(0, 10) + '.json' });
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
      out.textContent = 'Exportação descarregada.';
    } catch (e) { toast(e.message); }
  };
  const doImport = () => act(async () => {
    if (!file) { toast('Escolha primeiro o ficheiro exportado.'); return false; }
    if (opt.mode === 'replace' && !confirm('Substituir TUDO o que existe (espaços, localizações, artigos e fotos) pelo conteúdo do ficheiro?\n\nPode desfazer com Ctrl+Z enquanto o servidor não reiniciar.')) return false;
    let bundle;
    try { bundle = JSON.parse(await file.text()); } catch { throw new Error('O ficheiro não é um JSON válido.'); }
    const r = await api('POST', '/api/import?mode=' + opt.mode, bundle);
    toast(`Importado: ${r.spaces} espaços, ${r.locations} localizações, ${r.items} artigos, ${r.drafts} linhas de pré-registo.`);
    out.textContent = `Última importação (${r.mode === 'replace' ? 'substituição' : 'acrescentar'}): ${r.spaces} espaços, ${r.locations} localizações, ${r.items} artigos.`;
  });
  const sel = (key, options) => h('select', { onchange: e => { opt[key] = e.target.value; } }, options.map(([v, t]) => h('option', { value: v, selected: opt[key] === v }, t)));
  return [
    h('h1', {}, 'Importar / Exportar'),
    h('p', { class: 'muted' }, 'Leve os espaços, as localizações (com os códigos das etiquetas QR), os artigos e as fotos de uma instalação para outra — por exemplo da app autónoma para o Gestor de Contas.'),
    h('h2', {}, 'Exportar'),
    h('div', { class: 'row actions' }, sel('photos', [['1', 'Com fotos'], ['0', 'Sem fotos (mais leve)']]),
      h('button', { class: 'primary', onclick: doExport }, 'Descarregar exportação (.json)')),
    h('h2', {}, 'Importar'),
    h('div', { class: 'row actions' },
      h('input', { type: 'file', accept: '.json,application/json', onchange: e => { file = e.target.files[0] || null; } }),
      sel('mode', [['merge', 'Acrescentar ao que já existe'], ['replace', 'Substituir tudo']]),
      h('button', { onclick: doImport }, 'Importar')),
    h('p', { class: 'muted' }, 'Acrescentar não apaga nada (os identificadores são renumerados; os códigos das etiquetas mantêm-se, salvo se já existirem). Substituir apaga primeiro tudo o que há aqui.'),
    out,
  ];
}

// ---------- navegação ----------
// Barra lateral desenhada a partir de menu-def.js: Espaços (› zonas) em primeiro, depois Consulta, Registo e Mapas.
function renderNav(activeKey, activePath) {
  const menu = GaragemMenu.buildMenu(S, { prefix: '', spaceId: curSpace().id, activeKey, activePath });
  const item = (it, depth) => [
    h('a', {
      class: 'nav-item' + (depth ? ' sub' + Math.min(depth, 2) : '') + (it.active ? ' active' : '') + (it.action ? ' add' : ''),
      href: it.href, 'aria-current': it.active ? 'page' : null,
      title: it.property ? 'Imóvel: ' + (it.property.name || it.property.id) : null,
      onclick: it.action === 'new-space' ? e => { e.preventDefault(); newSpace(); } : null,
    }, `${it.icon} ${it.title}`, it.badge ? h('span', { class: 'nav-badge' }, String(it.badge)) : null),
    (it.current || depth > 0 ? it.children : []).map(c => item(c, depth + 1)),
  ];
  $('#nav-dynamic').replaceChildren(...menu.groups.map(g => h('div', { class: 'nav-group', role: 'group', 'aria-label': g.title },
    h('div', { class: 'nav-group-title' }, g.title), g.items.map(it => item(it, 0)))));
}
let lastRenderHash = null;
function render() {
  if (location.hash !== lastRenderHash) { cam3d.v = null; lastRenderHash = location.hash; } // cada ecrã abre na vista inicial
  const [, view, arg] = location.hash.split('/');
  const id = Number(arg);
  // O espaço atual acompanha o que se está a ver: o espaço aberto pelo menu ou o espaço da zona/artigo mostrado.
  const placeId = view === 'loc' || view === 'matrix' || view === 'labels' ? id : view === 'item' ? (S.items.find(i => i.id === id) || {}).location_id : null;
  const root = placeId ? pathOf(placeId)[0] : null;
  const wantSpace = view === 'espaco' ? id : root ? (root.space_id ?? (S.spaces[0] || {}).id) : null;
  if (wantSpace && wantSpace !== curSpace().id && S.spaces.some(s => s.id === wantSpace)) { setSpace(wantSpace); planSel = null; cam3d.v = null; }
  const content = view === 'loc' ? viewLocation(id) : view === 'item' ? viewItem(id) : view === 'mapa2d' ? viewMap2d() : view === 'mapa3d' || view === 'plan' ? viewMap3d()
    : view === 'labels' ? viewLabels(id) : view === 'matrix' ? viewMatrix(id) : view === 'pre' ? viewPre() : view === 'temporaria' ? viewTemp() : view === 'inventario' ? viewInventory() : view === 'dados' ? viewData() : viewHome();
  const activeKey = view === 'espaco' ? 'space:' + curSpace().id : view === 'pre' ? 'view:pre' : view === 'temporaria' ? 'view:temp'
    : view === 'plan' || view === 'mapa3d' ? 'view:mapa3d' : view === 'mapa2d' ? 'view:mapa2d' : view === 'inventario' ? 'view:inventario' : view === 'dados' ? 'view:dados'
    : view === 'item' && placeId == null ? 'view:temp' : placeId != null ? null : 'view:home';
  renderNav(activeKey, placeId ? pathOf(placeId).map(l => l.id) : []);
  $('#main').replaceChildren(...[content].flat(Infinity).filter(c => c != null && c !== false));
  $('#main').removeAttribute('aria-busy');
  const side = $('#side-plan'), ownMap = ['mapa2d', 'mapa3d', 'plan', 'inventario', 'dados'].includes(view);
  side.hidden = ownMap;
  if (ownMap) side.replaceChildren(); else side.replaceChildren(...sidePlan(placeId).flat(Infinity).filter(Boolean));
}
let lastHash = null;
window.addEventListener('hashchange', async () => {
  try { await reload(); } catch (e) { toast('Sem ligação ao NAS: ' + e.message); }
  render();
  if (location.hash !== lastHash) scrollTo(0, 0);
  lastHash = location.hash;
});

// Dentro de uma moldura (Gestor de Contas): avisa a página quando navega e obedece quando ela manda ir para outro sítio.
if (window.parent !== window) {
  window.addEventListener('hashchange', () => parent.postMessage({ garagem: 'hash', hash: location.hash }, location.origin));
  window.addEventListener('message', e => {
    if (e.origin === location.origin && e.data && e.data.garagem === 'goto' && e.data.hash !== location.hash) location.hash = e.data.hash || '#/';
  });
}

// Menu sempre aberto no PC; recolhido (com botão ☰) em ecrãs pequenos.
{
  const menu = $('.nav-collapsible'), small = matchMedia('(max-width: 860px)');
  const sync = () => { menu.open = !small.matches; };
  sync();
  small.addEventListener('change', sync);
  menu.addEventListener('click', e => { if (small.matches && e.target.closest('.nav-item')) menu.open = false; });
}

$('#btn-undo').addEventListener('click', () => stepHistory('undo'));
$('#btn-redo').addEventListener('click', () => stepHistory('redo'));
$('#btn-scan').addEventListener('click', async () => {
  try { const loc = await scanQR(); if (loc) location.hash = '#/loc/' + loc.id; }
  catch (e) { toast(e.message); }
});
$('#btn-new').addEventListener('click', () => {
  const [, view, arg] = location.hash.split('/');
  addItem(view === 'loc' && byId.has(Number(arg)) ? Number(arg) : null);
});

reload().then(render, e => {
  $('#main').replaceChildren(h('p', {}, 'Não foi possível ligar ao servidor: ' + e.message));
});
