"""Painel: património líquido e evolução, ativos/passivos, fluxo de caixa
(sankey) e despesas por categoria — inspirado no painel do Sure."""
from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import relatorios as rel
from .. import previsoes, retificacoes, tarefas
from ..backup import ultimo_backup
from ..deps import IntOpcional, entidade_escolhida, get_db, render, utilizador_atual
from ..formatos import parse_data
from ..models import PAPEIS, TIPOS_CONTA, Conta, Movimento, Utilizador
from .relatorios import data_referencia

router = APIRouter()
AGRUPAR = {"tipo": "Tipo de conta", "banco": "Banco", "nenhum": "Sem grupos"}
ORDENAR = {"nome": "Nome", "saldo": "Saldo (maior primeiro)", "banco": "Banco"}


def _organizar(contas, agrupar: str, ordenar: str) -> tuple[list, list]:
    """[(título do grupo, total, [(conta, saldo)])] para ativos e passivos."""
    chave_ordem = {
        "nome": lambda cs: cs[0].nome.lower(),
        "saldo": lambda cs: -abs(cs[1]),
        "banco": lambda cs: (cs[0].instituicao.nome.lower(), cs[0].nome.lower()),
    }[ordenar]

    def grupo(c):
        if agrupar == "banco":
            return c.instituicao.nome
        if agrupar == "tipo":
            return TIPOS_CONTA[c.tipo]
        return ""
    lados = []
    for passivo in (False, True):
        grupos = defaultdict(list)
        for c, s in contas:
            if (c.tipo in rel.PASSIVOS) == passivo:
                grupos[grupo(c)].append((c, s))
        lista = [(titulo, sum(s for _, s in itens), sorted(itens, key=chave_ordem)) for titulo, itens in grupos.items()]
        if agrupar == "tipo":   # ordem natural dos tipos
            ordem_tipos = list(TIPOS_CONTA.values())
            lista.sort(key=lambda g: ordem_tipos.index(g[0]))
        elif ordenar == "saldo":
            lista.sort(key=lambda g: -abs(g[1]))
        else:
            lista.sort(key=lambda g: g[0].lower())
        lados.append(lista)
    return lados[0], lados[1]


PERIODOS_PAINEL = {"mes": "Este mês", "trimestre": "Trimestre", "6meses": "6 meses", "ano": "12 meses"}


@router.get("/")
def painel(request: Request, entidade: IntOpcional = None, periodo: str = "mes", agrupar: str = "", ordenar: str = "",
           previstas: str = "",
           u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    visiveis, ent, papel = entidade_escolhida(request, db, u, entidade)
    backup = ultimo_backup(request.app.state.defs) if u.is_admin else None
    if not ent:
        return render(request, "painel.html", entidade=None, visiveis=visiveis, ultimo_backup=backup)
    ref = data_referencia(db, ent.id)
    p = rel.periodo(periodo if periodo in PERIODOS_PAINEL else "mes", ref)

    # Património: 12 fins de mês + a data de referência.
    meses = [rel.fim_do_mes(rel.somar_meses(ref.replace(day=1), -i)) for i in range(12, 0, -1)] + [ref]
    pat = rel.patrimonio(db, ent.id, meses)
    atual, ha_30 = pat["serie"][-1], rel.patrimonio(db, ent.id, [ref - timedelta(days=30)])["serie"][0]
    # Organização das contas (escolha memorizada na sessão).
    agrupar = agrupar if agrupar in AGRUPAR else request.session.get("painel_agrupar", "tipo")
    ordenar = ordenar if ordenar in ORDENAR else request.session.get("painel_ordenar", "nome")
    request.session["painel_agrupar"], request.session["painel_ordenar"] = agrupar, ordenar
    grupos_ativos, grupos_passivos = _organizar(pat["contas"], agrupar, ordenar)

    movs = rel.movimentos_reportaveis(db, ent.id, p.ini, p.fim)
    r = rel.resumir(movs)
    # Fluxo do mês: soma as receitas previstas (ordenados, cartões refeição) que ainda não entraram.
    previstas = previstas if previstas in ("1", "0") else request.session.get("painel_previstas", "1")
    request.session["painel_previstas"] = previstas
    estados = previsoes.estado_mes(db, ent.id, p.ini) if p.chave == "mes" else []
    pendentes = [e for e in estados if e.pendente] if previstas == "1" else []
    r_real = r
    if pendentes:
        r = replace(r, receitas=r.receitas + sum(e.valor for e in pendentes))
    despesas = [l for l in rel.linhas_ordenadas(r.por_categoria["DESPESA"]) if l.total > 0]
    n_sem = db.scalar(select(func.count()).select_from(Movimento).join(Conta).where(
        Conta.entidade_id == ent.id, Movimento.categoria_id.is_(None), ~Movimento.transferencia,
        ~Movimento.excluido))
    graficos = {
        "patrimonio": {"rotulos": [d.strftime("%d-%m-%Y") if d == ref else rel.rotulo_mes(d) for d in meses],
                       "liquido": [x["liquido"] / 100 for x in pat["serie"]],
                       "datas": [d.isoformat() for d in meses]},
        "sankey": rel.sankey(r, previstas=[(e.prev.descricao, e.valor) for e in pendentes]),
        "despesas": [{"nome": l.nome, "valor": l.total / 100, "cor": l.cor} for l in despesas],
    }
    prefs = request.app.state.prefs
    por_importar = None
    if tarefas.na_janela(prefs, date.today()) and PAPEIS[papel] >= PAPEIS["editar"]:
        por_importar = sum(b.por_fazer for b in tarefas.do_ciclo(db, ent.id, tarefas.inicio_ciclo(prefs, date.today())))
    return render(request, "painel.html", visiveis=visiveis, entidade=ent, p=p, por_importar=por_importar, periodos=PERIODOS_PAINEL, ref=ref,
                  atual=atual, ha_30=ha_30, grupos_ativos=grupos_ativos, grupos_passivos=grupos_passivos,
                  agrupar=agrupar, ordenar=ordenar, opcoes_agrupar=AGRUPAR, opcoes_ordenar=ORDENAR,
                  avisos=retificacoes.avisos(db, [c.id for c, _ in pat["contas"]]),
                  r=r, r_real=r_real, estados=estados, pendentes=pendentes, previstas=previstas,
                  total_previsto=sum(e.valor for e in pendentes), despesas=despesas, n_sem=n_sem, graficos=graficos, ultimo_backup=backup)


@router.get("/painel/variacao")
def variacao(request: Request, de: str, ate: str, entidade: IntOpcional = None,
             u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Fragmento HTML «O que mudou» entre duas datas (pedido pelo clique no gráfico do património)."""
    _, ent, _ = entidade_escolhida(request, db, u, entidade)
    if not ent:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    try:
        ini, fim = parse_data(de), parse_data(ate)
    except ValueError:
        ini = fim = None
    if not ini or not fim or ini > fim or (fim - ini).days > 400:
        raise HTTPException(status_code=400, detail="Período inválido.")
    return render(request, "_variacao.html", entidade=ent, v=rel.variacao_patrimonio(db, ent.id, ini, fim))
