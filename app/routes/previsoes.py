"""Receitas previstas (ordenados, cartões refeição): criar, editar, apagar e
ver o estado no mês corrente."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import previsoes
from .. import relatorios as rel
from ..deps import IntOpcional, auditar, entidade_escolhida, exigir_papel, flash, get_db, render, utilizador_atual
from ..formatos import montante_para_input, parse_montante
from ..models import PAPEIS, Categoria, Conta, ReceitaPrevista, Utilizador
from ..qualificacao import validar_padrao
from ..seguranca import ler_form
from .relatorios import data_referencia

router = APIRouter(prefix="/previsoes")

PADRAO_SALARIO = "VENCIMENTO|ORDENADO|SALARIO"


def _categorias_receita(db: Session) -> list[Categoria]:
    previsoes.categoria_refeicao(db)
    cats = db.scalars(select(Categoria).where(Categoria.natureza == "RECEITA")
                      .options(selectinload(Categoria.pai))).all()
    return sorted(cats, key=lambda c: c.nome_completo.lower())


def _contas(db: Session, entidade_id: int) -> list[Conta]:
    return db.scalars(select(Conta).where(Conta.entidade_id == entidade_id, Conta.ativa)
                      .options(selectinload(Conta.instituicao)).order_by(Conta.nome)).all()


def _carregar(db: Session, u: Utilizador, prev_id: int) -> ReceitaPrevista:
    p = db.get(ReceitaPrevista, prev_id)
    if not p:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    exigir_papel(db, u, p.conta.entidade_id, "editar")
    return p


@router.get("")
def lista(request: Request, entidade: IntOpcional = None, u: Utilizador = Depends(utilizador_atual),
          db: Session = Depends(get_db)):
    visiveis, ent, papel = entidade_escolhida(request, db, u, entidade)
    if not ent:
        return render(request, "previsoes/lista.html", visiveis=visiveis, entidade=None)
    ref = data_referencia(db, ent.id)
    todas = previsoes.da_entidade(db, ent.id)
    estados = {e.prev.id: e for e in previsoes.estado_mes(db, ent.id, ref, [p for p in todas if p.ativa and p.conta.ativa])}
    return render(request, "previsoes/lista.html", visiveis=visiveis, entidade=ent, todas=todas, estados=estados,
                  ref=ref, mes=rel.periodo("mes", ref).rotulo, pode_editar=PAPEIS[papel] >= PAPEIS["editar"])


def _form(request, db, ent, v, prev=None, erros=None, status_code=200):
    return render(request, "previsoes/form.html", status_code=status_code, entidade=ent, prev=prev, v=v,
                  erros=erros or [], contas=_contas(db, ent.id), categorias=_categorias_receita(db))


@router.get("/nova")
def nova(request: Request, entidade: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    exigir_papel(db, u, entidade, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, entidade)
    salario = db.scalar(select(Categoria).where(Categoria.nome == "Salário", Categoria.pai_id.is_(None)))
    return _form(request, db, ent, {"descricao": "Ordenado", "dia": 23, "padrao": PADRAO_SALARIO,
                                    "categoria_id": salario.id if salario else None, "ativa": True})


def _ler(form, db: Session, entidade_id: int) -> tuple[dict, list[str]]:
    erros = []
    v = {k: str(form.get(k, "")).strip() for k in ("descricao", "dia", "valor", "padrao", "conta_id", "categoria_id")}
    v["ativa"] = form.get("ativa") == "1"
    dados = {"descricao": v["descricao"][:100], "padrao": v["padrao"][:200] or None, "ativa": v["ativa"]}
    if not dados["descricao"]:
        erros.append("A descrição é obrigatória.")
    conta = db.get(Conta, int(v["conta_id"])) if v["conta_id"].isdigit() else None
    if not conta or conta.entidade_id != entidade_id:
        erros.append("Escolhe a conta onde a receita entra.")
    else:
        dados["conta_id"] = conta.id
    if v["dia"].isdigit() and 1 <= int(v["dia"]) <= 31:
        dados["dia"] = int(v["dia"])
    else:
        erros.append("O dia tem de estar entre 1 e 31.")
    dados["valor"] = None
    if v["valor"]:
        try:
            dados["valor"] = abs(parse_montante(v["valor"])) or None
        except ValueError as e:
            erros.append(str(e))
    cat = db.get(Categoria, int(v["categoria_id"])) if v["categoria_id"].isdigit() else None
    dados["categoria_id"] = cat.id if cat else None
    if dados["padrao"]:
        erro = validar_padrao(dados["padrao"], "contem")
        if erro:
            erros.append(erro)
    if not dados["valor"] and not dados["padrao"] and not dados["categoria_id"]:
        erros.append("Indica o valor, ou um padrão/categoria para o calcular a partir dos créditos já recebidos.")
    return {**v, "categoria_id": dados["categoria_id"]}, erros if erros else dados


@router.post("/nova")
async def criar(request: Request, entidade: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    exigir_papel(db, u, entidade, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, entidade)
    v, res = _ler(await ler_form(request), db, ent.id)
    if isinstance(res, list):
        return _form(request, db, ent, v, erros=res, status_code=400)
    p = ReceitaPrevista(**res)
    db.add(p)
    auditar(db, u, "previsao_criada", p.descricao)
    db.commit()
    flash(request, f"Receita prevista «{p.descricao}» criada.")
    return RedirectResponse(f"/previsoes?entidade={ent.id}", status_code=303)


@router.get("/{prev_id}")
def editar(request: Request, prev_id: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    p = _carregar(db, u, prev_id)
    _, ent, _ = entidade_escolhida(request, db, u, p.conta.entidade_id)
    v = {"descricao": p.descricao, "dia": p.dia, "valor": montante_para_input(p.valor) if p.valor else "",
         "padrao": p.padrao or "", "conta_id": p.conta_id, "categoria_id": p.categoria_id, "ativa": p.ativa}
    return _form(request, db, ent, v, prev=p)


@router.post("/{prev_id}")
async def guardar(request: Request, prev_id: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    p = _carregar(db, u, prev_id)
    _, ent, _ = entidade_escolhida(request, db, u, p.conta.entidade_id)
    v, res = _ler(await ler_form(request), db, ent.id)
    if isinstance(res, list):
        return _form(request, db, ent, v, prev=p, erros=res, status_code=400)
    for k, val in res.items():
        setattr(p, k, val)
    auditar(db, u, "previsao_editada", p.descricao)
    db.commit()
    flash(request, f"Receita prevista «{p.descricao}» guardada.")
    return RedirectResponse(f"/previsoes?entidade={ent.id}", status_code=303)


@router.post("/{prev_id}/apagar")
async def apagar(request: Request, prev_id: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    await ler_form(request)
    p = _carregar(db, u, prev_id)
    ent_id, nome = p.conta.entidade_id, p.descricao
    db.delete(p)
    auditar(db, u, "previsao_apagada", nome)
    db.commit()
    flash(request, f"Receita prevista «{nome}» apagada.")
    return RedirectResponse(f"/previsoes?entidade={ent_id}", status_code=303)
