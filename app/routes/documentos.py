"""Documentos do Paperless-ngx ligados a movimentos e a itens do recheio: procurar, ligar, desligar e miniaturas."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import paperless
from ..deps import auditar, exigir_papel, flash, get_db, render, utilizador_atual
from ..models import DocumentoLigado, ItemRecheio, Movimento, Utilizador
from ..seguranca import ler_form

router = APIRouter()


def _alvo(db: Session, u: Utilizador, alvo: str, alvo_id: int, minimo: str = "editar"):
    """(objeto, entidade_id, voltar) do movimento ou item do recheio, verificando a permissão."""
    if alvo == "movimento":
        m = db.get(Movimento, alvo_id)
        if not m:
            raise HTTPException(status_code=404, detail="Não encontrado.")
        exigir_papel(db, u, m.conta.entidade_id, minimo)
        return m, f"/movimentos/{m.id}/detalhe#documentos"
    if alvo == "recheio":
        it = db.get(ItemRecheio, alvo_id)
        if not it:
            raise HTTPException(status_code=404, detail="Não encontrado.")
        exigir_papel(db, u, it.entidade_id, minimo)
        return it, f"/patrimonio/recheio/{it.id}#documentos"
    raise HTTPException(status_code=404, detail="Não encontrado.")


def token(request: Request, u: Utilizador) -> str | None:
    return paperless.token_de(u, request.app.state.defs.secret_key)


def contexto(request: Request, u: Utilizador, db: Session, alvo: str, alvo_id: int) -> dict:
    """Variáveis da secção «Documentos» (documentos/_seccao.html)."""
    return {"docs": ligados(db, alvo, alvo_id), "paperless_cfg": paperless.configurado(),
            "paperless_ok": bool(token(request, u)), "doc_alvo": alvo, "doc_alvo_id": alvo_id,
            "paperless_link": paperless.url_browser() if paperless.configurado() else ""}


def ligados(db: Session, alvo: str, alvo_id: int) -> list[DocumentoLigado]:
    col = DocumentoLigado.movimento_id if alvo == "movimento" else DocumentoLigado.item_recheio_id
    return db.scalars(select(DocumentoLigado).where(col == alvo_id).order_by(DocumentoLigado.data, DocumentoLigado.id)).all()


@router.get("/documentos/procurar")
def procurar(request: Request, alvo: str, alvo_id: int, q: str = "", u: Utilizador = Depends(utilizador_atual),
             db: Session = Depends(get_db)):
    """Resultados (fragmento HTML): sem texto, as sugestões pelo valor/comerciante e data; com texto, a pesquisa."""
    obj, _ = _alvo(db, u, alvo, alvo_id)
    ja = {d.paperless_id for d in ligados(db, alvo, alvo_id)}
    erro, resultados, t = None, [], token(request, u)
    try:
        if q.strip():
            resultados = [(d, "") for d in paperless.procurar(q, token=t)]
        elif alvo == "movimento":
            resultados = paperless.sugestoes(obj, token=t)
        else:
            de = obj.data_compra - timedelta(days=30) if obj.data_compra else None
            ate = obj.data_compra + timedelta(days=30) if obj.data_compra else None
            termo = " ".join(x for x in (obj.loja, obj.marca, obj.modelo) if x) or obj.nome
            resultados = [(d, "loja/marca e data da compra") for d in paperless.procurar(termo, de, ate, token=t)]
    except paperless.ErroPaperless as e:
        erro = str(e)
    return render(request, "documentos/_resultados.html", alvo=alvo, alvo_id=alvo_id, resultados=resultados,
                  ja=ja, erro=erro, q=q)


@router.post("/documentos/ligar")
async def ligar(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    alvo, alvo_id, pid = str(form.get("alvo", "")), str(form.get("alvo_id", "")), str(form.get("paperless_id", ""))
    if not alvo_id.isdigit() or not pid.isdigit():
        raise HTTPException(status_code=400, detail="Pedido inválido.")
    obj, voltar = _alvo(db, u, alvo, int(alvo_id))
    if int(pid) in {d.paperless_id for d in ligados(db, alvo, obj.id)}:
        return RedirectResponse(voltar, status_code=303)
    try:
        d = paperless.documento(int(pid), token=token(request, u))
    except paperless.ErroPaperless as e:
        flash(request, f"Não foi possível ligar o documento: {e}", "erro")
        return RedirectResponse(voltar, status_code=303)
    db.add(DocumentoLigado(paperless_id=d.id, titulo=d.titulo[:200], correspondente=(d.correspondente or "")[:120] or None,
                           data=d.data, utilizador_id=u.id,
                           movimento_id=obj.id if alvo == "movimento" else None,
                           item_recheio_id=obj.id if alvo == "recheio" else None))
    auditar(db, u, "documento_ligado", f"{alvo} {obj.id} ← Paperless {d.id}")
    db.commit()
    flash(request, f"Documento «{d.titulo}» ligado.")
    return RedirectResponse(voltar, status_code=303)


@router.post("/documentos/{did}/desligar")
async def desligar(request: Request, did: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    await ler_form(request)
    d = db.get(DocumentoLigado, did)
    if not d:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    alvo, alvo_id = ("movimento", d.movimento_id) if d.movimento_id else ("recheio", d.item_recheio_id)
    _, voltar = _alvo(db, u, alvo, alvo_id)
    db.delete(d)
    auditar(db, u, "documento_desligado", f"{alvo} {alvo_id} ↛ Paperless {d.paperless_id}")
    db.commit()
    flash(request, "Documento desligado (continua no Paperless).")
    return RedirectResponse(voltar, status_code=303)


@router.get("/paperless/{pid}/miniatura")
def miniatura(request: Request, pid: int, u: Utilizador = Depends(utilizador_atual)):
    """Miniatura pedida ao Paperless pelo Gestor com o token de quem vê (o Paperless decide se pode)."""
    try:
        dados = paperless.miniatura(pid, token=token(request, u))
    except paperless.ErroPaperless:
        raise HTTPException(status_code=404, detail="Miniatura indisponível.")
    tipo = "image/webp" if dados[8:12] == b"WEBP" else "image/png" if dados[:4] == b"\x89PNG" else "image/jpeg"
    return Response(dados, media_type=tipo, headers={"Cache-Control": "private, max-age=86400"})
