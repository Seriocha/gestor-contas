"""Saúde › Telemóvel: emparelhar a app Android «Gestor Saúde» e receber os dados do Health Connect.

Páginas (com sessão): /saude/telemovel (aprovar o código, ver e desligar telemóveis).
API (sem sessão; JSON): /api/saude/emparelhar, /api/saude/emparelhar/estado (a app pede e espera o código) e, com o
token do telemóvel no cabeçalho «Authorization: Bearer …», /api/saude/estado e /api/saude/enviar.
"""
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import saude as sd
from .. import saude_telemovel as st
from .. import tv
from ..deps import auditar, entidades_visiveis, flash, get_db, papel_em, render, utilizador_atual
from ..models import PAPEIS, DispositivoSaude, MedidaSaude, PerfilSaude, Utilizador
from ..seguranca import ler_form

router = APIRouter()
MAX_CORPO = 5 * 1024 * 1024


def _perfis_editaveis(db: Session, u: Utilizador) -> list[PerfilSaude]:
    ids = [e.id for e, p in entidades_visiveis(db, u) if PAPEIS[p] >= PAPEIS["editar"] and e.tem("saude")]
    if not ids:
        return []
    return db.scalars(select(PerfilSaude).where(PerfilSaude.entidade_id.in_(ids)).order_by(PerfilSaude.nome)).all()


# ------------------------------------------------------------------ páginas
def _pagina(request: Request, db: Session, u: Utilizador, status_code: int = 200, **ctx):
    perfis = _perfis_editaveis(db, u)
    ids = [p.id for p in perfis]
    telemoveis = db.scalars(select(DispositivoSaude).where(DispositivoSaude.perfil_id.in_(ids))
                            .order_by(DispositivoSaude.criado_em)).all() if ids else []
    ctx.setdefault("codigo", "")
    ctx.setdefault("nome", "Telemóvel")
    ctx.setdefault("perfil_id", next((p.id for p in perfis if p.utilizador_id == u.id), perfis[0].id if perfis else None))
    return render(request, "saude/telemovel.html", status_code=status_code, perfis=perfis, telemoveis=telemoveis,
                  endereco=str(request.base_url).rstrip("/"), **ctx)


