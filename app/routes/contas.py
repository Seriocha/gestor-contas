"""Gestão de contas (criar, editar, arquivar). Criar/editar exige o papel
'admin' na entidade da conta; ver exige qualquer papel."""
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..deps import IntOpcional, auditar, entidade_escolhida, entidades_visiveis, exigir_papel, flash, get_db, render, utilizador_atual
from ..formatos import montante_para_input, normalizar_iban, parse_data, parse_montante
from ..seguranca import ler_form
from datetime import date

from .. import cobertura, retificacoes
from ..emprestimos import PADRAO_OMISSAO, parse_taxa, taxa_para_input
from ..models import (PAPEIS, TIPOS_CONTA, TIPOS_TAXA, Conta, Emprestimo, Entidade, Instituicao, Permissao,
                      Utilizador)
from ..servicos import saldo_conta

router = APIRouter(prefix="/contas")


def _opcoes(db: Session, entidade_id: int, conta_id: int | None = None) -> dict:
    """Listas para os selects do formulário, restritas à entidade da conta."""
    pagadoras = db.scalars(
        select(Conta).where(Conta.entidade_id == entidade_id, Conta.tipo.in_(("ORDEM", "CARTEIRA_DIGITAL")),
                            Conta.ativa, Conta.id != (conta_id or 0)).order_by(Conta.nome)
    ).all()
    titulares = db.scalars(
        select(Utilizador).join(Permissao).where(Permissao.entidade_id == entidade_id, Utilizador.ativo)
        .order_by(Utilizador.nome)
    ).all()
    return {
        "instituicoes": db.scalars(select(Instituicao).where(Instituicao.ativa).order_by(Instituicao.nome)).all(),
        "tipos": TIPOS_CONTA,
        "tipos_taxa": TIPOS_TAXA,
        "pagadoras": pagadoras,
        "possiveis_titulares": titulares,
    }


def _ler_campos(form, db: Session, entidade_id: int, conta_id: int | None) -> tuple[dict, list[str]]:
    erros = []
    v = {k: str(form.get(k, "")).strip() for k in (
        "nome", "tipo", "moeda", "iban", "instituicao_id", "saldo_inicial", "data_saldo_inicial",
        "conta_pagamento_id", "dia_fecho", "dia_pagamento", "notas")}
    dados: dict = {"nome": v["nome"], "tipo": v["tipo"], "moeda": (v["moeda"] or "EUR").upper(),
                   "iban": normalizar_iban(v["iban"]), "notas": v["notas"] or None}
    if not dados["nome"]:
        erros.append("O nome é obrigatório.")
    if dados["tipo"] not in TIPOS_CONTA:
        erros.append("Tipo de conta inválido.")
    if not re.fullmatch(r"[A-Z]{3}", dados["moeda"]):
        erros.append("Moeda inválida (usa o código de 3 letras, ex.: EUR).")
    inst = db.get(Instituicao, int(v["instituicao_id"])) if v["instituicao_id"].isdigit() else None
    if not inst:
        erros.append("Escolhe a instituição.")
    dados["instituicao_id"] = inst.id if inst else None
    try:
        dados["saldo_inicial"] = parse_montante(v["saldo_inicial"])
    except ValueError as e:
        erros.append(str(e))
    try:
        dados["data_saldo_inicial"] = parse_data(v["data_saldo_inicial"])
    except ValueError as e:
        erros.append(str(e))

    dados["conta_pagamento_id"] = dados["dia_fecho"] = dados["dia_pagamento"] = None
    if dados["tipo"] in ("CARTAO_CREDITO", "EMPRESTIMO"):
        if v["conta_pagamento_id"]:
            pag = db.get(Conta, int(v["conta_pagamento_id"])) if v["conta_pagamento_id"].isdigit() else None
            if not pag or pag.entidade_id != entidade_id or pag.id == conta_id:
                erros.append("A conta de pagamento tem de ser uma conta da mesma entidade.")
            else:
                dados["conta_pagamento_id"] = pag.id
        campos = [("dia_pagamento", "Dia de pagamento")]
        if dados["tipo"] == "CARTAO_CREDITO":
            campos.append(("dia_fecho", "Dia de fecho"))
        for campo, rotulo in campos:
            if v[campo]:
                if not v[campo].isdigit() or not 1 <= int(v[campo]) <= 31:
                    erros.append(f"{rotulo} tem de estar entre 1 e 31.")
                else:
                    dados[campo] = int(v[campo])

    dados["_emprestimo"] = None
    if dados["tipo"] == "EMPRESTIMO":
        # O saldo de um empréstimo é a dívida: guarda-se sempre negativo, seja qual for o sinal escrito.
        dados["saldo_inicial"] = -abs(dados.get("saldo_inicial") or 0)
        # Capital e data são opcionais: com o extrato do empréstimo, saem da 1.ª importação.
        if bool(dados["saldo_inicial"]) != bool(dados.get("data_saldo_inicial")):
            erros.append("Indica o capital em dívida e a data a que se refere — ou deixa ambos vazios "
                         "para os obter do extrato do empréstimo.")
        dados["_emprestimo"], erros_emp = _ler_emprestimo(form)
        erros += erros_emp

    titulares_ids = {int(x) for x in form.getlist("titulares") if str(x).isdigit()}
    titulares = db.scalars(
        select(Utilizador).join(Permissao)
        .where(Permissao.entidade_id == entidade_id, Utilizador.id.in_(titulares_ids))
    ).all() if titulares_ids else []
    dados["titulares"] = list(titulares)

    existente = db.scalar(select(Conta).where(Conta.entidade_id == entidade_id, Conta.nome == dados["nome"],
                                              Conta.id != (conta_id or 0)))
    if existente:
        erros.append("Já existe uma conta com esse nome nesta entidade.")
    return dados, erros


