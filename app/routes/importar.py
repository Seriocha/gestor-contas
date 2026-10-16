"""Importação de extratos: upload → pré-visualização → confirmar; histórico e anulação."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

import re
from datetime import date
from pathlib import Path

from fastapi.responses import FileResponse

from .. import arquivo, servicos, tarefas
from ..deps import IntOpcional, auditar, entidade_escolhida, entidades_visiveis, exigir_admin, exigir_papel, flash, get_db, render, utilizador_atual
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


def _so_digitos(t: str | None) -> str:
    return re.sub(r"\D", "", t or "")


def _entrada(request, db: Session, contas: list[Conta]) -> list[dict]:
    """Ficheiros na pasta _Entrada, com o banco detetado e a conta sugerida."""
    defs = request.app.state.defs
    try:
        ficheiros = arquivo.listar_entrada(defs.arquivo_dir, maximo=30)
    except OSError:
        return []
    if not ficheiros:
        return []
    ultimas = {(cid, p): q for cid, p, q in db.execute(
        select(Importacao.conta_id, Importacao.parser, func.max(Importacao.criado_em))
        .where(Importacao.estado == "confirmada").group_by(Importacao.conta_id, Importacao.parser))}
    itens = []
    for f in ficheiros:
        item = {"f": f, "banco": None, "ini": None, "fim": None, "n": 0, "conta_id": None, "erro": None}
        try:
            if f.tamanho > servicos.TAMANHO_MAX:
                raise ValueError("ficheiro demasiado grande")
            e = ler_extrato(f.nome, arquivo.ficheiro_entrada(defs.arquivo_dir, f.nome).read_bytes())
            item.update(banco=e.banco, n=len(e.movimentos),
                        ini=e.movimentos[0].data_mov if e.movimentos else None,
                        fim=e.movimentos[-1].data_mov if e.movimentos else None)
            candidatas = [c for c in contas if tarefas.parser_da_conta(c) == e.parser]
            ref = _so_digitos(e.conta_ref)[-4:]
            pelo_numero = [c for c in candidatas if ref and _so_digitos(c.iban).endswith(ref)]
            # entre várias contas do mesmo banco/tipo: a que recebeu este formato mais recentemente
            usadas = sorted((c for c in candidatas if (c.id, e.parser) in ultimas),
                            key=lambda c: ultimas[(c.id, e.parser)], reverse=True)
            escolha = (pelo_numero or usadas or candidatas or [None])[0]
            item["conta_id"] = escolha.id if escolha else None
        except Exception:
            item["erro"] = "formato não reconhecido"
        itens.append(item)
    return itens


def _pagina(request, db, u, status_code=200, **ctx):
    contas = _contas_editaveis(db, u)
    defs = request.app.state.defs
    return render(request, "importar/form.html", status_code=status_code, contas=contas,
                  historico=_historico(db, u), entrada=_entrada(request, db, contas),
                  arquivo_partilhado=defs.arquivo_partilhado, **ctx)


def _arquivar(request, imp: Importacao, conta: Conta, nome: str, conteudo: bytes) -> str | None:
    """Cópia organizada do extrato no arquivo. Devolve um aviso se não foi possível."""
    raiz = request.app.state.defs.arquivo_dir
    rel = arquivo.caminho_relativo(conta.entidade.nome, conta.instituicao.nome, conta.nome,
                                   imp.data_ini, imp.data_fim, Path(nome).suffix)
    try:
        imp.arquivo = arquivo.arquivar(raiz, rel, conteudo)
    except OSError as e:
        return f"Não foi possível guardar a cópia no arquivo ({e})."
    return None


@router.get("/tarefas")
def tarefas_do_mes(request: Request, entidade: IntOpcional = None, u: Utilizador = Depends(utilizador_atual),
                   db: Session = Depends(get_db)):
    """Instruções e ligações para tirar os extratos de cada banco, com o estado no ciclo atual."""
    visiveis, ent, papel = entidade_escolhida(request, db, u, entidade)
    prefs, hoje = request.app.state.prefs, date.today()
    inicio = tarefas.inicio_ciclo(prefs, hoje)
    bancos = tarefas.do_ciclo(db, ent.id, inicio) if ent else []
    ativas, dia_ini, dia_fim = tarefas.janela(prefs)
    return render(request, "importar/tarefas.html", visiveis=visiveis, entidade=ent, bancos=bancos, inicio=inicio,
                  pode_editar=bool(papel) and PAPEIS[papel] >= PAPEIS["editar"], ativas=ativas,
                  dia_ini=dia_ini, dia_fim=dia_fim, na_janela=tarefas.na_janela(prefs, hoje),
                  por_fazer=sum(b.por_fazer for b in bancos))


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
    entrada = str(form.get("entrada", ""))
    if entrada:
        try:
            caminho = arquivo.ficheiro_entrada(request.app.state.defs.arquivo_dir, entrada)
        except FileNotFoundError:
            return _pagina(request, db, u, 400, erro="O ficheiro já não está na pasta _Entrada.", conta_id=conta.id)
        if caminho.stat().st_size > servicos.TAMANHO_MAX:
            return _pagina(request, db, u, 400, erro="Ficheiro demasiado grande (máx. 10 MB).", conta_id=conta.id)
        nome_ficheiro, conteudo = entrada, caminho.read_bytes()
    else:
        if ficheiro is None or not getattr(ficheiro, "filename", ""):
            return _pagina(request, db, u, 400, erro="Escolhe o ficheiro do extrato.", conta_id=conta.id)
        conteudo = await ficheiro.read(servicos.TAMANHO_MAX + 1)
        if len(conteudo) > servicos.TAMANHO_MAX:
            return _pagina(request, db, u, 400, erro="Ficheiro demasiado grande (máx. 10 MB).", conta_id=conta.id)
        nome_ficheiro = ficheiro.filename
    try:
        extrato = ler_extrato(nome_ficheiro, conteudo)
    except FormatoDesconhecido as e:
        return _pagina(request, db, u, 400, erro=str(e), conta_id=conta.id)
    except Exception as e:  # ficheiro corrompido / colunas com valores inesperados
        return _pagina(request, db, u, 400, erro=f"Não foi possível ler o ficheiro: {e}", conta_id=conta.id)

    sha = servicos.guardar_upload(request.app.state.defs.data_dir, nome_ficheiro, conteudo)
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
                  sha=sha, nome=nome_ficheiro, entrada=entrada)


@router.post("/confirmar")
async def confirmar(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    conta_txt, sha, nome = str(form.get("conta_id", "")), str(form.get("sha", "")), str(form.get("nome", ""))
    entrada = str(form.get("entrada", ""))
    conta = db.get(Conta, int(conta_txt)) if conta_txt.isdigit() else None
    if not conta:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    exigir_papel(db, u, conta.entidade_id, "editar")
    try:
        conteudo = servicos.ler_upload(request.app.state.defs.data_dir, sha)
        extrato = ler_extrato(nome, conteudo)
    except (FileNotFoundError, FormatoDesconhecido):
        flash(request, "O ficheiro da pré-visualização já não está disponível. Volta a carregá-lo.", "erro")
        return RedirectResponse("/importar", status_code=303)
    erro_sub, _ = servicos.escolher_subconta(extrato, conta)
    if erro_sub or extrato.moeda != conta.moeda:
        raise HTTPException(status_code=400, detail=erro_sub or "Moeda do extrato diferente da da conta.")
    imp, msg = servicos.confirmar(db, conta, extrato, nome, sha, u)
    aviso_arquivo = _arquivar(request, imp, conta, nome, conteudo)
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
    if imp.arquivo:
        pagamentos += f" Cópia arquivada em {imp.arquivo}."
        if entrada and arquivo.retirar_da_entrada(request.app.state.defs.arquivo_dir, entrada):
            pagamentos += " Ficheiro retirado da pasta _Entrada."
    flash(request, f"Importados {imp.n_novos} movimentos novos ({imp.n_duplicados} já existiam); "
                   f"{imp.n_qualificados} categorizados pelas regras." + pagamentos + (f" {msg}" if msg else ""))
    if aviso_arquivo:
        flash(request, aviso_arquivo, "erro")
    return RedirectResponse(f"/contas/{conta.id}/movimentos", status_code=303)


@router.get("/{imp_id}/ficheiro")
def descarregar(request: Request, imp_id: int, u: Utilizador = Depends(utilizador_atual),
                db: Session = Depends(get_db)):
    """O extrato original (cópia do arquivo; se não houver, a do upload)."""
    imp = db.get(Importacao, imp_id)
    if not imp:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    exigir_papel(db, u, imp.conta.entidade_id, "ver")
    defs = request.app.state.defs
    caminho = arquivo.resolver(defs.arquivo_dir, imp.arquivo or "")
    if not caminho:
        caminho = next(servicos.pasta_uploads(defs.data_dir).glob(f"{imp.sha256}*"), None)
    if not caminho:
        raise HTTPException(status_code=404, detail="O ficheiro já não existe.")
    return FileResponse(caminho, filename=Path(imp.arquivo).name if imp.arquivo else imp.ficheiro_nome)


@router.post("/arquivar-antigas")
async def arquivar_antigas(request: Request, u: Utilizador = Depends(exigir_admin), db: Session = Depends(get_db)):
    """Copia para o arquivo organizado as importações feitas antes de existir o arquivo."""
    await ler_form(request)
    defs = request.app.state.defs
    n, falhas = 0, 0
    for imp in db.scalars(select(Importacao).where(Importacao.estado == "confirmada", Importacao.arquivo.is_(None))
                          .options(selectinload(Importacao.conta))).all():
        try:
            conteudo = servicos.ler_upload(defs.data_dir, imp.sha256)
        except FileNotFoundError:
            falhas += 1
            continue
        if _arquivar(request, imp, imp.conta, imp.ficheiro_nome, conteudo):
            falhas += 1
        else:
            n += 1
    auditar(db, u, "arquivo_antigas", f"{n} arquivadas, {falhas} sem ficheiro")
    db.commit()
    flash(request, f"{n} importação(ões) copiada(s) para o arquivo." + (f" {falhas} sem ficheiro original." if falhas else ""))
    return RedirectResponse("/admin/importadores#arquivo", status_code=303)


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
