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


@router.get("/perfil")
def perfil_form(request: Request, u: Utilizador = Depends(utilizador_atual)):
    return render(request, "perfil.html")


@router.post("/perfil")
async def perfil(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    if not verificar_password(u.password_hash, str(form.get("atual", ""))):
        return render(request, "perfil.html", status_code=400, erro="A password atual está errada.")
    erro = validar_password_nova(str(form.get("password", "")), str(form.get("password2", "")))
    if erro:
        return render(request, "perfil.html", status_code=400, erro=erro)
    u.password_hash = hash_password(str(form["password"]))
    u.versao_sessao += 1  # termina as sessões noutros dispositivos
    auditar(db, u, "password_alterada")
    db.commit()
    _iniciar_sessao(request, u)
    flash(request, "Password alterada. As outras sessões foram terminadas.")
    return RedirectResponse("/perfil", status_code=303)
