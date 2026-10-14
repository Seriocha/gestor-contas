"""Painel: património líquido e evolução, ativos/passivos, fluxo de caixa
(sankey) e despesas por categoria — inspirado no painel do Sure."""
from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import relatorios as rel
from ..backup import ultimo_backup
from ..deps import IntOpcional, entidade_escolhida, get_db, render, utilizador_atual
from ..models import TIPOS_CONTA, Conta, Movimento, Utilizador
from .relatorios import data_referencia

router = APIRouter()
PERIODOS_PAINEL = {"mes": "Este mês", "trimestre": "Trimestre", "6meses": "6 meses", "ano": "12 meses"}


@router.get("/")
def painel(request: Request, entidade: IntOpcional = None, periodo: str = "mes",
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
    grupos_ativos, grupos_passivos = defaultdict(list), defaultdict(list)
    for c, s in pat["contas"]:
        (grupos_passivos if c.tipo in rel.PASSIVOS else grupos_ativos)[TIPOS_CONTA[c.tipo]].append((c, s))

    movs = rel.movimentos_reportaveis(db, ent.id, p.ini, p.fim)
    r = rel.resumir(movs)
    despesas = [l for l in rel.linhas_ordenadas(r.por_categoria["DESPESA"]) if l.total > 0]
    n_sem = db.scalar(select(func.count()).select_from(Movimento).join(Conta).where(
        Conta.entidade_id == ent.id, Movimento.categoria_id.is_(None), ~Movimento.transferencia,
        ~Movimento.excluido))
    graficos = {
        "patrimonio": {"rotulos": [d.strftime("%d-%m-%Y") if d == ref else rel.rotulo_mes(d) for d in meses],
                       "liquido": [x["liquido"] / 100 for x in pat["serie"]]},
        "sankey": rel.sankey(r),
        "despesas": [{"nome": l.nome, "valor": l.total / 100, "cor": l.cor} for l in despesas],
    }
    return render(request, "painel.html", visiveis=visiveis, entidade=ent, p=p, periodos=PERIODOS_PAINEL, ref=ref,
                  atual=atual, ha_30=ha_30, grupos_ativos=dict(grupos_ativos), grupos_passivos=dict(grupos_passivos),
                  r=r, despesas=despesas, n_sem=n_sem, graficos=graficos, ultimo_backup=backup)
