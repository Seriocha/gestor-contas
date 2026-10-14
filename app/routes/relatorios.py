"""Relatórios: receitas e despesas por categoria, tendências, património, CSV."""
import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import relatorios as rel
from ..deps import entidade_escolhida, get_db, render, utilizador_atual
from ..formatos import parse_data
from ..models import Conta, Etiqueta, Movimento, Utilizador

router = APIRouter(prefix="/relatorios")


def data_referencia(db: Session, entidade_id: int) -> date:
    """Último dia com movimentos (os extratos chegam com atraso); hoje se não houver."""
    ultimo = db.scalar(select(func.max(Movimento.data_mov)).join(Conta).where(Conta.entidade_id == entidade_id))
    return min(ultimo, date.today()) if ultimo else date.today()


def _contexto(request: Request, db: Session, u: Utilizador, entidade, periodo, de, ate, conta, etiqueta):
    visiveis, ent, papel = entidade_escolhida(request, db, u, entidade)
    if not ent:
        return None
    try:
        d_de, d_ate = parse_data(de), parse_data(ate)
    except ValueError:
        d_de = d_ate = None
    ref = data_referencia(db, ent.id)
    p = rel.periodo(periodo if periodo in rel.PRESETS else "mes", ref, d_de, d_ate)
    ant = p.anterior
    contas = rel.contas_da_entidade(db, ent.id)
    conta_ids = [conta] if conta and conta in {c.id for c in contas} else None
    movs = rel.movimentos_reportaveis(db, ent.id, p.ini, p.fim, conta_ids, etiqueta or None)
    movs_ant = rel.movimentos_reportaveis(db, ent.id, ant.ini, ant.fim, conta_ids, etiqueta or None)
    r, r_ant = rel.resumir(movs), rel.resumir(movs_ant)
    # Tendências: sempre 12 meses até ao fim do período, para haver contexto.
    janela = rel.Periodo("ano", rel.somar_meses(p.fim.replace(day=1), -11), p.fim, "12 meses")
    movs_janela = rel.movimentos_reportaveis(db, ent.id, janela.ini, janela.fim, conta_ids, etiqueta or None)
    return {
        "visiveis": visiveis, "entidade": ent, "p": p, "ant": ant, "presets": rel.PRESETS, "contas": contas,
        "etiquetas": db.scalars(select(Etiqueta).order_by(Etiqueta.nome)).all(),
        "f": {"periodo": p.chave, "de": de, "ate": ate, "conta": conta or "", "etiqueta": etiqueta or ""},
        "r": r, "r_ant": r_ant, "janela": janela, "movs_janela": movs_janela,
        "despesas": rel.linhas_ordenadas(r.por_categoria["DESPESA"], r_ant.por_categoria["DESPESA"]),
        "receitas": rel.linhas_ordenadas(r.por_categoria["RECEITA"], r_ant.por_categoria["RECEITA"]),
    }


@router.get("")
def relatorios(request: Request, entidade: int | None = None, periodo: str = "mes", de: str = "", ate: str = "",
               conta: int = 0, etiqueta: int = 0, u: Utilizador = Depends(utilizador_atual),
               db: Session = Depends(get_db)):
    ctx = _contexto(request, db, u, entidade, periodo, de, ate, conta, etiqueta)
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
        "patrimonio": {"rotulos": [rel.rotulo_mes(d) for d in datas], "liquido": [x["liquido"] / 100 for x in pat["serie"]]},
        "despesas": [{"nome": l.nome, "valor": l.total / 100, "cor": l.cor} for l in ctx["despesas"] if l.total > 0],
    }
    return render(request, "relatorios.html", **ctx, mensal=mensal, estat=estat[:12], pat=pat, pat_ant=pat_ant,
                  graficos=graficos)


@router.get("/csv")
def exportar_csv(request: Request, entidade: int | None = None, periodo: str = "mes", de: str = "", ate: str = "",
                 conta: int = 0, etiqueta: int = 0, u: Utilizador = Depends(utilizador_atual),
                 db: Session = Depends(get_db)):
    """Categorias nas linhas, meses nas colunas (12 meses até ao fim do período)."""
    ctx = _contexto(request, db, u, entidade, periodo, de, ate, conta, etiqueta)
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
