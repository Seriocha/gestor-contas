"""Garagem / Espaços: a interface (app/static/garagem/) corre dentro de uma moldura na página «Garagem» do Gestor,
e fala com esta API (mesmo contrato JSON da app autónoma). A lógica está em app/garagem.py.

- /garagem/                 página do Gestor (menu + moldura)
- /garagem/app/             a interface (modo embebido) e os seus ficheiros estáticos
- /garagem/app/api/...      a API, por entidade: ver = ler, editar = alterar, administrar = substituir/apagar tudo
- /garagem/l/{código}       abre uma localização a partir do QR de uma etiqueta
Os pedidos que alteram dados levam o cabeçalho X-CSRF (o token vai no <meta name="csrf"> da interface).
"""
import secrets
import time
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import garagem as g
from ..deps import auditar, entidades_visiveis, get_db, render, utilizador_atual
from ..models import GarLocal, Utilizador
from ..seguranca import csrf_token

router = APIRouter()
APP_DIR = Path(__file__).resolve().parent.parent
ESTATICOS = APP_DIR / "static" / "garagem"
P = "/garagem/app"
LIMPEZA_SEGUNDOS = 24 * 3600


def _historicos(request: Request) -> dict:
    estado = request.app.state
    if getattr(estado, "garagem_hist", None) is None:
        estado.garagem_hist = {}
    return estado.garagem_hist


def contexto(request: Request, db: Session, u: Utilizador) -> g.Ctx:
    """A entidade da Garagem: a última escolhida (se ainda tiver o módulo e o utilizador a vir) ou a 1.ª."""
    visiveis = [(e, p) for e, p in entidades_visiveis(db, u) if e.tem("garagem")]
    if not visiveis:
        raise g.GaragemErro("O módulo Garagem não está ligado a nenhuma das suas entidades (Administração › Entidades).", 404)
    escolha = request.session.get("garagem_entidade")
    ent, papel = next(((e, p) for e, p in visiveis if e.id == escolha), visiveis[0])
    request.session["garagem_entidade"] = ent.id
    hists = _historicos(request)
    fotos = request.app.state.defs.data_dir / "garagem" / "fotos"
    ctx = g.Ctx(db=db, ent=ent.id, papel=papel, fotos=fotos, hist=hists.setdefault(ent.id, g.Historico()))
    g.garantir_espaco(ctx)
    ultima = getattr(request.app.state, "garagem_limpeza", 0)
    if time.time() - ultima > LIMPEZA_SEGUNDOS:               # de vez em quando, apaga as fotos que já não estão em nada
        request.app.state.garagem_limpeza = time.time()
        g.limpar_fotos(db, fotos, hists)
    return ctx


def ctx_ler(request: Request, db: Session = Depends(get_db), u: Utilizador = Depends(utilizador_atual)) -> g.Ctx:
    return contexto(request, db, u)


def ctx_escrever(request: Request, ctx: g.Ctx = Depends(ctx_ler)) -> g.Ctx:
    esperado = request.session.get("csrf") or ""
    if not esperado or not secrets.compare_digest(request.headers.get("x-csrf", "").encode(), esperado.encode()):
        raise g.GaragemErro("Pedido inválido (CSRF). Recarregue a página.", 400)
    return ctx


def ctx_pagina(request: Request, db: Session = Depends(get_db), u: Utilizador = Depends(utilizador_atual)) -> g.Ctx:
    try:
        return contexto(request, db, u)
    except g.GaragemErro as e:
        raise HTTPException(status_code=e.status, detail=e.mensagem)


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
    except Exception:
        return {}
    return b if isinstance(b, dict) else {}


# ---------------------------------------------------------------------------------------------------------------------
# páginas
# ---------------------------------------------------------------------------------------------------------------------
@router.get("/garagem", include_in_schema=False)
@router.get("/garagem/", include_in_schema=False)
def pagina(request: Request, ctx: g.Ctx = Depends(ctx_pagina), u: Utilizador = Depends(utilizador_atual)):
    return render(request, "garagem/pagina.html")


@router.get("/garagem/entidade/{entidade_id}", include_in_schema=False)
def mudar_entidade(entidade_id: int, request: Request, db: Session = Depends(get_db), u: Utilizador = Depends(utilizador_atual)):
    if any(e.id == entidade_id and e.tem("garagem") for e, _ in entidades_visiveis(db, u)):
        request.session["garagem_entidade"] = entidade_id
    return RedirectResponse("/garagem/", status_code=303)


