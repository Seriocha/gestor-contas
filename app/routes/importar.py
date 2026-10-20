"""Importação de extratos: upload → pré-visualização → confirmar; histórico e anulação."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from datetime import date

from .. import investimentos, recibos_envio, servicos, tarefas
from ..deps import (IntOpcional, auditar, entidade_escolhida, entidades_visiveis, exigir_papel, flash, get_db, papel_em,
                    render, utilizador_atual)
from ..models import PAPEIS, Conta, Importacao, ImportacaoPendente, Utilizador
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
    return render(request, "importar/form.html", status_code=status_code, contas=_contas_editaveis(db, u), **ctx)


@router.get("/historico")
def historico(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    return render(request, "importar/historico.html", historico=_historico(db, u))


@router.get("/validar")
def por_validar(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """O que espera validação: abastecimentos do telemóvel (por entidade) e, para administradores, os extratos da
    pasta partilhada da NAS."""
    from sqlalchemy import func
    from .. import pasta_importacao as pi
    from ..models import AbastecimentoPendente, Bem
    abast = []
    for e, _ in entidades_visiveis(db, u):
        n = db.scalar(select(func.count()).select_from(AbastecimentoPendente).join(Bem).where(Bem.entidade_id == e.id)) or 0
        if n:
            abast.append((e, n))
    ctx = {"abastecimentos": abast}
    if u.is_admin:
        defs = request.app.state.defs
        ctx["pasta_ok"], ctx["pasta_msg"] = pi.estado_pasta(defs.import_dir)
        ctx["pendentes"] = pi.pendentes(db)
        ctx["historico"] = db.scalars(select(ImportacaoPendente).where(ImportacaoPendente.estado.in_(("validada", "rejeitada")))
                                      .order_by(ImportacaoPendente.tratado_em.desc()).limit(10)).all()
        ctx["intervalos"], ctx["intervalo"] = pi.INTERVALOS, pi.intervalo_minutos(request.app.state.prefs)
        from .. import paperless
        from .documentos import token as token_paperless
        ctx["paperless_cfg"], ctx["paperless_ok"] = paperless.configurado(), bool(token_paperless(request, u))
        ctx["paperless_nome"] = request.app.state.prefs.get("paperless_extratos", "")
        confirmadas = select(func.count()).select_from(Importacao).where(Importacao.estado == "confirmada")
        ctx["n_importacoes"] = db.scalar(confirmadas) or 0
        ctx["n_ligadas"] = db.scalar(confirmadas.where(Importacao.paperless_id.is_not(None))) or 0
    return render(request, "importar/validar.html", **ctx)


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


def _recibos_ctx(request: Request, db: Session, u: Utilizador, **extra) -> dict:
    """Quem pode enviar: o próprio (com token) e, para administradores, qualquer utilizador com token."""
    from .. import paperless
    segredo = request.app.state.defs.secret_key
    if u.is_admin:
        candidatos = db.scalars(select(Utilizador).where(Utilizador.ativo).order_by(Utilizador.nome)).all()
    else:
        candidatos = [u]
    return {"paperless_cfg": paperless.configurado(), "max_ficheiros": recibos_envio.MAX_FICHEIROS,
            "utilizadores": [(x, bool(paperless.token_de(x, segredo))) for x in candidatos], **extra}


@router.get("/recibos")
def recibos_form(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Enviar recibos de vencimento (PDF com password) para o Paperless: tira a password em memória e envia."""
    return render(request, "importar/recibos.html", **_recibos_ctx(request, db, u, resultados=None, uid=u.id))


