"""Página do empréstimo: resumo, quadro previsional, revisão da taxa,
acerto do capital e processamento das prestações."""
import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import emprestimos as emp
from ..deps import auditar, exigir_papel, flash, get_db, render, utilizador_atual
from ..formatos import fmt_montante, parse_data, parse_montante
from ..models import PAPEIS, TIPOS_TAXA, Conta, Utilizador
from ..seguranca import ler_form

router = APIRouter(prefix="/contas/{conta_id}/emprestimo")


def _emprestimo(db: Session, u: Utilizador, conta_id: int, minimo: str) -> tuple[Conta, str]:
    c = db.get(Conta, conta_id)
    if not c or c.tipo != "EMPRESTIMO":
        raise HTTPException(status_code=404, detail="Não encontrado.")
    papel = exigir_papel(db, u, c.entidade_id, minimo)
    if not c.emprestimo:
        raise HTTPException(status_code=404, detail="Faltam os dados do empréstimo: edita a conta.")
    return c, papel


@router.get("")
def pagina(request: Request, conta_id: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    c, papel = _emprestimo(db, u, conta_id, "ver")
    hoje = date.today()
    r = emp.resumo(db, c, hoje)
    previsao = emp.quadro(r.capital, c.emprestimo.taxa_e5, r.n_restantes, r.ultima_data or hoje, c.dia_pagamento,
                          c.emprestimo.encargos, meses=12, primeiro_numero=(r.ultima_n or 0) + 1)
    historico: dict = {}
    for m in reversed(emp.movimentos(db, c)):  # agrupa P/J/E de cada prestação, mais recente primeiro
        t = emp._tipo(m)
        if not t:
            continue
        achado = re.search(r"PRESTACAO (\d+)", m.descricao_norm or "")
        numero = achado.group(1) if achado else "?"
        h = historico.setdefault(numero, {"numero": numero, "data": m.data_valor or m.data_mov, "prestacao": 0,
                                          "juros": 0, "encargos": 0, "par_id": None, "em_divida": None,
                                          "estimada": (m.hash_dedup or "").startswith(emp.ESTIMATIVA)})
        if t == "P":
            h["prestacao"], h["par_id"] = m.montante, m.par_id
        elif t == "J":
            h["juros"] += -m.montante
        else:
            h["encargos"] += -m.montante
        if h["em_divida"] is None and m.saldo_apos is not None:
            h["em_divida"] = -m.saldo_apos  # 1.º visto (o último movimento da prestação)
    for h in historico.values():
        h["capital"] = h["prestacao"] - h["juros"] - h["encargos"]
    return render(request, "emprestimo.html", conta=c, e=c.emprestimo, r=r, previsao=previsao,
                  historico=list(historico.values())[:24], tipos_taxa=TIPOS_TAXA, fmt_taxa=emp.fmt_taxa,
                  taxa_input=emp.taxa_para_input(c.emprestimo.taxa_e5), hoje=hoje,
                  pode_editar=PAPEIS[papel] >= PAPEIS["editar"])


@router.post("/taxa")
async def atualizar_taxa(request: Request, conta_id: int, u: Utilizador = Depends(utilizador_atual),
                         db: Session = Depends(get_db)):
    """Revisão da taxa (ex.: Euribor a cada 6/12 meses): vale para as prestações seguintes."""
    c, _ = _emprestimo(db, u, conta_id, "editar")
    form = await ler_form(request)
    try:
        taxa = emp.parse_taxa(str(form.get("taxa", "")))
    except ValueError as e:
        flash(request, str(e), "erro")
        return RedirectResponse(f"/contas/{c.id}/emprestimo", status_code=303)
    if taxa is None:
        flash(request, "Indica a nova TAN.", "erro")
    else:
        antiga = c.emprestimo.taxa_e5
        c.emprestimo.taxa_e5 = taxa
        auditar(db, u, "emprestimo_taxa", f"{c.id}: {emp.fmt_taxa(antiga)} → {emp.fmt_taxa(taxa)}")
        db.commit()
        flash(request, f"TAN atualizada de {emp.fmt_taxa(antiga)} para {emp.fmt_taxa(taxa)}. "
                       "Vale para as prestações processadas a partir de agora.")
    return RedirectResponse(f"/contas/{c.id}/emprestimo", status_code=303)


@router.post("/acertar")
async def acertar(request: Request, conta_id: int, u: Utilizador = Depends(utilizador_atual),
                  db: Session = Depends(get_db)):
    c, _ = _emprestimo(db, u, conta_id, "editar")
    form = await ler_form(request)
    try:
        capital = abs(parse_montante(str(form.get("capital", ""))))
        data = parse_data(str(form.get("data", ""))) or date.today()
    except ValueError as e:
        flash(request, str(e), "erro")
        return RedirectResponse(f"/contas/{c.id}/emprestimo", status_code=303)
    dif = emp.acertar_capital(db, c, capital, data)
    auditar(db, u, "emprestimo_acerto", f"{c.id}: capital {capital} em {data} (diferença {dif})")
    db.commit()
    flash(request, f"Capital em dívida acertado para {fmt_montante(capital)} (diferença de {fmt_montante(dif)}, "
                   "fora dos relatórios)." if dif else "O capital em dívida já coincidia; nada a acertar.")
    return RedirectResponse(f"/contas/{c.id}/emprestimo", status_code=303)


@router.post("/processar")
async def processar(request: Request, conta_id: int, u: Utilizador = Depends(utilizador_atual),
                    db: Session = Depends(get_db)):
    """Procura de novo débitos de prestação (ex.: depois de mudar o padrão ou a conta de débito)."""
    c, _ = _emprestimo(db, u, conta_id, "editar")
    await ler_form(request)
    r = emp.processar(db, c)
    auditar(db, u, "emprestimo_processar", f"{c.id}: {r}")
    db.commit()
    flash(request, f"Ligadas ao débito: {r['ligadas']} · estimadas: {r['estimadas']} · estimativas substituídas: "
                   f"{r['substituidas']}." if any(r.values()) else "Nada de novo: prestações e débitos já estão tratados.")
    return RedirectResponse(f"/contas/{c.id}/emprestimo", status_code=303)
