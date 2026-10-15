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
    erro_sub, avisos_sub = servicos.escolher_subconta(extrato, conta)
    avisos = list(extrato.avisos) + avisos_sub
    erros = [erro_sub] if erro_sub else []
    if extrato.moeda != conta.moeda:
        erros.append(f"O extrato está em {extrato.moeda} e a conta em {conta.moeda}.")
    if conta.instituicao.parser and conta.instituicao.parser != extrato.parser.split("_")[0]:
        avisos.append(f"O ficheiro parece ser do {extrato.banco}, mas a conta é do {conta.instituicao.nome}. "
                      "Confirma que escolheste a conta certa.")
    if extrato.emprestimo and conta.tipo != "EMPRESTIMO":
        erros.append("Este ficheiro é um extrato de empréstimo: escolhe a conta do empréstimo "
                     "(cria-a em Contas, com o tipo «Empréstimo (habitação)», se ainda não existir).")
    elif conta.tipo == "EMPRESTIMO" and not extrato.emprestimo:
        erros.append("A conta escolhida é um empréstimo: aqui só se importa o extrato do empréstimo. "
                     "O extrato da conta à ordem (com o débito da prestação) importa-se na conta à ordem.")
    e_cartao = conta.tipo == "CARTAO_CREDITO"
    if extrato.cartao and not e_cartao:
        erros.append("Este ficheiro é um extrato de cartão de crédito: escolhe a conta do cartão "
                     "(cria-a em Contas, com o tipo «Cartão de crédito», se ainda não existir).")
    elif e_cartao and not extrato.cartao:
        avisos.append("A conta escolhida é um cartão de crédito, mas o ficheiro parece um extrato de conta à ordem.")
    if extrato.refeicao and conta.tipo != "CARTAO_REFEICAO":
        erros.append("Este ficheiro é um extrato de cartão refeição: escolhe a conta do cartão "
                     "(cria-a em Contas, com o tipo «Cartão refeição», se ainda não existir).")
    elif conta.tipo == "CARTAO_REFEICAO" and not extrato.refeicao:
        avisos.append("A conta escolhida é um cartão refeição, mas o ficheiro parece um extrato de outro tipo.")
    sem_saldo = extrato.movimentos and all(m.saldo is None for m in extrato.movimentos)
    if sem_saldo and conta.saldo_inicial == 0 and not servicos.saldo_conta(db, conta).n_movimentos:
        avisos.append("Este extrato não traz saldo. Confirma o saldo inicial da conta (para um cartão: o valor em "
                      "dívida, negativo, no dia anterior ao 1.º movimento) em Contas › Editar.")
    if e_cartao and not conta.conta_pagamento_id:
        avisos.append("Este cartão não tem «conta que paga o cartão»: sem ela, o débito do pagamento na conta "
                      "à ordem não é reconhecido como transferência.")
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
    erro_sub, _ = servicos.escolher_subconta(extrato, conta)
    if erro_sub or extrato.moeda != conta.moeda:
        raise HTTPException(status_code=400, detail=erro_sub or "Moeda do extrato diferente da da conta.")
    imp, msg = servicos.confirmar(db, conta, extrato, nome, sha, u)
    auditar(db, u, "importacao", f"{imp.id}: {conta.nome} — {imp.n_novos} novos, {imp.n_duplicados} duplicados")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, "A importação foi cancelada: o ficheiro tem movimentos que não foi possível distinguir "
                       "entre si. Nada foi gravado.", "erro")
        return RedirectResponse("/importar", status_code=303)
    pagamentos = (f" {imp.n_pagamentos_cartao} movimentos de pagamento de cartão marcados como transferência."
                  if imp.n_pagamentos_cartao else "")
    if imp.n_transferencias:
        pagamentos += f" {imp.n_transferencias} transferência(s) ligada(s) ao outro lado noutra conta."
    emp = imp.emprestimo
    if emp["ligadas"]:
        pagamentos += f" {emp['ligadas']} prestação(ões) do empréstimo ligada(s) ao débito na conta à ordem."
    if emp["estimadas"]:
        pagamentos += f" {emp['estimadas']} prestação(ões) estimada(s) até chegar o extrato do empréstimo."
    if emp["substituidas"]:
        pagamentos += f" {emp['substituidas']} estimativa(s) substituída(s) pelos valores reais."
    flash(request, f"Importados {imp.n_novos} movimentos novos ({imp.n_duplicados} já existiam); "
                   f"{imp.n_qualificados} categorizados pelas regras." + pagamentos + (f" {msg}" if msg else ""))
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
