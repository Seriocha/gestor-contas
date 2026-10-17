// Campos de data sempre em dd/mm/aaaa: o <input type="date"> mostra o formato da língua do browser
// (num browser em inglês, mm/dd/aaaa). Cada um passa a texto dd/mm/aaaa (o servidor aceita este formato)
// e ganha um botão que abre o calendário nativo, escondido, para escolher a data.
(() => {
  const paraPt = v => /^\d{4}-\d{2}-\d{2}$/.test(v) ? `${v.slice(8, 10)}/${v.slice(5, 7)}/${v.slice(0, 4)}` : v;
  const paraIso = v => {
    const m = (v || '').match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
    return m ? `${m[3]}-${m[2]}-${m[1]}` : '';
  };
  const arrumar = v => {                    // «17102026» ou «17-10-2026» → «17/10/2026»
    const d = (v || '').replace(/\D/g, '');
    return d.length === 8 ? `${d.slice(0, 2)}/${d.slice(2, 4)}/${d.slice(4)}` : v;
  };

  document.querySelectorAll('input[type=date]').forEach(campo => {
    const nativo = document.createElement('input');
    nativo.type = 'date';
    nativo.tabIndex = -1;
    nativo.className = 'data-nativa';
    nativo.setAttribute('aria-hidden', 'true');
    if (campo.min) nativo.min = campo.min;
    if (campo.max) nativo.max = campo.max;

    // O browser esvazia o .value de um campo date que não esteja em aaaa-mm-dd (ex.: um filtro que voltou
    // como «01/09/2026»); o atributo value guarda o original.
    const original = campo.value || campo.getAttribute('value') || '';
    campo.type = 'text';
    campo.value = paraPt(original);
    campo.defaultValue = campo.value;
    campo.placeholder = 'dd/mm/aaaa';
    campo.inputMode = 'numeric';
    campo.autocomplete = 'off';
    campo.maxLength = 10;
    campo.pattern = '\\d{2}/\\d{2}/\\d{4}';
    campo.title = 'Data no formato dd/mm/aaaa';
    campo.removeAttribute('min');
    campo.removeAttribute('max');

    const caixa = document.createElement('span');
    caixa.className = 'campo-data';
    campo.replaceWith(caixa);
    const botao = document.createElement('button');
    botao.type = 'button';
    botao.className = 'botao-data';
    botao.setAttribute('aria-label', 'Escolher no calendário');
    botao.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/></svg>';
    caixa.append(campo, botao, nativo);

    botao.addEventListener('click', () => {
      if (campo.disabled) return;
      nativo.value = paraIso(campo.value);
      try { nativo.showPicker(); } catch (e) { nativo.focus(); nativo.click(); }
    });
    nativo.addEventListener('change', () => {
      campo.value = paraPt(nativo.value);
      campo.dispatchEvent(new Event('input', { bubbles: true }));
      campo.dispatchEvent(new Event('change', { bubbles: true }));
    });
    campo.addEventListener('blur', () => { campo.value = arrumar(campo.value); });
  });
})();
