"""Administração: utilizadores, permissões por entidade e entidades."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import preferencias
from .. import backup as bk
from ..backup import fazer_backup, ultimo_backup
from ..deps import auditar, exigir_admin, flash, get_db, render
from ..models import (FONTES_EXTRATOS, MODULOS, PAPEIS, PAPEIS_ROTULO, TIPOS_ENTIDADE, Auditoria, Conta, Entidade,
                      Instituicao, Permissao, Utilizador)
from ..seguranca import hash_password, ler_form, validar_password_nova

router = APIRouter(prefix="/admin")

# Decisão por omissão (DESIGN.md §14): novos utilizadores editam a Família
# e não veem a entidade Mãe.
PAPEL_OMISSAO = {"Família": "editar"}
PERIODOS_ATIVIDADE = {"dia": ("Último dia", timedelta(days=1)), "semana": ("Última semana", timedelta(days=7)),
                      "mes": ("Último mês", timedelta(days=31))}


def _entidades(db: Session) -> list[Entidade]:
    return db.scalars(select(Entidade).order_by(Entidade.ordem, Entidade.nome)).all()


def _papeis_do_form(form, entidades) -> dict[int, str]:
    papeis = {}
    for e in entidades:
        p = str(form.get(f"papel_{e.id}", ""))
        if p in PAPEIS:
            papeis[e.id] = p
    return papeis


def _aplicar_papeis(db: Session, u: Utilizador, papeis: dict[int, str]) -> None:
    for p in list(u.permissoes):
        if p.entidade_id not in papeis:
            u.permissoes.remove(p)
        else:
            p.papel = papeis.pop(p.entidade_id)
    for entidade_id, papel in papeis.items():
        u.permissoes.append(Permissao(entidade_id=entidade_id, papel=papel))


def _n_admins_ativos(db: Session, excluir: int | None = None) -> int:
    q = select(func.count()).select_from(Utilizador).where(Utilizador.is_admin, Utilizador.ativo)
    if excluir:
        q = q.where(Utilizador.id != excluir)
    return db.scalar(q)


@router.get("/utilizadores")
def utilizadores(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    lista = db.scalars(select(Utilizador).options(selectinload(Utilizador.permissoes))
                       .order_by(Utilizador.nome)).all()
    return render(request, "admin/utilizadores.html", lista=lista, entidades=_entidades(db), rotulos=PAPEIS_ROTULO)


@router.get("/preferencias")
def preferencias_form(request: Request, admin: Utilizador = Depends(exigir_admin)):
    return render(request, "admin/preferencias.html", prefs=request.app.state.prefs)


@router.get("/backups")
def backups(request: Request, periodo: str = "", admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    q = select(Auditoria, Utilizador.nome).outerjoin(Utilizador).order_by(Auditoria.id.desc())
    if periodo in PERIODOS_ATIVIDADE:
        q = q.where(Auditoria.quando >= datetime.now() - PERIODOS_ATIVIDADE[periodo][1]).limit(500)
    else:
        periodo = ""
        q = q.limit(30)
    eventos = db.execute(q).all()
    return render(request, "admin/backups.html", eventos=eventos, periodo=periodo,
                  periodos_atividade={k: r for k, (r, _) in PERIODOS_ATIVIDADE.items()},
                  ultimo_backup=ultimo_backup(request.app.state.defs), est=bk.estado(request.app.state.defs))


@router.get("/utilizadores/novo")
def novo_form(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    entidades = _entidades(db)
    papeis = {e.id: PAPEL_OMISSAO.get(e.nome, "") for e in entidades}
    return render(request, "admin/utilizador_form.html", alvo=None, entidades=entidades, papeis=papeis,
                  rotulos=PAPEIS_ROTULO, v={"ativo": True})


@router.post("/utilizadores/novo")
async def novo(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    form = await ler_form(request)
    entidades = _entidades(db)
    v = {k: str(form.get(k, "")).strip() for k in ("nome", "login", "email")}
    v["is_admin"] = form.get("is_admin") == "1"
    v["ativo"] = True
    papeis = _papeis_do_form(form, entidades)
    erros = []
    if not v["nome"] or not v["login"]:
        erros.append("Nome e utilizador são obrigatórios.")
    if db.scalar(select(Utilizador).where(func.lower(Utilizador.login) == v["login"].lower())):
        erros.append("Já existe um utilizador com esse login.")
    erro_pw = validar_password_nova(str(form.get("password", "")), str(form.get("password2", "")))
    if erro_pw:
        erros.append(erro_pw)
    if erros:
        return render(request, "admin/utilizador_form.html", status_code=400, alvo=None, entidades=entidades,
                      papeis=papeis, rotulos=PAPEIS_ROTULO, v=v, erros=erros)
    u = Utilizador(nome=v["nome"], login=v["login"], email=v["email"] or None, is_admin=v["is_admin"],
                   password_hash=hash_password(str(form["password"])))
    db.add(u)
    _aplicar_papeis(db, u, papeis)
    db.flush()
    auditar(db, admin, "utilizador_criado", f"{u.id}: {u.login}")
    db.commit()
    flash(request, f"Utilizador «{u.login}» criado.")
    return RedirectResponse("/admin/utilizadores", status_code=303)


def _utilizadores_paperless(request: Request, db: Session) -> list[tuple[Utilizador, bool]]:
    """Utilizadores ativos e se têm token do Paperless (pessoal ou, para administradores, o partilhado)."""
    from .. import paperless
    segredo = request.app.state.defs.secret_key
    return [(u, bool(paperless.token_de(u, segredo)))
            for u in db.scalars(select(Utilizador).where(Utilizador.ativo).order_by(Utilizador.nome))]


def _circuito_ctx(request: Request, db: Session, admin: Utilizador, modelo: str, params: dict, uid: int | None, **extra):
    from .. import paperless, paperless_circuitos as pc
    m = pc.MODELOS.get(modelo) or pc.MODELOS["recibo_vencimento"]
    valores = {nome: params.get(nome, omissao) for nome, _, omissao, _ in m.parametros}
    return dict(modelos=pc.MODELOS, modelo=m, valores=valores, utilizadores=_utilizadores_paperless(request, db),
                uid=uid or admin.id, paperless_cfg=paperless.configurado(), paperless_link=paperless.url_browser(), **extra)


@router.get("/paperless")
def paperless_circuitos(request: Request, modelo: str = "recibo_vencimento", admin: Utilizador = Depends(exigir_admin),
                        db: Session = Depends(get_db)):
    """Circuitos de documentos no Paperless: escolher o modelo e os parâmetros, pré-visualizar e aplicar."""
    return render(request, "admin/paperless.html", **_circuito_ctx(request, db, admin, modelo, {}, None, plano=None, erros=[]))


@router.post("/paperless")
async def paperless_circuito_executar(request: Request, admin: Utilizador = Depends(exigir_admin),
                                      db: Session = Depends(get_db)):
    """acao=ver: o que existe e o que seria criado (nada muda); acao=aplicar: cria o que falta."""
    from .. import paperless, paperless_circuitos as pc
    form = await ler_form(request)
    modelo = str(form.get("modelo", ""))
    if modelo not in pc.MODELOS:
        return RedirectResponse("/admin/paperless", status_code=303)
    params = {nome: str(form.get(nome, "")).strip()[:200] for nome, _, _, _ in pc.MODELOS[modelo].parametros}
    uid = int(form["utilizador"]) if str(form.get("utilizador", "")).isdigit() else admin.id
    u = db.get(Utilizador, uid)
    token = paperless.token_de(u, request.app.state.defs.secret_key) if u else None
    erros = pc.validar(modelo, params)
    if not token:
        erros.append(f"{u.nome if u else 'Esse utilizador'} não tem token do Paperless (Perfil, ou Utilizadores › Ligações).")
    plano = None
    if not erros:
        try:
            if form.get("acao") == "aplicar":
                plano = pc.aplicar(modelo, params, token, senha_email=str(form.get("senha", "")) or None)
                criados = [x for x in plano.passos if x.estado == "criado"]
                auditar(db, admin, "paperless_circuito", f"{modelo} ({u.login}): {len(criados)} criados, "
                                                          f"{sum(1 for x in plano.passos if x.estado == 'erro')} erros")
                db.commit()
            else:
                plano = pc.planear(modelo, params, token)
        except paperless.ErroPaperless as e:
            erros.append(str(e))
    return render(request, "admin/paperless.html", status_code=400 if erros else 200,
                  **_circuito_ctx(request, db, admin, modelo, params, uid, plano=plano, erros=erros))


@router.get("/utilizadores/{uid}")
def editar_form(request: Request, uid: int, admin: Utilizador = Depends(exigir_admin),
                db: Session = Depends(get_db)):
    alvo = db.get(Utilizador, uid) or _404()
    papeis = {p.entidade_id: p.papel for p in alvo.permissoes}
    v = {"nome": alvo.nome, "login": alvo.login, "email": alvo.email or "", "is_admin": alvo.is_admin,
         "ativo": alvo.ativo}
    from .. import chaves_api
    return render(request, "admin/utilizador_form.html", alvo=alvo, entidades=_entidades(db), papeis=papeis,
                  rotulos=PAPEIS_ROTULO, v=v, tokens_admin=chaves_api.pelo_admin(request.app.state.prefs),
                  ligacoes=chaves_api.estado(alvo, request.app.state.defs.secret_key))


@router.post("/utilizadores/{uid}/ligacao")
async def ligacao_utilizador(request: Request, uid: int, admin: Utilizador = Depends(exigir_admin),
                             db: Session = Depends(get_db)):
    """Chave da API do Paperless/Immich de um utilizador, configurada pelo administrador (com a preferência ligada)."""
    from .. import chaves_api
    alvo = db.get(Utilizador, uid) or _404()
    form = await ler_form(request)
    servico = str(form.get("servico", ""))
    if servico not in chaves_api.SERVICOS:
        _404()
    if not chaves_api.pelo_admin(request.app.state.prefs):
        flash(request, "Liga primeiro, em Preferências, a opção de as chaves serem configuradas pelo administrador.", "erro")
        return RedirectResponse(f"/admin/utilizadores/{uid}#ligacoes", status_code=303)
    acao = str(form.get("acao", ""))
    msg, tipo = chaves_api.gerir(alvo, servico, acao, str(form.get("token", "")), request.app.state.defs.secret_key)
    if tipo == "ok" and acao != "testar":
        auditar(db, admin, f"{servico}_token_{'removido' if acao == 'remover' else 'guardado'}", alvo.login)
        db.commit()
    flash(request, msg, tipo)
    return RedirectResponse(f"/admin/utilizadores/{uid}#ligacoes", status_code=303)


@router.post("/utilizadores/{uid}")
async def editar(request: Request, uid: int, admin: Utilizador = Depends(exigir_admin),
                 db: Session = Depends(get_db)):
    alvo = db.get(Utilizador, uid) or _404()
    form = await ler_form(request)
    entidades = _entidades(db)
    v = {k: str(form.get(k, "")).strip() for k in ("nome", "email")}
    v["login"] = alvo.login
    v["is_admin"] = form.get("is_admin") == "1"
    v["ativo"] = form.get("ativo") == "1"
    papeis = _papeis_do_form(form, entidades)
    erros = []
    if not v["nome"]:
        erros.append("O nome é obrigatório.")
    if (not v["is_admin"] or not v["ativo"]) and alvo.is_admin and _n_admins_ativos(db, excluir=alvo.id) == 0:
        erros.append("Tem de ficar pelo menos um administrador ativo.")
    nova_pw = str(form.get("password", ""))
    if nova_pw:
        erro_pw = validar_password_nova(nova_pw, str(form.get("password2", "")))
        if erro_pw:
            erros.append(erro_pw)
    if erros:
        return render(request, "admin/utilizador_form.html", status_code=400, alvo=alvo, entidades=entidades,
                      papeis=papeis, rotulos=PAPEIS_ROTULO, v=v, erros=erros)

    alvo.nome, alvo.email = v["nome"], v["email"] or None
    alvo.is_admin, alvo.ativo = v["is_admin"], v["ativo"]
    _aplicar_papeis(db, alvo, papeis)
    if nova_pw:
        alvo.password_hash = hash_password(nova_pw)
        alvo.versao_sessao += 1
    auditar(db, admin, "utilizador_editado", f"{alvo.id}: {alvo.login}" + (" (password redefinida)" if nova_pw else ""))
    db.commit()
    if alvo.id == admin.id and nova_pw:
        request.session["ver"] = alvo.versao_sessao
    flash(request, f"Utilizador «{alvo.login}» atualizado.")
    return RedirectResponse("/admin/utilizadores", status_code=303)


@router.get("/entidades")
def entidades(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    return render(request, "admin/entidades.html", entidades=_entidades(db), modulos=MODULOS)


@router.post("/entidades")
async def nova_entidade(request: Request, admin: Utilizador = Depends(exigir_admin),
                        db: Session = Depends(get_db)):
    form = await ler_form(request)
    dados, erros = _ler_entidade(form, db, None)
    if erros:
        flash(request, " ".join(erros), "erro")
        return RedirectResponse("/admin/entidades", status_code=303)
    if "modulos" not in dados:
        # nova entidade: tudo ligado (desliga-se em Editar); o Lar só nas heranças
        dados["modulos"] = ",".join(m for m in MODULOS if m != "lar" or dados.get("tipo") == "heranca")
    e = Entidade(**dados, ordem=(db.scalar(select(func.max(Entidade.ordem))) or 0) + 1)
    db.add(e)
    db.flush()
    # Quem cria fica a administrar; os restantes acessos dão-se em Utilizadores.
    db.add(Permissao(utilizador_id=admin.id, entidade_id=e.id, papel="admin"))
    auditar(db, admin, "entidade_criada", f"{e.id}: {e.nome}")
    db.commit()
    flash(request, f"Entidade «{e.nome}» criada.")
    return RedirectResponse("/admin/entidades", status_code=303)


def _ler_entidade(form, db: Session, atual: Entidade | None) -> tuple[dict, list[str]]:
    v = {k: str(form.get(k, "")).strip() for k in ("nome", "descricao", "tipo", "quota", "quota_futura", "quota_futura_nota")}
    erros = []
    if not v["nome"]:
        erros.append("Indica o nome.")
    elif db.scalar(select(Entidade).where(func.lower(Entidade.nome) == v["nome"].lower(),
                                          Entidade.id != (atual.id if atual else 0))):
        erros.append("Já existe uma entidade com esse nome.")
    tipo = v["tipo"] if v["tipo"] in TIPOS_ENTIDADE else "pessoal"
    quota = futura = None
    if tipo == "heranca":
        quota = _percentagem(v["quota"])
        if quota is None:
            erros.append("Numa herança indica a parte que detém (ex.: 1/6 ou 16,67).")
        if v["quota_futura"]:
            futura = _percentagem(v["quota_futura"])
            if futura is None:
                erros.append("A parte no 2.º cenário é inválida (ex.: 1/2 ou 50).")
    # Os módulos só mudam quando o formulário os traz (marca «modulos_presentes»); senão ficam como estão.
    modulos = [m for m in form.getlist("modulos") if m in MODULOS] if form.get("modulos_presentes") else None
    extra = {"modulos": ",".join(modulos)} if modulos is not None else {}
    if form.get("perfil_presente"):              # perfil de importação: bancos (nenhum marcado = todos) e fontes
        ids = sorted({int(x) for x in form.getlist("bancos") if str(x).isdigit()})
        extra["bancos"] = ",".join(map(str, ids))
        extra["fontes"] = ",".join(f for f in FONTES_EXTRATOS if f in form.getlist("fontes"))
    return {"nome": v["nome"][:100], "descricao": v["descricao"] or None, "tipo": tipo, "quota": quota,
            "quota_futura": futura, "quota_futura_nota": (v["quota_futura_nota"][:100] or None) if futura else None,
            **extra}, erros


def _ctx_entidade(db: Session, e: Entidade) -> dict:
    """Contas de todas as entidades (para incluir/retirar deste grupo), bancos e fontes do perfil de importação."""
    contas = db.scalars(select(Conta).options(selectinload(Conta.entidade), selectinload(Conta.instituicao))
                        .order_by(Conta.ativa.desc(), Conta.nome)).all()
    return {"todas_contas": sorted(contas, key=lambda c: (c.entidade_id != e.id, c.entidade.nome, not c.ativa, c.nome)),
            "instituicoes": db.scalars(select(Instituicao).where(Instituicao.ativa).order_by(Instituicao.nome)).all(),
            "fontes": FONTES_EXTRATOS, "outras_entidades": [x for x in _entidades(db) if x.id != e.id],
            "tipos": TIPOS_ENTIDADE, "modulos": MODULOS}


def _aplicar_contas(db: Session, e: Entidade, form) -> tuple[list[str], list[str]]:
    """Contas marcadas passam para esta entidade; as desta entidade desmarcadas vão para o destino escolhido.
    Devolve (resumo das mudanças, erros)."""
    from .. import servicos
    if not form.get("contas_presentes"):
        return [], []
    marcadas = {int(x) for x in form.getlist("contas") if str(x).isdigit()}
    proprias = list(db.scalars(select(Conta).where(Conta.entidade_id == e.id)))
    retirar = [c for c in proprias if c.id not in marcadas]
    destino = db.get(Entidade, int(form["destino"])) if str(form.get("destino", "")).isdigit() else None
    if retirar and (not destino or destino.id == e.id):
        return [], [f"Escolhe para que entidade vão as contas retiradas ({', '.join(c.nome for c in retirar)})."]
    feito, erros = [], []
    for c in db.scalars(select(Conta).where(Conta.id.in_(marcadas), Conta.entidade_id != e.id)) if marcadas else []:
        origem = c.entidade.nome
        erro = servicos.mudar_entidade(db, c, e.id)
        (erros.append(erro) if erro else feito.append(f"{c.nome} ({origem} → {e.nome})"))
    for c in retirar:
        erro = servicos.mudar_entidade(db, c, destino.id)
        (erros.append(erro) if erro else feito.append(f"{c.nome} ({e.nome} → {destino.nome})"))
    return feito, erros


def _percentagem(txt: str) -> float | None:
    """'1/6' → 16,666…; '16,67' ou '16,67%' → 16,67. Entre 0 (exclusive) e 100."""
    t = txt.replace("%", "").replace(" ", "").replace(",", ".")
    try:
        if "/" in t:
            a, b = t.split("/", 1)
            v = float(a) / float(b) * 100
        else:
            v = float(t)
    except (ValueError, ZeroDivisionError):
        return None
    return v if 0 < v <= 100 else None


@router.get("/entidades/{eid}")
def editar_entidade_form(request: Request, eid: int, admin: Utilizador = Depends(exigir_admin),
                         db: Session = Depends(get_db)):
    e = db.get(Entidade, eid) or _404()
    v = {"nome": e.nome, "descricao": e.descricao or "", "tipo": e.tipo or "pessoal", "quota": e.quota_txt,
         "quota_futura": e.quota_futura_txt, "quota_futura_nota": e.quota_futura_nota or "",
         "modulos": (e.modulos or "").split(","), "bancos": e.ids_bancos, "fontes": (e.fontes or "").split(","),
         "contas": {c.id for c in e.contas}}
    return render(request, "admin/entidade_form.html", e=e, v=v, erros=[], **_ctx_entidade(db, e))


@router.post("/entidades/{eid}")
async def editar_entidade(request: Request, eid: int, admin: Utilizador = Depends(exigir_admin),
                          db: Session = Depends(get_db)):
    e = db.get(Entidade, eid) or _404()
    form = await ler_form(request)
    dados, erros = _ler_entidade(form, db, e)

    def de_novo(erros):
        v = {k: str(form.get(k, "")) for k in ("nome", "descricao", "tipo", "quota", "quota_futura", "quota_futura_nota")}
        v["modulos"] = form.getlist("modulos")
        v["bancos"] = {int(x) for x in form.getlist("bancos") if str(x).isdigit()}
        v["fontes"] = form.getlist("fontes")
        v["contas"] = {int(x) for x in form.getlist("contas") if str(x).isdigit()}
        return render(request, "admin/entidade_form.html", status_code=400, e=e, v=v, erros=erros, **_ctx_entidade(db, e))
    if erros:
        return de_novo(erros)
    antes = f"{e.nome} ({e.tipo}{', %g%%' % e.quota if e.quota else ''})"
    for k, val in dados.items():
        setattr(e, k, val)
    mudancas, erros = _aplicar_contas(db, e, form)
    if erros:
        db.rollback()
        return de_novo(erros)
    auditar(db, admin, "entidade_editada", f"{e.id}: {antes} → {e.nome} ({e.tipo}{', %g%%' % e.quota if e.quota else ''}"
                                           f"{', futura %g%%' % e.quota_futura if e.quota_futura else ''}; módulos {e.modulos}; "
                                           f"bancos {e.bancos or 'todos'}; fontes {e.fontes or 'nenhuma'})")
    for m in mudancas:
        auditar(db, admin, "conta_mudou_entidade", m)
    db.commit()
    flash(request, f"Entidade «{e.nome}» guardada." + (f" Contas movidas: {'; '.join(mudancas)}." if mudancas else ""))
    return RedirectResponse("/admin/entidades", status_code=303)


@router.get("/importadores")
def importadores(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    """Tabela com os atributos de cada importador (vindos das fichas dos parsers) e a sua utilização."""
    from ..models import Importacao
    from ..parsers import todos
    uso = {}
    for parser, n, ultima in db.execute(select(Importacao.parser, func.count(), func.max(Importacao.criado_em))
                                        .where(Importacao.estado == "confirmada").group_by(Importacao.parser)):
        uso[parser] = {"n": n, "ultima": ultima}
    return render(request, "admin/importadores.html", importadores=todos(), uso=uso)


@router.post("/preferencias")
async def gravar_preferencias(request: Request, admin: Utilizador = Depends(exigir_admin),
                              db: Session = Depends(get_db)):
    form = await ler_form(request)
    mostrar = "1" if form.get("mostrar_iban_completo") == "1" else "0"
    preferencias.gravar(db, "mostrar_iban_completo", mostrar)
    ativas = "1" if form.get("tarefas_ativas") == "1" else "0"
    preferencias.gravar(db, "tarefas_ativas", ativas)
    avaliacao = "1" if form.get("avaliacao_automatica") == "1" else "0"
    preferencias.gravar(db, "avaliacao_automatica", avaliacao)
    minha_parte = "1" if form.get("mostrar_minha_parte") == "1" else "0"
    preferencias.gravar(db, "mostrar_minha_parte", minha_parte)
    tokens_admin = "1" if form.get("tokens_pelo_admin") == "1" else "0"
    preferencias.gravar(db, "tokens_pelo_admin", tokens_admin)
    dias = {}
    for chave in ("tarefas_dia_ini", "tarefas_dia_fim"):
        v = str(form.get(chave, "")).strip()
        if v:
            if not (v.isdigit() and 1 <= int(v) <= 31):
                flash(request, "Os dias do lembrete têm de estar entre 1 e 31.", "erro")
                return RedirectResponse("/admin/preferencias", status_code=303)
            preferencias.gravar(db, chave, str(int(v)))
            dias[chave] = int(v)
    auditar(db, admin, "preferencias", f"mostrar_iban_completo={mostrar} tarefas_ativas={ativas} avaliacao_automatica={avaliacao} mostrar_minha_parte={minha_parte} tokens_pelo_admin={tokens_admin} {dias}")
    db.commit()
    request.app.state.prefs = preferencias.carregar(db)
    flash(request, "Preferências guardadas.")
    return RedirectResponse("/admin/preferencias", status_code=303)


@router.get("/consolidar")
def consolidar_form(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    from .. import consolidacao
    return render(request, "admin/consolidar.html", passos=consolidacao.PASSOS,
                  diagnosticos=[consolidacao.diagnostico(db, e) for e in _entidades(db)],
                  resultado=request.session.pop("consolidacao", None))


@router.post("/consolidar")
async def consolidar(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    """Backup e depois os passos escolhidos, em todas as entidades escolhidas."""
    from .. import consolidacao
    form = await ler_form(request)
    passos = {p for p in form.getlist("passos") if p in consolidacao.PASSOS}
    ids = {int(x) for x in form.getlist("entidades") if str(x).isdigit()}
    entidades = [e for e in _entidades(db) if e.id in ids]
    if not passos or not entidades:
        flash(request, "Escolhe pelo menos um passo e uma entidade.", "erro")
        return RedirectResponse("/admin/consolidar", status_code=303)
    destino = fazer_backup(request.app.state.defs)
    resultado = []
    for e in entidades:
        r = consolidacao.consolidar(db, e, passos)
        resultado.append({"entidade": e.nome, "passos": {k: r[k] for k in consolidacao.PASSOS if k in r}})
    auditar(db, admin, "consolidacao", f"{sorted(passos)} em {[e.nome for e in entidades]} (backup {destino.name})")
    db.commit()
    request.session["consolidacao"] = resultado
    flash(request, f"Consolidação feita (backup antes: {destino.name}).")
    return RedirectResponse("/admin/consolidar", status_code=303)


@router.get("/recheio")
def recheio_form(admin: Utilizador = Depends(exigir_admin)):
    """Passou para Património › Recheio."""
    return RedirectResponse("/patrimonio/recheio", status_code=303)


@router.post("/recheio")
async def recheio_guardar(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    """Categorias de recheio e garantia por omissão; cria logo os itens das compras já classificadas nelas."""
    from .. import recheio
    from ..models import Categoria
    form = await ler_form(request)
    marcadas = {int(x) for x in form.getlist("categorias") if str(x).isdigit()}
    for c in db.scalars(select(Categoria)):
        c.recheio = c.id in marcadas
    g = str(form.get("garantia", "")).strip()
    if g.isdigit() and 0 < int(g) <= 240:
        preferencias.gravar(db, "recheio_garantia_meses", str(int(g)))
    vm = str(form.get("valor_minimo", "")).strip()
    if vm.isdigit() and int(vm) <= 100000:
        preferencias.gravar(db, "recheio_valor_minimo", str(int(vm)))
    db.flush()
    db.info.pop("recheio_cats", None)
    n = sum(recheio.preencher(db, e.id) for e in _entidades(db))
    auditar(db, admin, "recheio_categorias", f"{len(marcadas)} categorias; {n} itens criados")
    db.commit()
    request.app.state.prefs = preferencias.carregar(db)
    flash(request, f"Categorias de recheio guardadas." + (f" {n} compra{'s' if n != 1 else ''} já classificada{'s' if n != 1 else ''} "
                                                         f"{'passaram' if n != 1 else 'passou'} ao recheio." if n else ""))
    return RedirectResponse("/patrimonio/recheio", status_code=303)


@router.get("/tarefas")
def tarefas_antigo(admin: Utilizador = Depends(exigir_admin)):
    """Passou para Importar › Por validar (extratos da pasta) e Administração › Fuelio."""
    return RedirectResponse("/importar/validar", status_code=303)


@router.get("/fuelio")
def tarefas(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    """Importar (uma vez, ou para acertar) o histórico de abastecimentos do Fuelio."""
    from ..models import Abastecimento, Bem
    carros = db.scalars(select(Bem).where(Bem.tipo == "carro").order_by(Bem.entidade_id, Bem.nome)).all()
    n = dict(db.execute(select(Abastecimento.bem_id, func.count()).group_by(Abastecimento.bem_id)).all())
    return render(request, "admin/fuelio.html", carros=carros, n_abast=n, entidades={e.id: e for e in _entidades(db)})


@router.post("/pasta/ler")
async def pasta_ler(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    from .. import pasta_importacao as pi
    await ler_form(request)
    defs = request.app.state.defs
    r = pi.ler(db, defs.import_dir, defs.data_dir)
    db.commit()
    if not r.pasta_ok:
        flash(request, r.mensagem, "erro")
    else:
        n = len(r.novos)
        msg = f"Pasta lida: {n} extrato{'s' if n != 1 else ''} novo{'s' if n != 1 else ''} por validar"
        if r.erros:
            msg += f", {len(r.erros)} com erro"
        if r.ignorados:
            msg += f", {r.ignorados} já conhecido{'s' if r.ignorados != 1 else ''}"
        flash(request, msg + ".")
    return RedirectResponse("/importar/validar", status_code=303)


@router.post("/paperless/ler")
async def paperless_ler(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    """Extratos no Paperless (com a etiqueta ou o tipo escolhido) → por validar, como os da pasta partilhada."""
    from .. import extratos, paperless
    from .documentos import token as token_paperless
    form = await ler_form(request)
    nome = str(form.get("nome", "")).strip()[:80] or request.app.state.prefs.get("paperless_extratos", "")
    if nome != request.app.state.prefs.get("paperless_extratos"):
        preferencias.gravar(db, "paperless_extratos", nome)
        request.app.state.prefs = preferencias.carregar(db)
    t = token_paperless(request, admin)
    if not t:
        flash(request, "Guarda o teu token do Paperless em Perfil.", "erro")
        return RedirectResponse("/importar/validar#paperless", status_code=303)
    try:
        r = extratos.ler_paperless(db, request.app.state.defs.data_dir, t, nome)
    except paperless.ErroPaperless as e:
        db.rollback()
        flash(request, str(e), "erro")
        return RedirectResponse("/importar/validar#paperless", status_code=303)
    db.commit()
    if r.mensagem:
        flash(request, r.mensagem, "erro")
    else:
        n = len(r.novos)
        msg = f"Paperless lido: {n} extrato{'s' if n != 1 else ''} novo{'s' if n != 1 else ''} por validar"
        if r.erros:
            msg += f", {len(r.erros)} com erro"
        if r.ligados:
            msg += f", {r.ligados} já importado{'s' if r.ligados != 1 else ''} por outra via (ligado{'s' if r.ligados != 1 else ''} ao documento)"
        if r.ignorados:
            msg += f", {r.ignorados} já conhecido{'s' if r.ignorados != 1 else ''}"
        flash(request, msg + ".")
    return RedirectResponse("/importar/validar#paperless", status_code=303)


@router.post("/paperless/ligar")
async def paperless_ligar(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    """Importações já feitas: procura o mesmo ficheiro no Paperless (MD5) para ligar os movimentos ao documento."""
    from .. import extratos, paperless
    from .documentos import token as token_paperless
    await ler_form(request)
    t = token_paperless(request, admin)
    if not t:
        flash(request, "Guarda o teu token do Paperless em Perfil.", "erro")
        return RedirectResponse("/importar/validar#paperless", status_code=303)
    try:
        n = extratos.ligar_existentes(db, request.app.state.defs.data_dir, t)
    except paperless.ErroPaperless as e:
        db.rollback()
        flash(request, str(e), "erro")
        return RedirectResponse("/importar/validar#paperless", status_code=303)
    db.commit()
    flash(request, f"{n} importaç{'ão ligada' if n == 1 else 'ões ligadas'} ao documento do Paperless.")
    return RedirectResponse("/importar/validar#paperless", status_code=303)


@router.post("/pasta/validar-todos")
async def pasta_validar_todos(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    """Valida de uma vez os extratos com conta detetada e sem avisos (ex.: o histórico mensal em PDF)."""
    from .. import extratos
    from .documentos import token as token_paperless
    form = await ler_form(request)
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()] if hasattr(form, "getlist") else []
    r = extratos.validar_todos(db, admin, request.app.state.defs, token_paperless(request, admin), ids or None)
    auditar(db, admin, "pasta_validar_todos", f"{len(r.validados)} validados, {r.movimentos} movimentos novos, "
                                              f"{len(r.saltados)} por validar à mão")
    db.commit()
    msg = (f"{len(r.validados)} extrato{'s' if len(r.validados) != 1 else ''} validado{'s' if len(r.validados) != 1 else ''}: "
           f"{r.movimentos} movimentos novos.")
    if r.saltados:
        msg += (f" {len(r.saltados)} ficaram por validar à mão — ex.: «{r.saltados[0][0].ficheiro}»: {r.saltados[0][1]}.")
    flash(request, msg, "erro" if r.saltados and not r.validados else "ok")
    return RedirectResponse("/importar/validar#pasta", status_code=303)


@router.post("/pasta/intervalo")
async def pasta_intervalo(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    from .. import pasta_importacao as pi
    form = await ler_form(request)
    v = str(form.get("minutos", ""))
    if v.isdigit() and int(v) in pi.INTERVALOS:
        preferencias.gravar(db, "pasta_intervalo_minutos", v)
        db.commit()
        request.app.state.prefs = preferencias.carregar(db)
        flash(request, f"A pasta passa a ser lida a cada {pi.INTERVALOS[int(v)]} (a partir da próxima leitura).")
    return RedirectResponse("/importar/validar", status_code=303)


@router.get("/pasta/{pid}")
def pasta_validar(request: Request, pid: int, conta: str = "", admin: Utilizador = Depends(exigir_admin),
                  db: Session = Depends(get_db)):
    """Pré-visualização de um extrato da pasta (com a conta detetada, ou a escolhida) para validar."""
    from .. import pasta_importacao as pi
    from ..models import Conta, ImportacaoPendente
    from .importar import mostrar_previa
    p = db.get(ImportacaoPendente, pid)
    if not p or p.estado != "pendente":
        flash(request, "Este extrato já não está por validar.", "erro")
        return RedirectResponse("/importar/validar", status_code=303)
    c = db.get(Conta, int(conta)) if conta.isdigit() else (db.get(Conta, p.conta_id) if p.conta_id else None)
    if c is None:
        c = pi.sugerir_conta(db, p, request.app.state.defs.data_dir) or             db.scalars(select(Conta).where(Conta.ativa).order_by(Conta.entidade_id, Conta.nome)).first()
        if c is None:
            flash(request, "Ainda não há contas.", "erro")
            return RedirectResponse("/importar/validar", status_code=303)
    try:
        nome, conteudo = pi.extrato_de(p, request.app.state.defs.data_dir)
    except FileNotFoundError:
        flash(request, "O conteúdo deste extrato já não está guardado. Rejeita-o e volta a ler a pasta.", "erro")
        return RedirectResponse("/importar/validar", status_code=303)
    return mostrar_previa(request, db, admin, c, nome, conteudo, pendente=p)


@router.post("/pasta/{pid}/rejeitar")
async def pasta_rejeitar(request: Request, pid: int, admin: Utilizador = Depends(exigir_admin),
                         db: Session = Depends(get_db)):
    from ..models import ImportacaoPendente
    await ler_form(request)
    p = db.get(ImportacaoPendente, pid)
    if not p or p.estado not in ("pendente", "erro"):
        raise HTTPException(status_code=404, detail="Não encontrado.")
    p.estado, p.tratado_em, p.tratado_por = "rejeitada", datetime.now(), admin.id
    auditar(db, admin, "pasta_rejeitada", p.caminho)
    db.commit()
    flash(request, f"«{p.ficheiro}» rejeitado: não é importado e não volta a ser proposto (o ficheiro fica na pasta).")
    return RedirectResponse("/importar/validar", status_code=303)


@router.post("/fuelio")
async def tarefa_fuelio(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    from ..models import Bem
    from .patrimonio import importar_fuelio_bem
    form = await ler_form(request)
    bid = str(form.get("bem_id", ""))
    b = db.get(Bem, int(bid)) if bid.isdigit() else None
    f = form.get("ficheiro")
    if not b or b.tipo != "carro" or f is None or not getattr(f, "filename", ""):
        flash(request, "Escolhe o carro e o ficheiro do Fuelio.", "erro")
        return RedirectResponse("/admin/fuelio", status_code=303)
    msg, erro = importar_fuelio_bem(db, b, f.filename, await f.read(5 * 1024 * 1024 + 1), admin)
    flash(request, msg, "erro" if erro else "ok")
    return RedirectResponse("/admin/fuelio", status_code=303)


@router.post("/backup")
async def backup_agora(request: Request, admin: Utilizador = Depends(exigir_admin),
                       db: Session = Depends(get_db)):
    await ler_form(request)
    destino = fazer_backup(request.app.state.defs)
    auditar(db, admin, "backup_manual", destino.name)
    db.commit()
    flash(request, f"Backup criado: {destino.name}")
    return RedirectResponse("/admin/backups", status_code=303)


@router.post("/pacote")
async def pacote_agora(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    """Gera já o pacote cifrado para a cloud (normalmente é feito uma vez por dia, sozinho)."""
    import asyncio
    from ..main import VERSAO
    await ler_form(request)
    try:
        p = await asyncio.to_thread(bk.fazer_pacote, request.app.state.defs, VERSAO)
    except Exception as e:                                   # ErroBackup, disco cheio, permissões…
        flash(request, f"O pacote não foi criado: {e}", "erro")
        return RedirectResponse("/admin/backups#pacote", status_code=303)
    auditar(db, admin, "pacote_cloud", f"{p.caminho.name} ({p.ficheiros} ficheiros)")
    db.commit()
    flash(request, f"Pacote criado e verificado: {p.caminho.name}.")
    return RedirectResponse("/admin/backups#pacote", status_code=303)


@router.get("/desempenho")
async def desempenho_pagina(request: Request, admin: Utilizador = Depends(exigir_admin)):
    """Painel de desempenho: tempos das páginas e queries lentas desde o arranque, e diagnóstico da base de dados."""
    import asyncio
    from .. import desempenho
    diag = await asyncio.to_thread(desempenho.diagnosticar, request.app.state.defs.db_path)
    return render(request, "admin/desempenho.html", m=request.app.state.metricas.resumo(), d=diag,
                  limite_sql=desempenho.LIMITE_SQL_MS)


@router.post("/desempenho")
async def desempenho_acao(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    import asyncio
    from .. import desempenho
    form = await ler_form(request)
    acao = str(form.get("acao", ""))
    defs = request.app.state.defs
    if acao == "recomecar":
        request.app.state.metricas.recomecar()
        flash(request, "Medição recomeçada.")
        return RedirectResponse("/admin/desempenho", status_code=303)
    if acao not in ("analyze", "checkpoint", "indices", "vacuum"):
        raise HTTPException(status_code=400, detail="Ação desconhecida.")
    try:
        if acao == "vacuum":                # reescreve o ficheiro todo: primeiro uma cópia verificada
            await asyncio.to_thread(fazer_backup, defs)
        msg = await asyncio.to_thread(desempenho.executar, acao, defs.db_path, db.get_bind())
    except Exception as e:                  # base ocupada, disco cheio…
        flash(request, f"A ação não foi feita: {e}", "erro")
        return RedirectResponse("/admin/desempenho", status_code=303)
    auditar(db, admin, "base_" + acao, msg[:200])
    db.commit()
    flash(request, msg + (" Backup feito antes." if acao == "vacuum" else ""))
    return RedirectResponse("/admin/desempenho", status_code=303)


@router.post("/verificar-base")
async def verificar_base(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    import asyncio
    await ler_form(request)
    res = await asyncio.to_thread(bk.verificar_base, request.app.state.defs)
    auditar(db, admin, "verificar_base", res[:200])
    db.commit()
    ok = res == "ok"
    flash(request, "Base de dados verificada: sem problemas." if ok else f"A verificação encontrou problemas: {res}",
          "ok" if ok else "erro")
    return RedirectResponse("/admin/backups", status_code=303)


def _404():
    raise HTTPException(status_code=404, detail="Não encontrado.")
