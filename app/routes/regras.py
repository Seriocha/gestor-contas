"""Regras de categorização automática: criar, editar, testar e aplicar."""
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..deps import auditar, entidades_visiveis, exigir_editor, flash, get_db, render
from ..formatos import montante_para_input, parse_montante
from ..models import MODOS_REGRA, PAPEIS, SENTIDOS, Categoria, Conta, Etiqueta, Movimento, Regra, Utilizador
from ..qualificacao import aplicar_regras, arvore_categorias, compilar, corresponde, validar_padrao
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
    }


@router.get("")
def lista(request: Request, u: Utilizador = Depends(exigir_editor), db: Session = Depends(get_db)):
    regras = db.scalars(select(Regra).options(selectinload(Regra.categoria).selectinload(Categoria.pai),
                                              selectinload(Regra.conta), selectinload(Regra.etiqueta))
                        .order_by(Regra.prioridade, Regra.id)).all()
    movs = _movimentos_editaveis(db, u, so_nao_manuais=False)
    usos = {r.id: 0 for r in regras}
    for m in movs:
        if m.regra_id in usos:
            usos[m.regra_id] += 1
    sem = sum(1 for m in movs if m.categoria_id is None and not m.transferencia and not m.excluido)
    return render(request, "regras/lista.html", regras=regras, usos=usos, sem_categoria=sem,
                  modos=MODOS_REGRA, sentidos=SENTIDOS)


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
        "etiqueta_id": fk("etiqueta_id", Etiqueta),
        "marcar_transferencia": form.get("marcar_transferencia") == "1", "excluir": form.get("excluir") == "1",
        "ativa": form.get("ativa") == "1",
        "prioridade": int(form["prioridade"]) if str(form.get("prioridade", "")).isdigit() else 100,
    }
    if not (dados["categoria_id"] or dados["etiqueta_id"] or dados["marcar_transferencia"] or dados["excluir"]):
        erros.append("Escolhe pelo menos uma ação (categoria, etiqueta, transferência ou excluir).")
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
            "categoria_id": r.categoria_id, "etiqueta_id": r.etiqueta_id,
            "montante_min": montante_para_input(r.montante_min), "montante_max": montante_para_input(r.montante_max),
            "marcar_transferencia": r.marcar_transferencia, "excluir": r.excluir, "ativa": r.ativa,
            "prioridade": r.prioridade}


@router.get("/nova")
def nova_form(request: Request, mov: int | None = None, u: Utilizador = Depends(exigir_editor),
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
