"""Início (página de entrada): património em gráfico e agenda das próximas tarefas."""
import json
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .. import inicio, layout_inicio, preferencias
from ..deps import IntOpcional, entidade_escolhida, get_db, render, utilizador_atual
from ..seguranca import ler_form
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
    # No Início de uma entidade pessoal, a parte nas heranças — só o 2.º cenário (ex.: 1/2 por morte da mãe).
    herancas = []
    if not ent.heranca and preferencias.ligada(request.app.state.prefs, "mostrar_minha_parte"):
        herancas = [(e, liq, e.parte_futura(liq)) for e, liq in totais if e.heranca and e.quota_futura is not None]
    proximos, depois = inicio.agenda(db, [ent], request.app.state.prefs, hoje)   # só a entidade escolhida
    grupos, passivos = inicio.detalhe(db, ent, hoje)
    total_ativos = sum(g.total for g in grupos)
    return render(request, "inicio.html", visiveis=visiveis, entidade=ent, v=v, hoje=hoje, totais=totais,
                  proximos=proximos, depois=depois, horizonte=inicio.HORIZONTE, grupos=grupos, passivos=passivos,
                  total_ativos=total_ativos, variacao=inicio.variacao_mensal(v), herancas=herancas,
                  graficos=inicio.graficos(v, hoje, grupos), layout=layout_inicio.ler(db, u.id),
                  blocos=layout_inicio.BLOCOS, larguras=layout_inicio.LARGURAS,
                  personalizado=layout_inicio.personalizado(db, u.id))


@router.post("/inicio/layout")
async def gravar_layout(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Grava a disposição dos blocos do Início (só deste utilizador). layout vazio = repor a de omissão."""
    form = await ler_form(request)
    txt = str(form.get("layout", "")).strip()
    try:
        itens = json.loads(txt) if txt else None
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "erro": "Layout inválido."}, status_code=400)
    if itens is not None and not isinstance(itens, list):
        return JSONResponse({"ok": False, "erro": "Layout inválido."}, status_code=400)
    layout_inicio.gravar(db, u.id, itens)
    db.commit()
    return JSONResponse({"ok": True, "layout": layout_inicio.ler(db, u.id)})
