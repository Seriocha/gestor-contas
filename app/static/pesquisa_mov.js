// Pesquisa de um movimento do extrato para associar (ida à oficina): texto ou valor; sem texto, os perto da data.
(() => {
  document.querySelectorAll('.pesquisa-mov').forEach(caixa => {
    const form = caixa.closest('form');
    const escondido = caixa.querySelector('input[name=movimento_id]');
    const escolhido = caixa.querySelector('.mov-escolhido');
    const pesquisa = caixa.querySelector('.mov-pesquisa');
    const lista = caixa.querySelector('.mov-resultados');
    let temporizador = null, pedido = 0;

    const texto = (tag, cls, t) => { const e = document.createElement(tag); if (cls) e.className = cls; e.textContent = t; return e; };
    const limpar = () => {
      escondido.value = '';
      escolhido.replaceChildren(texto('span', 'nota', 'nenhum'));
    };
    const escolher = r => {
      escondido.value = r.id;
      const linha = document.createElement('span');
      linha.append(`${r.data} · ${r.descricao} · `, texto('strong', '', r.valor), ' ', texto('span', 'nota', r.conta));
      const tirar = texto('button', 'ligacao', 'Tirar');
      tirar.type = 'button';
      tirar.addEventListener('click', limpar);
      escolhido.replaceChildren(linha, ' ', tirar);
      const custo = form.elements.custo;
      if (custo && !custo.value) custo.value = r.custo;
      lista.hidden = true;
      pesquisa.value = '';
    };
    escolhido.querySelector('[data-limpar]')?.addEventListener('click', limpar);

    const procurar = async () => {
      const n = ++pedido;
      const params = new URLSearchParams({ q: pesquisa.value, data: form.elements.data?.value || '', manutencao: caixa.dataset.manutencao || '' });
      let resultados = [];
      try {
        const r = await fetch(`${caixa.dataset.url}?${params}`);
        resultados = r.ok ? await r.json() : [];
      } catch (e) { resultados = []; }
      if (n !== pedido) return;                       // chegou uma resposta mais recente
      lista.replaceChildren();
      if (!resultados.length) lista.append(texto('li', 'nota', 'Nenhum débito encontrado.'));
      resultados.forEach(r => {
        const li = document.createElement('li');
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'mov-opcao' + (r.ligado ? ' ligado' : '');
        b.append(texto('span', 'sem-quebra', r.data), texto('span', 'mov-opcao-desc', r.descricao),
                 texto('span', 'nota', r.conta + (r.deste_carro ? ' · deste carro' : '') + (r.ligado ? ' · já noutra ida' : '')),
                 texto('strong', 'num neg', r.valor));
        b.addEventListener('click', () => escolher(r));
        li.append(b);
        lista.append(li);
      });
      lista.hidden = false;
    };
    pesquisa.addEventListener('input', () => { clearTimeout(temporizador); temporizador = setTimeout(procurar, 250); });
    pesquisa.addEventListener('focus', () => { if (lista.hidden) procurar(); });
    pesquisa.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); procurar(); } });
  });
})();
