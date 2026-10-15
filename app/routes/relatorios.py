"""Relatórios: receitas e despesas por categoria, tendências, património, CSV."""
import csv
import io
import re
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import relatorios as rel
from .. import previsoes, servicos
from ..deps import (IntOpcional, auditar, entidade_escolhida, exigir_papel, flash, get_db, render,
                    utilizador_atual)
from ..formatos import parse_data
from ..models import PAPEIS, Conta, Etiqueta, Movimento, Utilizador
from ..seguranca import ler_form

router = APIRouter(prefix="/relatorios")


def data_referencia(db: Session, entidade_id: int) -> date:
    """Último dia com movimentos (os extratos chegam com atraso); hoje se não houver."""
    ultimo = db.scalar(select(func.max(Movimento.data_mov)).join(Conta).where(Conta.entidade_id == entidade_id))
    return min(ultimo, date.today()) if ultimo else date.today()


def _mes_escolhido(mes: str, ref: date) -> date | None:
    """'aaaa-mm' → último dia desse mês (ou a data de referência, se for o mês dela)."""
    m = re.fullmatch(r"(\d{4})-(\d{2})", mes or "")
    if not m or not 1 <= int(m.group(2)) <= 12:
        return None
    d = date(int(m.group(1)), int(m.group(2)), 1)
    return ref if (d.year, d.month) == (ref.year, ref.month) else rel.fim_do_mes(d)


def _meses_disponiveis(db: Session, entidade_id: int, ref: date) -> list[tuple[str, str]]:
    """[('aaaa-mm', 'Out 2026')] do mês de referência para trás até ao 1.º movimento (máx. 36)."""
    primeiro = db.scalar(select(func.min(Movimento.data_mov)).join(Conta).where(Conta.entidade_id == entidade_id)) or ref
    out, d = [], ref.replace(day=1)
    while d >= primeiro.replace(day=1) and len(out) < 36:
        out.append((f"{d:%Y-%m}", f"{rel.MESES_PT[d.month - 1].capitalize()} {d.year}"))
        d = rel.somar_meses(d, -1)
    return out


def _contexto(request: Request, db: Session, u: Utilizador, entidade, periodo, de, ate, conta, etiqueta,
              previstas: str = "0", mes: str = ""):
    visiveis, ent, papel = entidade_escolhida(request, db, u, entidade)
    if not ent:
        return None
    try:
        d_de, d_ate = parse_data(de), parse_data(ate)
    except ValueError:
        d_de = d_ate = None
    ref_dados = data_referencia(db, ent.id)
    periodo = periodo if periodo in rel.PRESETS else "mes"
    ref = (_mes_escolhido(mes, ref_dados) if periodo == "mes" else None) or ref_dados
    p = rel.periodo(periodo, ref, d_de, d_ate)
    ant = p.anterior
    contas = rel.contas_da_entidade(db, ent.id)
    conta_ids = [conta] if conta and conta in {c.id for c in contas} else None
    movs = rel.movimentos_reportaveis(db, ent.id, p.ini, p.fim, conta_ids, etiqueta or None)
    movs_ant = rel.movimentos_reportaveis(db, ent.id, ant.ini, ant.fim, conta_ids, etiqueta or None)
    r, r_ant = rel.resumir(movs), rel.resumir(movs_ant)
    # Receitas previstas (ordenados, cartões refeição) ainda por receber no mês de referência.
    pendentes = [] if etiqueta else previsoes.pendentes(db, ent.id, p.ini, p.fim, ref_dados, conta_ids)
    r_real = r
    if previstas == "1":
        r = previsoes.somar(r, pendentes)
    # Tendências: sempre 12 meses até ao fim do período, para haver contexto.
    janela = rel.Periodo("ano", rel.somar_meses(p.fim.replace(day=1), -11), p.fim, "12 meses")
    movs_janela = rel.movimentos_reportaveis(db, ent.id, janela.ini, janela.fim, conta_ids, etiqueta or None)
    return {
        "visiveis": visiveis, "entidade": ent, "p": p, "ant": ant, "presets": rel.PRESETS, "contas": contas,
        "etiquetas": db.scalars(select(Etiqueta).order_by(Etiqueta.nome)).all(),
        "meses": _meses_disponiveis(db, ent.id, ref_dados),
        "f": {"periodo": p.chave, "mes": f"{ref:%Y-%m}", "de": de, "ate": ate, "conta": conta or "", "etiqueta": etiqueta or ""},
        "r": r, "r_ant": r_ant, "r_real": r_real, "pendentes": pendentes, "previstas": previstas,
        "total_previsto": sum(e.valor for e in pendentes), "janela": janela, "movs_janela": movs_janela,
        "despesas": rel.linhas_ordenadas(r.por_categoria["DESPESA"], r_ant.por_categoria["DESPESA"]),
        "receitas": rel.linhas_ordenadas(r.por_categoria["RECEITA"], r_ant.por_categoria["RECEITA"]),
    }


