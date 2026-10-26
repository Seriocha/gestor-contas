"""Regras de categorização automática: criar, editar, testar e aplicar."""
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, lazyload, selectinload

from ..deps import IntOpcional, auditar, entidades_visiveis, exigir_editor, flash, get_db, render
from ..formatos import montante_para_input, parse_montante
from ..models import Bem, MODOS_REGRA, PAPEIS, SENTIDOS, Categoria, Conta, Entidade, Etiqueta, Movimento, Regra, Utilizador
from .. import ia
from ..qualificacao import (aplicar_regras, arvore_categorias, compilar, corresponde, qualificar_manual,
                            validar_padrao)
from ..sugestoes import agrupar, padrao_de
from ..seguranca import ler_form

router = APIRouter(prefix="/regras")


def _entidades_editaveis(db: Session, u: Utilizador) -> list[int]:
    return [e.id for e, p in entidades_visiveis(db, u) if PAPEIS[p] >= PAPEIS["editar"]]


def _movimentos_editaveis(db: Session, u: Utilizador, so_nao_manuais: bool = True):
    q = (select(Movimento).join(Conta).where(Conta.entidade_id.in_(_entidades_editaveis(db, u)))
         .options(selectinload(Movimento.etiquetas)))
    if so_nao_manuais:
        q = q.where((Movimento.qualificado_por.is_(None)) | (Movimento.qualificado_por != "manual"))
    return db.scalars(q).all()


def _opcoes(db: Session, u: Utilizador) -> dict:
    return {
        "categorias": arvore_categorias(db), "modos": MODOS_REGRA, "sentidos": SENTIDOS,
        "etiquetas": db.scalars(select(Etiqueta).order_by(Etiqueta.nome)).all(),
        "contas": db.scalars(select(Conta).where(Conta.entidade_id.in_(_entidades_editaveis(db, u)))
                             .options(selectinload(Conta.entidade)).order_by(Conta.entidade_id, Conta.nome)).all(),
        "bens_por_entidade": _bens_por_entidade(db, u),
    }


def _bens_por_entidade(db: Session, u: Utilizador) -> list:
    """[(entidade, [bens])] das entidades que o utilizador edita — o «universo» de cada bem."""
    from ..models import Bem, Entidade
    r = []
    for eid in _entidades_editaveis(db, u):
        bens = db.scalars(select(Bem).where(Bem.entidade_id == eid).order_by(Bem.tipo, Bem.nome)).all()
        if bens:
            r.append((db.get(Entidade, eid), bens))
    return r


@router.get("")
def lista(request: Request, u: Utilizador = Depends(exigir_editor), db: Session = Depends(get_db)):
    regras = db.scalars(select(Regra).options(selectinload(Regra.categoria).selectinload(Categoria.pai),
                                              selectinload(Regra.conta), selectinload(Regra.etiqueta),
                                              selectinload(Regra.bem))
                        .order_by(Regra.prioridade, Regra.id)).all()
    # contagens na base (carregar os movimentos todos para os contar levava segundos)
    nas_entidades = (select(func.count()).select_from(Movimento).join(Conta)
                     .where(Conta.entidade_id.in_(_entidades_editaveis(db, u))))
    usos = {r.id: 0 for r in regras}
    for n, rid in db.execute(nas_entidades.add_columns(Movimento.regra_id).where(Movimento.regra_id.is_not(None))
                             .group_by(Movimento.regra_id)).all():
        if rid in usos:
            usos[rid] = n
    sem = db.scalar(nas_entidades.where(Movimento.categoria_id.is_(None), ~Movimento.transferencia,
                                        ~Movimento.excluido)) or 0
    return render(request, "regras/lista.html", regras=regras, usos=usos, sem_categoria=sem,
                  modos=MODOS_REGRA, sentidos=SENTIDOS)


