// Menu do módulo "Garagem", descrito em dados e sem dependências: é a única fonte do menu.
// - No navegador (global GaragemMenu) desenha a barra lateral da própria app.
// - No servidor (require) alimenta GET /api/menu, para uma app anfitriã (ex.: o Gestor de Contas) desenhar o mesmo
//   menu como um grupo isolado do seu, sem conhecer a estrutura interna deste módulo.
(function (root) {
  const ICON = { estante: '🗄️', armario: '🚪', bancada: '🛠️', prateleira: '🪜', caixa: '📦', gaveta: '🗃️', mezanine: '🏗️', zona: '📍', outro: '📌' };
  const byName = (a, b) => a.name.localeCompare(b.name, 'pt', { numeric: true });

  // state: { spaces, locations, items, drafts }
  // opts:  prefix     texto antes de "#/..." nos endereços ('' na app; '/garagem/' no servidor, para o anfitrião)
  //        spaceId    espaço atual (mostra as suas zonas)
  //        activeKey  chave do item ativo (ex.: 'view:inventario', 'space:2')
  //        activePath ids da localização aberta, da raiz para dentro (ativa a zona mais interior que esteja no menu)
  //        propertyId só os espaços ligados a este imóvel do anfitrião
  function buildMenu(state, opts) {
    const o = Object.assign({ prefix: '', spaceId: null, activeKey: null, activePath: [], propertyId: null }, opts || {});
    const locs = state.locations || [], items = state.items || [], allSpaces = state.spaces || [];
    const spaces = allSpaces.filter(s => !o.propertyId || String(s.property_id || '') === String(o.propertyId));
    const first = allSpaces.length ? allSpaces[0].id : null;
    const href = h => o.prefix + h;

    const kidsOf = id => locs.filter(l => l.parent_id === id && l.grid_row == null).sort(byName);
    const zoneItem = l => ({
      key: 'loc:' + l.id, title: l.name, icon: ICON[l.type] || ICON.outro, href: href('#/loc/' + l.id), type: l.type,
      children: l.type === 'mezanine' ? kidsOf(l.id).map(zoneItem) : [],
    });
    const zoneIds = new Set();
    const walk = z => { zoneIds.add(Number(z.key.slice(4))); z.children.forEach(walk); };
    const spaceItems = spaces.map(s => {
      const roots = locs.filter(l => l.parent_id == null && (l.space_id == null ? first : l.space_id) === s.id).sort(byName);
      const children = roots.map(zoneItem);
      children.forEach(walk);
      return {
        key: 'space:' + s.id, title: s.name, icon: '🏠', href: href('#/espaco/' + s.id), current: s.id === o.spaceId,
        property: s.property_id || s.property_name ? { id: s.property_id || null, name: s.property_name || null } : null,
        size: { w: s.w, d: s.d, h: s.h }, children,
      };
    });
    // A zona mais interior da localização aberta que exista no menu fica ativa.
    let activeKey = o.activeKey;
    if (!activeKey) {
      const hit = o.activePath.slice().reverse().find(id => zoneIds.has(id));
      if (hit != null) activeKey = 'loc:' + hit;
    }
    const nTemp = items.filter(i => i.location_id == null).length, nDraft = (state.drafts || []).length;
    const groups = [
      { id: 'espacos', title: 'Espaços', items: spaceItems.concat([{ key: 'action:new-space', title: 'Novo espaço', icon: '＋', href: href('#/mapa3d'), action: 'new-space', children: [] }]) },
      { id: 'consulta', title: 'Consulta', items: [
        { key: 'view:home', title: 'Procurar', icon: '🔍', href: href('#/') },
        { key: 'view:inventario', title: 'Inventário completo', icon: '📦', href: href('#/inventario') },
      ] },
      { id: 'registo', title: 'Registo', items: [
        { key: 'view:pre', title: 'Pré-registo', icon: '📝', href: href('#/pre'), badge: nDraft || null },
        { key: 'view:temp', title: 'Área temporária', icon: '📥', href: href('#/temporaria'), badge: nTemp || null },
      ] },
      { id: 'mapas', title: 'Edição de Mapas', items: [
        { key: 'view:mapa3d', title: 'Mapa 3D', icon: '🧊', href: href('#/mapa3d') },
        { key: 'view:mapa2d', title: 'Mapa 2D', icon: '🗺️', href: href('#/mapa2d') },
      ] },
      { id: 'dados', title: 'Dados', items: [
        { key: 'view:dados', title: 'Importar / Exportar', icon: '💾', href: href('#/dados') },
      ] },
    ];
    const mark = it => { it.children = it.children || []; it.active = it.key === activeKey; it.children.forEach(mark); };
    groups.forEach(g => g.items.forEach(mark));
    return { groups };
  }

  const api = { buildMenu };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.GaragemMenu = api;
})(typeof self !== 'undefined' ? self : globalThis);
