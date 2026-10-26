"""Lar (Santa Casa): conciliação das faturas da farmácia com as notas de lançamento, cartas de aviso, recibos e
pagamentos (app/lar.py). Os documentos vêm do Paperless com o token de quem vê a página; os pagamentos são os
movimentos da entidade da utente (é preciso acesso a essa entidade)."""
import time
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import lar, paperless, preferencias
from .. import paperless_gestao as pg
from ..deps import auditar, entidades_visiveis, exigir_papel, flash, get_db, render, utilizador_atual
from ..models import PAPEIS, Conta, Entidade, LarValidacao, Movimento, Utilizador
from ..seguranca import ler_form
from .documentos import token as token_paperless

router = APIRouter()
_CACHE: dict = {}             # (token, ids) → (instante, documentos): o texto de centenas de documentos pesa
CACHE_SEGUNDOS = 600


def _entidade(db: Session, u: Utilizador, prefs: dict) -> Entidade | None:
    visiveis = [e for e, _ in entidades_visiveis(db, u)]
    pref = prefs.get("lar_entidade")
    if pref and pref.isdigit():
        return next((e for e in visiveis if e.id == int(pref)), None)
    return next((e for e in visiveis if e.nome.strip().lower() in ("mãe", "mae")), None)


def _data_pref(prefs: dict, chave: str) -> date | None:
    try:
        return date.fromisoformat(prefs.get(chave) or "")
    except ValueError:
        return None


def _pode_validar(db: Session, u: Utilizador, ent: Entidade | None) -> bool:
    if u.is_admin:
        return True
    if not ent:
        return False
    papel = dict((e.id, p) for e, p in entidades_visiveis(db, u)).get(ent.id)
    return papel is not None and PAPEIS[papel] >= PAPEIS["editar"]


def _correspondente(lista: list[dict], nome: str, palavras: tuple) -> dict | None:
    if nome:
        return next((c for c in lista if (c.get("name") or "").strip().lower() == nome.strip().lower()), None)
    for c in lista:
        n = lar._simples(c.get("name") or "").lower()
        if all(p in n for p in palavras):
            return c
    return None


def _documentos(token: str, cid: int, renovar: bool) -> list[tuple[int, str]]:
    k = (token, cid)
    agora = time.time()
    if not renovar and k in _CACHE and agora - _CACHE[k][0] < CACHE_SEGUNDOS:
        return _CACHE[k][1]
    docs = [(d["id"], d.get("content") or "") for d in pg.listar("documents", token, correspondent__id=cid, fields="id,content")]
    _CACHE[k] = (agora, docs)
    return docs