def _grupos(db: Session, u: Utilizador) -> list:
    # Só o que está dentro da cobertura dos dados (desde o início da análise de cada entidade).
    def consulta(*cols):
        return (select(*cols).join(Conta, Conta.id == Movimento.conta_id).join(Entidade, Entidade.id == Conta.entidade_id)
                .where(Conta.entidade_id.in_(_entidades_editaveis(db, u)), ~Movimento.transferencia, ~Movimento.excluido,
                       or_(Entidade.data_inicio.is_(None), Movimento.data_mov >= Entidade.data_inicio)))
    sem = db.scalars(consulta(Movimento).where(Movimento.categoria_id.is_(None))
                     .options(selectinload(Movimento.conta), lazyload(Movimento.bem))).all()
    # o histórico (os categorizados, a maioria) só serve para sugerir: chegam as colunas, sem objetos completos
    com = db.execute(consulta(Movimento.id, Movimento.montante, Movimento.descricao_norm, Movimento.descricao_original,
                              Movimento.categoria_id).where(Movimento.categoria_id.is_not(None))).all()
    grupos = agrupar(sem, com)
    ia.aplicar_cache(db, grupos)
    return grupos


@router.get("/por-classificar")
def por_classificar(request: Request, todos: int = 0, u: Utilizador = Depends(exigir_editor),
                    db: Session = Depends(get_db)):
    """Movimentos sem categoria agrupados pelo padrão da descrição, com sugestão e regra num clique."""
    grupos = _grupos(db, u)
    n_mov = sum(g.n for g in grupos)
    mostrar = grupos if todos else grupos[:40]
    cobertos = sum(g.n for g in mostrar)
    return render(request, "regras/por_classificar.html", grupos=mostrar, n_grupos=len(grupos), n_mov=n_mov,
                  cobertos=cobertos, todos=todos, categorias=arvore_categorias(db),
                  ia_disponivel=ia.disponivel(), ia_modelo=ia.modelo(),
                  por_perguntar=sum(1 for g in grupos if not g.pessoal and g.comerciante is None and not g.origem))


@router.post("/por-classificar/ia")
async def pedir_sugestoes_ia(request: Request, u: Utilizador = Depends(exigir_editor), db: Session = Depends(get_db)):
    await ler_form(request)
    try:
        n = ia.sugerir(db, [g for g in _grupos(db, u) if g.origem != "histórico"])
    except ia.ErroIA as e:
        db.rollback()
        flash(request, f"Sugestões da IA indisponíveis: {e}", "erro")
        return RedirectResponse("/regras/por-classificar", status_code=303)
    auditar(db, u, "sugestoes_ia", f"{n} padrões ({ia.modelo()})")
    db.commit()
    flash(request, f"A IA sugeriu categoria para {n} padrão(ões). Confirma cada uma antes de aplicar."
          if n else "Não havia padrões novos para perguntar à IA.")
    return RedirectResponse("/regras/por-classificar", status_code=303)


