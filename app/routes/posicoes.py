"""Posições (fundos) das contas de investimento e de pensões: criar, editar, atualizar cotação, apagar, liquidez."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import posicoes as pz
from ..deps import auditar, exigir_papel, flash, get_db, utilizador_atual
from ..formatos import parse_data, parse_montante
from ..models import Conta, Posicao, Utilizador
from ..seguranca import ler_form

router = APIRouter(prefix="/contas/{conta_id}/posicoes")


def _conta(db: Session, u: Utilizador, conta_id: int) -> Conta:
    c = db.get(Conta, conta_id)
    if not c or not pz.tem_posicoes(c):
        raise HTTPException(status_code=404, detail="Não encontrado.")
    exigir_papel(db, u, c.entidade_id, "editar")
    return c


def _num(txt: str, rotulo: str, erros: list) -> float | None:
    t = (txt or "").strip().replace(" ", "")
    if not t:
        return None
    try:
        v = float(t.replace(".", "").replace(",", ".")) if "," in t else float(t)
        if v < 0:
            raise ValueError
        return v
    except ValueError:
        erros.append(f"{rotulo} inválida.")
        return None


def _voltar(c: Conta) -> RedirectResponse:
    return RedirectResponse(f"/contas/{c.id}/movimentos#posicoes", status_code=303)


@router.post("")
async def guardar(request: Request, conta_id: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Nova posição, ou edição (com id). Só a cotação e a data: atualização rápida."""
    form = await ler_form(request)
    c = _conta(db, u, conta_id)
    erros = []
    pid = str(form.get("id", ""))
    p = next((x for x in c.posicoes if str(x.id) == pid), None) if pid.isdigit() else None
    so_cotacao = form.get("so_cotacao") == "1" and p is not None
    nome = str(form.get("nome", p.nome if p else "")).strip()[:120]
    if not nome:
        erros.append("Indica o nome do fundo.")
    unidades = p.unidades if so_cotacao else _num(str(form.get("unidades", "")), "Quantidade de unidades", erros)
    cotacao = _num(str(form.get("cotacao", "")), "Cotação", erros)
    try:
        data = parse_data(str(form.get("data_cotacao", ""))) if form.get("data_cotacao") else date.today()
        if data > date.today():
            raise ValueError
    except ValueError:
        data = date.today()
        erros.append("Data da cotação inválida.")
    if not so_cotacao:
        try:
            aquisicao = abs(parse_montante(str(form.get("valor_aquisicao")))) if form.get("valor_aquisicao") else None
        except ValueError:
            aquisicao = None
            erros.append("Valor de aquisição inválido.")
        risco = str(form.get("risco", ""))
        risco = int(risco) if risco.isdigit() and 1 <= int(risco) <= 7 else None
    if cotacao is None:
        erros.append("Indica a cotação (valor de cada unidade).")
    if erros:
        flash(request, " ".join(erros), "erro")
        return _voltar(c)
    if so_cotacao:
        p.cotacao, p.data_cotacao = cotacao, data
    else:
        dados = dict(nome=nome, gestora=str(form.get("gestora", "")).strip()[:80] or None,
                     categoria=str(form.get("categoria", "")).strip()[:60] or None,
                     isin=str(form.get("isin", "")).strip().upper()[:12] or None, risco=risco,
                     unidades=unidades or 0, cotacao=cotacao, data_cotacao=data, valor_aquisicao=aquisicao)
        if p:
            for k, v in dados.items():
                setattr(p, k, v)
        else:
            p = Posicao(conta_id=c.id, **dados)
            c.posicoes.append(p)
    db.flush()
    pz.sincronizar(db, c, data, u.id)
    auditar(db, u, "posicao", f"{c.nome}: {p.nome} {p.unidades} × {p.cotacao}")
    db.commit()
    flash(request, f"«{p.nome}»: {p.valor / 100:,.2f} €".replace(",", " ").replace(".", ",") + " — saldo da conta acertado.")
    return _voltar(c)


@router.post("/{pid}/apagar")
async def apagar(request: Request, conta_id: int, pid: int, u: Utilizador = Depends(utilizador_atual),
                 db: Session = Depends(get_db)):
    await ler_form(request)
    c = _conta(db, u, conta_id)
    p = next((x for x in c.posicoes if x.id == pid), None)
    if not p:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    c.posicoes.remove(p)
    db.flush()
    pz.sincronizar(db, c, date.today(), u.id)
    db.commit()
    flash(request, f"Posição «{p.nome}» apagada (saldo da conta acertado).")
    return _voltar(c)


@router.post("/liquidez")
async def liquidez(request: Request, conta_id: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    c = _conta(db, u, conta_id)
    txt = str(form.get("liquidez", "")).strip()
    try:
        c.liquidez = parse_montante(txt) if txt else None
    except ValueError as e:
        flash(request, f"Liquidez inválida: {e}", "erro")
        return _voltar(c)
    db.flush()
    pz.sincronizar(db, c, date.today(), u.id)
    db.commit()
    flash(request, "Liquidez guardada — saldo da conta acertado.")
    return _voltar(c)
