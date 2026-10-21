/* Vista da TV: navegação com as setas do comando, relógio, «Ocultar valores» e espera da aprovação do emparelhamento.
   ES5 de propósito (browsers de TV antigos): nada de let/const, arrow functions, fetch ou optional chaining. */
(function () {
  "use strict";

  function visivel(el) {
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  function focaveis() {
    var todos = document.querySelectorAll("[data-nav]"), out = [], i;
    for (i = 0; i < todos.length; i++) if (visivel(todos[i])) out.push(todos[i]);
    return out;
  }

  function focar(el) {
    if (!el) return;
    el.focus();
    var r = el.getBoundingClientRect(), h = window.innerHeight;
    if (r.top < h * 0.08 || r.bottom > h * 0.92) window.scrollBy(0, r.top - h * 0.3);
  }

  // Elemento mais próximo na direção pedida: distância entre as margens no eixo da seta + o dobro do desvio no outro
  // eixo (zero quando se sobrepõem — um cartão largo por baixo de um botão conta como «logo abaixo»).
  function mover(dir) {
    var atual = document.activeElement, lista = focaveis();
    if (!atual || !atual.hasAttribute || !atual.hasAttribute("data-nav")) {
      focar(document.querySelector("[data-inicial]") || lista[0]);
      return;
    }
    var a = atual.getBoundingClientRect();
    var melhor = null, melhorPontos = Infinity, i, r, eixo, desvio, pontos;
    for (i = 0; i < lista.length; i++) {
      if (lista[i] === atual) continue;
      r = lista[i].getBoundingClientRect();
      if (dir === "direita") eixo = r.left - a.right;
      else if (dir === "esquerda") eixo = a.left - r.right;
      else if (dir === "baixo") eixo = r.top - a.bottom;
      else eixo = a.top - r.bottom;
      if (eixo < -4) continue;
      desvio = (dir === "direita" || dir === "esquerda")
        ? Math.max(0, r.top - a.bottom, a.top - r.bottom)
        : Math.max(0, r.left - a.right, a.left - r.right);
      pontos = Math.max(0, eixo) + 2 * desvio;
      // desempate: o que estiver mais alinhado com o centro do atual
      pontos += 0.01 * ((dir === "direita" || dir === "esquerda")
        ? Math.abs((r.top + r.bottom) - (a.top + a.bottom)) : Math.abs((r.left + r.right) - (a.left + a.right)));
      if (pontos < melhorPontos) { melhorPontos = pontos; melhor = lista[i]; }
    }
    if (melhor) focar(melhor);
    else if (dir === "baixo") window.scrollBy(0, window.innerHeight * 0.4);   // blocos maiores do que o ecrã
    else if (dir === "cima") window.scrollBy(0, -window.innerHeight * 0.4);
  }

  var TECLAS = { 37: "esquerda", 38: "cima", 39: "direita", 40: "baixo" };
  document.addEventListener("keydown", function (ev) {
    var dir = TECLAS[ev.keyCode];
    if (dir) { ev.preventDefault(); mover(dir); return; }
    if (ev.keyCode === 10009 && history.length > 1) { ev.preventDefault(); history.back(); }   // «Voltar» do comando Samsung
  });

  // relógio
  var relogio = document.getElementById("relogio");
  function horas() {
    var d = new Date(), h = d.getHours(), m = d.getMinutes();
    relogio.textContent = (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m;
  }
  if (relogio) setInterval(horas, 15000);

  // «Ocultar valores»: fica lembrado nesta TV (se o browser deixar guardar)
  var botao = document.getElementById("privado");
  function lerPrivado() { try { return localStorage.getItem("gestor_tv_privado") === "1"; } catch (e) { return false; } }
  function aplicar(on) {
    document.body.className = document.body.className.replace(/\s*privado/g, "") + (on ? " privado" : "");
    if (botao) botao.textContent = on ? "Mostrar valores" : "Ocultar valores";
  }
  aplicar(lerPrivado());
  if (botao) botao.addEventListener("click", function () {
    var on = document.body.className.indexOf("privado") < 0;
    try { localStorage.setItem("gestor_tv_privado", on ? "1" : "0"); } catch (e) { /* sem armazenamento: só nesta página */ }
    aplicar(on);
  });

  // emparelhar: perguntar ao servidor de 3 em 3 segundos se o código já foi aprovado
  var emp = document.getElementById("emparelhar");
  if (emp) {
    var perguntar = function () {
      var x = new XMLHttpRequest();
      x.open("GET", emp.getAttribute("data-estado") + "?t=" + Date.now());
      x.onload = function () {
        var estado = "espera";
        try { estado = JSON.parse(x.responseText).estado; } catch (e) { /* resposta inesperada: tentar outra vez */ }
        if (estado === "aprovado") { window.location.href = "/tv"; return; }
        if (estado === "expirado") { window.location.reload(); return; }
        setTimeout(perguntar, 3000);
      };
      x.onerror = function () { setTimeout(perguntar, 6000); };
      x.send();
    };
    setTimeout(perguntar, 3000);
  } else {
    focar(document.querySelector("[data-inicial]") || focaveis()[0]);
  }
})();
