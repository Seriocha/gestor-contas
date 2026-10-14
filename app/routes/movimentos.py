"""Lista de movimentos (global por entidade ou de uma conta) e qualificação:
categoria, etiquetas, transferência, excluir — um a um ou em massa."""
from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..deps import (IntOpcional, auditar, entidade_escolhida, exigir_papel, flash, get_db, papel_em, render,
                    utilizador_atual)
from ..formatos import parse_data
from ..models import PAPEIS, Categoria, Conta, Etiqueta, Movimento, Utilizador
from ..qualificacao import aplicar_regras, arvore_categorias, qualificar_manual, repor
from ..seguranca import ler_form
from ..servicos import resumo_cartao, saldo_conta

router = APIRouter()
POR_PAGINA = 200
TIPOS_FILTRO = {"": "Todos", "saidas": "Saídas", "entradas": "Entradas", "sem": "Sem categoria",
                "transferencias": "Transferências", "excluidos": "Excluídos",
                "manual": "Qualificados à mão", "regra": "Qualificados por regra"}


def _filtrar(q, *, conta_id, categoria, etiqueta, tipo, texto, d_de, d_ate):
    if conta_id:
        q = q.where(Movimento.conta_id == conta_id)
    if categoria:
        q = q.where(or_(Movimento.categoria_id == categoria,
                        Movimento.categoria_id.in_(select(Categoria.id).where(Categoria.pai_id == categoria))))
    if etiqueta:
        q = q.where(Movimento.etiquetas.any(id=etiqueta))
    if tipo == "saidas":
        q = q.where(Movimento.montante < 0)
    elif tipo == "entradas":
        q = q.where(Movimento.montante > 0)
    elif tipo == "sem":
        q = q.where(Movimento.categoria_id.is_(None), ~Movimento.transferencia, ~Movimento.excluido)
    elif tipo == "transferencias":
        q = q.where(Movimento.transferencia)
    elif tipo == "excluidos":
        q = q.where(Movimento.excluido)
    elif tipo == "manual":
        q = q.where(Movimento.qualificado_por == "manual")
    elif tipo == "regra":
        q = q.where(Movimento.qualificado_por == "regra")
    if d_de:
        q = q.where(Movimento.data_mov >= d_de)
    if d_ate:
        q = q.where(Movimento.data_mov <= d_ate)
    if texto.strip():
        termo = f"%{texto.strip()}%"
        q = q.where(or_(Movimento.descricao_original.ilike(termo), Movimento.notas.ilike(termo)))
    return q


