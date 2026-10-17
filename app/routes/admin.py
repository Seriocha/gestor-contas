"""Administração: utilizadores, permissões por entidade e entidades."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import preferencias
from ..backup import fazer_backup, ultimo_backup
from ..deps import auditar, exigir_admin, flash, get_db, render
from ..models import PAPEIS, PAPEIS_ROTULO, Auditoria, Entidade, Permissao, Utilizador
from ..seguranca import hash_password, ler_form, validar_password_nova

router = APIRouter(prefix="/admin")

# Decisão por omissão (DESIGN.md §14): novos utilizadores editam a Família
# e não veem a entidade Mãe.
PAPEL_OMISSAO = {"Família": "editar"}


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
    eventos = db.execute(select(Auditoria, Utilizador.nome).outerjoin(Utilizador)
                         .order_by(Auditoria.id.desc()).limit(30)).all()
    return render(request, "admin/utilizadores.html", lista=lista, entidades=_entidades(db),
                  rotulos=PAPEIS_ROTULO, eventos=eventos, prefs=request.app.state.prefs,
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
    return render(request, "admin/entidades.html", entidades=_entidades(db))


@router.post("/entidades")
async def nova_entidade(request: Request, admin: Utilizador = Depends(exigir_admin),
                        db: Session = Depends(get_db)):
    form = await ler_form(request)
    nome = str(form.get("nome", "")).strip()
    if not nome or db.scalar(select(Entidade).where(func.lower(Entidade.nome) == nome.lower())):
        flash(request, "Nome em falta ou já existente.", "erro")
        return RedirectResponse("/admin/entidades", status_code=303)
    e = Entidade(nome=nome, descricao=str(form.get("descricao", "")).strip() or None,
                 ordem=(db.scalar(select(func.max(Entidade.ordem))) or 0) + 1)
    db.add(e)
    db.flush()
    # Quem cria fica a administrar; os restantes acessos dão-se em Utilizadores.
    db.add(Permissao(utilizador_id=admin.id, entidade_id=e.id, papel="admin"))
    auditar(db, admin, "entidade_criada", f"{e.id}: {e.nome}")
    db.commit()
    flash(request, f"Entidade «{e.nome}» criada.")
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
    dias = {}
    for chave in ("tarefas_dia_ini", "tarefas_dia_fim"):
        v = str(form.get(chave, "")).strip()
        if v:
            if not (v.isdigit() and 1 <= int(v) <= 31):
                flash(request, "Os dias do lembrete têm de estar entre 1 e 31.", "erro")
                return RedirectResponse("/admin/utilizadores#preferencias", status_code=303)
            preferencias.gravar(db, chave, str(int(v)))
            dias[chave] = int(v)
    auditar(db, admin, "preferencias", f"mostrar_iban_completo={mostrar} tarefas_ativas={ativas} avaliacao_automatica={avaliacao} {dias}")
    db.commit()
    request.app.state.prefs = preferencias.carregar(db)
    flash(request, "Preferências guardadas.")
    return RedirectResponse("/admin/utilizadores#preferencias", status_code=303)


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
