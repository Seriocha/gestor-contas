// Gráficos da página Saúde (Chart.js). Os dados chegam em <script id="dados-saude">; cada <canvas data-saude="…">
// escolhe o gráfico. Cores do tema (mesmos tokens do resto da app) e sem euros.
(() => {
  const fonte = document.getElementById('dados-saude');
  if (!fonte || !window.Chart) return;
  const d = JSON.parse(fonte.textContent);
  const css = getComputedStyle(document.documentElement);
  const tok = (nome, omissao) => (css.getPropertyValue(nome) || '').trim() || omissao;
  const COR = {
    azul: tok('--serie-receitas', '#2a78d6'), vermelho: tok('--serie-despesas', '#e34948'),
    verde: tok('--pos', '#12813A'), suave: tok('--suave', '#667085'), linha: tok('--linha', '#e3e6ec'),
    roxo: '#7c5cd6', ciano: '#1f9fb0', amarelo: tok('--destaque', '#FFD23F'),
  };
  const num = (v, casas = 0) => v == null ? '—'
    : new Intl.NumberFormat('pt-PT', { minimumFractionDigits: casas, maximumFractionDigits: casas }).format(v);
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.locale = 'pt-PT';
  Chart.defaults.color = COR.suave;
  Chart.defaults.borderColor = COR.linha;
  const pontos = d.rotulos.length <= 45 ? 2 : 0;
  const linha = (label, data, cor, eixo = 'y', extra = {}) => ({
    label, data, borderColor: cor, backgroundColor: cor + '22', borderWidth: 2, tension: 0.25, spanGaps: true,
    pointRadius: pontos, pointHoverRadius: 5, pointHitRadius: 10, yAxisID: eixo, ...extra });
  const eixoX = { grid: { display: false }, ticks: { maxRotation: 0, autoSkipPadding: 14 } };
  const eixoY = (titulo, extra = {}) => ({ grid: { color: COR.linha }, border: { display: false },
    title: { display: !!titulo, text: titulo }, ticks: { maxTicksLimit: 5 }, ...extra });
  const base = (escalas, legenda = true, unidades = {}) => ({
    maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, scales: { x: eixoX, ...escalas },
    plugins: { legend: { display: legenda, labels: { boxWidth: 12 } },
               tooltip: { callbacks: { label: c => `${c.dataset.label}: ${num(c.parsed.y, unidades[c.dataset.label]?.[1] ?? 1)}${unidades[c.dataset.label]?.[0] ?? ''}` } } } });

  const graficos = {
    peso(c) {
      const sets = [linha('Peso', d.peso, COR.azul)];
      const esc = { y: eixoY('kg') };
      if (d.gordura.some(v => v != null)) {
        sets.push(linha('Gordura', d.gordura, COR.vermelho, 'y2'));
        esc.y2 = eixoY('%', { position: 'right', grid: { display: false } });
      }
      new Chart(c, { type: 'line', data: { labels: d.rotulos, datasets: sets },
        options: base(esc, sets.length > 1, { Peso: [' kg', 1], Gordura: ['%', 1] }) });
    },
    passos(c) {
      new Chart(c, { type: 'bar', data: { labels: d.rotulos, datasets: [
        { label: 'Passos', data: d.passos, backgroundColor: COR.verde, borderRadius: 3, maxBarThickness: 18 }] },
        options: base({ y: eixoY('', { beginAtZero: true }) }, false, { Passos: ['', 0] }) });
    },
    sono(c) {
      const s = d.sono, fases = s.sono_profundo_min.some(v => v != null);
      const sets = fases ? [
        { label: 'Profundo', data: s.sono_profundo_min, backgroundColor: COR.roxo },
        { label: 'Leve', data: s.sono_leve_min, backgroundColor: COR.azul },
        { label: 'REM', data: s.sono_rem_min, backgroundColor: COR.ciano },
        { label: 'Acordado', data: s.sono_acordado_min, backgroundColor: COR.amarelo },
      ] : [{ label: 'Sono', data: s.sono_min, backgroundColor: COR.azul }];
      sets.forEach(x => Object.assign(x, { stack: 'sono', maxBarThickness: 18 }));
      const u = Object.fromEntries(sets.map(x => [x.label, [' h', 1]]));
      new Chart(c, { type: 'bar', data: { labels: d.rotulos, datasets: sets },
        options: base({ x: { ...eixoX, stacked: true }, y: eixoY('h', { stacked: true, beginAtZero: true }) }, true, u) });
    },
    coracao(c) {
      const sets = [], esc = {};
      if (d.fc.some(v => v != null)) { sets.push(linha('FC em repouso', d.fc, COR.vermelho)); esc.y = eixoY('bpm'); }
      if (d.vfc.some(v => v != null)) {
        sets.push(linha('VFC', d.vfc, COR.roxo, sets.length ? 'y2' : 'y'));
        esc[sets.length > 1 ? 'y2' : 'y'] = eixoY('ms', sets.length > 1 ? { position: 'right', grid: { display: false } } : {});
      }
      new Chart(c, { type: 'line', data: { labels: d.rotulos, datasets: sets },
        options: base(esc, sets.length > 1, { 'FC em repouso': [' bpm', 0], VFC: [' ms', 0] }) });
    },
    respiracao(c) {
      const sets = [], esc = {};
      if (d.spo2.some(v => v != null)) { sets.push(linha('SpO2', d.spo2, COR.azul)); esc.y = eixoY('%'); }
      if (d.resp.some(v => v != null)) {
        sets.push(linha('Respiração', d.resp, COR.verde, sets.length ? 'y2' : 'y'));
        esc[sets.length > 1 ? 'y2' : 'y'] = eixoY('rpm', sets.length > 1 ? { position: 'right', grid: { display: false } } : {});
      }
      new Chart(c, { type: 'line', data: { labels: d.rotulos, datasets: sets },
        options: base(esc, sets.length > 1, { SpO2: ['%', 1], 'Respiração': [' rpm', 1] }) });
    },
  };
  document.querySelectorAll('canvas[data-saude]').forEach(c => {
    try { graficos[c.dataset.saude]?.(c); } catch (e) { console.error('Gráfico', c.dataset.saude, e); }
  });
})();