@router.get("")
def relatorios(request: Request, entidade: IntOpcional = None, periodo: str = "mes", de: str = "", ate: str = "",
               conta: IntOpcional = None, etiqueta: IntOpcional = None, previstas: str = "", mes: str = "",
               u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    # a escolha «somar as previstas / só o real» é a mesma do painel
    previstas = previstas if previstas in ("1", "0") else request.session.get("painel_previstas", "1")
    request.session["painel_previstas"] = previstas
    ctx = _contexto(request, db, u, entidade, periodo, de, ate, conta, etiqueta, previstas, mes)
    if not ctx:
        return render(request, "relatorios.html", entidade=None, visiveis=[])
    ent, p, ant, janela = ctx["entidade"], ctx["p"], ctx["ant"], ctx["janela"]
    mensal = rel.serie_mensal(ctx["movs_janela"], janela)
    estat = rel.estatisticas_categorias(ctx["movs_janela"], janela)
    datas = [rel.fim_do_mes(m) for m in janela.meses()]
    datas[-1] = min(datas[-1], p.fim)
    pat = rel.patrimonio(db, ent.id, datas)
    pat_ant = rel.patrimonio(db, ent.id, [ant.fim])["serie"][0]
    graficos = {
        "tendencia": {"rotulos": [x["rotulo"] for x in mensal], "receitas": [x["receitas"] / 100 for x in mensal],
                      "despesas": [x["despesas"] / 100 for x in mensal], "saldo": [x["saldo"] / 100 for x in mensal]},
        "patrimonio": {"rotulos": [rel.rotulo_mes(d) for d in datas], "liquido": [x["liquido"] / 100 for x in pat["serie"]],
                       "datas": [d.isoformat() for d in datas]},
        "despesas": [{"nome": l.nome, "valor": l.total / 100, "cor": l.cor} for l in ctx["despesas"] if l.total > 0],
    }
    return render(request, "relatorios.html", **ctx, mensal=mensal, estat=estat[:12], pat=pat, pat_ant=pat_ant,
                  graficos=graficos)


@router.get("/csv")
def exportar_csv(request: Request, entidade: IntOpcional = None, periodo: str = "mes", de: str = "", ate: str = "",
                 conta: IntOpcional = None, etiqueta: IntOpcional = None, mes: str = "",
                 u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Categorias nas linhas, meses nas colunas (12 meses até ao fim do período)."""
    ctx = _contexto(request, db, u, entidade, periodo, de, ate, conta, etiqueta, mes=mes)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    if ctx:
        janela = ctx["janela"]
        meses = janela.meses()
        w.writerow(["Natureza", "Categoria"] + [rel.rotulo_mes(m) for m in meses] + ["Total"])
        for natureza, titulo in (("RECEITA", "Receitas"), ("DESPESA", "Despesas")):
            linhas = rel.estatisticas_categorias(ctx["movs_janela"], janela, natureza)
            totais = [0] * len(meses)
            for l in linhas:
                w.writerow([titulo, l["nome"]] + [f"{v / 100:.2f}".replace(".", ",") for v in l["serie"]]
                           + [f"{l['total'] / 100:.2f}".replace(".", ",")])
                totais = [a + b for a, b in zip(totais, l["serie"])]
            w.writerow([titulo, "TOTAL"] + [f"{v / 100:.2f}".replace(".", ",") for v in totais]
                       + [f"{sum(totais) / 100:.2f}".replace(".", ",")])
    nome = f"relatorio-{ctx['entidade'].nome if ctx else 'vazio'}-{date.today():%Y%m%d}.csv"
    conteudo = "﻿" + buf.getvalue()  # BOM: o Excel abre com acentos corretos
    return StreamingResponse(iter([conteudo]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{nome.encode("ascii", "ignore").decode()}"'})


@router.get("/cartoes")
def pagamentos_cartoes(request: Request, entidade: IntOpcional = None, u: Utilizador = Depends(utilizador_atual),
                       db: Session = Depends(get_db)):
    """Pagamentos de cartão: os pares encontrados (cartão ↔ conta pagadora) e os pagamentos sem par."""
    visiveis, ent, papel = entidade_escolhida(request, db, u, entidade)
    if not ent:
        return render(request, "relatorios_cartoes.html", entidade=None, visiveis=visiveis)
    return render(request, "relatorios_cartoes.html", visiveis=visiveis, entidade=ent,
                  cartoes=servicos.relatorio_pagamentos_cartao(db, ent.id),
                  pode_editar=PAPEIS[papel] >= PAPEIS["editar"])


@router.post("/cartoes/procurar")
async def procurar_pares(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Volta a procurar pares em todos os cartões da entidade (ex.: depois de atualizar a app)."""
    form = await ler_form(request)
    entidade_id = int(form["entidade"]) if str(form.get("entidade", "")).isdigit() else 0
    exigir_papel(db, u, entidade_id, "editar")
    n = sum(servicos.ligar_pagamentos_cartao(db, c) for c in rel.contas_da_entidade(db, entidade_id)
            if c.tipo == "CARTAO_CREDITO")
    auditar(db, u, "pagamentos_cartao_procurados", f"entidade {entidade_id}: {n}")
    db.commit()
    flash(request, f"Procura concluída: {n} pagamento(s) marcado(s) ou ligado(s)." if n else "Nada de novo encontrado.")
    return RedirectResponse(f"/relatorios/cartoes?entidade={entidade_id}", status_code=303)
