// Linha de mensagens no fundo do ecrã: todas as mensagens da interface aparecem aqui.
//  - as mensagens das ações (flash) já vêm do servidor dentro de #mensagens;
//  - os avisos ao nível da página (.aviso filho direto de <main>), todos os erros e confirmações (.aviso-erro,
//    .aviso-ok, também dentro de secções) e o que tiver data-mensagem mudam-se para aqui — ao abrir e quando chegam
//    depois (fragmentos carregados por JavaScript: documentos do Paperless, fotos do Immich, análise da IA…);
//  - window.mensagem(texto, tipo) para o JavaScript (em vez de alert()).
// As de sucesso desaparecem ao fim de 6 s; erros e alertas ficam até se fechar (×).
(() => {
  const caixa = document.getElementById('mensagens');
  if (!caixa) return;
  const TIPOS = ['ok', 'erro', 'alerta'];

  function preparar(m) {
    if (!m.querySelector('.mensagem-fechar')) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'mensagem-fechar';
      b.setAttribute('aria-label', 'Fechar mensagem');
      b.textContent = '×';
      m.append(b);
    }
    m.querySelector('.mensagem-fechar').addEventListener('click', () => m.remove());
    if (m.dataset.tipo === 'ok') setTimeout(() => m.remove(), 6000);
  }

  function criar(conteudo, tipo) {
    const m = document.createElement('div');
    m.className = `mensagem mensagem-${tipo}`;
    m.dataset.tipo = tipo;
    const t = document.createElement('div');
    t.className = 'mensagem-texto';
    if (conteudo instanceof Node) t.append(...conteudo.childNodes); else t.textContent = conteudo;
    m.append(t);
    caixa.append(m);
    preparar(m);
    return m;
  }

  window.mensagem = (texto, tipo = 'alerta') => criar(String(texto), TIPOS.includes(tipo) ? tipo : 'alerta');

  const SELETOR = 'main > .aviso, main .aviso-erro, main .aviso-ok, main [data-mensagem]';
  function mudar(el) {
    if (!el.isConnected || caixa.contains(el)) return;
    const tipo = TIPOS.find(t => el.classList.contains(`aviso-${t}`)) || 'alerta';
    criar(el, tipo);
    el.remove();
  }
  caixa.querySelectorAll('.mensagem').forEach(preparar);
  document.querySelectorAll(SELETOR).forEach(mudar);
  // o que chega depois (innerHTML de fragmentos)
  new MutationObserver(registos => {
    for (const r of registos) for (const n of r.addedNodes) {
      if (n.nodeType !== 1 || caixa.contains(n)) continue;
      if (n.matches(SELETOR)) mudar(n);
      n.querySelectorAll?.(SELETOR).forEach(mudar);
    }
  }).observe(document.querySelector('main') || document.body, {childList: true, subtree: true});
})();