def _ler_emprestimo(form) -> tuple[dict, list[str]]:
    erros = []
    t = {k: str(form.get(k, "")).strip() for k in (
        "emp_prazo", "emp_taxa", "emp_tipo_taxa", "emp_indexante", "emp_spread", "emp_padrao", "emp_encargos",
        "emp_montante", "emp_data_contrato", "emp_ultima")}
    dados = {"tipo_taxa": t["emp_tipo_taxa"] if t["emp_tipo_taxa"] in TIPOS_TAXA else "variavel",
             "indexante": t["emp_indexante"][:30] or None, "padrao_prestacao": t["emp_padrao"][:200] or None}
    if not t["emp_prazo"].isdigit() or not 1 <= int(t["emp_prazo"]) <= 600:
        erros.append("Indica o número total de prestações do contrato (1 a 600).")
    else:
        dados["prazo_meses"] = int(t["emp_prazo"])
    dados["ultima_prestacao_n"] = None
    if t["emp_ultima"]:
        if not t["emp_ultima"].isdigit() or int(t["emp_ultima"]) > dados.get("prazo_meses", 600):
            erros.append("A última prestação paga tem de ser um número até ao total de prestações.")
        else:
            dados["ultima_prestacao_n"] = int(t["emp_ultima"])
    try:
        dados["taxa_e5"] = parse_taxa(t["emp_taxa"])
        if dados["taxa_e5"] is None:
            erros.append("Indica a TAN em vigor.")
        dados["spread_e5"] = parse_taxa(t["emp_spread"])
    except ValueError as e:
        erros.append(str(e))
    try:
        dados["encargos"] = abs(parse_montante(t["emp_encargos"]))
        dados["montante_contratado"] = abs(parse_montante(t["emp_montante"])) or None
        dados["data_contrato"] = parse_data(t["emp_data_contrato"])
    except ValueError as e:
        erros.append(str(e))
    return dados, erros


def _gravar_emprestimo(db: Session, c: Conta, dados_emp: dict | None) -> None:
    if dados_emp is None:
        if c.emprestimo:
            db.delete(c.emprestimo)
        return
    if c.emprestimo is None:
        c.emprestimo = Emprestimo(**dados_emp)
    else:
        for k, val in dados_emp.items():
            setattr(c.emprestimo, k, val)


def _valores_form(c: Conta) -> dict:
    e = c.emprestimo
    emp = {} if not e else {
        "emp_prazo": e.prazo_meses, "emp_ultima": e.ultima_prestacao_n or "", "emp_taxa": taxa_para_input(e.taxa_e5), "emp_tipo_taxa": e.tipo_taxa,
        "emp_indexante": e.indexante or "", "emp_spread": taxa_para_input(e.spread_e5),
        "emp_padrao": e.padrao_prestacao or "", "emp_encargos": montante_para_input(e.encargos),
        "emp_montante": montante_para_input(e.montante_contratado),
        "emp_data_contrato": e.data_contrato.isoformat() if e.data_contrato else ""}
    return {**emp,
        "nome": c.nome, "tipo": c.tipo, "moeda": c.moeda, "iban": c.iban or "",
        "instituicao_id": c.instituicao_id, "saldo_inicial": montante_para_input(c.saldo_inicial),
        "data_saldo_inicial": c.data_saldo_inicial.isoformat() if c.data_saldo_inicial else "",
        "conta_pagamento_id": c.conta_pagamento_id, "dia_fecho": c.dia_fecho or "",
        "dia_pagamento": c.dia_pagamento or "", "notas": c.notas or "",
        "titulares": {t.id for t in c.titulares},
    }