@router.post("/recibos")
async def recibos_enviar(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    from .. import paperless, recibos_envio as re_
    form = await ler_form(request)
    uid = int(form["utilizador"]) if str(form.get("utilizador", "")).isdigit() else u.id
    if uid != u.id and not u.is_admin:
        raise HTTPException(status_code=403, detail="Sem permissão para esta operação.")
    dono = db.get(Utilizador, uid) or u
    token = paperless.token_de(dono, request.app.state.defs.secret_key)
    ficheiros = []
    for f in form.getlist("ficheiros"):
        if getattr(f, "filename", ""):
            conteudo = await f.read(servicos.TAMANHO_MAX + 1)
            if len(conteudo) <= servicos.TAMANHO_MAX:
                ficheiros.append((f.filename, conteudo))
    erro = None
    if not ficheiros:
        erro = "Escolhe os ficheiros PDF dos recibos."
    elif not token:
        erro = f"{dono.nome} não tem token do Paperless (Perfil, ou Administração › Utilizadores › Ligações)."
    resultados = None
    if not erro:
        senhas = [str(form.get("senha", "")), str(form.get("senha2", ""))]      # nunca guardadas nem registadas
        resultados = re_.processar(ficheiros, senhas, token, enviar=form.get("acao") != "verificar")
        enviados = sum(1 for r in resultados if r.estado == "enviado")
        if enviados:
            auditar(db, u, "recibos_paperless", f"{enviados} enviado(s) com o token de {dono.login}")
            db.commit()
    return render(request, "importar/recibos.html", status_code=400 if erro else 200,
                  **_recibos_ctx(request, db, u, resultados=resultados, erro=erro, uid=uid))


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
    return mostrar_previa(request, db, u, conta, ficheiro.filename, conteudo)


def mostrar_previa(request: Request, db: Session, u: Utilizador, conta: Conta, nome: str, conteudo: bytes,
                   pendente=None):
    """Pré-visualização de um extrato para uma conta (carregado à mão ou vindo da pasta partilhada da NAS)."""
    class _F:                       # o código abaixo usa ficheiro.filename
        filename = nome
    ficheiro = _F()
    try:
        extrato = ler_extrato(ficheiro.filename, conteudo)
    except FormatoDesconhecido as e:
        return _pagina(request, db, u, 400, erro=str(e), conta_id=conta.id)
    except Exception as e:  # ficheiro corrompido / colunas com valores inesperados
        return _pagina(request, db, u, 400, erro=f"Não foi possível ler o ficheiro: {e}", conta_id=conta.id)

    if extrato.debito_cartao:
        return _previa_cartao(request, db, u, conta, extrato, ficheiro.filename, conteudo, pendente)
    if investimentos.e_investimento(conta):
        extrato.avisos = list(extrato.avisos) + investimentos.adaptar_extrato(extrato)
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
        avisos.append(f"Este mesmo ficheiro já foi importado em {ja.criado_em:%d/%m/%Y %H:%M}.")
    if extrato.conta_ref:
        avisos.append(f"N.º de conta indicado no ficheiro: {extrato.conta_ref}.")
    linhas = servicos.preparar(db, conta, extrato)
    if not linhas:
        erros.append("O ficheiro não tem movimentos.")
    novos = [l for l in linhas if not l.duplicado]
    return render(request, "importar/previa.html", conta=conta, extrato=extrato, linhas=linhas,
                  n_novos=len(novos), soma_novos=sum(l.montante for l in novos), avisos=avisos, erros=erros,
                  sha=sha, nome=ficheiro.filename, pendente=pendente,
                  contas_pendente=_contas_para(db, u, pendente) if pendente else [])


def _previa_cartao(request: Request, db: Session, u: Utilizador, conta: Conta, extrato, nome: str, conteudo: bytes,
                   pendente):
    """Ficheiro de um cartão de débito: não cria movimentos — liga as compras aos débitos da conta à ordem."""
    from .. import cartoes
    sha = servicos.guardar_upload(request.app.state.defs.data_dir, nome, conteudo)
    contas = [c for c in cartoes.contas_possiveis(db, extrato)
              if PAPEIS.get(papel_em(db, u, c.entidade_id) or "", 0) >= PAPEIS["editar"]]
    banco = extrato.banco.split(" (")[0]
    if not contas:
        return render(request, "importar/previa_cartao.html", conta=conta, extrato=extrato, p=None, sha=sha, nome=nome,
                      contas=[], pendente=pendente, aviso_conta=None,
                      erros=[f"Este ficheiro é do cartão de débito …{extrato.conta_ref}, mas não há nenhuma conta à ordem "
                             f"do {banco}. Cria-a em Contas e volta a carregá-lo."])
    aviso_conta = None
    if conta not in contas:              # escolheu-se outra conta: passa para a conta à ordem do banco do cartão
        aviso_conta = f"O cartão de débito pertence a uma conta à ordem do {banco}: escolhida «{contas[0].nome}»."
        conta = contas[0]
    return render(request, "importar/previa_cartao.html", conta=conta, extrato=extrato, p=cartoes.previa(db, conta, extrato),
                  erros=[], sha=sha, nome=nome, contas=contas, pendente=pendente, aviso_conta=aviso_conta)


@router.post("/cartao/confirmar")
async def confirmar_cartao(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    from .. import cartoes
    form = await ler_form(request)
    conta_txt, sha, nome = str(form.get("conta_id", "")), str(form.get("sha", "")), str(form.get("nome", ""))
    conta = db.get(Conta, int(conta_txt)) if conta_txt.isdigit() else None
    if not conta or conta.tipo != "ORDEM":
        raise HTTPException(status_code=404, detail="Não encontrado.")
    exigir_papel(db, u, conta.entidade_id, "editar")
    try:
        extrato = ler_extrato(nome, servicos.ler_upload(request.app.state.defs.data_dir, sha))
    except (FileNotFoundError, FormatoDesconhecido):
        flash(request, "O ficheiro já não está disponível. Volta a carregá-lo.", "erro")
        return RedirectResponse("/importar", status_code=303)
    if not extrato.debito_cartao:
        raise HTTPException(status_code=400, detail="Não é um ficheiro de cartão de débito.")
    cartao, novas, ligadas = cartoes.registar(db, conta, extrato)
    auditar(db, u, "cartao_debito", f"{conta.nome} …{cartao.final}: {novas} compras, {ligadas} ligadas")
    msg = (f"Cartão «{cartao.nome}» (…{cartao.final}): {novas} compra{'s' if novas != 1 else ''} nova{'s' if novas != 1 else ''}, "
           f"{ligadas} ligada{'s' if ligadas != 1 else ''} ao débito na conta {conta.nome}.")
    pend_txt = str(form.get("pendente", ""))
    pend = db.get(ImportacaoPendente, int(pend_txt)) if pend_txt.isdigit() else None
    if pend and pend.estado == "pendente" and pend.sha256 == sha:
        from datetime import datetime
        from .. import pasta_importacao
        pend.estado, pend.conta_id = "validada", conta.id
        pend.tratado_em, pend.tratado_por = datetime.now(), u.id
        aviso = pasta_importacao.mover_para_importados(request.app.state.defs.import_dir, pend, date.today())
        pend.nota = aviso
        msg += f" Ficheiro {'movido para importados/' if not aviso else 'NÃO movido: ' + aviso}"
    else:
        pend = None
    db.commit()
    flash(request, msg)
    return RedirectResponse("/importar/validar" if pend else f"/contas/{conta.id}/cartoes", status_code=303)


def _contas_para(db: Session, u: Utilizador, pendente) -> list[Conta]:
    """Contas que se podem escolher para um extrato da pasta (as ativas que o utilizador pode editar)."""
    contas = db.scalars(select(Conta).where(Conta.ativa).order_by(Conta.entidade_id, Conta.nome)).all()
    return [c for c in contas if PAPEIS.get(papel_em(db, u, c.entidade_id) or "", 0) >= PAPEIS["editar"]]


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
    if investimentos.e_investimento(conta):
        investimentos.adaptar_extrato(extrato)
    if erro_sub or extrato.moeda != conta.moeda:
        raise HTTPException(status_code=400, detail=erro_sub or "Moeda do extrato diferente da da conta.")
    imp, msg = servicos.confirmar(db, conta, extrato, nome, sha, u)
    auditar(db, u, "importacao", f"{imp.id}: {conta.nome} — {imp.n_novos} novos, {imp.n_duplicados} duplicados")
    pend_txt = str(form.get("pendente", ""))
    pend = db.get(ImportacaoPendente, int(pend_txt)) if pend_txt.isdigit() else None
    if pend and (pend.estado != "pendente" or pend.sha256 != sha):
        pend = None
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
    # extrato da pasta partilhada / do Paperless: validado → fica registado e o ficheiro da pasta passa para
    # importados/; em qualquer caso, guarda-se onde está o original (documento no Paperless, pelo MD5)
    from .. import extratos
    from ..routes.documentos import token as token_paperless
    aviso = extratos.finalizar(db, pend, imp, request.app.state.defs, u, token_paperless(request, u))
    db.commit()
    if pend and not pend.caminho.startswith("paperless:"):
        pagamentos += f" Ficheiro {'movido para importados/' if not aviso else 'NÃO movido: ' + aviso}"
    flash(request, f"Importados {imp.n_novos} movimentos novos ({imp.n_duplicados} já existiam); "
                   f"{imp.n_qualificados} categorizados pelas regras." + pagamentos + (f" {msg}" if msg else ""))
    if pend:
        return RedirectResponse("/importar/validar", status_code=303)
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


@router.get("/cartao")
def previa_cartao_conta(request: Request, sha: str, nome: str, conta: int, u: Utilizador = Depends(utilizador_atual),
                        db: Session = Depends(get_db)):
    """Pré-visualização do ficheiro de um cartão de débito já carregado, para outra conta à ordem."""
    c = db.get(Conta, conta)
    if not c:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    exigir_papel(db, u, c.entidade_id, "editar")
    try:
        conteudo = servicos.ler_upload(request.app.state.defs.data_dir, sha)
    except FileNotFoundError:
        flash(request, "O ficheiro já não está disponível. Volta a carregá-lo.", "erro")
        return RedirectResponse("/importar", status_code=303)
    return mostrar_previa(request, db, u, c, nome, conteudo)


@router.get("/{imp_id}/original")
def original(request: Request, imp_id: int, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """O ficheiro original de uma importação (a cópia guardada pelo Gestor; PDF abre no browser)."""
    from fastapi.responses import Response
    from urllib.parse import quote
    imp = db.get(Importacao, imp_id)
    if not imp:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    exigir_papel(db, u, imp.conta.entidade_id, "ver")
    try:
        conteudo = servicos.ler_upload(request.app.state.defs.data_dir, imp.sha256)
    except FileNotFoundError:
        if imp.paperless_id:
            from .. import extratos
            return RedirectResponse(extratos.href(imp), status_code=303)
        raise HTTPException(status_code=404, detail="O ficheiro original já não está guardado.")
    tipos = {b"%PDF-": "application/pdf"}
    tipo = next((t for k, t in tipos.items() if conteudo.startswith(k)), "application/octet-stream")
    modo = "inline" if tipo == "application/pdf" else "attachment"
    return Response(conteudo, media_type=tipo,
                    headers={"Content-Disposition": f"{modo}; filename*=UTF-8''{quote(imp.ficheiro_nome)}",
                             "X-Content-Type-Options": "nosniff"})
