"""Vista para a televisão (Samsung Tizen, Android TV / Vodafone Box): só de consulta, navegável com o comando.

A TV não usa a sessão normal: tem o seu próprio cookie, obtido por emparelhamento (ver app/tv.py), e só há rotas GET
de consulta — não consegue alterar nada. Aprovar e desligar televisões faz-se com sessão iniciada (Perfil › Televisões).
"""
import secrets
from datetime import date, datetime, timedelta

import segno
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import inicio
from .. import relatorios as rel
from .. import tv
from ..deps import auditar, entidades_visiveis, flash, get_db, render, utilizador_atual
from ..models import DispositivoTV, Entidade, Utilizador
from ..seguranca import ler_form

router = APIRouter()


class NaoEmparelhado(Exception):
    pass


def _definir_cookie(request: Request, resp, nome: str, valor: str, max_age: int) -> None:
    resp.set_cookie(nome, valor, max_age=max_age, httponly=True, samesite="lax",
                    secure=request.app.state.defs.cookie_secure, path="/tv")


def tv_atual(request: Request, db: Session = Depends(get_db)) -> tuple[DispositivoTV, list[Entidade]]:
    token = request.cookies.get(tv.COOKIE)
    d = db.scalar(select(DispositivoTV).where(DispositivoTV.token_hash == tv.hash_token(token))) if token else None
    if not d:
        raise NaoEmparelhado()
    u = d.utilizador
    if not u.ativo or u.versao_sessao != d.versao_sessao:
        raise NaoEmparelhado()        # password mudada ou utilizador desativado: emparelhar de novo
    escolhidas = set(d.ids_entidades)
    ents = [e for e, _ in entidades_visiveis(db, u) if e.id in escolhidas]   # permissões de hoje, não as de então
    if not ents:
        raise NaoEmparelhado()
    agora = datetime.now()
    if not d.ultimo_acesso or agora - d.ultimo_acesso > timedelta(minutes=5):
        d.ultimo_acesso = agora
        db.commit()
    return d, ents


def _pagina(request: Request, template: str, d: DispositivoTV, ents: list[Entidade], e: int | None, pagina: str, **ctx):
    ent = next((x for x in ents if x.id == e), ents[0])
    ctx.update(dispositivo=d, entidades=ents, ent=ent, pagina=pagina, hoje=date.today(), agora=datetime.now())
    if callable(ctx.get("dados")):
        ctx["dados"] = ctx["dados"](ent)
    resp = request.app.state.templates.TemplateResponse(request, template, ctx)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ páginas da TV
@router.get("/tv")
def resumo(request: Request, e: int | None = None, alvo=Depends(tv_atual), db: Session = Depends(get_db)):
    d, ents = alvo
    return _pagina(request, "tv/resumo.html", d, ents, e, "resumo", dados=lambda ent: tv.resumo(db, ent, date.today()))


@router.get("/tv/contas")
def contas(request: Request, e: int | None = None, alvo=Depends(tv_atual), db: Session = Depends(get_db)):
    d, ents = alvo
    return _pagina(request, "tv/contas.html", d, ents, e, "contas", dados=lambda ent: tv.contas(db, ent, date.today()))


@router.get("/tv/mes")
def mes(request: Request, e: int | None = None, m: str = "", alvo=Depends(tv_atual), db: Session = Depends(get_db)):
    d, ents = alvo
    atual = date.today().replace(day=1)
    try:
        ini = datetime.strptime(m, "%Y-%m").date() if m else atual
    except ValueError:
        ini = atual
    ini = min(ini, atual)
    nav = {"anterior": rel.somar_meses(ini, -1).strftime("%Y-%m"),
           "seguinte": rel.somar_meses(ini, 1).strftime("%Y-%m") if ini < atual else None, "m": ini.strftime("%Y-%m")}
    return _pagina(request, "tv/mes.html", d, ents, e, "mes", nav=nav, dados=lambda ent: tv.mes(db, ent, ini))


@router.get("/tv/agenda")
def agenda(request: Request, e: int | None = None, alvo=Depends(tv_atual), db: Session = Depends(get_db)):
    d, ents = alvo

    def dados(ent):
        proximos, _ = inicio.agenda(db, [ent], request.app.state.prefs, date.today(), dias=45)
        return proximos
    return _pagina(request, "tv/agenda.html", d, ents, e, "agenda", dados=dados)


@router.post("/tv/desligar")
def desligar_esta(request: Request, alvo=Depends(tv_atual), db: Session = Depends(get_db)):
    """Botão «Desligar esta TV» na própria televisão (o cookie samesite=lax não segue em POSTs de outros sites)."""
    d, _ = alvo
    auditar(db, d.utilizador, "tv_desligar", d.nome)
    db.delete(d)
    db.commit()
    resp = RedirectResponse("/tv/emparelhar", status_code=303)
    resp.delete_cookie(tv.COOKIE, path="/tv")
    return resp