@router.post("/por-classificar/aplicar")
async def aplicar_grupo(request: Request, u: Utilizador = Depends(exigir_editor), db: Session = Depends(get_db)):
    """Classifica os movimentos de um grupo e, se pedido, cria a regra para os futuros."""
    form = await ler_form(request)
    padrao = str(form.get("padrao", "")).strip().upper()[:200]
    original = str(form.get("original", "")).strip()
    sentido = str(form.get("sentido", "")) if form.get("sentido") in ("SAIDA", "ENTRADA") else None
    acao = str(form.get("categoria_id", ""))
    erro = validar_padrao(padrao, "contem")
    cat = db.get(Categoria, int(acao)) if acao.isdigit() else None
    if erro or not (cat or acao in ("T", "X")):
        flash(request, erro or "Escolhe a categoria (ou transferência / excluir).", "erro")
        return RedirectResponse("/regras/por-classificar", status_code=303)
    # Todos os movimentos sem categoria (no mesmo sentido) cujo texto contém o padrão — não só os do
    # grupo de onde veio: «ROYAL VERSAILLES» apanha também «PASTELARIA ROYAL VERSAILLES».
    rx = compilar(padrao, "contem")
    movs = [m for g in _grupos(db, u) if g.sentido == sentido for m in g.movimentos
            if rx.search((m.descricao_norm or m.descricao_original or "").upper())]
    if not movs:
        flash(request, f"Nenhum movimento sem categoria contém «{padrao}» ({original}).", "erro")
        return RedirectResponse("/regras/por-classificar", status_code=303)
    regra = None
    if form.get("criar_regra") == "1":
        regra = Regra(padrao=padrao, modo="contem", sentido=sentido, categoria_id=cat.id if cat else None,
                      marcar_transferencia=acao == "T", excluir=acao == "X", ativa=True, prioridade=100)
        db.add(regra)
        db.flush()
        aplicar_regras(db, movs, [regra])
    else:
        for m in movs:
            qualificar_manual(db, m, categoria_id=cat.id if cat else ..., transferencia=True if acao == "T" else ...,
                              excluido=True if acao == "X" else ...)
    oque = cat.nome_completo if cat else ("transferência" if acao == "T" else "excluído")
    auditar(db, u, "classificar_grupo", f"{padrao} → {oque}: {len(movs)} movimentos" + (" + regra" if regra else ""))
    db.commit()
    flash(request, f"«{padrao}»: {len(movs)} movimento(s) → {oque}." +
          (" Regra criada para os próximos." if regra else ""))
    return RedirectResponse("/regras/por-classificar", status_code=303)


@router.post("/aplicar")
async def aplicar_todas(request: Request, u: Utilizador = Depends(exigir_editor), db: Session = Depends(get_db)):
    """Reaplica todas as regras ativas aos movimentos não qualificados à mão."""
    await ler_form(request)
    n = aplicar_regras(db, _movimentos_editaveis(db, u))
    auditar(db, u, "regras_aplicadas", f"{n} movimentos")
    db.commit()
    flash(request, f"Regras aplicadas: {n} movimentos qualificados. Os qualificados à mão não foram alterados.")
    return RedirectResponse("/regras", status_code=303)



def _ler(form, db: Session) -> tuple[dict, list[str]]:
    erros = []
    padrao = str(form.get("padrao", "")).strip()[:200]
    modo = str(form.get("modo", "contem"))
    if modo not in MODOS_REGRA:
        modo = "contem"
    erro = validar_padrao(padrao, modo)
    if erro:
        erros.append(erro)
    sentido = str(form.get("sentido", "")) or None
    if sentido not in (None, "ENTRADA", "SAIDA"):
        sentido = None

    def montante(campo):
        txt = str(form.get(campo, "")).strip()
        if not txt:
            return None
        try:
            return abs(parse_montante(txt))
        except ValueError as e:
            erros.append(str(e))
            return None

    def fk(campo, modelo):
        v = str(form.get(campo, ""))
        return db.get(modelo, int(v)).id if v.isdigit() and db.get(modelo, int(v)) else None

    dados = {
        "padrao": padrao, "modo": modo, "sentido": sentido,
        "montante_min": montante("montante_min"), "montante_max": montante("montante_max"),
        "conta_id": fk("conta_id", Conta), "categoria_id": fk("categoria_id", Categoria),
        "etiqueta_id": fk("etiqueta_id", Etiqueta), "bem_id": fk("bem_id", Bem),
        "marcar_transferencia": form.get("marcar_transferencia") == "1", "excluir": form.get("excluir") == "1",
        "ativa": form.get("ativa") == "1",
        "prioridade": int(form["prioridade"]) if str(form.get("prioridade", "")).isdigit() else 100,
    }
    if not (dados["categoria_id"] or dados["etiqueta_id"] or dados["marcar_transferencia"] or dados["excluir"]
            or dados["bem_id"]):
        erros.append("Escolhe pelo menos uma ação (categoria, etiqueta, bem, transferência ou excluir).")
    if dados["bem_id"] and dados["conta_id"]:
        bem, conta = db.get(Bem, dados["bem_id"]), db.get(Conta, dados["conta_id"])
        if bem.entidade_id != conta.entidade_id:
            erros.append(f"A conta «{conta.nome}» não é da entidade de «{bem.nome}» — a regra nunca corresponderia.")
    return dados, erros


