// Gráficos do painel e dos relatórios (Chart.js, sem dependências externas).
// Os dados chegam num <script type="application/json" id="dados-graficos">;
// cada <canvas data-grafico="..."> escolhe o gráfico a desenhar.
(() => {
  const fonte = document.getElementById('dados-graficos');
  if (!fonte || !window.Chart) return;
  const dados = JSON.parse(fonte.textContent);
  const css = getComputedStyle(document.documentElement);
  const tok = (nome, omissao) => (css.getPropertyValue(nome) || '').trim() || omissao;
  const COR = {
    texto: tok('--texto', '#1d2330'),
    suave: tok('--suave', '#667085'),
    linha: tok('--linha', '#e3e6ec'),
    superficie: tok('--superficie', '#ffffff'),
    receitas: tok('--serie-receitas', '#2a78d6'),
    despesas: tok('--serie-despesas', '#e34948'),
    patrimonio: tok('--serie-patrimonio', '#2a78d6'),
  };
  const eur = new Intl.NumberFormat('pt-PT', { style: 'currency', currency: 'EUR' });
  const eurCurto = v => Math.abs(v) >= 1000
    ? new Intl.NumberFormat('pt-PT', { maximumFractionDigits: 1 }).format(v / 1000) + ' mil €'
    : eur.format(v);

  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.color = COR.suave;
  Chart.defaults.borderColor = COR.linha;
  Chart.defaults.plugins.tooltip.callbacks.label = ctx =>
    `${ctx.dataset.label ? ctx.dataset.label + ': ' : ''}${eur.format(ctx.parsed.y ?? ctx.parsed)}`;

  const eixos = {
    x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkipPadding: 12 } },
    y: { grid: { color: COR.linha }, border: { display: false }, ticks: { callback: eurCurto, maxTicksLimit: 5 } },
  };

  const desenhar = {
    patrimonio(canvas) {
      const d = dados.patrimonio;
      const detalhe = document.getElementById('detalhe-variacao');
      let escolhido = null;
      const grafico = new Chart(canvas, {
        type: 'line',
        data: { labels: d.rotulos, datasets: [{
          label: 'Património líquido', data: d.liquido, borderColor: COR.patrimonio, borderWidth: 2,
          backgroundColor: COR.patrimonio + '1f', fill: true, tension: 0.25,
          pointRadius: 0, pointHoverRadius: 5, pointHitRadius: 12,
        }] },
        options: { maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
                   plugins: { legend: { display: false } }, scales: eixos,
                   onHover: (e, el) => { if (detalhe) e.native.target.style.cursor = el.length ? 'pointer' : 'default'; },
                   onClick: (e, el) => { if (detalhe && el.length && d.datas) abrirVariacao(el[0].index); } },
      });

      // Clique num ponto: o que mudou desde o ponto anterior (ou desde o início do mês, no 1.º).
      async function abrirVariacao(i) {
        const fim = d.datas[i];
        let ini;
        if (i > 0) {
          const a = new Date(d.datas[i - 1] + 'T12:00:00'); a.setDate(a.getDate() + 1);
          ini = a.toISOString().slice(0, 10);
        } else {
          ini = fim.slice(0, 8) + '01';
        }
        escolhido = i;
        grafico.data.datasets[0].pointRadius = d.datas.map((_, k) => (k === i ? 6 : 0));
        grafico.update('none');
        detalhe.hidden = false;
        detalhe.innerHTML = '<p class="nota">A carregar…</p>';
        const url = `/painel/variacao?entidade=${detalhe.dataset.entidade}&de=${ini}&ate=${fim}`;
        try {
          const r = await fetch(url);
          if (!r.ok) throw new Error(r.status);
          detalhe.innerHTML = await r.text();
        } catch (err) {
          detalhe.innerHTML = '<p class="aviso aviso-erro">Não foi possível obter o detalhe deste período.</p>';
        }
        detalhe.querySelector('[data-fechar-variacao]')?.addEventListener('click', () => {
          detalhe.hidden = true; escolhido = null;
          grafico.data.datasets[0].pointRadius = 0; grafico.update('none');
        });
        detalhe.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    },

    investimento(canvas) {
      // Valor da conta vs capital investido: a distância entre as linhas é o ganho/perda.
      const d = dados.investimento;
      new Chart(canvas, {
        type: 'line',
        data: { labels: d.rotulos, datasets: [
          { label: 'Valor da conta', data: d.valor, borderColor: COR.patrimonio, backgroundColor: COR.patrimonio + '1f',
            borderWidth: 2, fill: '+1', tension: 0.2, pointRadius: 0, pointHoverRadius: 5, pointHitRadius: 12 },
          { label: 'Capital investido', data: d.capital, borderColor: COR.suave, borderWidth: 2, borderDash: [6, 4],
            stepped: true, fill: false, pointRadius: 0, pointHoverRadius: 4, pointHitRadius: 12 },
        ] },
        options: { maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
          plugins: { legend: { position: 'top', align: 'end', labels: { boxWidth: 12, boxHeight: 2 } },
            tooltip: { callbacks: { afterBody: items => {
              const v = items.find(i => i.datasetIndex === 0)?.parsed.y, c = items.find(i => i.datasetIndex === 1)?.parsed.y;
              if (v == null || c == null) return '';
              const g = v - c;
              return `Ganho/perda: ${g >= 0 ? '+' : ''}${eur.format(g)}`;
            } } } },
          scales: eixos },
      });
    },

    bem(canvas) {
      // Histórico do valor de uma casa/carro: valor estimado e o intervalo (mín.–máx.) dado pela IA.
      const d = dados.bem;
      new Chart(canvas, {
        type: 'line',
        data: { labels: d.rotulos, datasets: [
          { label: 'Máximo', data: d.maximo, borderWidth: 0, pointRadius: 0, fill: false },
          { label: 'Mínimo', data: d.minimo, borderWidth: 0, pointRadius: 0, backgroundColor: COR.patrimonio + '1f', fill: '-1' },
          { label: 'Valor', data: d.valor, borderColor: COR.patrimonio, borderWidth: 2, tension: 0.2, pointRadius: 3,
            pointHoverRadius: 5, fill: false },
        ] },
        options: { maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
          plugins: { legend: { display: false } }, scales: eixos },
      });
    },

    despesas_bem(canvas) {
      // Despesas de um bem por mês, empilhadas pelas maiores categorias.
      const d = dados.despesas_bem;
      new Chart(canvas, {
        type: 'bar',
        data: { labels: d.rotulos, datasets: d.series.map(s => ({ label: s.nome, data: s.valores, backgroundColor: s.cor,
          borderRadius: 3, maxBarThickness: 26, stack: 'despesas' })) },
        options: { maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
          plugins: { legend: { position: 'top', align: 'end', labels: { boxWidth: 10, boxHeight: 10 } } },
          scales: { x: { ...eixos.x, stacked: true }, y: { ...eixos.y, stacked: true } } },
      });
    },

    tendencia(canvas) {
      const d = dados.tendencia;
      const barra = { borderRadius: 4, borderSkipped: 'start', maxBarThickness: 22, categoryPercentage: 0.7, barPercentage: 0.9 };
      new Chart(canvas, {
        data: { labels: d.rotulos, datasets: [
          { type: 'bar', label: 'Receitas', data: d.receitas, backgroundColor: COR.receitas, ...barra, order: 2 },
          { type: 'bar', label: 'Despesas', data: d.despesas, backgroundColor: COR.despesas, ...barra, order: 2 },
          { type: 'line', label: 'Saldo do mês', data: d.saldo, borderColor: COR.texto, backgroundColor: COR.texto,
            borderWidth: 2, pointRadius: 3, pointHoverRadius: 5, tension: 0.2, order: 1 },
        ] },
        options: { maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
                   plugins: { legend: { position: 'top', align: 'end', labels: { boxWidth: 12, boxHeight: 12, useBorderRadius: true, borderRadius: 3 } } },
                   scales: eixos },
      });
    },

    donut(canvas) {
      // A paleta tem 8 cores e nunca se reciclam: as categorias sem cor própria
      // e as que passam das 7 maiores juntam-se em "Outras".
      const NEUTRA = '#8a8a85';
      const comCor = dados.despesas.filter(l => l.cor !== NEUTRA);
      let linhas = comCor.slice(0, 7);
      const resto = dados.despesas.filter(l => !linhas.includes(l)).reduce((s, l) => s + l.valor, 0);
      if (resto > 0) linhas = linhas.concat([{ nome: 'Outras', valor: resto, cor: NEUTRA }]);
      const total = linhas.reduce((s, l) => s + l.valor, 0);
      new Chart(canvas, {
        type: 'doughnut',
        data: { labels: linhas.map(l => l.nome), datasets: [{
          data: linhas.map(l => l.valor), backgroundColor: linhas.map(l => l.cor),
          borderColor: COR.superficie, borderWidth: 2, hoverOffset: 6,
        }] },
        options: { maintainAspectRatio: false, cutout: '62%',
          plugins: {
            legend: { position: 'right', labels: { boxWidth: 12, boxHeight: 12, useBorderRadius: true, borderRadius: 3 } },
            tooltip: { callbacks: { label: ctx => `${ctx.label}: ${eur.format(ctx.parsed)} (${(ctx.parsed / total * 100).toFixed(0)}%)` } },
          } },
      });
    },

    sankey(canvas) {
      const fluxos = dados.sankey;
      const cores = {};
      fluxos.forEach(f => { cores[f.to] = cores[f.to] || f.cor; cores[f.from] = cores[f.from] || f.cor; });
      cores['Rendimento'] = COR.receitas;
      new Chart(canvas, {
        type: 'sankey',
        data: { datasets: [{
          data: fluxos.map(f => ({ from: f.from, to: f.to, flow: f.flow })),
          colorFrom: c => cores[c.dataset.data[c.dataIndex].from],
          colorTo: c => cores[c.dataset.data[c.dataIndex].to],
          colorMode: 'gradient', alpha: 0.45, borderWidth: 0, nodeWidth: 10, nodePadding: 18,
          size: 'max', color: COR.texto,
          labels: Object.fromEntries(Object.keys(cores).map(n => {
            const tot = fluxos.filter(f => f.to === n || f.from === n)
              .reduce((s, f) => s + (n === 'Rendimento' ? (f.to === n ? f.flow : 0) : f.flow), 0);
            return [n, `${n} · ${eurCurto(tot)}`];
          })),
        }] },
        options: { maintainAspectRatio: false, plugins: { legend: { display: false },
          tooltip: { callbacks: { label: ctx => {
            const f = ctx.dataset.data[ctx.dataIndex];
            return `${f.from} → ${f.to}: ${eur.format(f.flow)}`;
          } } } } },
      });
    },
  };

  document.querySelectorAll('canvas[data-grafico]').forEach(c => {
    const tipo = c.dataset.grafico;
    if (desenhar[tipo] && dados[tipo === 'donut' ? 'despesas' : tipo]) {
      try { desenhar[tipo](c); } catch (e) { console.error('Gráfico', tipo, e); }
    }
  });
})();
