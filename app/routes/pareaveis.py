"""Ecrã «Movimentos pareáveis»: sugestões de débito ↔ crédito do mesmo valor em
contas diferentes, para ligar (e classificar) ou ignorar."""
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import pareaveis, servicos
from ..deps import IntOpcional, auditar, entidade_escolhida, exigir_papel, flash, get_db, render, utilizador_atual
from ..formatos import parse_montante
from ..models import PAPEIS, Categoria, Conta, Movimento, ParIgnorado, Utilizador
from ..qualificacao import arvore_categorias, qualificar_manual
from ..seguranca import ler_form

router = APIRouter(prefix="/movimentos-pares")


@router.get("")
def lista(request: Request, entidade: IntOpcional = None, dias: IntOpcional = 3, conta: IntOpcional = None,
          vmin: str = "", anteriores: IntOpcional = 0, u: Utilizador = Depends(utilizador_atual),
          db: Session = Depends(get_db)):
    visiveis, ent, papel = entidade_escolhida(request, db, u, entidade)
    if not ent:
        return render(request, "movimentos/pares.html", entidade=None, visiveis=visiveis)
    dias = dias if dias in pareaveis.DIAS else 3
    try:
        valor_min = abs(parse_montante(vmin)) if vmin.strip() else None
    except ValueError:
        valor_min = None
    desde = None if anteriores else ent.data_inicio
    pares, total = pareaveis.candidatos(db, ent.id, dias, desde=desde, conta_id=conta or None, valor_min=valor_min)
    f = {"entidade": ent.id, "dias": dias, "conta": conta or "", "vmin": vmin, "anteriores": 1 if anteriores else ""}
    return render(request, "movimentos/pares.html", visiveis=visiveis, entidade=ent, pares=pares, total=total,
                  f=f, voltar="/movimentos-pares?" + urlencode({k: v for k, v in f.items() if v != ""}),
                  opcoes_dias=pareaveis.DIAS, corte=ent.data_inicio,
                  contas=db.scalars(select(Conta).where(Conta.entidade_id == ent.id).order_by(Conta.nome)).all(),
                  categorias=arvore_categorias(db), pode_editar=PAPEIS[papel] >= PAPEIS["editar"])


def _pares_do_form(db: Session, u: Utilizador, form, unicos: bool = True) -> list[tuple[Movimento, Movimento]]:
    """Os pares marcados — ou só o da linha, quando se usa o botão dessa linha («so»)."""
    out, usados = [], set()
    chaves = [form["so"]] if form.get("so") else form.getlist("pares")
    for chave in chaves:
        d_id, _, c_id = str(chave).partition("-")
        if not (d_id.isdigit() and c_id.isdigit()):
            continue
        d, c = db.get(Movimento, int(d_id)), db.get(Movimento, int(c_id))
        if not d or not c or (unicos and (d.id in usados or c.id in usados)):
            continue   # ao ligar, dois pares com o mesmo movimento: só o primeiro conta
        if d.conta.entidade_id != c.conta.entidade_id or d.montante >= 0 or c.montante <= 0:
            raise HTTPException(status_code=400, detail="Par inválido.")
        exigir_papel(db, u, d.conta.entidade_id, "editar")
        out.append((d, c))
        usados |= {d.id, c.id}
    return out


def _voltar(form) -> str:
    v = str(form.get("voltar", ""))
    return v if v.startswith("/movimentos-pares") else "/movimentos-pares"


@router.post("/ligar")
async def ligar(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    pares = _pares_do_form(db, u, form)
    como = str(form.get("como", "T"))
    cat = db.get(Categoria, int(como)) if como.isdigit() else None
    if not pares or not (cat or como in ("T", "X")):
        flash(request, "Escolhe pelo menos um par e como classificar.", "erro")
        return RedirectResponse(_voltar(form), status_code=303)
    for d, c in pares:
        servicos.ligar_manual(db, d, c, transferencia=como == "T")
        for m in (d, c):
            if cat:
                qualificar_manual(db, m, categoria_id=cat.id)
            elif como == "X":
                qualificar_manual(db, m, excluido=True)
    oque = "transferência" if como == "T" else cat.nome_completo if cat else "excluídos dos relatórios"
    auditar(db, u, "pares_ligados", f"{len(pares)} pares ({oque}): " + ", ".join(f"{d.id}↔{c.id}" for d, c in pares))
    db.commit()
    flash(request, f"{len(pares)} par(es) ligado(s) e classificado(s): {oque}.")
    return RedirectResponse(_voltar(form), status_code=303)


@router.post("/ignorar")
async def ignorar(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    pares = _pares_do_form(db, u, form, unicos=False)
    for d, c in pares:
        if not db.scalar(select(ParIgnorado.id).where(ParIgnorado.debito_id == d.id, ParIgnorado.credito_id == c.id)):
            db.add(ParIgnorado(debito_id=d.id, credito_id=c.id))
    auditar(db, u, "pares_ignorados", ", ".join(f"{d.id}↔{c.id}" for d, c in pares))
    db.commit()
    flash(request, f"{len(pares)} sugestão(ões) ignorada(s): não voltam a aparecer.")
    return RedirectResponse(_voltar(form), status_code=303)