def _listar(request: Request, db: Session, u: Utilizador, *, entidade: int | None, conta: Conta | None,
            categoria: int, etiqueta: int, tipo: str, q: str, de: str, ate: str, pagina: int):
    if conta:
        papel = exigir_papel(db, u, conta.entidade_id, "ver")
        visiveis, ent, _ = entidade_escolhida(request, db, u, conta.entidade_id)
    else:
        visiveis, ent, papel = entidade_escolhida(request, db, u, entidade)
    if not ent:
        return render(request, "movimentos/lista.html", entidade=None, visiveis=visiveis)
    erro = None
    try:
        d_de, d_ate = parse_data(de), parse_data(ate)
    except ValueError as e:
        d_de = d_ate = None
        erro = str(e)
    base = select(Movimento).join(Conta).where(Conta.entidade_id == ent.id)
    filtro = _filtrar(base, conta_id=conta.id if conta else None, categoria=categoria, etiqueta=etiqueta,
                      tipo=tipo, texto=q, d_de=d_de, d_ate=d_ate)
    sub = filtro.with_only_columns(Movimento.montante).subquery()
    total, entradas, saidas = db.execute(select(
        func.count(), func.coalesce(func.sum(func.max(sub.c.montante, 0)), 0),
        func.coalesce(func.sum(func.min(sub.c.montante, 0)), 0)).select_from(sub)).one()
    pagina = max(1, pagina or 1)
    lista = db.scalars(filtro.options(selectinload(Movimento.conta), selectinload(Movimento.etiquetas),
                                      selectinload(Movimento.categoria).selectinload(Categoria.pai))
                       .order_by(Movimento.data_mov.desc(), Movimento.ordem.desc(), Movimento.id.desc())
                       .offset((pagina - 1) * POR_PAGINA).limit(POR_PAGINA)).all()
    n_sem = db.scalar(_filtrar(base.with_only_columns(func.count()), conta_id=conta.id if conta else None,
                               categoria=0, etiqueta=0, tipo="sem", texto="", d_de=None, d_ate=None))
    filtros = {"categoria": categoria or "", "etiqueta": etiqueta or "", "tipo": tipo, "q": q, "de": de, "ate": ate}
    if not conta:
        filtros["entidade"] = ent.id
    return render(
        request, "movimentos/lista.html", visiveis=visiveis, entidade=ent, conta=conta,
        saldo=saldo_conta(db, conta) if conta else None,
        cartao=resumo_cartao(db, conta, date.today()) if conta and conta.tipo == "CARTAO_CREDITO" else None,
        contas=db.scalars(select(Conta).where(Conta.entidade_id == ent.id).order_by(Conta.nome)).all(),
        categorias=arvore_categorias(db), etiquetas=db.scalars(select(Etiqueta).order_by(Etiqueta.nome)).all(),
        tipos=TIPOS_FILTRO, movimentos=lista, total=total, entradas=entradas, saidas=saidas, n_sem=n_sem,
        pagina=pagina, paginas=max(1, -(-total // POR_PAGINA)), erro=erro, f=filtros,
        query=urlencode({k: v for k, v in filtros.items() if v}),
        pode_editar=PAPEIS[papel] >= PAPEIS["editar"])


@router.get("/movimentos")
def movimentos(request: Request, entidade: IntOpcional = None, conta: IntOpcional = None, categoria: IntOpcional = None,
               etiqueta: IntOpcional = None, tipo: str = "", q: str = "", de: str = "", ate: str = "", pagina: IntOpcional = 1,
               u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    if conta:
        return RedirectResponse(f"/contas/{conta}/movimentos?" + urlencode(
            {k: v for k, v in {"categoria": categoria or "", "etiqueta": etiqueta or "", "tipo": tipo, "q": q, "de": de,
                               "ate": ate}.items() if v}), status_code=303)
    return _listar(request, db, u, entidade=entidade, conta=None, categoria=categoria, etiqueta=etiqueta,
                   tipo=tipo if tipo in TIPOS_FILTRO else "", q=q, de=de, ate=ate, pagina=pagina)


@router.get("/contas/{conta_id}/movimentos")
def movimentos_conta(request: Request, conta_id: int, categoria: IntOpcional = None, etiqueta: IntOpcional = None, tipo: str = "",
                     q: str = "", de: str = "", ate: str = "", pagina: IntOpcional = 1,
                     u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    c = db.get(Conta, conta_id)
    if not c:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    return _listar(request, db, u, entidade=None, conta=c, categoria=categoria, etiqueta=etiqueta,
                   tipo=tipo if tipo in TIPOS_FILTRO else "", q=q, de=de, ate=ate, pagina=pagina)


def _movimento_editavel(db: Session, u: Utilizador, mov_id: int) -> Movimento:
    m = db.get(Movimento, mov_id)
    if not m:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    exigir_papel(db, u, m.conta.entidade_id, "editar")
    return m


def _int(valor) -> int | None:
    return int(valor) if str(valor or "").isdigit() else None


@router.post("/movimentos/{mov_id}/categoria")
async def mudar_categoria(request: Request, mov_id: int, u: Utilizador = Depends(utilizador_atual),
                          db: Session = Depends(get_db)):
    """Usado pelo seletor de categoria em cada linha (fetch)."""
    m = _movimento_editavel(db, u, mov_id)
    form = await ler_form(request)
    qualificar_manual(db, m, categoria_id=_int(form.get("categoria_id")))
    db.commit()
    return JSONResponse({"ok": True, "categoria": m.categoria.nome_completo if m.categoria else None})


@router.get("/movimentos/{mov_id}")
def editar_form(request: Request, mov_id: int, voltar: str = "", u: Utilizador = Depends(utilizador_atual),
                db: Session = Depends(get_db)):
    m = db.get(Movimento, mov_id)
    if not m:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    papel = exigir_papel(db, u, m.conta.entidade_id, "ver")
    return render(request, "movimentos/editar.html", m=m, categorias=arvore_categorias(db),
                  etiquetas=db.scalars(select(Etiqueta).order_by(Etiqueta.nome)).all(),
                  pode_editar=PAPEIS[papel] >= PAPEIS["editar"], voltar=_voltar_seguro(voltar))


def _voltar_seguro(voltar: str) -> str:
    return voltar if voltar.startswith("/") and not voltar.startswith("//") else "/movimentos"


@router.post("/movimentos/{mov_id}")
async def editar(request: Request, mov_id: int, u: Utilizador = Depends(utilizador_atual),
                 db: Session = Depends(get_db)):
    m = _movimento_editavel(db, u, mov_id)
    form = await ler_form(request)
    nova_etiqueta = str(form.get("nova_etiqueta", "")).strip()[:50]
    ids = [int(x) for x in form.getlist("etiquetas") if str(x).isdigit()]
    if nova_etiqueta:
        et = db.scalar(select(Etiqueta).where(func.lower(Etiqueta.nome) == nova_etiqueta.lower()))
        if not et:
            et = Etiqueta(nome=nova_etiqueta)
            db.add(et)
            db.flush()
        ids.append(et.id)
    qualificar_manual(db, m, categoria_id=_int(form.get("categoria_id")), transferencia=form.get("transferencia") == "1",
                      excluido=form.get("excluido") == "1", etiquetas_ids=ids,
                      notas=str(form.get("notas", "")).strip()[:2000])
    db.commit()
    flash(request, "Movimento atualizado.")
    return RedirectResponse(_voltar_seguro(str(form.get("voltar", ""))), status_code=303)


def _ids_do_filtro(db: Session, u: Utilizador, form) -> list[int]:
    """Todos os movimentos (todas as páginas) que correspondem ao filtro enviado."""
    conta = db.get(Conta, _int(form.get("f_conta"))) if _int(form.get("f_conta")) else None
    entidade_id = conta.entidade_id if conta else _int(form.get("f_entidade"))
    if not entidade_id:
        raise HTTPException(status_code=400, detail="Filtro sem entidade.")
    exigir_papel(db, u, entidade_id, "editar")
    try:
        d_de, d_ate = parse_data(str(form.get("f_de", ""))), parse_data(str(form.get("f_ate", "")))
    except ValueError:
        raise HTTPException(status_code=400, detail="Datas do filtro inválidas.")
    tipo = str(form.get("f_tipo", ""))
    base = select(Movimento.id).join(Conta).where(Conta.entidade_id == entidade_id)
    return list(db.scalars(_filtrar(base, conta_id=conta.id if conta else None, categoria=_int(form.get("f_categoria")),
                                    etiqueta=_int(form.get("f_etiqueta")), tipo=tipo if tipo in TIPOS_FILTRO else "",
                                    texto=str(form.get("f_q", "")), d_de=d_de, d_ate=d_ate)))


@router.post("/movimentos-lote")
async def lote(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Ação em massa sobre os movimentos selecionados ou sobre todo o filtro."""
    form = await ler_form(request)
    voltar = _voltar_seguro(str(form.get("voltar", "")))
    acao = str(form.get("acao", ""))
    todo_filtro = form.get("ambito") == "filtro"
    ids = _ids_do_filtro(db, u, form) if todo_filtro else [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    if not ids or not acao:
        flash(request, "Escolhe os movimentos e a ação." if not todo_filtro else "O filtro não tem movimentos.", "erro")
        return RedirectResponse(voltar, status_code=303)
    movs = db.scalars(select(Movimento).where(Movimento.id.in_(ids))
                      .options(selectinload(Movimento.conta), selectinload(Movimento.etiquetas))).all()
    cache: dict[int, str | None] = {}
    for m in movs:
        e = m.conta.entidade_id
        if e not in cache:
            cache[e] = papel_em(db, u, e)
        if not cache[e] or PAPEIS[cache[e]] < PAPEIS["editar"]:
            raise HTTPException(status_code=403, detail="Sem permissão para esta operação.")
    tipo, _, valor = acao.partition(":")
    extra = ""
    if tipo in ("repor_regras", "repor_limpar"):
        n = repor(db, movs, reaplicar_regras=tipo == "repor_regras")
        extra = f" Qualificação reposta; {n} voltaram a ser categorizados pelas regras." if tipo == "repor_regras"             else " Qualificação reposta: ficaram sem categoria."
    else:
        for m in movs:
            if tipo == "categoria":
                qualificar_manual(db, m, categoria_id=_int(valor))
            elif tipo == "etiqueta_mais":
                # Etiquetas não contam como qualificação manual: não bloqueiam as regras.
                et = db.get(Etiqueta, _int(valor) or 0)
                if et and et not in m.etiquetas:
                    m.etiquetas.append(et)
            elif tipo == "etiqueta_menos":
                m.etiquetas = [e for e in m.etiquetas if e.id != _int(valor)]
            elif tipo in ("transferencia", "nao_transferencia"):
                qualificar_manual(db, m, transferencia=tipo == "transferencia")
            elif tipo in ("excluir", "incluir"):
                qualificar_manual(db, m, excluido=tipo == "excluir")
            else:
                raise HTTPException(status_code=400, detail="Ação desconhecida.")
    auditar(db, u, "qualificacao_lote", f"{acao} em {len(movs)} movimentos" + (" (todo o filtro)" if todo_filtro else ""))
    db.commit()
    flash(request, f"{len(movs)} movimentos atualizados." + extra)
    return RedirectResponse(voltar, status_code=303)
