"""Administração: utilizadores, permissões por entidade e entidades."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import preferencias
from ..backup import fazer_backup, ultimo_backup
from ..deps import auditar, exigir_admin, flash, get_db, render
from ..models import MODULOS, PAPEIS, PAPEIS_ROTULO, TIPOS_ENTIDADE, Auditoria, Entidade, Permissao, Utilizador
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
def utilizadores(request: Request, periodo: str = "", admin: Utilizador = Depends(exigir_admin),
                 db: Session = Depends(get_db)):
    lista = db.scalars(select(Utilizador).options(selectinload(Utilizador.permissoes))
                       .order_by(Utilizador.nome)).all()
    q = select(Auditoria, Utilizador.nome).outerjoin(Utilizador).order_by(Auditoria.id.desc())
    if periodo in PERIODOS_ATIVIDADE:
        q = q.where(Auditoria.quando >= datetime.now() - PERIODOS_ATIVIDADE[periodo][1]).limit(500)
    else:
        periodo = ""
        q = q.limit(30)
    eventos = db.execute(q).all()
    return render(request, "admin/utilizadores.html", lista=lista, entidades=_entidades(db),
                  rotulos=PAPEIS_ROTULO, eventos=eventos, prefs=request.app.state.prefs, periodo=periodo,
                  periodos_atividade={k: r for k, (r, _) in PERIODOS_ATIVIDADE.items()},
                  ultimo_backup=ultimo_backup(request.app.state.defs))


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


@router.get("/utilizadores/{uid}")
def editar_form(request: Request, uid: int, admin: Utilizador = Depends(exigir_admin),
                db: Session = Depends(get_db)):
    alvo = db.get(Utilizador, uid) or _404()
    papeis = {p.entidade_id: p.papel for p in alvo.permissoes}
    v = {"nome": alvo.nome, "login": alvo.login, "email": alvo.email or "", "is_admin": alvo.is_admin,
         "ativo": alvo.ativo}
    return render(request, "admin/utilizador_form.html", alvo=alvo, entidades=_entidades(db), papeis=papeis,
                  rotulos=PAPEIS_ROTULO, v=v)


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
        dados["modulos"] = ",".join(MODULOS)        # nova entidade: tudo ligado (desliga-se em Editar)
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
    return {"nome": v["nome"][:100], "descricao": v["descricao"] or None, "tipo": tipo, "quota": quota,
            "quota_futura": futura, "quota_futura_nota": (v["quota_futura_nota"][:100] or None) if futura else None,
            **({"modulos": ",".join(modulos)} if modulos is not None else {})}, erros


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
         "modulos": (e.modulos or "").split(",")}
    return render(request, "admin/entidade_form.html", e=e, v=v, tipos=TIPOS_ENTIDADE, modulos=MODULOS, erros=[])


@router.post("/entidades/{eid}")
async def editar_entidade(request: Request, eid: int, admin: Utilizador = Depends(exigir_admin),
                          db: Session = Depends(get_db)):
    e = db.get(Entidade, eid) or _404()
    form = await ler_form(request)
    dados, erros = _ler_entidade(form, db, e)
    if erros:
        v = {k: str(form.get(k, "")) for k in ("nome", "descricao", "tipo", "quota", "quota_futura", "quota_futura_nota")}
        v["modulos"] = form.getlist("modulos")
        return render(request, "admin/entidade_form.html", status_code=400, e=e, v=v, tipos=TIPOS_ENTIDADE,
                      modulos=MODULOS, erros=erros)
    antes = f"{e.nome} ({e.tipo}{', %g%%' % e.quota if e.quota else ''})"
    for k, val in dados.items():
        setattr(e, k, val)
    auditar(db, admin, "entidade_editada", f"{e.id}: {antes} → {e.nome} ({e.tipo}{', %g%%' % e.quota if e.quota else ''}"
                                           f"{', futura %g%%' % e.quota_futura if e.quota_futura else ''}; módulos {e.modulos})")
    db.commit()
    flash(request, f"Entidade «{e.nome}» guardada.")
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
    dias = {}
    for chave in ("tarefas_dia_ini", "tarefas_dia_fim"):
        v = str(form.get(chave, "")).strip()
        if v:
            if not (v.isdigit() and 1 <= int(v) <= 31):
                flash(request, "Os dias do lembrete têm de estar entre 1 e 31.", "erro")
                return RedirectResponse("/admin/utilizadores#preferencias", status_code=303)
            preferencias.gravar(db, chave, str(int(v)))
            dias[chave] = int(v)
    auditar(db, admin, "preferencias", f"mostrar_iban_completo={mostrar} tarefas_ativas={ativas} avaliacao_automatica={avaliacao} mostrar_minha_parte={minha_parte} {dias}")
    db.commit()
    request.app.state.prefs = preferencias.carregar(db)
    flash(request, "Preferências guardadas.")
    return RedirectResponse("/admin/utilizadores#preferencias", status_code=303)


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
def recheio_form(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    from ..models import Categoria
    from ..qualificacao import arvore_categorias
    return render(request, "admin/recheio.html", categorias=[c for c in arvore_categorias(db) if c.natureza == "DESPESA"],
                  garantia=request.app.state.prefs.get("recheio_garantia_meses", "36"),
                  valor_minimo=request.app.state.prefs.get("recheio_valor_minimo", "50"))


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
    return RedirectResponse("/admin/recheio", status_code=303)


@router.get("/tarefas")
def tarefas(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    """Tarefas administrativas pontuais (ex.: importar o histórico do Fuelio)."""
    from ..models import Abastecimento, Bem
    carros = db.scalars(select(Bem).where(Bem.tipo == "carro").order_by(Bem.entidade_id, Bem.nome)).all()
    n = dict(db.execute(select(Abastecimento.bem_id, func.count()).group_by(Abastecimento.bem_id)).all())
    from .. import pasta_importacao as pi
    from ..models import ImportacaoPendente
    defs = request.app.state.defs
    pasta_ok, pasta_msg = pi.estado_pasta(defs.import_dir)
    historico = db.scalars(select(ImportacaoPendente).where(ImportacaoPendente.estado.in_(("validada", "rejeitada")))
                           .order_by(ImportacaoPendente.tratado_em.desc()).limit(10)).all()
    return render(request, "admin/tarefas.html", carros=carros, n_abast=n,
                  entidades={e.id: e for e in _entidades(db)}, pasta_ok=pasta_ok, pasta_msg=pasta_msg,
                  pendentes=pi.pendentes(db), historico=historico, intervalos=pi.INTERVALOS,
                  intervalo=pi.intervalo_minutos(request.app.state.prefs))


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
    return RedirectResponse("/admin/tarefas#pasta", status_code=303)


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
    return RedirectResponse("/admin/tarefas#pasta", status_code=303)


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
        return RedirectResponse("/admin/tarefas#pasta", status_code=303)
    c = db.get(Conta, int(conta)) if conta.isdigit() else (db.get(Conta, p.conta_id) if p.conta_id else None)
    if c is None:
        c = db.scalars(select(Conta).where(Conta.ativa).order_by(Conta.entidade_id, Conta.nome)).first()
        if c is None:
            flash(request, "Ainda não há contas.", "erro")
            return RedirectResponse("/admin/tarefas#pasta", status_code=303)
    try:
        nome, conteudo = pi.extrato_de(p, request.app.state.defs.data_dir)
    except FileNotFoundError:
        flash(request, "O conteúdo deste extrato já não está guardado. Rejeita-o e volta a ler a pasta.", "erro")
        return RedirectResponse("/admin/tarefas#pasta", status_code=303)
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
    return RedirectResponse("/admin/tarefas#pasta", status_code=303)


@router.post("/tarefas/fuelio")
async def tarefa_fuelio(request: Request, admin: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    from ..models import Bem
    from .patrimonio import importar_fuelio_bem
    form = await ler_form(request)
    bid = str(form.get("bem_id", ""))
    b = db.get(Bem, int(bid)) if bid.isdigit() else None
    f = form.get("ficheiro")
    if not b or b.tipo != "carro" or f is None or not getattr(f, "filename", ""):
        flash(request, "Escolhe o carro e o ficheiro do Fuelio.", "erro")
        return RedirectResponse("/admin/tarefas", status_code=303)
    msg, erro = importar_fuelio_bem(db, b, f.filename, await f.read(5 * 1024 * 1024 + 1), admin)
    flash(request, msg, "erro" if erro else "ok")
    return RedirectResponse("/admin/tarefas", status_code=303)


@router.post("/backup")
async def backup_agora(request: Request, admin: Utilizador = Depends(exigir_admin),
                       db: Session = Depends(get_db)):
    await ler_form(request)
    destino = fazer_backup(request.app.state.defs)
    auditar(db, admin, "backup_manual", destino.name)
    db.commit()
    flash(request, f"Backup criado: {destino.name}")
    return RedirectResponse("/admin/utilizadores", status_code=303)


def _404():
    raise HTTPException(status_code=404, detail="Não encontrado.")