# ------------------------------------------------------------------ emparelhar (lado da TV)
@router.get("/tv/emparelhar")
def emparelhar(request: Request, db: Session = Depends(get_db)):
    try:
        tv_atual(request, db)
        return RedirectResponse("/tv", status_code=303)
    except NaoEmparelhado:
        pass
    pedidos: tv.Emparelhamentos = request.app.state.emparelhamentos
    p = pedidos.por_segredo(request.cookies.get(tv.COOKIE_PEDIDO))
    if not p or p.token or p.expirado:
        p = pedidos.novo()
    url = f"{str(request.base_url).rstrip('/')}/tv/aprovar?codigo={p.codigo}"
    qr = segno.make(url, error="m").svg_inline(scale=6, border=2, dark="#0b0d12", light="#ffffff")
    resp = request.app.state.templates.TemplateResponse(request, "tv/emparelhar.html", {
        "codigo": p.codigo, "url_aprovar": f"{str(request.base_url).rstrip('/')}/tv/aprovar", "qr": qr,
        "minutos": tv.VALIDADE_PEDIDO // 60})
    resp.headers["Cache-Control"] = "no-store"
    _definir_cookie(request, resp, tv.COOKIE_PEDIDO, p.segredo, tv.VALIDADE_PEDIDO)
    resp.delete_cookie(tv.COOKIE, path="/tv")
    return resp


@router.get("/tv/emparelhar/estado")
def estado(request: Request):
    pedidos: tv.Emparelhamentos = request.app.state.emparelhamentos
    p = pedidos.por_segredo(request.cookies.get(tv.COOKIE_PEDIDO))
    if not p:
        return JSONResponse({"estado": "expirado"})
    if not p.token:
        return JSONResponse({"estado": "espera"})
    pedidos.remover(p)
    resp = JSONResponse({"estado": "aprovado"})
    _definir_cookie(request, resp, tv.COOKIE, p.token, tv.DURACAO_COOKIE)
    resp.delete_cookie(tv.COOKIE_PEDIDO, path="/tv")
    return resp


# ------------------------------------------------------------------ aprovar e gerir (com sessão iniciada)
def _form_aprovar(request: Request, db: Session, u: Utilizador, status_code: int = 200, **ctx):
    ctx.setdefault("codigo", "")
    ctx.setdefault("nome", "TV da sala")
    visiveis = entidades_visiveis(db, u)
    ctx.setdefault("marcadas", {e.id for e, _ in visiveis})
    return render(request, "tv/aprovar.html", status_code=status_code, visiveis=visiveis, **ctx)


@router.get("/tv/aprovar")
def aprovar_form(request: Request, codigo: str = "", u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    return _form_aprovar(request, db, u, codigo="".join(ch for ch in codigo if ch.isdigit())[:6])


@router.post("/tv/aprovar")
async def aprovar(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    codigo = str(form.get("codigo", "")).strip()
    nome = str(form.get("nome", "")).strip()[:60] or "Televisão"
    visiveis = {e.id for e, _ in entidades_visiveis(db, u)}
    marcadas = {int(x) for x in form.getlist("entidade") if str(x).isdigit() and int(x) in visiveis}
    bloqueios = request.app.state.bloqueios_tv
    if bloqueios.segundos_restantes(u.login):
        return _form_aprovar(request, db, u, 429, codigo=codigo, nome=nome, marcadas=marcadas,
                             erro="Demasiados códigos errados. Espera uns minutos e pede um código novo na TV.")
    if not marcadas:
        return _form_aprovar(request, db, u, 400, codigo=codigo, nome=nome, marcadas=marcadas,
                             erro="Escolhe pelo menos uma entidade para a TV mostrar.")
    p = request.app.state.emparelhamentos.por_codigo(codigo)
    if not p:
        bloqueios.falha(u.login)
        return _form_aprovar(request, db, u, 400, codigo=codigo, nome=nome, marcadas=marcadas,
                             erro="Código inválido ou expirado. Confirma o número que aparece na TV.")
    bloqueios.sucesso(u.login)
    token = secrets.token_urlsafe(32)
    db.add(DispositivoTV(utilizador_id=u.id, nome=nome, token_hash=tv.hash_token(token),
                         entidades=",".join(str(i) for i in sorted(marcadas)), versao_sessao=u.versao_sessao))
    auditar(db, u, "tv_emparelhar", nome)
    db.commit()
    p.token = token
    flash(request, f"«{nome}» emparelhada. A TV abre sozinha dentro de segundos.")
    return RedirectResponse("/perfil#tv", status_code=303)


@router.post("/tv/dispositivos/{did}/desligar")
async def desligar(request: Request, did: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    await ler_form(request)
    d = db.get(DispositivoTV, did)
    if not d or (d.utilizador_id != u.id and not u.is_admin):
        raise HTTPException(status_code=404, detail="Não encontrado.")
    auditar(db, u, "tv_desligar", d.nome)
    db.delete(d)
    db.commit()
    flash(request, f"«{d.nome}» desligada: a TV volta a pedir código.")
    return RedirectResponse("/perfil#tv", status_code=303)


def dispositivos_de(db: Session, u: Utilizador) -> list[DispositivoTV]:
    """As TVs do utilizador (todas, para um administrador)."""
    q = select(DispositivoTV).order_by(DispositivoTV.criado_em)
    if not u.is_admin:
        q = q.where(DispositivoTV.utilizador_id == u.id)
    return db.scalars(q).all()
