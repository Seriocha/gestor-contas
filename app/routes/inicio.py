"""Início (página de entrada): património em gráfico e agenda das próximas tarefas."""
from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from .. import inicio
from ..deps import IntOpcional, entidade_escolhida, get_db, render, utilizador_atual
from ..models import Utilizador

router = APIRouter()


@router.get("/inicio")
def pagina(request: Request, entidade: IntOpcional = None, u: Utilizador = Depends(utilizador_atual),
           db: Session = Depends(get_db)):
    visiveis, ent, _ = entidade_escolhida(request, db, u, entidade)
    if not ent:
        return render(request, "inicio.html", entidade=None, visiveis=visiveis)
    hoje = date.today()
    v = inicio.visao(db, ent, hoje)
    # o património total de cada entidade (com a parte de cada herança), para o resumo no topo
    totais = []
    for e, _ in visiveis:
        ve = v if e.id == ent.id else inicio.visao(db, e, hoje)
        totais.append((e, ve.hoje["liquido"]))
    proximos, depois = inicio.agenda(db, [e for e, _ in visiveis], request.app.state.prefs, hoje)
    return render(request, "inicio.html", visiveis=visiveis, entidade=ent, v=v, hoje=hoje, totais=totais,
                  proximos=proximos, depois=depois, horizonte=inicio.HORIZONTE, graficos=inicio.graficos(v, hoje))