@router.get(P + "/", include_in_schema=False)
@router.get(P + "/index.html", include_in_schema=False)
def interface(request: Request, ctx: g.Ctx = Depends(ctx_pagina)):
    """A interface da Garagem: o index.html estático com o <base> do prefixo e o token anti-CSRF."""
    html = (ESTATICOS / "index.html").read_text(encoding="utf-8")
    html = html.replace('<base href="/">', f'<base href="{P}/">')
    html = html.replace('<meta charset="utf-8">', f'<meta charset="utf-8">\n  <meta name="csrf" content="{csrf_token(request)}">', 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.get("/garagem/l/{codigo}", include_in_schema=False)
@router.get(P + "/l/{codigo}", include_in_schema=False)
def etiqueta(codigo: str, request: Request, db: Session = Depends(get_db), u: Utilizador = Depends(utilizador_atual)):
    """O QR de uma etiqueta abre a localização (mudando para a entidade a que pertence, se o utilizador a vir)."""
    loc = db.scalar(select(GarLocal).where(GarLocal.code == codigo.upper()))
    if loc and any(e.id == loc.entidade_id and e.tem("garagem") for e, _ in entidades_visiveis(db, u)):
        request.session["garagem_entidade"] = loc.entidade_id
        return RedirectResponse(f"/garagem/#/loc/{loc.id}", status_code=303)
    return RedirectResponse("/garagem/", status_code=303)


@router.get(P + "/photos/{nome}", include_in_schema=False)
def foto(nome: str, ctx: g.Ctx = Depends(ctx_ler)):
    p = g.caminho_foto(ctx.fotos, nome)
    if not p:
        raise g.GaragemErro("Foto inexistente.", 404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=2592000, immutable"})


# ---------------------------------------------------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------------------------------------------------
@router.get(P + "/api/state")
def api_estado(ctx: g.Ctx = Depends(ctx_ler)):
    return g.estado(ctx)


@router.get(P + "/api/menu")
def api_menu(request: Request, ctx: g.Ctx = Depends(ctx_ler)):
    menu = g.menu_da_entidade(ctx, "/garagem/", int(request.query_params.get("space") or 0) or None, request.query_params.get("property"))
    return {"module": "garagem", "title": "Garagem", "icon": "🧰", "base": "/garagem/", **menu}


@router.get(P + "/api/spaces")
def api_espacos(request: Request, ctx: g.Ctx = Depends(ctx_ler)):
    return g.espacos(ctx, request.query_params.get("property_id"))


@router.post(P + "/api/spaces")
async def api_espaco_criar(request: Request, ctx: g.Ctx = Depends(ctx_escrever)):
    return {"id": g.criar_espaco(ctx, await _body(request))}


@router.put(P + "/api/spaces/{id_}")
async def api_espaco_alterar(id_: int, request: Request, ctx: g.Ctx = Depends(ctx_escrever)):
    g.atualizar_espaco(ctx, id_, await _body(request))
    return {"ok": True}


@router.delete(P + "/api/spaces/{id_}")
def api_espaco_apagar(id_: int, ctx: g.Ctx = Depends(ctx_escrever)):
    g.apagar_espaco(ctx, id_)
    return {"ok": True}


@router.post(P + "/api/locations")
async def api_local_criar(request: Request, ctx: g.Ctx = Depends(ctx_escrever)):
    return {"id": g.criar_local(ctx, await _body(request))}


@router.put(P + "/api/locations/{id_}")
async def api_local_alterar(id_: int, request: Request, ctx: g.Ctx = Depends(ctx_escrever)):
    g.atualizar_local(ctx, id_, await _body(request))
    return {"ok": True}


@router.delete(P + "/api/locations/{id_}")
def api_local_apagar(id_: int, ctx: g.Ctx = Depends(ctx_escrever)):
    g.apagar_local(ctx, id_)
    return {"ok": True}


@router.post(P + "/api/locations/{id_}/grid")
async def api_grelha_criar(id_: int, request: Request, ctx: g.Ctx = Depends(ctx_escrever)):
    g.grelha_criar(ctx, id_, await _body(request))
    return {"ok": True}


@router.delete(P + "/api/locations/{id_}/grid")
async def api_grelha_apagar(id_: int, request: Request, ctx: g.Ctx = Depends(ctx_escrever)):
    g.grelha_apagar(ctx, id_, bool((await _body(request)).get("moveItems")))
    return {"ok": True}


@router.post(P + "/api/locations/{id_}/duplicate")
async def api_duplicar(id_: int, request: Request, ctx: g.Ctx = Depends(ctx_escrever)):
    return {"ids": g.duplicar(ctx, id_, await _body(request))}


@router.post(P + "/api/items/move")
async def api_artigos_mover(request: Request, ctx: g.Ctx = Depends(ctx_escrever)):
    return {"moved": g.mover_artigos(ctx, await _body(request))}


@router.post(P + "/api/items")
async def api_artigo_criar(request: Request, ctx: g.Ctx = Depends(ctx_escrever)):
    return {"id": g.criar_artigo(ctx, await _body(request))}


@router.put(P + "/api/items/{id_}")
async def api_artigo_alterar(id_: int, request: Request, ctx: g.Ctx = Depends(ctx_escrever)):
    g.atualizar_artigo(ctx, id_, await _body(request))
    return {"ok": True}


@router.delete(P + "/api/items/{id_}")
def api_artigo_apagar(id_: int, ctx: g.Ctx = Depends(ctx_escrever)):
    g.apagar_artigo(ctx, id_)
    return {"ok": True}


@router.post(P + "/api/drafts/confirm")
async def api_rascunhos_confirmar(request: Request, ctx: g.Ctx = Depends(ctx_escrever)):
    return g.confirmar_rascunhos(ctx, await _body(request))


@router.post(P + "/api/drafts")
async def api_rascunhos_criar(request: Request, ctx: g.Ctx = Depends(ctx_escrever)):
    return {"ok": True, "added": g.criar_rascunhos(ctx, await _body(request))}


@router.put(P + "/api/drafts/{id_}")
async def api_rascunho_alterar(id_: int, request: Request, ctx: g.Ctx = Depends(ctx_escrever)):
    g.atualizar_rascunho(ctx, id_, await _body(request))
    return {"ok": True}


@router.delete(P + "/api/drafts/{id_}")
def api_rascunho_apagar(id_: int, ctx: g.Ctx = Depends(ctx_escrever)):
    g.apagar_rascunho(ctx, id_)
    return {"ok": True}


@router.delete(P + "/api/drafts")
def api_rascunhos_limpar(ctx: g.Ctx = Depends(ctx_escrever)):
    g.apagar_rascunho(ctx)
    return {"ok": True}


@router.post(P + "/api/photos")
async def api_foto(request: Request, ctx: g.Ctx = Depends(ctx_escrever)):
    return {"file": g.guardar_foto(ctx, await request.body())}


@router.get(P + "/api/qr.svg")
def api_qr(request: Request, ctx: g.Ctx = Depends(ctx_ler)):
    texto = request.query_params.get("text", "")
    if not texto:
        raise g.GaragemErro("Falta o texto.")
    return Response(g.qr_svg(texto), media_type="image/svg+xml")


@router.post(P + "/api/undo")
def api_desfazer(ctx: g.Ctx = Depends(ctx_escrever)):
    return {"label": g.desfazer(ctx)}


@router.post(P + "/api/redo")
def api_refazer(ctx: g.Ctx = Depends(ctx_escrever)):
    return {"label": g.desfazer(ctx, refazer=True)}


@router.post(P + "/api/reset")
async def api_reset(request: Request, ctx: g.Ctx = Depends(ctx_escrever), u: Utilizador = Depends(utilizador_atual)):
    if (await _body(request)).get("confirm") != "APAGAR":
        raise g.GaragemErro("Confirmação em falta.")
    g.reposicao_total(ctx)
    auditar(ctx.db, u, "garagem_reset", f"entidade {ctx.ent}")
    ctx.db.commit()
    return {"ok": True}


@router.get(P + "/api/export")
def api_exportar(request: Request, ctx: g.Ctx = Depends(ctx_ler), u: Utilizador = Depends(utilizador_atual)):
    dados = g.exportar(ctx, com_fotos=request.query_params.get("photos") != "0")
    auditar(ctx.db, u, "garagem_exportar", f"entidade {ctx.ent}: {len(dados['locations'])} localizações, {len(dados['items'])} artigos")
    ctx.db.commit()
    return JSONResponse(dados, headers={"Content-Disposition": f'attachment; filename="garagem-export-{date.today().isoformat()}.json"'})


@router.post(P + "/api/import")
async def api_importar(request: Request, ctx: g.Ctx = Depends(ctx_escrever), u: Utilizador = Depends(utilizador_atual)):
    modo = "replace" if request.query_params.get("mode") == "replace" else "merge"
    resultado = g.importar(ctx, await _body(request), modo)
    auditar(ctx.db, u, "garagem_importar", f"entidade {ctx.ent} ({modo}): {resultado['locations']} localizações, {resultado['items']} artigos")
    ctx.db.commit()
    return resultado
