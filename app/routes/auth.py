"""Login, logout, configuração inicial (1.º administrador) e perfil."""
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import auditar, flash, get_db, render, utilizador_atual
from ..models import Entidade, Permissao, Utilizador
from ..seguranca import Bloqueios, hash_password, ler_form, validar_password_nova, verificar_password

__all__ = ["router", "Bloqueios"]

router = APIRouter()


def _ha_utilizadores(db: Session) -> bool:
    return bool(db.scalar(select(func.count()).select_from(Utilizador)))


def _destino_seguro(nxt: str | None) -> str:
    # Só caminhos locais — evita redirecionamentos para sites externos.
    if not nxt or nxt == "/":
        return "/inicio"                  # página de entrada
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/inicio"


def _iniciar_sessao(request: Request, u: Utilizador) -> None:
    request.session.clear()
    request.session["uid"] = u.id
    request.session["ver"] = u.versao_sessao


@router.get("/login")
def login_form(request: Request, next: str = "/", db: Session = Depends(get_db)):
    if not _ha_utilizadores(db):
        return RedirectResponse("/setup", status_code=303)
    return render(request, "login.html", next=_destino_seguro(next))


@router.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    form = await ler_form(request)
    login_txt = str(form.get("login", "")).strip()
    nxt = _destino_seguro(str(form.get("next", "/")))
    bloqueios: Bloqueios = request.app.state.bloqueios

    restante = bloqueios.segundos_restantes(login_txt)
    if restante:
        return render(request, "login.html", status_code=429, next=nxt, login=login_txt,
                      erro=f"Demasiadas tentativas falhadas. Tenta de novo daqui a {restante // 60 + 1} min.")

    u = db.scalar(select(Utilizador).where(func.lower(Utilizador.login) == login_txt.lower()))
    if not verificar_password(u.password_hash if u else None, str(form.get("password", ""))) or not u.ativo:
        bloqueios.falha(login_txt)
        auditar(db, u, "login_falhado", login_txt)
        db.commit()
        return render(request, "login.html", status_code=401, next=nxt, login=login_txt,
                      erro="Utilizador ou password incorretos.")

    bloqueios.sucesso(login_txt)
    u.ultimo_login = datetime.now()
    auditar(db, u, "login")
    db.commit()
    _iniciar_sessao(request, u)
    return RedirectResponse(nxt, status_code=303)


@router.post("/logout")
async def logout(request: Request):
    await ler_form(request)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/setup")
def setup_form(request: Request, db: Session = Depends(get_db)):
    if _ha_utilizadores(db):
        return RedirectResponse("/login", status_code=303)
    return render(request, "setup.html")


@router.post("/setup")
async def setup(request: Request, db: Session = Depends(get_db)):
    """Cria o 1.º administrador, com papel 'admin' em todas as entidades.
    Só funciona enquanto não existir nenhum utilizador."""
    if _ha_utilizadores(db):
        return RedirectResponse("/login", status_code=303)
    form = await ler_form(request)
    nome = str(form.get("nome", "")).strip()
    login_txt = str(form.get("login", "")).strip()
    erro = None
    if not nome or not login_txt:
        erro = "Nome e utilizador são obrigatórios."
    else:
        erro = validar_password_nova(str(form.get("password", "")), str(form.get("password2", "")))
    if erro:
        return render(request, "setup.html", status_code=400, erro=erro, nome=nome, login=login_txt)

    u = Utilizador(nome=nome, login=login_txt, email=str(form.get("email", "")).strip() or None,
                   password_hash=hash_password(str(form["password"])), is_admin=True)
    db.add(u)
    db.flush()
    for e in db.scalars(select(Entidade)):
        db.add(Permissao(utilizador_id=u.id, entidade_id=e.id, papel="admin"))
    auditar(db, u, "setup", "1.º administrador criado")
    db.commit()
    _iniciar_sessao(request, u)
    flash(request, f"Bem-vindo, {u.nome}. Começa por criar as tuas contas.")
    return RedirectResponse("/contas", status_code=303)


def _paperless_ctx(request: Request, u: Utilizador) -> dict:
    from .. import paperless
    segredo = request.app.state.defs.secret_key
    pessoal = bool(paperless.decifrar(u.paperless_token, segredo))
    return {"paperless_cfg": paperless.configurado(), "paperless_pessoal": pessoal,
            "paperless_partilhado": not pessoal and bool(paperless.token_de(u, segredo)),
            "paperless_link": paperless.url_browser()}


