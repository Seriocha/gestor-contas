"""Dependências partilhadas pelas rotas: sessão de BD, utilizador atual,
permissões por entidade e renderização de páginas."""
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import PAPEIS, Auditoria, Entidade, Permissao, Utilizador
from .seguranca import csrf_token


class NaoAutenticado(Exception):
    pass


def get_db(request: Request):
    db = request.app.state.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def utilizador_atual(request: Request, db: Session = Depends(get_db)) -> Utilizador:
    uid = request.session.get("uid")
    u = db.get(Utilizador, uid) if uid else None
    if not u or not u.ativo or u.versao_sessao != request.session.get("ver"):
        request.session.pop("uid", None)
        request.session.pop("ver", None)
        raise NaoAutenticado()
    request.state.utilizador = u
    return u


def exigir_admin(u: Utilizador = Depends(utilizador_atual)) -> Utilizador:
    if not u.is_admin:
        raise HTTPException(status_code=403, detail="Só administradores.")
    return u


def entidades_visiveis(db: Session, u: Utilizador) -> list[tuple[Entidade, str]]:
    linhas = db.execute(
        select(Entidade, Permissao.papel)
        .join(Permissao, Permissao.entidade_id == Entidade.id)
        .where(Permissao.utilizador_id == u.id)
        .order_by(Entidade.ordem, Entidade.nome)
    ).all()
    return [(e, p) for e, p in linhas]


def papel_em(db: Session, u: Utilizador, entidade_id: int) -> str | None:
    p = db.get(Permissao, (u.id, entidade_id))
    return p.papel if p else None


def exigir_papel(db: Session, u: Utilizador, entidade_id: int, minimo: str) -> str:
    """404 quando o utilizador nem sabe que a entidade existe; 403 quando a vê
    mas não tem papel suficiente."""
    papel = papel_em(db, u, entidade_id)
    if papel is None:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    if PAPEIS[papel] < PAPEIS[minimo]:
        raise HTTPException(status_code=403, detail="Sem permissão para esta operação.")
    return papel


def auditar(db: Session, u: Utilizador | None, acao: str, detalhe: str = "") -> None:
    db.add(Auditoria(utilizador_id=u.id if u else None, acao=acao, detalhe=detalhe))


def flash(request: Request, mensagem: str, tipo: str = "ok") -> None:
    request.session.setdefault("flash", []).append([tipo, mensagem])


def render(request: Request, template: str, status_code: int = 200, **ctx):
    ctx.setdefault("utilizador", getattr(request.state, "utilizador", None))
    ctx["flashes"] = request.session.pop("flash", [])
    ctx["csrf_token"] = csrf_token(request)
    return request.app.state.templates.TemplateResponse(request, template, ctx, status_code=status_code)