def _exemplos(db: Session, u: Utilizador, regra: Regra) -> tuple[int, list[Movimento]]:
    if not regra.padrao:
        return 0, []
    try:
        compilar(regra.padrao, regra.modo)
    except Exception:
        return 0, []
    movs = [m for m in _movimentos_editaveis(db, u, so_nao_manuais=False) if corresponde(regra, m)]
    movs.sort(key=lambda m: m.data_mov, reverse=True)
    return len(movs), movs[:15]


def _valores(r: Regra) -> dict:
    return {"padrao": r.padrao, "modo": r.modo, "sentido": r.sentido or "", "conta_id": r.conta_id,
            "categoria_id": r.categoria_id, "etiqueta_id": r.etiqueta_id, "bem_id": r.bem_id,
            "montante_min": montante_para_input(r.montante_min), "montante_max": montante_para_input(r.montante_max),
            "marcar_transferencia": r.marcar_transferencia, "excluir": r.excluir, "ativa": r.ativa,
            "prioridade": r.prioridade}


@router.get("/analise")
def analise(request: Request, u: Utilizador = Depends(exigir_editor), db: Session = Depends(get_db)):
    """Otimizar regras: redundantes, sem uso e agregáveis, pela simulação sobre os movimentos."""
    from .. import analise_regras
    from ..qualificacao import regras_ativas
    regras = regras_ativas(db)
    # só as colunas que a simulação usa (carregar milhares de objetos Movimento levava segundos)
    movs = (select(Movimento.id, Movimento.conta_id, Conta.entidade_id, Movimento.montante, Movimento.descricao_norm,
                   Movimento.descricao_original).join(Conta, Conta.id == Movimento.conta_id)
            .where(Conta.entidade_id.in_(_entidades_editaveis(db, u))))
    return render(request, "regras/analise.html", a=analise_regras.analisar(regras, db.execute(movs).all()),
                  modos=MODOS_REGRA, sentidos=SENTIDOS)


@router.post("/analise/agregar")
async def analise_agregar(request: Request, u: Utilizador = Depends(exigir_editor), db: Session = Depends(get_db)):
    from .. import analise_regras
    form = await ler_form(request)
    regras = [r for r in (db.get(Regra, int(x)) for x in form.getlist("ids") if str(x).isdigit()) if r]
    if len(regras) < 2 or len({(analise_regras.condicoes(r), analise_regras.acoes(r)) for r in regras}) != 1 \
            or any(r.modo == "regex" for r in regras):
        flash(request, "Estas regras não se podem agregar (condições ou ações diferentes).", "erro")
        return RedirectResponse("/regras/analise", status_code=303)
    if len(analise_regras.agregar_padrao(regras)) > 200:
        flash(request, "O padrão agregado ficaria demasiado longo (máx. 200 caracteres).", "erro")
        return RedirectResponse("/regras/analise", status_code=303)
    fica = analise_regras.agregar(db, regras)
    auditar(db, u, "regras_agregadas", f"{len(regras)} → {fica.id}: {fica.padrao}")
    db.commit()
    flash(request, f"{len(regras)} regras agregadas numa: «{fica.padrao}».")
    return RedirectResponse("/regras/analise", status_code=303)


@router.post("/analise/apagar")
async def analise_apagar(request: Request, u: Utilizador = Depends(exigir_editor), db: Session = Depends(get_db)):
    form = await ler_form(request)
    regras = [r for r in (db.get(Regra, int(x)) for x in form.getlist("ids") if str(x).isdigit()) if r]
    for r in regras:
        auditar(db, u, "regra_apagada", f"{r.id}: {r.padrao} (otimizar regras)")
        db.delete(r)
    db.commit()
    flash(request, f"{len(regras)} regra{'s' if len(regras) != 1 else ''} apagada{'s' if len(regras) != 1 else ''}. "
                   "Os movimentos já qualificados mantêm a categoria." if regras else "Nenhuma regra marcada.")
    return RedirectResponse("/regras/analise", status_code=303)