ORDENS_CONTAS = {
    "tipo": lambda cs: (list(TIPOS_CONTA).index(cs[0].tipo), cs[0].nome.lower()),
    "nome": lambda cs: cs[0].nome.lower(),
    "banco": lambda cs: (cs[0].instituicao.nome.lower(), cs[0].nome.lower()),
    "saldo": lambda cs: (cs[1].atual, cs[0].nome.lower()),
}


AGRUPAR_CONTAS = {"tipo": "Tipo de conta", "banco": "Banco", "nenhum": "Sem grupos"}


def _agrupar(contas: list, agrupar: str) -> list[tuple[str, list]]:
    """[(título, [(conta, saldo)])], mantendo a ordem escolhida dentro de cada grupo."""
    if agrupar == "nenhum":
        return [("", contas)]
    chave = (lambda c: c.tipo_rotulo) if agrupar == "tipo" else (lambda c: c.instituicao.nome)
    grupos: dict[str, list] = {}
    for c, s in contas:
        grupos.setdefault(chave(c), []).append((c, s))
    if agrupar == "tipo":
        ordem = list(TIPOS_CONTA.values())
        return sorted(grupos.items(), key=lambda g: ordem.index(g[0]) if g[0] in ordem else len(ordem))
    return sorted(grupos.items(), key=lambda g: g[0].lower())


