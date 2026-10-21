"""Viagens: registo, sugestão e associação dos gastos, e modelos de viagem."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
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


def _uso_modelos(db: Session) -> dict[int, int]:
    """{modelo_id: n.º de viagens (de todas as entidades) que o usam}."""
    return dict(db.execute(select(Viagem.modelo_id, func.count()).where(Viagem.modelo_id.is_not(None))
                           .group_by(Viagem.modelo_id)).all())


def _nome_livre(db: Session, nome: str, exceto: int | None = None) -> bool:
    q = select(ModeloViagem.id).where(ModeloViagem.nome == nome)
    if exceto:
        q = q.where(ModeloViagem.id != exceto)
    return not db.scalar(q)


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
    if not ent.tem("viagens"):
        return render(request, "_sem_modulo.html", visiveis=visiveis, entidade=ent, titulo="Viagens",
                      destino="/viagens", modulo="viagens")
    viagens = db.scalars(select(Viagem).where(Viagem.entidade_id == ent.id)
                         .options(selectinload(Viagem.modelo)).order_by(Viagem.data_ini.desc())).all()
    modelos = _modelos(db)
    db.commit()
    return render(request, "viagens/lista.html", visiveis=visiveis, entidade=ent,
                  viagens=[(v, vg.resumo(db, v)) for v in viagens], modelos=modelos, uso=_uso_modelos(db),
                  pode_editar=PAPEIS[papel] >= PAPEIS["editar"])


# ------------------------------------------------------------------ criar / editar
def _form(request, db, ent, v, viagem=None, erros=None, status_code=200):
    return render(request, "viagens/form.html", status_code=status_code, entidade=ent, viagem=viagem, v=v,
                  erros=erros or [], modelos=_modelos(db), motivos=MOTIVOS_VIAGEM,
                  etiquetas=vg.etiquetas_livres(db, viagem))


def _ler(form) -> tuple[dict, list[str], dict]:
    campos = ("nome", "destino", "pais", "data_ini", "data_fim", "pessoas", "motivo", "orcamento", "moeda",
              "notas", "modelo_id", "etiqueta_id")
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
             "modelo_id": int(v["modelo_id"]) if v["modelo_id"].isdigit() else None,
             "etiqueta_id": int(v["etiqueta_id"]) if v["etiqueta_id"].isdigit() else None}
    return v, erros, dados


@router.get("/nova")
def nova(request: Request, entidade: int, nome: str = "", destino: str = "", pais: str = "", data_ini: str = "",
         data_fim: str = "", motivo: str = "", u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Formulário de viagem nova; os campos podem vir preenchidos (de um período detetado em Detetar viagens)."""
    exigir_papel(db, u, entidade, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, entidade)
    modelo = vg.modelo_omissao(db)
    db.commit()
    return _form(request, db, ent, {"pessoas": "2", "motivo": motivo if motivo in MOTIVOS_VIAGEM else "lazer",
                                    "modelo_id": str(modelo.id), "nome": nome[:100], "destino": destino[:120],
                                    "pais": pais[:80], "data_ini": data_ini[:10], "data_fim": data_fim[:10]})


# ------------------------------------------------------------------ detetar períodos de viagem
def _periodo(db: Session, entidade_id: int, chave: str):
    from .. import viagens_detecao as vd
    p = next((x for x in vd.detetar(db, entidade_id) if x.chave == chave), None)
    if not p:
        raise HTTPException(status_code=404, detail="Período não encontrado (os movimentos mudaram?).")
    return p


@router.get("/detetar")
def detetar(request: Request, entidade: IntOpcional = None, todos: int = 0, u: Utilizador = Depends(utilizador_atual),
            db: Session = Depends(get_db)):
    """Períodos de viagem detetados pela análise cronológica dos gastos (e, opcionalmente, revistos pela IA)."""
    from .. import ia
    from .. import viagens_detecao as vd
    visiveis, ent, papel = entidade_escolhida(request, db, u, entidade)
    if not ent:
        return render(request, "viagens/detetar.html", entidade=None, visiveis=visiveis)
    periodos = vd.detetar(db, ent.id)
    registados = sum(1 for p in periodos if p.viagem)
    if not todos:
        periodos = [p for p in periodos if not p.viagem]
    return render(request, "viagens/detetar.html", visiveis=visiveis, entidade=ent, periodos=periodos,
                  registados=registados, todos=todos, ia_ok=ia.disponivel(),
                  pode_editar=PAPEIS[papel] >= PAPEIS["editar"], fotos_ok=bool(_chave_immich(request, u)))


