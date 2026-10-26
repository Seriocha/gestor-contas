"""Dependências partilhadas pelas rotas: sessão de BD, utilizador atual,
permissões por entidade e renderização de páginas."""
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from pydantic import BeforeValidator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import PAPEIS, Auditoria, Entidade, Permissao, Utilizador
from .seguranca import csrf_token


class NaoAutenticado(Exception):
    pass


def _vazio_para_none(v):
    return None if isinstance(v, str) and not v.strip() else v


# Parâmetro inteiro opcional vindo de um formulário GET: um <select> em
# "Todas" envia "conta=" (texto vazio), que deve valer como "sem filtro".
IntOpcional = Annotated[int | None, BeforeValidator(_vazio_para_none)]


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


def entidade_escolhida(request: Request, db: Session, u: Utilizador,
                       entidade_id: int | None) -> tuple[list[tuple[Entidade, str]], Entidade | None, str | None]:
    """Entidade pedida (ou a última usada, guardada na sessão, ou a 1.ª
    visível). Devolve (visíveis, entidade, papel)."""
    visiveis = entidades_visiveis(db, u)
    por_id = {e.id: (e, p) for e, p in visiveis}
    escolha = entidade_id or request.session.get("entidade")
    if escolha not in por_id:
        if entidade_id:
            raise HTTPException(status_code=404, detail="Não encontrado.")
        escolha = visiveis[0][0].id if visiveis else None
    if escolha is None:
        return visiveis, None, None
    request.session["entidade"] = escolha
    return visiveis, *por_id[escolha]


def pode_editar_algo(db: Session, u: Utilizador) -> bool:
    return any(PAPEIS[p] >= PAPEIS["editar"] for _, p in entidades_visiveis(db, u))


def exigir_editor(u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)) -> Utilizador:
    """Categorias, etiquetas e regras são partilhadas: basta 'editar' numa entidade."""
    if not pode_editar_algo(db, u):
        raise HTTPException(status_code=403, detail="Sem permissão para esta operação.")
    return u


def auditar(db: Session, u: Utilizador | None, acao: str, detalhe: str = "") -> None:
    db.add(Auditoria(utilizador_id=u.id if u else None, acao=acao, detalhe=detalhe))


def flash(request: Request, mensagem: str, tipo: str = "ok") -> None:
    request.session.setdefault("flash", []).append([tipo, mensagem])


def por_validar(db: Session, u: Utilizador) -> int:
    """O que espera validação (contador no menu «Importar»): extratos da pasta da NAS (administradores) e
    abastecimentos registados na página sem login, das entidades que o utilizador vê."""
    from .models import AbastecimentoPendente, Bem, ImportacaoPendente
    n = 0
    if u.is_admin:
        n += db.scalar(select(func.count()).select_from(ImportacaoPendente).where(ImportacaoPendente.estado == "pendente")) or 0
    ids = [e.id for e, _ in entidades_visiveis(db, u)]
    if ids:
        n += db.scalar(select(func.count()).select_from(AbastecimentoPendente).join(Bem).where(Bem.entidade_id.in_(ids))) or 0
    return n


def render(request: Request, template: str, status_code: int = 200, **ctx):
    ctx.setdefault("utilizador", getattr(request.state, "utilizador", None))
    if ctx["utilizador"] is not None and "n_por_validar" not in ctx:
        try:
            with request.app.state.SessionLocal() as db:
                ctx["n_por_validar"] = por_validar(db, ctx["utilizador"])
                # o menu «Lar (Santa Casa)» só aparece a quem vê uma entidade com o módulo (as heranças)
                ctx["menu_lar"] = any(e.tem("lar") for e, _ in entidades_visiveis(db, ctx["utilizador"]))
        except Exception:          # o contador nunca impede a página de abrir
            ctx["n_por_validar"] = 0
    ctx["flashes"] = request.session.pop("flash", [])
    ctx["csrf_token"] = csrf_token(request)
    return request.app.state.templates.TemplateResponse(request, template, ctx, status_code=status_code)