def _immich_ctx(request: Request, u: Utilizador) -> dict:
    from .. import immich
    segredo = request.app.state.defs.secret_key
    pessoal = bool(immich.decifrar(u.immich_token, segredo))
    return {"immich_cfg": immich.configurado(), "immich_pessoal": pessoal,
            "immich_partilhado": not pessoal and bool(immich.chave_de(u, segredo)), "immich_link": immich.url_browser()}


@router.get("/perfil")
def perfil_form(request: Request, u: Utilizador = Depends(utilizador_atual)):
    return render(request, "perfil.html", **_paperless_ctx(request, u), **_immich_ctx(request, u))


@router.post("/perfil/immich")
async def perfil_immich(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Chave pessoal da API do Immich: guardar (depois de a testar), testar ou remover."""
    from .. import immich
    form = await ler_form(request)
    acao, segredo = str(form.get("acao", "")), request.app.state.defs.secret_key
    if acao == "remover":
        u.immich_token = None
        auditar(db, u, "immich_token_removido")
        db.commit()
        flash(request, "Chave do Immich removida.")
    elif acao == "testar":
        c = immich.chave_de(u, segredo)
        try:
            flash(request, immich.testar(c) if c else "Ainda não guardaste a tua chave.", "ok" if c else "erro")
        except immich.ErroImmich as e:
            flash(request, f"Não foi possível: {e}", "erro")
    else:
        chave = str(form.get("token", "")).strip()
        if not chave:
            flash(request, "Cola a chave da API do Immich.", "erro")
            return RedirectResponse("/perfil#immich", status_code=303)
        try:
            msg = immich.testar(chave)
        except immich.ErroImmich as e:
            if "401" in str(e) or "403" in str(e):
                flash(request, f"Chave recusada pelo Immich ({e}). Não foi guardada.", "erro")
                return RedirectResponse("/perfil#immich", status_code=303)
            msg = f"Guardada, mas não foi possível confirmar agora: {e}"
        u.immich_token = immich.cifrar(chave, segredo)
        auditar(db, u, "immich_token_guardado")
        db.commit()
        flash(request, "Chave do Immich guardada. " + msg)
    return RedirectResponse("/perfil#immich", status_code=303)


@router.post("/perfil/paperless")
async def perfil_paperless(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Token pessoal do Paperless: guardar (depois de o testar), testar ou remover."""
    from .. import paperless
    form = await ler_form(request)
    acao, segredo = str(form.get("acao", "")), request.app.state.defs.secret_key
    if acao == "remover":
        u.paperless_token = None
        auditar(db, u, "paperless_token_removido")
        db.commit()
        flash(request, "Token do Paperless removido.")
    elif acao == "testar":
        t = paperless.token_de(u, segredo)
        try:
            flash(request, paperless.testar(t) if t else "Ainda não guardaste o teu token.", "ok" if t else "erro")
        except paperless.ErroPaperless as e:
            flash(request, f"Não foi possível: {e}", "erro")
    else:
        token = str(form.get("token", "")).strip()
        if not token:
            flash(request, "Cola o token da API do Paperless.", "erro")
            return RedirectResponse("/perfil#paperless", status_code=303)
        try:
            msg = paperless.testar(token)
        except paperless.ErroPaperless as e:
            if "401" in str(e) or "403" in str(e):
                flash(request, f"Token recusado pelo Paperless ({e}). Não foi guardado.", "erro")
                return RedirectResponse("/perfil#paperless", status_code=303)
            msg = f"Guardado, mas não foi possível confirmar agora: {e}"
        u.paperless_token = paperless.cifrar(token, segredo)
        auditar(db, u, "paperless_token_guardado")
        db.commit()
        flash(request, "Token do Paperless guardado. " + msg)
    return RedirectResponse("/perfil#paperless", status_code=303)


@router.post("/perfil")
async def perfil(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    if not verificar_password(u.password_hash, str(form.get("atual", ""))):
        return render(request, "perfil.html", status_code=400, erro="A password atual está errada.", **_paperless_ctx(request, u), **_immich_ctx(request, u))
    erro = validar_password_nova(str(form.get("password", "")), str(form.get("password2", "")))
    if erro:
        return render(request, "perfil.html", status_code=400, erro=erro, **_paperless_ctx(request, u), **_immich_ctx(request, u))
    u.password_hash = hash_password(str(form["password"]))
    u.versao_sessao += 1  # termina as sessões noutros dispositivos
    auditar(db, u, "password_alterada")
    db.commit()
    _iniciar_sessao(request, u)
    flash(request, "Password alterada. As outras sessões foram terminadas.")
    return RedirectResponse("/perfil", status_code=303)