@router.get("/lar")
def pagina(request: Request, renovar: int = 0, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    prefs = request.app.state.prefs
    token = token_paperless(request, u)
    ctx = {"paperless_cfg": paperless.configurado(), "token_ok": bool(token), "erros": [], "c": None, "prefs": prefs,
           "entidades": [e for e, _ in entidades_visiveis(db, u)], "correspondentes": [],
           "link": paperless.url_browser() if paperless.configurado() else ""}
    ent = _entidade(db, u, prefs)
    ctx["entidade"] = ent
    ctx["pode_validar"] = _pode_validar(db, u, ent)
    if not token:
        return render(request, "lar.html", **ctx)
    try:
        lista = sorted(pg.listar("correspondents", token), key=lambda c: (c.get("name") or "").lower())
        ctx["correspondentes"] = lista
        sc = _correspondente(lista, prefs.get("lar_corr_santa_casa", ""), ("santa casa",))
        fa = _correspondente(lista, prefs.get("lar_corr_farmacia", ""), ("farmacia", "nova"))
        ctx.update(corr_sc=sc, corr_fa=fa)
        if not sc:
            ctx["erros"].append("Não encontrei o correspondente da Santa Casa no Paperless: escolhe-o em «Configurar».")
        if not fa:
            ctx["erros"].append("Não encontrei o correspondente da farmácia no Paperless: escolhe-o em «Configurar».")
        if sc:
            movimentos = []
            if ent:
                movimentos = db.scalars(select(Movimento).join(Conta).where(
                    Conta.entidade_id == ent.id, Movimento.montante < 0,
                    Movimento.data_mov >= date.today() - timedelta(days=800))).all()
            validados = {v.chave: {"em": v.em, "por": v.utilizador.nome if v.utilizador else "", "nota": v.nota or ""}
                         for v in db.scalars(select(LarValidacao))}
            ctx["c"] = lar.conciliar(_documentos(token, fa["id"], bool(renovar)) if fa else [],
                                     _documentos(token, sc["id"], bool(renovar)), movimentos,
                                     pago_ate=_data_pref(prefs, "lar_pago_ate"),
                                     validar_desde=_data_pref(prefs, "lar_validar_desde"), validados=validados)
    except paperless.ErroPaperless as e:
        ctx["erros"].append(str(e))
    return render(request, "lar.html", **ctx)


@router.post("/lar/config")
async def config(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    ent = str(form.get("entidade", ""))
    if ent.isdigit():
        exigir_papel(db, u, int(ent), "editar")
    preferencias.gravar(db, "lar_entidade", ent if ent.isdigit() else "")
    preferencias.gravar(db, "lar_corr_santa_casa", str(form.get("santa_casa", "")).strip()[:128])
    preferencias.gravar(db, "lar_corr_farmacia", str(form.get("farmacia", "")).strip()[:128])
    for chave in ("lar_pago_ate", "lar_validar_desde"):
        v = str(form.get(chave, "")).strip()
        try:
            v = date.fromisoformat(v).isoformat() if v else ""
        except ValueError:
            v = ""
        preferencias.gravar(db, chave, v)
    auditar(db, u, "lar_config", "")
    db.commit()
    request.app.state.prefs = preferencias.carregar(db)
    _CACHE.clear()
    flash(request, "Configuração do Lar guardada.")
    return RedirectResponse("/lar", status_code=303)


@router.post("/lar/validar")
async def validar(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Valida (ou anula a validação de) documentos da Santa Casa vistos na conciliação. Quem pode: administradores
    ou quem tem «editar» na entidade da utente."""
    form = await ler_form(request)
    if not _pode_validar(db, u, _entidade(db, u, request.app.state.prefs)):
        flash(request, "Sem permissão para validar (precisas de «editar» na entidade da utente).", "erro")
        return RedirectResponse("/lar", status_code=303)
    acao = str(form.get("acao", ""))
    if ":" in acao:                                   # botão de uma linha: «validar:NLD 2026/221»
        acao, um = acao.split(":", 1)
        chaves = [um]
    elif acao == "validar_mes":                       # «Validar o mês»: os documentos por validar desse mês
        acao, chaves = "validar", [str(x) for x in form.getlist("mes_chave")]
    else:
        chaves = [str(x) for x in form.getlist("chave")]
    chaves = [k.strip()[:30] for k in chaves if k.strip()]
    nota = str(form.get("nota", "")).strip()[:200] or None
    existentes = {v.chave: v for v in db.scalars(select(LarValidacao).where(LarValidacao.chave.in_(chaves)))} if chaves else {}
    n = 0
    for k in chaves:
        if acao == "validar" and k not in existentes:
            db.add(LarValidacao(chave=k, nota=nota, utilizador_id=u.id))
            n += 1
        elif acao == "anular" and k in existentes:
            db.delete(existentes[k])
            n += 1
    if n:
        auditar(db, u, "lar_validacao", f"{acao}: {', '.join(chaves[:10])}{'…' if len(chaves) > 10 else ''}")
        db.commit()
    flash(request, f"{n} documento{'s' if n != 1 else ''} {'validado' if acao == 'validar' else 'com a validação anulada'}{'s' if n != 1 else ''}.")
    return RedirectResponse("/lar#documentos", status_code=303)