@router.get("/nova")
def nova_form(request: Request, mov: IntOpcional = None, u: Utilizador = Depends(exigir_editor),
              db: Session = Depends(get_db)):
    v = {"modo": "contem", "prioridade": 50, "ativa": True}
    m = db.get(Movimento, mov) if mov else None
    if m and m.conta.entidade_id in _entidades_editaveis(db, u):
        # Sugestão: a descrição sem números (referências mudam de mês para mês).
        v["padrao"] = re.sub(r"\s+", " ", re.sub(r"[\d/.-]{3,}", " ", m.descricao_norm)).strip()[:60]
        v["sentido"] = "SAIDA" if m.montante < 0 else "ENTRADA"
        v["categoria_id"] = m.categoria_id
    regra = Regra(**{k: v.get(k) for k in ("padrao", "modo")}, sentido=v.get("sentido"))
    n, exemplos = _exemplos(db, u, regra) if v.get("padrao") else (0, [])
    return render(request, "regras/form.html", regra=None, v=v, n=n, exemplos=exemplos, **_opcoes(db, u))


@router.post("/nova")
async def nova(request: Request, u: Utilizador = Depends(exigir_editor), db: Session = Depends(get_db)):
    return await _guardar(request, u, db, None)


@router.get("/{regra_id}")
def editar_form(request: Request, regra_id: int, u: Utilizador = Depends(exigir_editor),
                db: Session = Depends(get_db)):
    r = db.get(Regra, regra_id) or _404()
    n, exemplos = _exemplos(db, u, r)
    return render(request, "regras/form.html", regra=r, v=_valores(r), n=n, exemplos=exemplos, **_opcoes(db, u))


@router.post("/{regra_id}")
async def editar(request: Request, regra_id: int, u: Utilizador = Depends(exigir_editor),
                 db: Session = Depends(get_db)):
    return await _guardar(request, u, db, db.get(Regra, regra_id) or _404())


async def _guardar(request: Request, u: Utilizador, db: Session, r: Regra | None):
    form = await ler_form(request)
    dados, erros = _ler(form, db)
    teste = Regra(**dados)
    teste.bem = db.get(Bem, dados["bem_id"]) if dados["bem_id"] else None
    if erros or form.get("testar"):
        v = {**dados, "montante_min": str(form.get("montante_min", "")), "montante_max": str(form.get("montante_max", "")),
             "sentido": dados["sentido"] or ""}
        n, exemplos = _exemplos(db, u, teste) if not any("padrão" in e or "regular" in e for e in erros) else (0, [])
        return render(request, "regras/form.html", status_code=400 if erros else 200, regra=r, v=v, erros=erros,
                      n=n, exemplos=exemplos, **_opcoes(db, u))
    nova = r is None
    if nova:
        r = Regra()
        db.add(r)
    for k, val in dados.items():
        setattr(r, k, val)
    db.flush()
    n = 0
    if form.get("aplicar") == "1":
        n = aplicar_regras(db, [m for m in _movimentos_editaveis(db, u) if corresponde(r, m)], [r])
    auditar(db, u, "regra_criada" if nova else "regra_editada", f"{r.id}: {r.padrao}")
    db.commit()
    flash(request, f"Regra guardada" + (f" e aplicada a {n} movimentos." if n else "."))
    return RedirectResponse("/regras", status_code=303)


@router.post("/{regra_id}/apagar")
async def apagar(request: Request, regra_id: int, u: Utilizador = Depends(exigir_editor),
                 db: Session = Depends(get_db)):
    r = db.get(Regra, regra_id) or _404()
    await ler_form(request)
    auditar(db, u, "regra_apagada", f"{r.id}: {r.padrao}")
    db.delete(r)
    db.commit()
    flash(request, "Regra apagada. Os movimentos já categorizados mantêm a categoria.")
    return RedirectResponse("/regras", status_code=303)


def _404():
    raise HTTPException(status_code=404, detail="Não encontrado.")
