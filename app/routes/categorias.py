"""Categorias (2 níveis) e etiquetas. Partilhadas entre entidades."""
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import auditar, exigir_editor, flash, get_db, render
from ..models import CORES, NATUREZAS, Categoria, Etiqueta, Movimento, Regra, Utilizador, movimento_etiqueta
from ..qualificacao import arvore_categorias
from ..seguranca import ler_form

router = APIRouter()


def _contagens(db: Session) -> dict[int, int]:
    return dict(db.execute(select(Movimento.categoria_id, func.count()).where(Movimento.categoria_id.is_not(None))
                           .group_by(Movimento.categoria_id)).all())


@router.get("/categorias")
def lista(request: Request, u: Utilizador = Depends(exigir_editor), db: Session = Depends(get_db)):
    n_etiquetas = dict(db.execute(select(movimento_etiqueta.c.etiqueta_id, func.count())
                                  .group_by(movimento_etiqueta.c.etiqueta_id)).all())
    arvore, contagens = arvore_categorias(db), _contagens(db)
    totais = {r.id: contagens.get(r.id, 0) + sum(contagens.get(f.id, 0) for f in r.filhas) for r in arvore}
    return render(request, "categorias/lista.html", arvore=arvore, contagens=contagens, totais=totais,
                  naturezas=NATUREZAS, cores=CORES,
                  etiquetas=db.scalars(select(Etiqueta).order_by(Etiqueta.nome)).all(), n_etiquetas=n_etiquetas)


def _ler(form, db: Session, cat: Categoria | None) -> tuple[dict, str | None]:
    nome = str(form.get("nome", "")).strip()[:80]
    pai_id = int(form["pai_id"]) if str(form.get("pai_id", "")).isdigit() else None
    natureza = str(form.get("natureza", "DESPESA"))
    cor = str(form.get("cor", "")).strip()
    if not nome:
        return {}, "O nome é obrigatório."
    pai = db.get(Categoria, pai_id) if pai_id else None
    if pai_id and (not pai or pai.pai_id is not None or (cat and pai.id == cat.id)):
        return {}, "A categoria-mãe tem de ser uma categoria de topo."
    if cat and pai and cat.filhas:
        return {}, "Uma categoria com subcategorias não pode passar a subcategoria."
    if pai:
        natureza = pai.natureza  # as subcategorias herdam a natureza
    if natureza not in NATUREZAS:
        return {}, "Natureza inválida."
    if cor and not re.fullmatch(r"#[0-9a-fA-F]{6}", cor):
        return {}, "Cor inválida."
    repetida = select(Categoria).where(func.lower(Categoria.nome) == nome.lower(),
                                       Categoria.id != (cat.id if cat else 0))
    repetida = repetida.where(Categoria.pai_id == pai.id) if pai else repetida.where(Categoria.pai_id.is_(None))
    if db.scalar(repetida):
        return {}, "Já existe uma categoria com esse nome."
    return {"nome": nome, "pai_id": pai.id if pai else None, "natureza": natureza,
            "cor": (cor or None) if not pai else None}, None


@router.post("/categorias")
async def criar(request: Request, u: Utilizador = Depends(exigir_editor), db: Session = Depends(get_db)):
    form = await ler_form(request)
    dados, erro = _ler(form, db, None)
    if erro:
        flash(request, erro, "erro")
    else:
        c = Categoria(**dados, ordem=(db.scalar(select(func.max(Categoria.ordem))) or 0) + 1)
        db.add(c)
        db.flush()
        auditar(db, u, "categoria_criada", c.nome_completo)
        db.commit()
        flash(request, f"Categoria «{c.nome_completo}» criada.")
    return RedirectResponse("/categorias", status_code=303)


@router.post("/categorias/{cat_id}")
async def editar(request: Request, cat_id: int, u: Utilizador = Depends(exigir_editor),
                 db: Session = Depends(get_db)):
    c = db.get(Categoria, cat_id) or _404()
    form = await ler_form(request)
    dados, erro = _ler(form, db, c)
    if erro:
        flash(request, erro, "erro")
        return RedirectResponse("/categorias", status_code=303)
    for k, v in dados.items():
        setattr(c, k, v)
    for f in c.filhas:
        f.natureza = c.natureza
    auditar(db, u, "categoria_editada", c.nome_completo)
    db.commit()
    flash(request, f"Categoria «{c.nome_completo}» atualizada.")
    return RedirectResponse("/categorias", status_code=303)


@router.post("/categorias/{cat_id}/apagar")
async def apagar(request: Request, cat_id: int, u: Utilizador = Depends(exigir_editor),
                 db: Session = Depends(get_db)):
    """Os movimentos da categoria (e das subcategorias) passam para a
    categoria de destino, ou ficam sem categoria."""
    c = db.get(Categoria, cat_id) or _404()
    form = await ler_form(request)
    destino = db.get(Categoria, int(form["destino"])) if str(form.get("destino", "")).isdigit() else None
    ids = [c.id] + [f.id for f in c.filhas]
    if destino and destino.id in ids:
        destino = None
    n = 0
    for m in db.scalars(select(Movimento).where(Movimento.categoria_id.in_(ids))):
        m.categoria_id = destino.id if destino else None
        n += 1
    for r in db.scalars(select(Regra).where(Regra.categoria_id.in_(ids))):
        if destino:
            r.categoria_id = destino.id
    nome = c.nome_completo
    db.delete(c)
    auditar(db, u, "categoria_apagada", f"{nome}: {n} movimentos → {destino.nome_completo if destino else 'sem categoria'}")
    db.commit()
    flash(request, f"Categoria «{nome}» apagada; {n} movimentos " +
          (f"passaram para «{destino.nome_completo}»." if destino else "ficaram sem categoria."))
    return RedirectResponse("/categorias", status_code=303)


@router.post("/etiquetas")
async def criar_etiqueta(request: Request, u: Utilizador = Depends(exigir_editor), db: Session = Depends(get_db)):
    form = await ler_form(request)
    nome = str(form.get("nome", "")).strip()[:50]
    if not nome or db.scalar(select(Etiqueta).where(func.lower(Etiqueta.nome) == nome.lower())):
        flash(request, "Nome em falta ou etiqueta já existente.", "erro")
    else:
        db.add(Etiqueta(nome=nome))
        db.commit()
        flash(request, f"Etiqueta «{nome}» criada.")
    return RedirectResponse("/categorias#etiquetas", status_code=303)


@router.post("/etiquetas/{et_id}/apagar")
async def apagar_etiqueta(request: Request, et_id: int, u: Utilizador = Depends(exigir_editor),
                          db: Session = Depends(get_db)):
    et = db.get(Etiqueta, et_id) or _404()
    await ler_form(request)
    db.delete(et)
    db.commit()
    flash(request, f"Etiqueta «{et.nome}» apagada.")
    return RedirectResponse("/categorias#etiquetas", status_code=303)


def _404():
    raise HTTPException(status_code=404, detail="Não encontrado.")
