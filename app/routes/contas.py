"""Gestão de contas (criar, editar, arquivar). Criar/editar exige o papel
'admin' na entidade da conta; ver exige qualquer papel."""
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..deps import IntOpcional, auditar, entidades_visiveis, exigir_papel, flash, get_db, render, utilizador_atual
from ..formatos import montante_para_input, mascarar_iban, parse_data, parse_montante
from ..seguranca import ler_form
from ..models import PAPEIS, TIPOS_CONTA, Conta, Entidade, Instituicao, Permissao, Utilizador
from ..servicos import saldo_conta

router = APIRouter(prefix="/contas")


def _opcoes(db: Session, entidade_id: int, conta_id: int | None = None) -> dict:
    """Listas para os selects do formulário, restritas à entidade da conta."""
    pagadoras = db.scalars(
        select(Conta).where(Conta.entidade_id == entidade_id, Conta.tipo.in_(("ORDEM", "CARTEIRA_DIGITAL")),
                            Conta.ativa, Conta.id != (conta_id or 0)).order_by(Conta.nome)
    ).all()
    titulares = db.scalars(
        select(Utilizador).join(Permissao).where(Permissao.entidade_id == entidade_id, Utilizador.ativo)
        .order_by(Utilizador.nome)
    ).all()
    return {
        "instituicoes": db.scalars(select(Instituicao).where(Instituicao.ativa).order_by(Instituicao.nome)).all(),
        "tipos": TIPOS_CONTA,
        "pagadoras": pagadoras,
        "possiveis_titulares": titulares,
    }


def _ler_campos(form, db: Session, entidade_id: int, conta_id: int | None) -> tuple[dict, list[str]]:
    erros = []
    v = {k: str(form.get(k, "")).strip() for k in (
        "nome", "tipo", "moeda", "iban", "instituicao_id", "saldo_inicial", "data_saldo_inicial",
        "conta_pagamento_id", "dia_fecho", "dia_pagamento", "notas")}
    dados: dict = {"nome": v["nome"], "tipo": v["tipo"], "moeda": (v["moeda"] or "EUR").upper(),
                   "iban_mascarado": mascarar_iban(v["iban"]), "notas": v["notas"] or None}
    if not dados["nome"]:
        erros.append("O nome é obrigatório.")
    if dados["tipo"] not in TIPOS_CONTA:
        erros.append("Tipo de conta inválido.")
    if not re.fullmatch(r"[A-Z]{3}", dados["moeda"]):
        erros.append("Moeda inválida (usa o código de 3 letras, ex.: EUR).")
    inst = db.get(Instituicao, int(v["instituicao_id"])) if v["instituicao_id"].isdigit() else None
    if not inst:
        erros.append("Escolhe a instituição.")
    dados["instituicao_id"] = inst.id if inst else None
    try:
        dados["saldo_inicial"] = parse_montante(v["saldo_inicial"])
    except ValueError as e:
        erros.append(str(e))
    try:
        dados["data_saldo_inicial"] = parse_data(v["data_saldo_inicial"])
    except ValueError as e:
        erros.append(str(e))

    dados["conta_pagamento_id"] = dados["dia_fecho"] = dados["dia_pagamento"] = None
    if dados["tipo"] == "CARTAO_CREDITO":
        if v["conta_pagamento_id"]:
            pag = db.get(Conta, int(v["conta_pagamento_id"])) if v["conta_pagamento_id"].isdigit() else None
            if not pag or pag.entidade_id != entidade_id or pag.id == conta_id:
                erros.append("A conta de pagamento tem de ser uma conta da mesma entidade.")
            else:
                dados["conta_pagamento_id"] = pag.id
        for campo, rotulo in (("dia_fecho", "Dia de fecho"), ("dia_pagamento", "Dia de pagamento")):
            if v[campo]:
                if not v[campo].isdigit() or not 1 <= int(v[campo]) <= 31:
                    erros.append(f"{rotulo} tem de estar entre 1 e 31.")
                else:
                    dados[campo] = int(v[campo])

    titulares_ids = {int(x) for x in form.getlist("titulares") if str(x).isdigit()}
    titulares = db.scalars(
        select(Utilizador).join(Permissao)
        .where(Permissao.entidade_id == entidade_id, Utilizador.id.in_(titulares_ids))
    ).all() if titulares_ids else []
    dados["titulares"] = list(titulares)

    existente = db.scalar(select(Conta).where(Conta.entidade_id == entidade_id, Conta.nome == dados["nome"],
                                              Conta.id != (conta_id or 0)))
    if existente:
        erros.append("Já existe uma conta com esse nome nesta entidade.")
    return dados, erros


def _valores_form(c: Conta) -> dict:
    return {
        "nome": c.nome, "tipo": c.tipo, "moeda": c.moeda, "iban": c.iban_mascarado or "",
        "instituicao_id": c.instituicao_id, "saldo_inicial": montante_para_input(c.saldo_inicial),
        "data_saldo_inicial": c.data_saldo_inicial.isoformat() if c.data_saldo_inicial else "",
        "conta_pagamento_id": c.conta_pagamento_id, "dia_fecho": c.dia_fecho or "",
        "dia_pagamento": c.dia_pagamento or "", "notas": c.notas or "",
        "titulares": {t.id for t in c.titulares},
    }