@router.get("/detetar/fotos")
def detetar_fotos(request: Request, entidade: int, ini: str, fim: str, u: Utilizador = Depends(utilizador_atual),
                  db: Session = Depends(get_db)):
    """Fragmento de um período detetado: as fotos do Immich nessas datas, os lugares e se confirmam a viagem."""
    from .. import immich
    from .. import viagens_detecao as vd
    exigir_papel(db, u, entidade, "ver")
    try:
        d_ini, d_fim = parse_data(ini), parse_data(fim)
    except ValueError:
        d_ini = d_fim = None
    if not d_ini or not d_fim or not (0 <= (d_fim - d_ini).days <= 120):
        raise HTTPException(status_code=400, detail="Datas inválidas.")
    ctx = {"r": None, "erro": None}
    chave = _chave_immich(request, u)
    if chave:
        try:
            ctx["r"] = vd.resumo_fotos(immich.fotos_entre(d_ini, d_fim, chave=chave))
        except immich.ErroImmich as e:
            ctx["erro"] = str(e)
    return render(request, "viagens/_fotos_periodo.html", **ctx)


def _lugares_fotos(request: Request, u: Utilizador, p) -> list | None:
    """Lugares (cidade, país) das fotos do período, para a IA; sem chave do Immich ou com erro, nenhum."""
    from .. import immich
    chave = _chave_immich(request, u)
    if not chave:
        return None
    try:
        return immich.fotos_entre(p.ini, p.fim, chave=chave).lugares
    except immich.ErroImmich:
        return None