@router.get("/saude/telemovel")
def telemovel(request: Request, codigo: str = "", u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    return _pagina(request, db, u, codigo="".join(c for c in codigo if c.isdigit())[:6])


@router.post("/saude/telemovel/aprovar")
async def aprovar(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    codigo = str(form.get("codigo", "")).strip()
    nome = str(form.get("nome", "")).strip()[:60] or "Telemóvel"
    pid = int(form["perfil_id"]) if str(form.get("perfil_id", "")).isdigit() else None
    perfil = next((p for p in _perfis_editaveis(db, u) if p.id == pid), None)
    bloqueios = request.app.state.bloqueios_saude
    if bloqueios.segundos_restantes(u.login):
        return _pagina(request, db, u, 429, codigo=codigo, nome=nome, perfil_id=pid,
                       erro="Demasiados códigos errados. Espera uns minutos e pede um código novo na app.")
    if not perfil:
        return _pagina(request, db, u, 400, codigo=codigo, nome=nome, perfil_id=pid,
                       erro="Escolhe o perfil de saúde que recebe os dados.")
    p = request.app.state.emparelhamentos_saude.por_codigo(codigo)
    if not p:
        bloqueios.falha(u.login)
        return _pagina(request, db, u, 400, codigo=codigo, nome=nome, perfil_id=pid,
                       erro="Código inválido ou expirado. Confirma o número que aparece na app.")
    bloqueios.sucesso(u.login)
    token = secrets.token_urlsafe(32)
    db.add(DispositivoSaude(perfil_id=perfil.id, utilizador_id=u.id, nome=nome, token_hash=tv.hash_token(token),
                            versao_sessao=u.versao_sessao))
    auditar(db, u, "saude_telemovel", f"emparelhado «{nome}» → {perfil.nome}")
    db.commit()
    p.token, p.perfil = token, perfil.nome
    flash(request, f"«{nome}» emparelhado com o perfil {perfil.nome}. A app continua sozinha dentro de segundos.")
    return RedirectResponse("/saude/telemovel", status_code=303)


@router.post("/saude/telemovel/{did}/desligar")
async def desligar(request: Request, did: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    await ler_form(request)
    d = db.get(DispositivoSaude, did)
    if not d or d.perfil_id not in {p.id for p in _perfis_editaveis(db, u)}:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    auditar(db, u, "saude_telemovel", f"desligado «{d.nome}»")
    db.delete(d)
    db.commit()
    flash(request, f"«{d.nome}» desligado: a app deixa de conseguir enviar e pede para emparelhar outra vez.")
    return RedirectResponse("/saude/telemovel", status_code=303)


# ------------------------------------------------------------------ API da app
async def _json(request: Request) -> dict:
    corpo = await request.body()
    if len(corpo) > MAX_CORPO:
        raise HTTPException(status_code=413, detail="Envio grande demais.")
    try:
        import json
        dados = json.loads(corpo or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="JSON inválido.") from None
    if not isinstance(dados, dict):
        raise HTTPException(status_code=400, detail="JSON inválido.")
    return dados


def telemovel_atual(request: Request, db: Session = Depends(get_db)) -> DispositivoSaude:
    cab = request.headers.get("authorization", "")
    token = cab[7:].strip() if cab.lower().startswith("bearer ") else ""
    d = db.scalar(select(DispositivoSaude).where(DispositivoSaude.token_hash == tv.hash_token(token))) if token else None
    if not d:
        raise HTTPException(status_code=401, detail="Telemóvel não emparelhado (ou desligado no Gestor).")
    u = d.utilizador
    papel = papel_em(db, u, d.perfil.entidade_id)
    if not u.ativo or u.versao_sessao != d.versao_sessao or papel is None or PAPEIS[papel] < PAPEIS["editar"]:
        raise HTTPException(status_code=401, detail="O emparelhamento já não é válido: emparelha outra vez.")
    return d


@router.post("/api/saude/emparelhar")
def pedir_codigo(request: Request):
    p = request.app.state.emparelhamentos_saude.novo()
    return {"codigo": p.codigo, "segredo": p.segredo, "validade_s": tv.VALIDADE_PEDIDO,
            "aprovar": f"{str(request.base_url).rstrip('/')}/saude/telemovel?codigo={p.codigo}"}


@router.post("/api/saude/emparelhar/estado")
async def estado_codigo(request: Request):
    dados = await _json(request)
    pedidos: tv.Emparelhamentos = request.app.state.emparelhamentos_saude
    p = pedidos.por_segredo(str(dados.get("segredo") or ""))
    if not p:
        return {"estado": "expirado"}
    if not p.token:
        return {"estado": "espera"}
    pedidos.remover(p)
    return {"estado": "aprovado", "token": p.token, "perfil": getattr(p, "perfil", "")}


@router.get("/api/saude/estado")
def estado(d: DispositivoSaude = Depends(telemovel_atual), db: Session = Depends(get_db)):
    ultimo = db.scalar(select(func.max(MedidaSaude.data)).where(MedidaSaude.perfil_id == d.perfil_id,
                                                                MedidaSaude.fonte == st.FONTE))
    return {"perfil": d.perfil.nome, "nome": d.nome, "ultimo_dia": ultimo.isoformat() if ultimo else None,
            "ultimo_envio": d.ultimo_envio.isoformat(timespec="seconds") if d.ultimo_envio else None}


@router.post("/api/saude/enviar")
async def enviar(request: Request, d: DispositivoSaude = Depends(telemovel_atual), db: Session = Depends(get_db)):
    dados = await _json(request)
    try:
        leitura, ignorados = st.converter(dados)
    except st.ErroEnvio as e:
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=400)
    res = sd.gravar(db, d.perfil, leitura) if (leitura.medidas or leitura.treinos) else {"novas": 0, "alteradas": 0, "treinos": 0}
    dias = leitura.dias
    d.ultimo_envio = datetime.now()
    d.ultimo_resumo = (f"{len(dias)} dias, {res['novas']} novos, {res['alteradas']} atualizados, {res['treinos']} treinos"
                       + (f", {ignorados} ignorados" if ignorados else ""))[:200]
    # só contagens: nada de valores de saúde nos registos
    auditar(db, d.utilizador, "saude_telemovel", f"envio de «{d.nome}» → {d.perfil.nome}: {d.ultimo_resumo}")
    db.commit()
    return {"ok": True, "perfil": d.perfil.nome, "dias": len(dias), "novos": res["novas"], "atualizados": res["alteradas"],
            "treinos": res["treinos"], "ignorados": ignorados, "resumo": d.ultimo_resumo}
