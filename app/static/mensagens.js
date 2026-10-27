// Moldura de mensagens no fundo do ecrã: todas as mensagens da interface aparecem aqui, numa barra fixa sempre visível
// (a última mensagem) que se abre para o histórico desta sessão do browser.
//  - as mensagens das ações (flash) já vêm do servidor dentro de #mensagens;
//  - os avisos ao nível da página (.aviso filho direto de <main>), todos os erros e confirmações (.aviso-erro,
//    .aviso-ok, também dentro de secções) e o que tiver data-mensagem mudam-se para aqui — ao abrir e quando chegam
//    depois (fragmentos carregados por JavaScript: documentos do Paperless, fotos do Immich, análise da IA…);
//  - window.mensagem(texto, tipo) para o JavaScript (em vez de alert()).
// O histórico (últimas 30) fica no sessionStorage: mudar de página não o perde; «Limpar» esvazia-o.
(() => {
  const moldura = document.getElementById('moldura-mensagens');
  const lista = document.getElementById('mensagens');
  if (!moldura || !lista) return;
  const ultima = document.getElementById('mm-ultima');
  const contagem = document.getElementById('mm-contagem');
  const abrir = document.getElementById('mm-abrir');
  const TIPOS = ['ok', 'erro', 'alerta'];
  const CHAVE = 'gestor_mensagens';
  const MAX = 30;

  const ler = () => { try { return JSON.parse(sessionStorage.getItem(CHAVE) || '[]'); } catch { return []; } };
  const guardar = h => { try { sessionStorage.setItem(CHAVE, JSON.stringify(h.slice(-MAX))); } catch { /* sem armazenamento */ } };
  const hora = d => d.toLocaleTimeString('pt-PT', {hour: '2-digit', minute: '2-digit'});

  function linha(html, tipo, quando, nova) {
    const m = document.createElement('div');
    m.className = `mensagem mensagem-${tipo}${nova ? ' nova' : ''}`;
    m.dataset.tipo = tipo;
    const h = document.createElement('span');
    h.className = 'mensagem-hora';
    h.textContent = hora(new Date(quando));
    const t = document.createElement('div');
    t.className = 'mensagem-texto';
    t.innerHTML = html;
    m.append(h, t);
    lista.prepend(m);               // as mais recentes em cima
    return m;
  }

  function atualizar() {
    const itens = lista.querySelectorAll('.mensagem');
    const topo = itens[0];
    moldura.dataset.tipo = topo ? topo.dataset.tipo : 'vazio';
    ultima.innerHTML = topo ? topo.querySelector('.mensagem-texto').innerHTML : 'Sem mensagens.';
    contagem.textContent = itens.length ? `${itens.length}` : '';
    abrir.hidden = !itens.length;
  }

  function registar(html, tipo) {
    // um aviso da página repete-se a cada visita: a mesma mensagem não se duplica, sobe com a hora nova
    const h = ler().filter(x => !(x.html === html && x.tipo === tipo));
    lista.querySelectorAll('.mensagem').forEach(m => {
      if (m.dataset.tipo === tipo && m.querySelector('.mensagem-texto').innerHTML === html) m.remove();
    });
    const quando = Date.now();
    h.push({html, tipo, quando});
    guardar(h);
    linha(html, tipo, quando, true);
    atualizar();
    moldura.classList.remove('pisca');
    void moldura.offsetWidth;      // recomeçar a animação
    moldura.classList.add('pisca');
  }

  // histórico das páginas anteriores, depois as novas desta página (flash do servidor)
  const novas = [...lista.querySelectorAll('.mensagem')].map(m => [m.querySelector('.mensagem-texto').innerHTML,
                                                                   TIPOS.includes(m.dataset.tipo) ? m.dataset.tipo : 'alerta']);
  lista.replaceChildren();
  ler().forEach(x => linha(x.html, x.tipo, x.quando, false));
  novas.forEach(([html, tipo]) => registar(html, tipo));
  atualizar();

  window.mensagem = (texto, tipo = 'alerta') => {
    const d = document.createElement('div');
    d.textContent = String(texto);
    registar(d.innerHTML, TIPOS.includes(tipo) ? tipo : 'alerta');
  };

  abrir.addEventListener('click', () => {
    const aberta = moldura.classList.toggle('aberta');
    abrir.setAttribute('aria-expanded', String(aberta));
    abrir.textContent = aberta ? 'Fechar ▾' : 'Histórico ▴';
  });
  document.getElementById('mm-limpar').addEventListener('click', () => {
    guardar([]);
    lista.replaceChildren();
    moldura.classList.remove('aberta');
    abrir.setAttribute('aria-expanded', 'false');
    abrir.textContent = 'Histórico ▴';
    atualizar();
  });

  const SELETOR = 'main > .aviso, main .aviso-erro, main .aviso-ok, main [data-mensagem]';
  function mudar(el) {
    if (!el.isConnected || moldura.contains(el)) return;
    const tipo = TIPOS.find(t => el.classList.contains(`aviso-${t}`)) || 'alerta';
    registar(el.innerHTML, tipo);
    el.remove();
  }
  document.querySelectorAll(SELETOR).forEach(mudar);
  // o que chega depois (innerHTML de fragmentos)
  new MutationObserver(registos => {
    for (const r of registos) for (const n of r.addedNodes) {
      if (n.nodeType !== 1 || moldura.contains(n)) continue;
      if (n.matches(SELETOR)) mudar(n);
      n.querySelectorAll?.(SELETOR).forEach(mudar);
    }
  }).observe(document.querySelector('main') || document.body, {childList: true, subtree: true});
})();
