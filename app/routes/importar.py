"""Importação de extratos: upload → pré-visualização → confirmar; histórico e anulação."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .. import servicos
from ..deps import IntOpcional, auditar, entidades_visiveis, exigir_papel, flash, get_db, render, utilizador_atual
from ..models import PAPEIS, Conta, Importacao, Utilizador
from ..parsers import FormatoDesconhecido, ler_extrato
from ..seguranca import ler_form

router = APIRouter(prefix="/importar")


def _contas_editaveis(db: Session, u: Utilizador) -> list[Conta]:
    ids = [e.id for e, papel in entidades_visiveis(db, u) if PAPEIS[papel] >= PAPEIS["editar"]]
    if not ids:
        return []
    return db.scalars(select(Conta).where(Conta.entidade_id.in_(ids), Conta.ativa)
                      .options(selectinload(Conta.entidade), selectinload(Conta.instituicao))
                      .order_by(Conta.entidade_id, Conta.nome)).all()


def _historico(db: Session, u: Utilizador) -> list[Importacao]:
    ids = [e.id for e, _ in entidades_visiveis(db, u)]
    if not ids:
        return []
    return db.scalars(select(Importacao).join(Conta).where(Conta.entidade_id.in_(ids))
                      .options(selectinload(Importacao.conta), selectinload(Importacao.utilizador))
                      .order_by(Importacao.id.desc()).limit(50)).all()


def _pagina(request, db, u, status_code=200, **ctx):
    return render(request, "importar/form.html", status_code=status_code, contas=_contas_editaveis(db, u),
                  historico=_historico(db, u), **ctx)


@router.get("")
def form(request: Request, conta: IntOpcional = None, u: Utilizador = Depends(utilizador_atual),
         db: Session = Depends(get_db)):
    return _pagina(request, db, u, conta_id=conta)


@router.post("")
async def previa(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    conta_txt = str(form.get("conta_id", ""))
    ficheiro = form.get("ficheiro")
    conta = db.get(Conta, int(conta_txt)) if conta_txt.isdigit() else None
    if not conta:
        return _pagina(request, db, u, 400, erro="Escolhe a conta.")
    exigir_papel(db, u, conta.entidade_id, "editar")
    if ficheiro is None or not getattr(ficheiro, "filename", ""):
        return _pagina(request, db, u, 400, erro="Escolhe o ficheiro do extrato.", conta_id=conta.id)
    conteudo = await ficheiro.read(servicos.TAMANHO_MAX + 1)
    if len(conteudo) > servicos.TAMANHO_MAX:
        return _pagina(request, db, u, 400, erro="Ficheiro demasiado grande (máx. 10 MB).", conta_id=conta.id)
    try:
        extrato = ler_extrato(ficheiro.filename, conteudo)
    except FormatoDesconhecido as e:
        return _pagina(request, db, u, 400, erro=str(e), conta_id=conta.id)
    except Exception as e:  # ficheiro corrompido / colunas com valores inesperados
        return _pagina(request, db, u, 400, erro=f"Não foi possível ler o ficheiro: {e}", conta_id=conta.id)

    sha = servicos.guardar_upload(request.app.state.defs.data_dir, ficheiro.filename, conteudo)
    avisos = list(extrato.avisos)
    erros = []
    if extrato.moeda != conta.moeda:
        erros.append(f"O extrato está em {extrato.moeda} e a conta em {conta.moeda}.")
    if conta.instituicao.parser and conta.instituicao.parser != extrato.parser:
        avisos.append(f"O ficheiro parece ser do {extrato.banco}, mas a conta é do {conta.instituicao.nome}. "
                      "Confirma que escolheste a conta certa.")
    ja = db.scalar(select(Importacao).where(Importacao.conta_id == conta.id, Importacao.sha256 == sha,
                                            Importacao.estado == "confirmada"))
    if ja:
        avisos.append(f"Este mesmo ficheiro já foi importado em {ja.criado_em:%d-%m-%Y %H:%M}.")
    if extrato.conta_ref:
        avisos.append(f"N.º de conta indicado no ficheiro: {extrato.conta_ref}.")
    linhas = servicos.preparar(db, conta, extrato)
    if not linhas:
        erros.append("O ficheiro não tem movimentos.")
    novos = [l for l in linhas if not l.duplicado]
    return render(request, "importar/previa.html", conta=conta, extrato=extrato, linhas=linhas,
                  n_novos=len(novos), soma_novos=sum(l.montante for l in novos), avisos=avisos, erros=erros,
                  sha=sha, nome=ficheiro.filename)


@router.post("/confirmar")
async def confirmar(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    conta_txt, sha, nome = str(form.get("conta_id", "")), str(form.get("sha", "")), str(form.get("nome", ""))
    conta = db.get(Conta, int(conta_txt)) if conta_txt.isdigit() else None
    if not conta:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    exigir_papel(db, u, conta.entidade_id, "editar")
    try:
        extrato = ler_extrato(nome, servicos.ler_upload(request.app.state.defs.data_dir, sha))
    except (FileNotFoundError, FormatoDesconhecido):
        flash(request, "O ficheiro da pré-visualização já não está disponível. Volta a carregá-lo.", "erro")
        return RedirectResponse("/importar", status_code=303)
    if extrato.moeda != conta.moeda:
        raise HTTPException(status_code=400, detail="Moeda do extrato diferente da da conta.")
    imp, msg = servicos.confirmar(db, conta, extrato, nome, sha, u)
    auditar(db, u, "importacao", f"{imp.id}: {conta.nome} — {imp.n_novos} novos, {imp.n_duplicados} duplicados")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, "A importação foi cancelada: o ficheiro tem movimentos que não foi possível distinguir "
                       "entre si. Nada foi gravado.", "erro")
        return RedirectResponse("/importar", status_code=303)
    flash(request, f"Importados {imp.n_novos} movimentos novos ({imp.n_duplicados} já existiam); "
                   f"{imp.n_qualificados} categorizados pelas regras." + (f" {msg}" if msg else ""))
    return RedirectResponse(f"/contas/{conta.id}/movimentos", status_code=303)


@router.post("/{imp_id}/anular")
async def anular(request: Request, imp_id: int, u: Utilizador = Depends(utilizador_atual),
                 db: Session = Depends(get_db)):
    imp = db.get(Importacao, imp_id)
    if not imp:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    exigir_papel(db, u, imp.conta.entidade_id, "editar")
    await ler_form(request)
    if imp.estado != "confirmada":
        return RedirectResponse("/importar", status_code=303)
    n = servicos.anular(db, imp)
    auditar(db, u, "importacao_anulada", f"{imp.id}: {n} movimentos removidos")
    db.commit()
    flash(request, f"Importação anulada: {n} movimentos removidos.")
    return RedirectResponse("/importar", status_code=303)