@router.post("/detetar/ia")
async def detetar_ia(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Pede à IA a análise de um período (ou de todos os que ainda não têm, até 12 de cada vez)."""
    from .. import ia
    from .. import viagens_detecao as vd
    form = await ler_form(request)
    ent_id = int(form["entidade"]) if str(form.get("entidade", "")).isdigit() else 0
    exigir_papel(db, u, ent_id, "editar")
    chave = str(form.get("chave", ""))
    alvo = [_periodo(db, ent_id, chave)] if chave else [p for p in vd.detetar(db, ent_id) if not p.ia and not p.viagem][:12]
    feitos, erro = 0, None
    for p in alvo:
        try:
            vd.analisar_com_ia(db, p, _lugares_fotos(request, u, p))
            feitos += 1
        except ia.ErroIA as e:
            erro = str(e)
            break
    db.commit()
    if feitos:
        auditar(db, u, "viagens_detetar_ia", f"{feitos} período(s)")
        db.commit()
        flash(request, f"IA: {feitos} período{'s' if feitos != 1 else ''} analisado{'s' if feitos != 1 else ''}.")
    if erro:
        flash(request, f"A análise pela IA falhou: {erro}", "erro")
    if not alvo:
        flash(request, "Todos os períodos já foram analisados pela IA.")
    destino = f"/viagens/detetar?entidade={ent_id}" + (f"#p-{chave}" if chave else "")
    return RedirectResponse(destino, status_code=303)


@router.post("/detetar/descartar")
async def detetar_descartar(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """«Não é viagem»: o período não volta a ser proposto."""
    from ..models import ViagemDescartada
    form = await ler_form(request)
    ent_id = int(form["entidade"]) if str(form.get("entidade", "")).isdigit() else 0
    exigir_papel(db, u, ent_id, "editar")
    p = _periodo(db, ent_id, str(form.get("chave", "")))
    db.add(ViagemDescartada(entidade_id=ent_id, data_ini=p.ini, data_fim=p.fim))
    auditar(db, u, "viagem_descartada", f"{p.ini:%d/%m/%Y} a {p.fim:%d/%m/%Y}")
    db.commit()
    flash(request, f"Período de {p.ini:%d/%m/%Y} a {p.fim:%d/%m/%Y} descartado: não volta a ser proposto.")
    return RedirectResponse(f"/viagens/detetar?entidade={ent_id}", status_code=303)


@router.post("/nova")
async def criar(request: Request, entidade: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    exigir_papel(db, u, entidade, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, entidade)
    v, erros, dados = _ler(await ler_form(request))
    if erros:
        return _form(request, db, ent, v, erros=erros, status_code=400)
    etiqueta_id = dados.pop("etiqueta_id")
    viagem = Viagem(entidade_id=ent.id, **dados)
    if not viagem.modelo_id or not db.get(ModeloViagem, viagem.modelo_id):
        viagem.modelo_id = vg.modelo_omissao(db).id
    db.add(viagem)
    db.flush()
    if etiqueta_id and etiqueta_id in {e.id for e in vg.etiquetas_livres(db, viagem)}:
        viagem.etiqueta_id = etiqueta_id
    db.flush()
    db.refresh(viagem)
    aprendidos = vg.sincronizar(db, viagem)
    auditar(db, u, "viagem_criada", viagem.nome)
    db.commit()
    flash(request, f"Viagem «{viagem.nome}» registada. Revê as sugestões de gastos abaixo."
          + (f" {aprendidos} movimento(s) que já tinham a etiqueta entraram na viagem e no modelo." if aprendidos else ""))
    return RedirectResponse(f"/viagens/{viagem.id}", status_code=303)


@router.get("/{vid}/editar")
def editar(request: Request, vid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    viagem, _ = _viagem(db, u, vid, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, viagem.entidade_id)
    v = {"nome": viagem.nome, "destino": viagem.destino or "", "pais": viagem.pais or "",
         "data_ini": viagem.data_ini.isoformat(), "data_fim": viagem.data_fim.isoformat(), "pessoas": str(viagem.pessoas),
         "motivo": viagem.motivo, "orcamento": montante_para_input(viagem.orcamento) if viagem.orcamento else "",
         "moeda": viagem.moeda or "", "notas": viagem.notas or "", "modelo_id": str(viagem.modelo_id or ""),
         "etiqueta_id": str(viagem.etiqueta_id or "")}
    return _form(request, db, ent, v, viagem=viagem)


@router.post("/{vid}/editar")
async def guardar(request: Request, vid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    viagem, _ = _viagem(db, u, vid, "editar")
    _, ent, _ = entidade_escolhida(request, db, u, viagem.entidade_id)
    v, erros, dados = _ler(await ler_form(request))
    if erros:
        return _form(request, db, ent, v, viagem=viagem, erros=erros, status_code=400)
    etiqueta_id = dados.pop("etiqueta_id")
    for k, val in dados.items():
        setattr(viagem, k, val)
    if etiqueta_id and etiqueta_id != viagem.etiqueta_id and etiqueta_id in {e.id for e in vg.etiquetas_livres(db, viagem)}:
        viagem.etiqueta_id = etiqueta_id
    db.flush()
    db.refresh(viagem)
    aprendidos = vg.sincronizar(db, viagem)
    auditar(db, u, "viagem_editada", viagem.nome)
    db.commit()
    flash(request, "Viagem guardada." + (f" {aprendidos} movimento(s) etiquetado(s) ensinado(s) ao modelo." if aprendidos else ""))
    return RedirectResponse(f"/viagens/{viagem.id}", status_code=303)


@router.post("/{vid}/apagar")
async def apagar(request: Request, vid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    await ler_form(request)
    viagem, _ = _viagem(db, u, vid, "editar")
    ent_id, nome, et_id = viagem.entidade_id, viagem.nome, viagem.etiqueta_id
    db.delete(viagem)
    et = db.get(Etiqueta, et_id) if et_id else None
    if et and et.nome.startswith("Viagem: "):
        db.delete(et)   # etiqueta criada para a viagem: os movimentos ficam, só perdem a marca (uma etiqueta tua fica)
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
    from .. import immich
    return render(request, "viagens/detalhe.html", visiveis=visiveis, entidade=ent, viagem=viagem, modelos=_modelos(db),
                  r=vg.resumo(db, viagem), sugestoes=vg.candidatos(db, viagem), motivos=MOTIVOS_VIAGEM,
                  pode_editar=PAPEIS[papel] >= PAPEIS["editar"], immich_cfg=immich.configurado())


# ------------------------------------------------------------------ fotografias (Immich)
def _chave_immich(request: Request, u: Utilizador) -> str | None:
    from .. import immich
    return immich.chave_de(u, request.app.state.defs.secret_key)


@router.get("/{vid}/fotos")
def fotos(request: Request, vid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Secção «Fotografias» (fragmento): as fotos tiradas nas datas da viagem, os lugares e o álbum no Immich."""
    from .. import immich
    viagem, papel = _viagem(db, u, vid)
    ctx = {"viagem": viagem, "pode_editar": PAPEIS[papel] >= PAPEIS["editar"],
           "immich_link": immich.url_browser(request.url.hostname), "nome_album": immich.nome_album(viagem.nome),
           "erro": None, "f": None, "album": None}
    chave = _chave_immich(request, u)
    if not chave:
        ctx["erro"] = "sem_chave"
    else:
        try:
            ctx["f"] = immich.fotos_entre(viagem.data_ini, viagem.data_fim, chave=chave)
            if viagem.immich_album_id:
                ctx["album"] = immich.album_existe(viagem.immich_album_id, chave=chave) or {"apagado": True}
        except immich.ErroImmich as e:
            ctx["erro"] = str(e)
    return render(request, "viagens/_fotos.html", **ctx)


@router.get("/fotos/{aid}/miniatura")
def miniatura_foto(request: Request, aid: str, u: Utilizador = Depends(utilizador_atual)):
    """Miniatura de uma foto do Immich, pedida com a chave de quem está a ver (o browser não fala com o Immich)."""
    import re
    from fastapi.responses import Response
    from .. import immich
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", aid):
        raise HTTPException(status_code=404, detail="Não encontrado.")
    try:
        img = immich.miniatura(aid, chave=_chave_immich(request, u))
    except immich.ErroImmich:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    tipo = "image/webp" if img[8:12] == b"WEBP" else "image/jpeg"
    return Response(img, media_type=tipo, headers={"Cache-Control": "private, max-age=86400"})


@router.post("/{vid}/album")
async def album(request: Request, vid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Cria no Immich o álbum da viagem com as fotos dessas datas (ou junta as que faltam ao álbum já ligado)."""
    from .. import immich
    viagem, _ = _viagem(db, u, vid, "editar")
    await ler_form(request)
    chave = _chave_immich(request, u)
    try:
        ids = [f.id for f in immich.fotos_entre(viagem.data_ini, viagem.data_fim, chave=chave).fotos]
        if not ids:
            flash(request, "Não há fotos no Immich nas datas desta viagem.", "erro")
            return RedirectResponse(f"/viagens/{vid}#fotos", status_code=303)
        existente = immich.album_existe(viagem.immich_album_id, chave=chave) if viagem.immich_album_id else None
        nome = immich.nome_album(viagem.nome)
        # link de volta para a viagem no Gestor, na descrição do álbum
        ligacao = f"Gestor de Contas: {str(request.base_url).rstrip('/')}/viagens/{viagem.id}"
        descricao = " · ".join(x for x in (viagem.destino, viagem.pais,
                                            f"{viagem.data_ini:%d/%m/%Y} a {viagem.data_fim:%d/%m/%Y}") if x) + "\n" + ligacao
        if existente:
            n = immich.juntar_ao_album(viagem.immich_album_id, ids, chave=chave)
            msg = f"Álbum atualizado: {n} foto{'s' if n != 1 else ''} nova{'s' if n != 1 else ''}." if n else "O álbum já tinha todas as fotos."
            # álbum criado antes com o nome simples da viagem / sem o link: passa ao formato novo (um nome dado à mão fica)
            novo_nome = nome if existente.get("albumName") == viagem.nome else None
            nova_desc = descricao if ligacao not in (existente.get("description") or "") else None
            if novo_nome or nova_desc:
                immich.atualizar_album(viagem.immich_album_id, chave=chave, nome=novo_nome, descricao=nova_desc)
        else:
            viagem.immich_album_id = immich.criar_album(nome, descricao, ids, chave=chave)
            msg = f"Álbum «{nome}» criado no Immich com {len(ids)} foto{'s' if len(ids) != 1 else ''}."
    except immich.ErroImmich as e:
        flash(request, f"Não foi possível: {e}", "erro")
        return RedirectResponse(f"/viagens/{vid}#fotos", status_code=303)
    auditar(db, u, "viagem_album", f"{viagem.nome}: {viagem.immich_album_id}")
    db.commit()
    flash(request, msg)
    return RedirectResponse(f"/viagens/{vid}#fotos", status_code=303)


@router.post("/{vid}/datas")
async def datas(request: Request, vid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Acertar só o intervalo da viagem enquanto se revêem as sugestões; as sugestões passam a ser as das novas datas."""
    viagem, _ = _viagem(db, u, vid, "editar")
    form = await ler_form(request)
    try:
        ini, fim = parse_data(str(form.get("data_ini", ""))), parse_data(str(form.get("data_fim", "")))
    except ValueError:
        ini = fim = None
    if not ini or not fim or fim < ini:
        flash(request, "Datas inválidas: o regresso não pode ser antes da partida.", "erro")
        return RedirectResponse(f"/viagens/{vid}#sugestoes", status_code=303)
    if (ini, fim) != (viagem.data_ini, viagem.data_fim):
        antes = f"{viagem.data_ini:%d/%m/%Y} a {viagem.data_fim:%d/%m/%Y}"
        viagem.data_ini, viagem.data_fim = ini, fim
        db.flush()
        vg.sincronizar(db, viagem)
        auditar(db, u, "viagem_datas", f"{viagem.nome}: {antes} → {ini:%d/%m/%Y} a {fim:%d/%m/%Y}")
        db.commit()
        flash(request, f"Datas da viagem: {ini:%d/%m/%Y} a {fim:%d/%m/%Y}. As sugestões foram recalculadas.")
    return RedirectResponse(f"/viagens/{vid}#sugestoes", status_code=303)


@router.post("/{vid}/album/desligar")
async def album_desligar(request: Request, vid: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Esquece o álbum ligado (o álbum fica no Immich)."""
    viagem, _ = _viagem(db, u, vid, "editar")
    await ler_form(request)
    viagem.immich_album_id = None
    db.commit()
    flash(request, "Álbum desligado da viagem (continua no Immich).")
    return RedirectResponse(f"/viagens/{vid}#fotos", status_code=303)


@router.post("/{vid}/destino-das-fotos")
async def destino_das_fotos(request: Request, vid: int, u: Utilizador = Depends(utilizador_atual),
                            db: Session = Depends(get_db)):
    """Preenche o destino e o país da viagem com o lugar mais fotografado."""
    viagem, _ = _viagem(db, u, vid, "editar")
    form = await ler_form(request)
    cidade, pais = str(form.get("cidade", "")).strip()[:120], str(form.get("pais", "")).strip()[:80]
    if cidade:
        viagem.destino = cidade
    if pais:
        viagem.pais = pais
    db.commit()
    flash(request, "Destino atualizado a partir das fotos.")
    return RedirectResponse(f"/viagens/{vid}#fotos", status_code=303)


@router.post("/{vid}/modelo")
async def trocar_modelo(request: Request, vid: int, u: Utilizador = Depends(utilizador_atual),
                        db: Session = Depends(get_db)):
    viagem, _ = _viagem(db, u, vid, "editar")
    form = await ler_form(request)
    mid = str(form.get("modelo_id", ""))
    m = db.get(ModeloViagem, int(mid)) if mid.isdigit() else None
    if m and m.id != viagem.modelo_id:
        viagem.modelo_id = m.id
        auditar(db, u, "viagem_modelo", f"{viagem.nome} → {m.nome}")
        db.commit()
    db.refresh(viagem)
    aprendidos = vg.sincronizar(db, viagem)
    db.commit()
    n = len(vg.candidatos(db, viagem))
    flash(request, f"Sugestões recalculadas com o modelo «{viagem.modelo.nome}»: {n}."
          + (f" {aprendidos} movimento(s) já etiquetado(s) da viagem ensinado(s) ao modelo." if aprendidos else ""))
    return RedirectResponse(f"/viagens/{viagem.id}#sugestoes", status_code=303)


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
                  padroes=sorted(m.padroes, key=lambda p: (-(p.positivos - p.negativos), p.padrao)),
                  em_uso=_viagens_do_modelo(db, u, m))


def _viagens_do_modelo(db: Session, u: Utilizador, m: ModeloViagem) -> list[tuple[Viagem, int]]:
    """Viagens (das entidades visíveis) que usam o modelo, com o n.º de sugestões calculadas agora."""
    from ..deps import entidades_visiveis
    ids = [e.id for e, _ in entidades_visiveis(db, u)]
    viagens = db.scalars(select(Viagem).where(Viagem.modelo_id == m.id, Viagem.entidade_id.in_(ids))
                         .order_by(Viagem.data_ini.desc())).all() if ids else []
    return [(v, len(vg.candidatos(db, v))) for v in viagens]


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
    if nome and not _nome_livre(db, nome, m.id):
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
    for v in db.scalars(select(Viagem).where(Viagem.modelo_id == m.id)):
        vg.sincronizar(db, v)
    db.commit()
    em_uso = _viagens_do_modelo(db, u, m)
    if em_uso:
        detalhe = ", ".join(f"{v.nome}: {n}" for v, n in em_uso)
        flash(request, f"Modelo «{m.nome}» guardado. Sugestões recalculadas — {detalhe}.")
    else:
        flash(request, f"Modelo «{m.nome}» guardado.")
    return RedirectResponse(f"/viagens/modelos/{m.id}#em-uso", status_code=303)


@router.post("/modelos/{mid}/renomear")
async def renomear_modelo(request: Request, mid: int, u: Utilizador = Depends(exigir_editor),
                          db: Session = Depends(get_db)):
    form = await ler_form(request)
    m = db.get(ModeloViagem, mid)
    if not m:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    nome = str(form.get("nome", "")).strip()[:80]
    voltar = "/viagens" if form.get("voltar") == "lista" else f"/viagens/modelos/{m.id}"
    if not nome:
        flash(request, "O nome não pode ficar vazio.", "erro")
    elif not _nome_livre(db, nome, m.id):
        flash(request, f"Já existe um modelo «{nome}».", "erro")
    else:
        antigo, m.nome = m.nome, nome
        auditar(db, u, "modelo_viagem_renomeado", f"{antigo} → {nome}")
        db.commit()
        flash(request, f"Modelo «{antigo}» passou a «{nome}».")
    return RedirectResponse(voltar, status_code=303)


@router.post("/modelos/{mid}/apagar")
async def apagar_modelo(request: Request, mid: int, u: Utilizador = Depends(exigir_editor),
                        db: Session = Depends(get_db)):
    await ler_form(request)
    m = db.get(ModeloViagem, mid)
    if not m:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    n = _uso_modelos(db).get(m.id, 0)
    if n:
        flash(request, f"O modelo «{m.nome}» está associado a {n} viagem(ns): muda-lhes o modelo antes de o apagar.", "erro")
        return RedirectResponse(f"/viagens/modelos/{m.id}", status_code=303)
    nome = m.nome
    db.delete(m)
    auditar(db, u, "modelo_viagem_apagado", nome)
    db.commit()
    flash(request, f"Modelo «{nome}» apagado (com o que tinha aprendido).")
    return RedirectResponse("/viagens", status_code=303)


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
