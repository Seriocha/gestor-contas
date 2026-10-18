// Início: modo «Personalizar» — mudar a ordem (arrastar ou ↑ ↓), a largura e os blocos visíveis; grava por utilizador.
(() => {
  const grelha = document.getElementById('inicio-blocos');
  const botao = document.getElementById('personalizar');
  const barra = document.getElementById('barra-personalizar');
  if (!grelha || !botao || !barra) return;
  const blocos = () => [...grelha.querySelectorAll('.bloco-inicio')];

  const estado = () => blocos().map(b => ({ id: b.dataset.bloco, w: Number(b.dataset.w), on: b.dataset.on === '1' }));
  const redesenhar = () => window.dispatchEvent(new Event('resize'));   // os gráficos ajustam-se à nova largura

  const ligar = ativo => {
    document.body.classList.toggle('a-personalizar', ativo);
    barra.hidden = !ativo;
    botao.hidden = ativo;
    blocos().forEach(b => { b.draggable = false; });
    redesenhar();
  };

  const definirLargura = (b, w) => {
    b.dataset.w = w;
    b.style.setProperty('--w', w);
    b.querySelectorAll('[data-largura]').forEach(x => x.setAttribute('aria-pressed', String(Number(x.dataset.largura) === w)));
    redesenhar();
  };
  const definirVisivel = (b, on) => {
    b.dataset.on = on ? '1' : '0';
    b.classList.toggle('oculto', !on);
    b.querySelector('[data-ver]').textContent = on ? 'Ocultar' : 'Mostrar';
  };

  botao.addEventListener('click', () => ligar(true));

  grelha.addEventListener('click', e => {
    if (!document.body.classList.contains('a-personalizar')) return;
    const b = e.target.closest('.bloco-inicio');
    if (!b) return;
    const col = e.target.closest('.grupo-colunas [data-colunas]');
    if (col) {
      const grupo = col.closest('.grupo-colunas').dataset.grupo, n = col.dataset.colunas;
      const tiles = grelha.querySelector(`.tiles[data-grupo="${grupo}"]`);
      tiles.dataset.colunas = n;
      if (n) tiles.style.setProperty('--colunas', n); else tiles.style.removeProperty('--colunas');
      col.parentElement.querySelectorAll('[data-colunas]').forEach(x => x.setAttribute('aria-pressed', String(x === col)));
      return;
    }
    const largura = e.target.closest('[data-largura]');
    const mover = e.target.closest('[data-mover]');
    if (largura) definirLargura(b, Number(largura.dataset.largura));
    else if (mover) {
      const lista = blocos(), i = lista.indexOf(b), j = i + Number(mover.dataset.mover);
      if (j < 0 || j >= lista.length) return;
      if (j < i) grelha.insertBefore(b, lista[j]); else grelha.insertBefore(lista[j], b);
      b.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      redesenhar();
    } else if (e.target.closest('[data-ver]')) definirVisivel(b, b.dataset.on !== '1');
    else if (e.target.closest('a')) e.preventDefault();          // a personalizar, as ligações não navegam
  });

  // arrastar pelo ⠿ (rato); no telemóvel usam-se as setas
  let arrastado = null;
  grelha.addEventListener('pointerdown', e => {
    const pega = e.target.closest('.bloco-pega');
    if (pega && document.body.classList.contains('a-personalizar')) pega.closest('.bloco-inicio').draggable = true;
  });
  grelha.addEventListener('dragstart', e => {
    arrastado = e.target.closest('.bloco-inicio');
    if (!arrastado || !arrastado.draggable) { e.preventDefault(); return; }
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', arrastado.dataset.bloco);
    requestAnimationFrame(() => arrastado && arrastado.classList.add('a-arrastar'));
  });
  grelha.addEventListener('dragover', e => {
    if (!arrastado) return;
    e.preventDefault();
    const alvo = e.target.closest('.bloco-inicio');
    if (!alvo || alvo === arrastado) return;
    const r = alvo.getBoundingClientRect();
    // antes ou depois do alvo, conforme o lado (blocos lado a lado) ou a metade (blocos empilhados)
    const depois = r.width < grelha.clientWidth * 0.9 ? e.clientX > r.left + r.width / 2 : e.clientY > r.top + r.height / 2;
    grelha.insertBefore(arrastado, depois ? alvo.nextSibling : alvo);
  });
  const largar = () => {
    if (!arrastado) return;
    arrastado.classList.remove('a-arrastar');
    arrastado.draggable = false;
    arrastado = null;
    redesenhar();
  };
  grelha.addEventListener('drop', e => { e.preventDefault(); largar(); });
  grelha.addEventListener('dragend', largar);

  const enviar = async layout => {
    const fd = new FormData();
    fd.append('csrf', barra.dataset.csrf);
    fd.append('layout', layout);
    const colunas = {};
    grelha.querySelectorAll('.tiles[data-grupo]').forEach(x => { if (x.dataset.colunas) colunas[x.dataset.grupo] = Number(x.dataset.colunas); });
    fd.append('colunas', JSON.stringify(colunas));
    const r = await fetch('/inicio/layout', { method: 'POST', body: fd });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) throw new Error(d.erro || 'Não foi possível guardar.');
  };

  barra.addEventListener('click', async e => {
    const acao = e.target.closest('[data-acao]');
    if (!acao) return;
    const tipo = acao.dataset.acao;
    if (tipo === 'cancelar') {
      location.reload();          // volta à disposição gravada
      return;
    }
    if (tipo === 'repor' && !confirm('Voltar à disposição original do Início?')) return;
    acao.disabled = true;
    try {
      await enviar(tipo === 'repor' ? '' : JSON.stringify(estado()));
      location.reload();
    } catch (err) {
      alert(err.message);
      acao.disabled = false;
    }
  });

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && document.body.classList.contains('a-personalizar')) barra.querySelector('[data-acao=cancelar]').click();
  });
})();
