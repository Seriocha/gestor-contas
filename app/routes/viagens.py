"""Viagens: registo, sugestão e associação dos gastos, e modelos de viagem."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import viagens as vg
from ..deps import (IntOpcional, auditar, entidade_escolhida, exigir_editor, exigir_papel, flash, get_db, render,
                    utilizador_atual)
from ..formatos import montante_para_input, parse_data, parse_montante
from ..models import (MOTIVOS_VIAGEM, PAPEIS, TIPOS_CONTA, Conta, Etiqueta, ModeloPadrao, ModeloViagem, Movimento,
                      Utilizador, Viagem)
from ..qualificacao import arvore_categorias
from ..seguranca import ler_form

router = APIRouter(prefix="/viagens")


def _viagem(db: Session, u: Utilizador, vid: int, minimo: str = "ver") -> tuple[Viagem, str]:
    v = db.get(Viagem, vid)
    if not v:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    return v, exigir_papel(db, u, v.entidade_id, minimo)


def _modelos(db: Session) -> list[ModeloViagem]:
    vg.modelo_omissao(db)
    return db.scalars(select(ModeloViagem).order_by(ModeloViagem.nome)).all()


# ------------------------------------------------------------------ lista
@router.get("")
def lista(request: Request, entidade: IntOpcional = None, u: Utilizador = Depends(utilizador_atual),
          db: Session = Depends(get_db)):
    visiveis, ent, papel = entidade_escolhida(request, db, u, entidade)
    if not ent:
        return render(request, "viagens/lista.html", entidade=None, visiveis=visiveis)
    viagens = db.scalars(select(Viagem).where(Viagem.entidade_id == ent.id)
                         .options(selectinload(Viagem.modelo)).order_by(Viagem.data_ini.desc())).all()
    modelos = _modelos(db)
    db.commit()
    return render(request, "viagens/lista.html", visiveis=visiveis, entidade=ent,
                  viagens=[(v, vg.resumo(db, v)) for v in viagens], modelos=modelos,
                  pode_editar=PAPEIS[papel] >= PAPEIS["editar"])


# ------------------------------------------------------------------ criar / editar
def _form(request, db, ent, v, viagem=None, erros=None, status_code=200):
    return render(request, "viagens/form.html", status_code=status_code, entidade=ent, viagem=viagem, v=v,
                  erros=erros or [], modelos=_modelos(db), motivos=MOTIVOS_VIAGEM)


def _ler(form) -> tuple[dict, list[str], dict]:
    campos = ("nome", "destino", "pais", "data_ini", "data_fim", "pessoas", "motivo", "orcamento", "moeda",
              "notas", "modelo_id")
    v = {k: str(form.get(k, "")).strip() for k in campos}
    erros, dados = [], {}
    if not v["nome"]:
        erros.append("Dá um nome à viagem (ex.: «Maurícias 2026»).")
    try:
        ini, fim = parse_data(v["data_ini"]), parse_data(v["data_fim"])
    except ValueError:
        ini = fim = None
    if not ini or not fim or fim < ini:
        erros.append("Indica as datas de partida e de regresso (o regresso não pode ser antes da partida).")
    orc = None
    if v["orcamento"]:
        try:
            orc = abs(parse_montante(v["orcamento"]))
        except ValueError as e:
            erros.append(str(e))
    dados = {"nome": v["nome"][:100], "destino": v["destino"][:120] or None, "pais": v["pais"][:80] or None,
             "data_ini": ini, "data_fim": fim, "pessoas": int(v["pessoas"]) if v["pessoas"].isdigit() and int(v["pessoas"]) > 0 else 1,
             "motivo": v["motivo"] if v["motivo"] in MOTIVOS_VIAGEM else "lazer", "orcamento": orc,
             "moeda": v["moeda"].upper()[:3] or None, "notas": v["notas"] or None,
             "modelo_id": int(v["modelo_id"]) if v["modelo_id"].isdigit() else None}
    return v, erros, dados


@router.get("/nova")
def nova(request: Request, entidade: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    exigir_papel(db, u, entidade, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, entidade)
    modelo = vg.modelo_omissao(db)
    db.commit()
    return _form(request, db, ent, {"pessoas": "2", "motivo": "lazer", "modelo_id": str(modelo.id)})


@router.post("/nova")
async def criar(request: Request, entidade: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    exigir_papel(db, u, entidade, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, entidade)
    v, erros, dados = _ler(await ler_form(request))
    if erros:
        return _form(request, db, ent, v, erros=erros, status_code=400)
    viagem = Viagem(entidade_id=ent.id, **dados)
    if not viagem.modelo_id or not db.get(ModeloViagem, viagem.modelo_id):
        viagem.modelo_id = vg.modelo_omissao(db).id
    db.add(viagem)
    db.flush()
    vg.etiqueta_da(db, viagem)
    auditar(db, u, "viagem_criada", viagem.nome)
    db.commit()
    flash(request, f"Viagem «{viagem.nome}» registada. Revê as sugestões de gastos abaixo.")
    return RedirectResponse(f"/viagens/{viagem.id}", status_code=303)


@router.get("/{vid}/editar")
def editar(request: Request, vid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    viagem, _ = _viagem(db, u, vid, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, viagem.entidade_id)
    v = {"nome": viagem.nome, "destino": viagem.destino or "", "pais": viagem.pais or "",
         "data_ini": viagem.data_ini.isoformat(), "data_fim": viagem.data_fim.isoformat(), "pessoas": str(viagem.pessoas),
         "motivo": viagem.motivo, "orcamento": montante_para_input(viagem.orcamento) if viagem.orcamento else "",
         "moeda": viagem.moeda or "", "notas": viagem.notas or "", "modelo_id": str(viagem.modelo_id or "")}
    return _form(request, db, ent, v, viagem=viagem)


@router.post("/{vid}/editar")
async def guardar(request: Request, vid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    viagem, _ = _viagem(db, u, vid, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, viagem.entidade_id)
    v, erros, dados = _ler(await ler_form(request))
    if erros:
        return _form(request, db, ent, v, viagem=viagem, erros=erros, status_code=400)
    for k, val in dados.items():
        setattr(viagem, k, val)
    auditar(db, u, "viagem_editada", viagem.nome)
    db.commit()
    flash(request, "Viagem guardada.")
    return RedirectResponse(f"/viagens/{viagem.id}", status_code=303)


@router.post("/{vid}/apagar")
async def apagar(request: Request, vid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    await ler_form(request)
    viagem, _ = _viagem(db, u, vid, "editar")
    ent_id, nome, et_id = viagem.entidade_id, viagem.nome, viagem.etiqueta_id
    db.delete(viagem)
    if et_id and db.get(Etiqueta, et_id):
        db.delete(db.get(Etiqueta, et_id))   # os movimentos ficam; só perdem a marca da viagem
    auditar(db, u, "viagem_apagada", nome)
    db.commit()
    flash(request, f"Viagem «{nome}» apagada (os movimentos ficam, sem a marca da viagem).")
    return RedirectResponse(f"/viagens?entidade={ent_id}", status_code=303)


# ------------------------------------------------------------------ ficha da viagem e sugestões
@router.get("/{vid}")
def ficha(request: Request, vid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    viagem, papel = _viagem(db, u, vid)
    visiveis, ent, _ = entidade_escolhida(request, db, u, viagem.entidade_id)
    if not viagem.modelo_id:
        viagem.modelo_id = vg.modelo_omissao(db).id
    vg.etiqueta_da(db, viagem)
    db.commit()
    return render(request, "viagens/detalhe.html", visiveis=visiveis, entidade=ent, viagem=viagem,
                  r=vg.resumo(db, viagem), sugestoes=vg.candidatos(db, viagem), motivos=MOTIVOS_VIAGEM,
                  pode_editar=PAPEIS[papel] >= PAPEIS["editar"])


async def _movimentos_do_form(request, db, viagem) -> tuple[list[Movimento], dict]:
    form = await ler_form(request)
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    movs = db.scalars(select(Movimento).join(Conta).where(Movimento.id.in_(ids), Conta.entidade_id == viagem.entidade_id)
                      .options(selectinload(Movimento.etiquetas))).all() if ids else []
    return movs, form


@router.post("/{vid}/associar")
async def associar(request: Request, vid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    viagem, _ = _viagem(db, u, vid, "editar")
    movs, form = await _movimentos_do_form(request, db, viagem)
    acao = str(form.get("acao", "associar"))
    aprender = form.get("aprender") == "1"
    if not movs:
        flash(request, "Seleciona movimentos.", "erro")
    elif acao == "rejeitar":
        n = vg.rejeitar(db, viagem, movs, aprender)
        flash(request, f"{n} sugestão(ões) rejeitada(s)" + (" — o modelo aprendeu com isto." if aprender else "."))
    else:
        n = vg.associar(db, viagem, movs, aprender)
        flash(request, f"{n} movimento(s) associado(s) à viagem" + (" — o modelo aprendeu com isto." if aprender else "."))
    auditar(db, u, f"viagem_{acao}", f"{viagem.nome}: {len(movs)} movimentos")
    db.commit()
    return RedirectResponse(f"/viagens/{viagem.id}#sugestoes", status_code=303)


@router.post("/{vid}/desassociar")
async def desassociar(request: Request, vid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    viagem, _ = _viagem(db, u, vid, "editar")
    movs, _ = await _movimentos_do_form(request, db, viagem)
    n = vg.desassociar(db, viagem, movs)
    auditar(db, u, "viagem_desassociar", f"{viagem.nome}: {n} movimentos")
    db.commit()
    flash(request, f"{n} movimento(s) retirado(s) da viagem.")
    return RedirectResponse(f"/viagens/{viagem.id}", status_code=303)


# ------------------------------------------------------------------ modelos
@router.get("/modelos/{mid}")
def modelo(request: Request, mid: int, u: Utilizador = Depends(exigir_editor), db: Session = Depends(get_db)):
    m = db.get(ModeloViagem, mid)
    if not m:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    contas = db.scalars(select(Conta).options(selectinload(Conta.entidade)).order_by(Conta.entidade_id, Conta.nome)).all()
    return render(request, "viagens/modelo.html", m=m, tipos=TIPOS_CONTA, contas=contas, categorias=arvore_categorias(db),
                  sel_tipos=set(filter(None, m.tipos_conta.split(","))), sel_contas=vg._ids(m.contas),
                  sel_cats=vg._ids(m.categorias), est=vg.estatisticas_modelo(db, m),
                  valor_min=montante_para_input(m.valor_min) if m.valor_min else "",
                  padroes=sorted(m.padroes, key=lambda p: (-(p.positivos - p.negativos), p.padrao)))


@router.post("/modelos/{mid}")
async def guardar_modelo(request: Request, mid: int, u: Utilizador = Depends(exigir_editor),
                         db: Session = Depends(get_db)):
    m = db.get(ModeloViagem, mid)
    if not m:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    form = await ler_form(request)
    nome = str(form.get("nome", "")).strip()[:80]
    if nome and nome != m.nome and db.scalar(select(ModeloViagem.id).where(ModeloViagem.nome == nome)):
        flash(request, "Já existe um modelo com esse nome.", "erro")
        return RedirectResponse(f"/viagens/modelos/{m.id}", status_code=303)
    m.nome = nome or m.nome
    for campo in ("dias_antes", "dias_depois"):
        val = str(form.get(campo, "")).strip()
        if val.isdigit() and int(val) <= 365:
            setattr(m, campo, int(val))
    m.tipos_conta = ",".join(t for t in form.getlist("tipos_conta") if t in TIPOS_CONTA)
    m.contas = ",".join(str(int(x)) for x in form.getlist("contas") if str(x).isdigit())
    m.categorias = ",".join(str(int(x)) for x in form.getlist("categorias") if str(x).isdigit())
    m.incluir = "\n".join(vg._lista(str(form.get("incluir", ""))))
    m.excluir = "\n".join(vg._lista(str(form.get("excluir", ""))))
    vmin = str(form.get("valor_min", "")).strip()
    try:
        m.valor_min = abs(parse_montante(vmin)) or None if vmin else None
    except ValueError:
        flash(request, "Valor mínimo inválido.", "erro")
    auditar(db, u, "modelo_viagem", m.nome)
    db.commit()
    flash(request, f"Modelo «{m.nome}» guardado.")
    return RedirectResponse(f"/viagens/modelos/{m.id}", status_code=303)


@router.post("/modelos/{mid}/copiar")
async def copiar_modelo(request: Request, mid: int, u: Utilizador = Depends(exigir_editor),
                        db: Session = Depends(get_db)):
    await ler_form(request)
    m = db.get(ModeloViagem, mid)
    if not m:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    nome, n = f"{m.nome} (cópia)"[:80], 1
    while db.scalar(select(ModeloViagem.id).where(ModeloViagem.nome == nome)):
        n += 1
        nome = f"{m.nome} (cópia {n})"[:80]
    novo = ModeloViagem(nome=nome, dias_antes=m.dias_antes, dias_depois=m.dias_depois, tipos_conta=m.tipos_conta,
                        contas=m.contas, incluir=m.incluir, excluir=m.excluir, categorias=m.categorias,
                        valor_min=m.valor_min)
    db.add(novo)
    db.flush()
    for p in m.padroes:
        db.add(ModeloPadrao(modelo_id=novo.id, padrao=p.padrao, positivos=p.positivos, negativos=p.negativos))
    auditar(db, u, "modelo_viagem_copiado", f"{m.nome} → {nome}")
    db.commit()
    flash(request, f"Modelo copiado: «{nome}». Ajusta-o e escolhe-o na viagem.")
    return RedirectResponse(f"/viagens/modelos/{novo.id}", status_code=303)


@router.post("/modelos/{mid}/padroes/{pid}/apagar")
async def apagar_padrao(request: Request, mid: int, pid: int, u: Utilizador = Depends(exigir_editor),
                        db: Session = Depends(get_db)):
    await ler_form(request)
    p = db.get(ModeloPadrao, pid)
    if p and p.modelo_id == mid:
        db.delete(p)
        db.commit()
        flash(request, f"«{p.padrao}» esquecido pelo modelo.")
    return RedirectResponse(f"/viagens/modelos/{mid}#aprendido", status_code=303)