@router.get("")
def lista(request: Request, arquivadas: IntOpcional = 0, u: Utilizador = Depends(utilizador_atual),
          db: Session = Depends(get_db)):
    blocos = []
    for entidade, papel in entidades_visiveis(db, u):
        q = select(Conta).where(Conta.entidade_id == entidade.id).options(
            selectinload(Conta.instituicao), selectinload(Conta.titulares), selectinload(Conta.conta_pagamento))
        if not arquivadas:
            q = q.where(Conta.ativa)
        contas = sorted(db.scalars(q).all(), key=lambda c: (list(TIPOS_CONTA).index(c.tipo), c.nome.lower()))
        blocos.append({"entidade": entidade, "papel": papel, "pode_gerir": PAPEIS[papel] >= PAPEIS["admin"],
                       "contas": [(c, saldo_conta(db, c)) for c in contas]})
    return render(request, "contas/lista.html", blocos=blocos, arquivadas=arquivadas)


@router.get("/nova")
def nova_form(request: Request, entidade: int, u: Utilizador = Depends(utilizador_atual),
              db: Session = Depends(get_db)):
    exigir_papel(db, u, entidade, "admin")
    ent = db.get(Entidade, entidade)
    valores = {"moeda": "EUR", "tipo": "ORDEM", "titulares": {u.id} if ent.nome != "Mãe" else set()}
    return render(request, "contas/form.html", entidade=ent, conta=None, v=valores, **_opcoes(db, entidade))


@router.post("/nova")
async def nova(request: Request, entidade: int, u: Utilizador = Depends(utilizador_atual),
               db: Session = Depends(get_db)):
    exigir_papel(db, u, entidade, "admin")
    form = await ler_form(request)
    ent = db.get(Entidade, entidade)
    dados, erros = _ler_campos(form, db, entidade, None)
    if erros:
        v = {k: str(form.get(k, "")) for k in form.keys()}
        v["titulares"] = {t.id for t in dados["titulares"]}
        return render(request, "contas/form.html", status_code=400, entidade=ent, conta=None, v=v,
                      erros=erros, **_opcoes(db, entidade))
    conta = Conta(entidade_id=entidade, **dados)
    db.add(conta)
    db.flush()
    auditar(db, u, "conta_criada", f"{conta.id}: {ent.nome} / {conta.nome}")
    db.commit()
    flash(request, f"Conta «{conta.nome}» criada.")
    return RedirectResponse("/contas", status_code=303)


def _conta_ou_404(db: Session, conta_id: int) -> Conta:
    c = db.get(Conta, conta_id)
    if not c:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    return c


@router.get("/{conta_id}/editar")
def editar_form(request: Request, conta_id: int, u: Utilizador = Depends(utilizador_atual),
                db: Session = Depends(get_db)):
    c = _conta_ou_404(db, conta_id)
    exigir_papel(db, u, c.entidade_id, "admin")
    return render(request, "contas/form.html", entidade=c.entidade, conta=c, v=_valores_form(c),
                  **_opcoes(db, c.entidade_id, c.id))


@router.post("/{conta_id}/editar")
async def editar(request: Request, conta_id: int, u: Utilizador = Depends(utilizador_atual),
                 db: Session = Depends(get_db)):
    c = _conta_ou_404(db, conta_id)
    exigir_papel(db, u, c.entidade_id, "admin")
    form = await ler_form(request)
    dados, erros = _ler_campos(form, db, c.entidade_id, c.id)
    if erros:
        v = {k: str(form.get(k, "")) for k in form.keys()}
        v["titulares"] = {t.id for t in dados["titulares"]}
        return render(request, "contas/form.html", status_code=400, entidade=c.entidade, conta=c, v=v,
                      erros=erros, **_opcoes(db, c.entidade_id, c.id))
    for k, val in dados.items():
        setattr(c, k, val)
    auditar(db, u, "conta_editada", f"{c.id}: {c.nome}")
    db.commit()
    flash(request, f"Conta «{c.nome}» atualizada.")
    return RedirectResponse("/contas", status_code=303)


@router.post("/{conta_id}/arquivar")
async def arquivar(request: Request, conta_id: int, u: Utilizador = Depends(utilizador_atual),
                   db: Session = Depends(get_db)):
    c = _conta_ou_404(db, conta_id)
    exigir_papel(db, u, c.entidade_id, "admin")
    await ler_form(request)
    c.ativa = not c.ativa
    auditar(db, u, "conta_arquivada" if not c.ativa else "conta_reativada", f"{c.id}: {c.nome}")
    db.commit()
    flash(request, f"Conta «{c.nome}» {'arquivada' if not c.ativa else 'reativada'}.")
    return RedirectResponse("/contas", status_code=303)