@router.get("")
def lista(request: Request, arquivadas: IntOpcional = 0, ordem: str = "", agrupar: str = "",
          u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    ordem = ordem if ordem.lstrip("-") in ORDENS_CONTAS else request.session.get("contas_ordem", "tipo")
    request.session["contas_ordem"] = ordem
    agrupar = agrupar if agrupar in AGRUPAR_CONTAS else request.session.get("contas_agrupar", "tipo")
    request.session["contas_agrupar"] = agrupar
    col, desc = ordem.lstrip("-"), ordem.startswith("-")
    blocos, todas = [], []
    for entidade, papel in entidades_visiveis(db, u):
        q = select(Conta).where(Conta.entidade_id == entidade.id).options(
            selectinload(Conta.instituicao), selectinload(Conta.titulares), selectinload(Conta.conta_pagamento))
        if not arquivadas:
            q = q.where(Conta.ativa)
        contas = sorted(((c, saldo_conta(db, c)) for c in db.scalars(q).all()), key=ORDENS_CONTAS[col], reverse=desc)
        todas += [c.id for c, _ in contas]
        blocos.append({"entidade": entidade, "papel": papel, "pode_gerir": PAPEIS[papel] >= PAPEIS["admin"],
                       "contas": contas, "grupos": _agrupar(contas, agrupar)})
    # Títulos das colunas: clicar ordena; clicar outra vez inverte (saldo começa pelo maior).
    ordens = {}
    for c in ORDENS_CONTAS:
        atual = col == c
        nova = (c if desc else f"-{c}") if atual else (f"-{c}" if c == "saldo" else c)
        ordens[c] = {"href": f"?ordem={nova}" + ("&arquivadas=1" if arquivadas else ""),
                     "seta": ("▼" if desc else "▲") if atual else ""}
    return render(request, "contas/lista.html", blocos=blocos, arquivadas=arquivadas, ordem=ordem, ordens=ordens,
                  agrupar=agrupar, opcoes_agrupar=AGRUPAR_CONTAS,
                  avisos=retificacoes.avisos(db, todas))


@router.get("/cobertura")
def ver_cobertura(request: Request, entidade: IntOpcional = None, u: Utilizador = Depends(utilizador_atual),
                  db: Session = Depends(get_db)):
    """Cronograma dos movimentos de cada conta e data de início da análise."""
    visiveis, ent, papel = entidade_escolhida(request, db, u, entidade)
    if not ent:
        return render(request, "contas/cobertura.html", visiveis=visiveis, entidade=None)
    return render(request, "contas/cobertura.html", visiveis=visiveis, entidade=ent,
                  cob=cobertura.calcular(db, ent.id, date.today()),
                  pode_editar=PAPEIS[papel] >= PAPEIS["editar"])


@router.post("/cobertura")
async def gravar_inicio(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    form = await ler_form(request)
    ent_id = int(form["entidade"]) if str(form.get("entidade", "")).isdigit() else 0
    exigir_papel(db, u, ent_id, "editar")
    ent = db.get(Entidade, ent_id)
    txt = "" if form.get("acao") == "remover" else str(form.get("data_inicio", "")).strip()
    try:
        nova = parse_data(txt) if txt else None
    except ValueError:
        flash(request, "Data inválida.", "erro")
        return RedirectResponse(f"/contas/cobertura?entidade={ent_id}", status_code=303)
    ent.data_inicio = nova
    auditar(db, u, "inicio_analise", f"{ent.nome}: {nova.isoformat() if nova else 'sem limite'}")
    db.commit()
    if nova:
        flash(request, f"Análise de «{ent.nome}» a partir de {nova:%d/%m/%Y}: os movimentos anteriores são "
                       "ignorados nos relatórios, fluxo de caixa e gráficos.")
    else:
        flash(request, f"«{ent.nome}»: todos os movimentos voltam a contar nas análises.")
    return RedirectResponse(f"/contas/cobertura?entidade={ent_id}", status_code=303)


@router.get("/nova")
def nova_form(request: Request, entidade: int, u: Utilizador = Depends(utilizador_atual),
              db: Session = Depends(get_db)):
    exigir_papel(db, u, entidade, "admin")
    ent = db.get(Entidade, entidade)
    valores = {"moeda": "EUR", "tipo": "ORDEM", "titulares": {u.id} if ent.nome != "Mãe" else set(),
               "emp_padrao": PADRAO_OMISSAO, "emp_tipo_taxa": "variavel"}
    return render(request, "contas/form.html", entidade=ent, conta=None, v=valores, **_opcoes(db, entidade))


@router.post("/nova")
async def nova(request: Request, entidade: int, u: Utilizador = Depends(utilizador_atual),
               db: Session = Depends(get_db)):
    exigir_papel(db, u, entidade, "admin")
    form = await ler_form(request)
    ent = db.get(Entidade, entidade)
    dados, erros = _ler_campos(form, db, entidade, None)
    if erros:
        v = {k: str(form.get(k, "")) for k in form.keys()}
        v["titulares"] = {t.id for t in dados["titulares"]}
        return render(request, "contas/form.html", status_code=400, entidade=ent, conta=None, v=v,
                      erros=erros, **_opcoes(db, entidade))
    dados_emp = dados.pop("_emprestimo")
    conta = Conta(entidade_id=entidade, **dados)
    db.add(conta)
    _gravar_emprestimo(db, conta, dados_emp)
    db.flush()
    auditar(db, u, "conta_criada", f"{conta.id}: {ent.nome} / {conta.nome}")
    db.commit()
    flash(request, f"Conta «{conta.nome}» criada.")
    return RedirectResponse("/contas", status_code=303)


def _conta_ou_404(db: Session, conta_id: int) -> Conta:
    c = db.get(Conta, conta_id)
    if not c:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    return c


@router.get("/{conta_id}/editar")
def editar_form(request: Request, conta_id: int, u: Utilizador = Depends(utilizador_atual),
                db: Session = Depends(get_db)):
    c = _conta_ou_404(db, conta_id)
    exigir_papel(db, u, c.entidade_id, "admin")
    return render(request, "contas/form.html", entidade=c.entidade, conta=c, v=_valores_form(c),
                  **_opcoes(db, c.entidade_id, c.id))


@router.post("/{conta_id}/editar")
async def editar(request: Request, conta_id: int, u: Utilizador = Depends(utilizador_atual),
                 db: Session = Depends(get_db)):
    c = _conta_ou_404(db, conta_id)
    exigir_papel(db, u, c.entidade_id, "admin")
    form = await ler_form(request)
    dados, erros = _ler_campos(form, db, c.entidade_id, c.id)
    if erros:
        v = {k: str(form.get(k, "")) for k in form.keys()}
        v["titulares"] = {t.id for t in dados["titulares"]}
        return render(request, "contas/form.html", status_code=400, entidade=c.entidade, conta=c, v=v,
                      erros=erros, **_opcoes(db, c.entidade_id, c.id))
    dados_emp = dados.pop("_emprestimo")
    for k, val in dados.items():
        setattr(c, k, val)
    _gravar_emprestimo(db, c, dados_emp)
    retificacoes.recalcular_contas(db, [c])  # o saldo inicial entra na diferença das retificações
    auditar(db, u, "conta_editada", f"{c.id}: {c.nome}")
    db.commit()
    flash(request, f"Conta «{c.nome}» atualizada.")
    return RedirectResponse("/contas", status_code=303)


@router.post("/{conta_id}/arquivar")
async def arquivar(request: Request, conta_id: int, u: Utilizador = Depends(utilizador_atual),
                   db: Session = Depends(get_db)):
    c = _conta_ou_404(db, conta_id)
    exigir_papel(db, u, c.entidade_id, "admin")
    await ler_form(request)
    c.ativa = not c.ativa
    auditar(db, u, "conta_arquivada" if not c.ativa else "conta_reativada", f"{c.id}: {c.nome}")
    db.commit()
    flash(request, f"Conta «{c.nome}» {'arquivada' if not c.ativa else 'reativada'}.")
    return RedirectResponse("/contas", status_code=303)

