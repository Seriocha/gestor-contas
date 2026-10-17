"""Lista de movimentos (global por entidade ou de uma conta) e qualificação:
categoria, etiquetas, transferência, excluir — um a um ou em massa."""
import re
import uuid
from datetime import date, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..deps import (IntOpcional, auditar, entidade_escolhida, exigir_papel, flash, get_db, papel_em, render,
                    utilizador_atual)
from ..formatos import fmt_montante, parse_data, parse_montante
from ..models import CORES, PAPEIS, Bem, Categoria, Conta, Entidade, Etiqueta, Movimento, RetificacaoSaldo, Utilizador, ViaVerde
from ..qualificacao import aplicar_regras, arvore_categorias, qualificar_manual, repor
from ..servicos import normalizar_descricao
from ..seguranca import ler_form
from .. import investimentos, retificacoes, servicos
from .. import relatorios as rel
from ..servicos import resumo_cartao, saldo_conta

router = APIRouter()
POR_PAGINA = 200
TIPOS_FILTRO = {"": "Todos", "saidas": "Saídas", "entradas": "Entradas", "sem": "Sem categoria",
                "transferencias": "Transferências", "excluidos": "Excluídos",
                "manual": "Qualificados à mão", "regra": "Qualificados por regra"}


_PARECE_VALOR = re.compile(r"^[-+]?\d[\d.,]*\s*€?$")


def _valor_procurado(texto: str) -> int | None:
    """«40,05», «-40.05», «1.000» ou «40,05 €» na caixa de procura → valor absoluto em cêntimos."""
    t = texto.strip()
    if not _PARECE_VALOR.match(t):
        return None
    try:
        return abs(parse_montante(t))
    except ValueError:
        return None


ORDENS = {  # coluna → expressões de ordenação (ascendente)
    "data": (Movimento.data_mov, Movimento.ordem, Movimento.id),
    "conta": (Conta.nome, Movimento.data_mov, Movimento.id),
    "descricao": (func.lower(Movimento.descricao_original), Movimento.data_mov, Movimento.id),
    "categoria": (Categoria.nome, Movimento.data_mov, Movimento.id),
    "montante": (Movimento.montante, Movimento.data_mov, Movimento.id),
}


def _ordenar(q, ordem: str):
    col, desc = ordem.lstrip("-"), ordem.startswith("-")
    if col not in ORDENS:
        col, desc = "data", True
    if col == "categoria":
        q = q.outerjoin(Categoria, Movimento.categoria_id == Categoria.id)
    return q.order_by(*[e.desc().nulls_last() if desc else e.asc().nulls_last() for e in ORDENS[col]])


def _filtrar(q, *, conta_id, categoria, etiqueta, tipo, texto, d_de, d_ate, vmin=None, vmax=None):
    if conta_id:
        q = q.where(Movimento.conta_id == conta_id)
    if categoria:
        q = q.where(or_(Movimento.categoria_id == categoria,
                        Movimento.categoria_id.in_(select(Categoria.id).where(Categoria.pai_id == categoria))))
    if etiqueta:
        q = q.where(Movimento.etiquetas.any(id=etiqueta))
    if tipo == "saidas":
        q = q.where(Movimento.montante < 0)
    elif tipo == "entradas":
        q = q.where(Movimento.montante > 0)
    elif tipo == "sem":
        q = q.where(Movimento.categoria_id.is_(None), ~Movimento.transferencia, ~Movimento.excluido)
    elif tipo == "transferencias":
        q = q.where(Movimento.transferencia)
    elif tipo == "excluidos":
        q = q.where(Movimento.excluido)
    elif tipo == "manual":
        q = q.where(Movimento.qualificado_por == "manual")
    elif tipo == "regra":
        q = q.where(Movimento.qualificado_por == "regra")
    if d_de:
        q = q.where(Movimento.data_mov >= d_de)
    if d_ate:
        q = q.where(Movimento.data_mov <= d_ate)
    if vmin is not None:
        q = q.where(func.abs(Movimento.montante) >= vmin)
    if vmax is not None:
        q = q.where(func.abs(Movimento.montante) <= vmax)
    if texto.strip():
        termo = f"%{texto.strip()}%"
        cond = [Movimento.descricao_original.ilike(termo), Movimento.notas.ilike(termo)]
        valor = _valor_procurado(texto)
        if valor is not None:  # procura também pelo montante (entrada ou saída)
            cond.append(func.abs(Movimento.montante) == valor)
        q = q.where(or_(*cond))
    return q


def _pares_da_pagina(db: Session, movimentos: list[Movimento]) -> dict[int, dict]:
    """Para cada movimento ligado (par_id): letra e cor do par, iguais nos dois lados, e onde está o outro.
    As letras seguem a ordem em que os pares aparecem na página (A, B, …, Z, AA, …)."""
    ligados = [m for m in movimentos if m.par_id]
    if not ligados:
        return {}
    na_pagina = {m.id for m in movimentos}
    fora = {m.par_id for m in ligados if m.par_id not in na_pagina}
    outros = {o.id: o for o in db.scalars(select(Movimento).where(Movimento.id.in_(fora))
                                          .options(selectinload(Movimento.conta)))} if fora else {}
    por_movimento = {m.id: m for m in movimentos}
    pares, chaves = {}, {}
    for m in ligados:
        chave = min(m.id, m.par_id)
        if chave not in chaves:
            n = len(chaves)
            letra = ""
            while True:
                letra = chr(65 + n % 26) + letra
                n = n // 26 - 1
                if n < 0:
                    break
            chaves[chave] = {"letra": letra, "cor": CORES[len(chaves) % len(CORES)]}
        outro = por_movimento.get(m.par_id) or outros.get(m.par_id)
        pares[m.id] = {**chaves[chave], "par_id": m.par_id, "na_pagina": m.par_id in na_pagina,
                       "outro": outro}
    return pares


DIAS_SEMANA = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def _calendario(db: Session, base, *, conta_id, categoria, etiqueta, tipo, q, vmin, vmax, mes: str,
                d_de: date | None, d_ate: date | None, base_q: dict) -> dict:
    """Calendário do mês para o topo dos filtros: movimentos e saldo de cada dia (com os outros filtros);
    clicar num dia filtra esse dia."""
    m = re.fullmatch(r"(\d{4})-(\d{2})", mes or "")
    if m and 1 <= int(m.group(2)) <= 12:
        ini = date(int(m.group(1)), int(m.group(2)), 1)
    elif d_de:
        ini = d_de.replace(day=1)
    else:
        ultimo = db.scalar(_filtrar(base.with_only_columns(func.max(Movimento.data_mov)), conta_id=conta_id,
                                    categoria=categoria, etiqueta=etiqueta, tipo=tipo, texto=q, d_de=None, d_ate=None,
                                    vmin=vmin, vmax=vmax))
        ini = (ultimo or date.today()).replace(day=1)
    fim = rel.fim_do_mes(ini)
    por_dia = {d: (n, s) for d, n, s in db.execute(_filtrar(
        base.with_only_columns(Movimento.data_mov, func.count(), func.sum(Movimento.montante)), conta_id=conta_id,
        categoria=categoria, etiqueta=etiqueta, tipo=tipo, texto=q, d_de=ini, d_ate=fim, vmin=vmin, vmax=vmax)
        .group_by(Movimento.data_mov))}
    sem_datas = {k: v for k, v in base_q.items() if k not in ("de", "ate", "pagina")}
    dia_escolhido = d_de if d_de and d_de == d_ate else None
    semanas, semana = [], [None] * ini.weekday()
    for n in range(1, fim.day + 1):
        d = ini.replace(day=n)
        qtd, soma = por_dia.get(d, (0, 0))
        escolhido = d == dia_escolhido
        href = "?" + urlencode({**sem_datas, "mes": f"{ini:%Y-%m}"} if escolhido
                               else {**sem_datas, "de": d.isoformat(), "ate": d.isoformat()})
        semana.append({"dia": d, "n": qtd, "soma": soma or 0, "escolhido": escolhido, "href": href,
                       "hoje": d == date.today()})
        if len(semana) == 7:
            semanas.append(semana)
            semana = []
    if semana:
        semanas.append(semana + [None] * (7 - len(semana)))
    anterior, seguinte = rel.somar_meses(ini, -1), rel.somar_meses(ini, 1)
    mes_inteiro = d_de == ini and d_ate == fim
    return {
        "rotulo": f"{rel.MESES_PT[ini.month - 1].capitalize()} {ini.year}", "semanas": semanas, "dias": DIAS_SEMANA,
        "anterior": "?" + urlencode({**sem_datas, "mes": f"{anterior:%Y-%m}"}),
        "seguinte": "?" + urlencode({**sem_datas, "mes": f"{seguinte:%Y-%m}"}),
        "mes_inteiro": "?" + urlencode({**sem_datas, "mes": f"{ini:%Y-%m}"} if mes_inteiro
                                       else {**sem_datas, "de": ini.isoformat(), "ate": fim.isoformat()}),
        "mes_inteiro_ativo": mes_inteiro, "dia_escolhido": dia_escolhido,
        "n": sum(v[0] for v in por_dia.values()), "soma": sum(v[1] or 0 for v in por_dia.values()),
    }


def _listar(request: Request, db: Session, u: Utilizador, *, entidade: int | None, conta: Conta | None,
            categoria: int, etiqueta: int, tipo: str, q: str, de: str, ate: str, pagina: int,
            vmin: str = "", vmax: str = "", ordem: str = "-data", anteriores: int = 0, mes: str = ""):
    if conta:
        papel = exigir_papel(db, u, conta.entidade_id, "ver")
        visiveis, ent, _ = entidade_escolhida(request, db, u, conta.entidade_id)
    else:
        visiveis, ent, papel = entidade_escolhida(request, db, u, entidade)
    if not ent:
        return render(request, "movimentos/lista.html", entidade=None, visiveis=visiveis)
    erro = None
    try:
        d_de, d_ate = parse_data(de), parse_data(ate)
    except ValueError as e:
        d_de = d_ate = None
        erro = str(e)
    try:
        v_min = abs(parse_montante(vmin)) if vmin.strip() else None
        v_max = abs(parse_montante(vmax)) if vmax.strip() else None
    except ValueError as e:
        v_min = v_max = None
        erro = str(e)
    base = select(Movimento).join(Conta).where(Conta.entidade_id == ent.id)
    # Cobertura dos dados: por omissão só se veem os movimentos desde o início da análise.
    corte = ent.data_inicio
    ocultos = 0
    if corte:
        q_antes = select(func.count()).select_from(Movimento).join(Conta).where(
            Conta.entidade_id == ent.id, Movimento.data_mov < corte)
        if conta:
            q_antes = q_antes.where(Movimento.conta_id == conta.id)
        ocultos = db.scalar(q_antes)
        if not anteriores:
            base = base.where(Movimento.data_mov >= corte)
    filtro = _filtrar(base, conta_id=conta.id if conta else None, categoria=categoria, etiqueta=etiqueta,
                      tipo=tipo, texto=q, d_de=d_de, d_ate=d_ate, vmin=v_min, vmax=v_max)
    sub = filtro.with_only_columns(Movimento.montante).subquery()
    total, entradas, saidas = db.execute(select(
        func.count(), func.coalesce(func.sum(func.max(sub.c.montante, 0)), 0),
        func.coalesce(func.sum(func.min(sub.c.montante, 0)), 0)).select_from(sub)).one()
    pagina = max(1, pagina or 1)
    ordem = ordem if ordem.lstrip("-") in ORDENS else "-data"
    lista = db.scalars(_ordenar(filtro, ordem).options(
        selectinload(Movimento.conta), selectinload(Movimento.etiquetas),
        selectinload(Movimento.categoria).selectinload(Categoria.pai))
        .offset((pagina - 1) * POR_PAGINA).limit(POR_PAGINA)).all()
    n_sem = db.scalar(_filtrar(base.with_only_columns(func.count()), conta_id=conta.id if conta else None,
                               categoria=0, etiqueta=0, tipo="sem", texto="", d_de=None, d_ate=None))
    filtros = {"categoria": categoria or "", "etiqueta": etiqueta or "", "tipo": tipo, "q": q, "de": de, "ate": ate,
               "vmin": vmin, "vmax": vmax, "anteriores": 1 if anteriores and corte else ""}
    if not conta:
        filtros["entidade"] = ent.id
    # Títulos das colunas: clicar ordena por essa coluna; clicar outra vez inverte.
    base_q = {k: v for k, v in filtros.items() if v}
    ordens = {}
    for col in ORDENS:
        atual = ordem.lstrip("-") == col
        if atual:
            nova = col if ordem.startswith("-") else f"-{col}"
        else:
            nova = f"-{col}" if col in ("data", "montante") else col
        seta = ("▼" if ordem.startswith("-") else "▲") if atual else ""
        ordens[col] = {"href": "?" + urlencode({**base_q, "ordem": nova}), "seta": seta}
    calendario = _calendario(db, base, conta_id=conta.id if conta else None, categoria=categoria, etiqueta=etiqueta,
                             tipo=tipo, q=q, vmin=v_min, vmax=v_max, mes=mes, d_de=d_de, d_ate=d_ate,
                             base_q={**base_q, **({"ordem": ordem} if ordem != "-data" else {})})
    return render(
        request, "movimentos/lista.html", visiveis=visiveis, entidade=ent, conta=conta,
        saldo=saldo_conta(db, conta) if conta else None,
        cartao=resumo_cartao(db, conta, date.today()) if conta and conta.tipo == "CARTAO_CREDITO" else None,
        retificaveis=conta is not None and retificacoes.retificavel(conta),
        desempenho=investimentos.desempenho(db, conta, date.today()) if investimentos.e_investimento(conta) else None,
        naturezas_inv={m.id: investimentos.natureza(m) for m in lista} if investimentos.e_investimento(conta) else {},
        retificacoes=retificacoes.estados(db, conta) if conta and retificacoes.retificavel(conta) else [],
        tem_partida=bool(conta and db.scalar(select(RetificacaoSaldo.id).where(
            RetificacaoSaldo.conta_id == conta.id, RetificacaoSaldo.partida))),
        contas=db.scalars(select(Conta).where(Conta.entidade_id == ent.id).order_by(Conta.nome)).all(),
        categorias=arvore_categorias(db), etiquetas=db.scalars(select(Etiqueta).order_by(Etiqueta.nome)).all(),
        bens=db.scalars(select(Bem).where(Bem.entidade_id == ent.id, Bem.data_venda.is_(None)).order_by(Bem.tipo, Bem.nome)).all(),
        tipos=TIPOS_FILTRO, movimentos=lista, total=total, entradas=entradas, saidas=saidas, n_sem=n_sem,
        pagina=pagina, paginas=max(1, -(-total // POR_PAGINA)), erro=erro, f=filtros,
        query=urlencode({**{k: v for k, v in filtros.items() if v}, **({"ordem": ordem} if ordem != "-data" else {})}),
        ordem=ordem, ordens=ordens, pares=_pares_da_pagina(db, lista), corte=corte, ocultos=ocultos, calendario=calendario,
        anteriores=bool(anteriores and corte),
        pode_editar=PAPEIS[papel] >= PAPEIS["editar"], pode_gerir=PAPEIS[papel] >= PAPEIS["admin"])


@router.get("/movimentos")
def movimentos(request: Request, entidade: IntOpcional = None, conta: IntOpcional = None, categoria: IntOpcional = None,
               etiqueta: IntOpcional = None, tipo: str = "", q: str = "", de: str = "", ate: str = "", pagina: IntOpcional = 1,
               vmin: str = "", vmax: str = "", ordem: str = "-data", anteriores: IntOpcional = 0, mes: str = "",
               u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    if conta:
        return RedirectResponse(f"/contas/{conta}/movimentos?" + urlencode(
            {k: v for k, v in {"categoria": categoria or "", "etiqueta": etiqueta or "", "tipo": tipo, "q": q, "de": de,
                               "ate": ate, "vmin": vmin, "vmax": vmax, "ordem": ordem if ordem != "-data" else "",
                               "anteriores": anteriores or "", "mes": mes}.items()
             if v}), status_code=303)
    return _listar(request, db, u, entidade=entidade, conta=None, categoria=categoria, etiqueta=etiqueta,
                   tipo=tipo if tipo in TIPOS_FILTRO else "", q=q, de=de, ate=ate, pagina=pagina,
                   vmin=vmin, vmax=vmax, ordem=ordem, anteriores=anteriores or 0, mes=mes)


@router.get("/contas/{conta_id}/movimentos")
def movimentos_conta(request: Request, conta_id: int, categoria: IntOpcional = None, etiqueta: IntOpcional = None, tipo: str = "",
                     q: str = "", de: str = "", ate: str = "", pagina: IntOpcional = 1,
                     vmin: str = "", vmax: str = "", ordem: str = "-data", anteriores: IntOpcional = 0, mes: str = "",
                     u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    c = db.get(Conta, conta_id)
    if not c:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    return _listar(request, db, u, entidade=None, conta=c, categoria=categoria, etiqueta=etiqueta,
                   tipo=tipo if tipo in TIPOS_FILTRO else "", q=q, de=de, ate=ate, pagina=pagina,
                   vmin=vmin, vmax=vmax, ordem=ordem, anteriores=anteriores or 0, mes=mes)


def _movimento_editavel(db: Session, u: Utilizador, mov_id: int) -> Movimento:
    m = db.get(Movimento, mov_id)
    if not m:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    exigir_papel(db, u, m.conta.entidade_id, "editar")
    return m


def _int(valor) -> int | None:
    return int(valor) if str(valor or "").isdigit() else None


@router.post("/movimentos/{mov_id}/categoria")
async def mudar_categoria(request: Request, mov_id: int, u: Utilizador = Depends(utilizador_atual),
                          db: Session = Depends(get_db)):
    """Usado pelo seletor de categoria em cada linha (fetch)."""
    m = _movimento_editavel(db, u, mov_id)
    form = await ler_form(request)
    qualificar_manual(db, m, categoria_id=_int(form.get("categoria_id")))
    db.commit()
    return JSONResponse({"ok": True, "categoria": m.categoria.nome_completo if m.categoria else None})


MANUAL = "manual:"  # prefixo do hash_dedup dos movimentos inseridos à mão
JANELA_CANDIDATOS = timedelta(days=10)


def e_manual(m: Movimento) -> bool:
    return (m.hash_dedup or "").startswith(MANUAL)


def _ler_movimento_manual(form, conta: Conta) -> tuple[dict, list[str]]:
    erros = []
    data, valor = None, 0
    try:
        data = parse_data(str(form.get("data", "")))
    except ValueError as e:
        erros.append(str(e))
    if not data and not erros:
        erros.append("Indica a data.")
    descricao = " ".join(str(form.get("descricao", "")).split())[:500]
    if not descricao:
        erros.append("Indica a descrição.")
    try:
        valor = abs(parse_montante(str(form.get("valor", ""))))
    except ValueError as e:
        erros.append(str(e))
    if not valor:
        erros.append("Indica um valor diferente de zero.")
    nat = str(form.get("natureza", ""))
    if investimentos.e_investimento(conta) and nat in investimentos.NATUREZAS:
        entrada = nat in ("aplicacao", "ganho")
    else:
        entrada = form.get("sentido") == "entrada"
    montante = valor if entrada else -valor
    return {"data_mov": data, "descricao_original": descricao, "descricao_norm": normalizar_descricao(descricao),
            "montante": montante}, erros


def _candidatos(db: Session, m: Movimento) -> list[Movimento]:
    """Movimentos que podem ser o outro lado desta transferência: noutra conta da mesma
    entidade, valor simétrico, até 10 dias de diferença, ainda sem par."""
    if m.par_id or not m.montante:
        return []
    return db.scalars(select(Movimento).join(Conta).where(
        Conta.entidade_id == m.conta.entidade_id, Movimento.conta_id != m.conta_id,
        Movimento.montante == -m.montante, Movimento.par_id.is_(None),
        Movimento.data_mov.between(m.data_mov - JANELA_CANDIDATOS, m.data_mov + JANELA_CANDIDATOS))
        .options(selectinload(Movimento.conta)).order_by(Movimento.data_mov).limit(10)).all()


@router.get("/contas/{conta_id}/movimentos/novo")
def novo_manual_form(request: Request, conta_id: int, u: Utilizador = Depends(utilizador_atual),
                     db: Session = Depends(get_db)):
    c = db.get(Conta, conta_id)
    if not c:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    exigir_papel(db, u, c.entidade_id, "editar")
    return render(request, "movimentos/manual.html", conta=c,
                  v={"data": date.today().isoformat(), "sentido": "saida", "natureza": "ganho"},
                  investimento=investimentos.e_investimento(c), naturezas=investimentos.NATUREZAS,
                  categorias=arvore_categorias(db), tem_extrato=_tem_extrato(db, c))


def _tem_extrato(db: Session, c: Conta) -> bool:
    return bool(db.scalar(select(func.count()).select_from(Movimento).where(
        Movimento.conta_id == c.id, Movimento.importacao_id.is_not(None))))


@router.post("/contas/{conta_id}/movimentos/novo")
async def novo_manual(request: Request, conta_id: int, u: Utilizador = Depends(utilizador_atual),
                      db: Session = Depends(get_db)):
    """Movimento inserido à mão (ex.: contas de depósitos e fundos, que não têm extrato para importar)."""
    c = db.get(Conta, conta_id)
    if not c:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    exigir_papel(db, u, c.entidade_id, "editar")
    form = await ler_form(request)
    dados, erros = _ler_movimento_manual(form, c)
    if erros:
        v = {k: str(form.get(k, "")) for k in form.keys()}
        return render(request, "movimentos/manual.html", status_code=400, conta=c, v=v, erros=erros,
                      investimento=investimentos.e_investimento(c), naturezas=investimentos.NATUREZAS,
                      categorias=arvore_categorias(db), tem_extrato=_tem_extrato(db, c))
    ordem = (db.scalar(select(func.max(Movimento.ordem)).where(Movimento.conta_id == c.id,
                                                                Movimento.data_mov == dados["data_mov"])) or 0) + 1
    m = Movimento(conta_id=c.id, moeda=c.moeda, ordem=ordem, hash_dedup=f"{MANUAL}{uuid.uuid4().hex}",
                  notas=str(form.get("notas", "")).strip()[:2000] or None, **dados)
    db.add(m)
    categoria_id = _int(form.get("categoria_id"))
    transferencia = form.get("transferencia") == "1"
    nat = str(form.get("natureza", ""))
    if investimentos.e_investimento(c) and nat in investimentos.NATUREZAS:
        transferencia = nat in ("aplicacao", "resgate")
        if not transferencia and not categoria_id:
            categoria_id = investimentos.categoria_rendimentos(db).id
    if categoria_id or transferencia:
        db.flush()
        qualificar_manual(db, m, categoria_id=categoria_id if not transferencia else None, transferencia=transferencia)
    else:
        aplicar_regras(db, [m])
    db.flush()
    retificacoes.recalcular_contas(db, [c])
    auditar(db, u, "movimento_manual", f"{m.id}: {c.nome} {dados['data_mov']} {dados['montante']}")
    db.commit()
    flash(request, "Movimento criado." + (" Se for o outro lado de uma transferência, liga-o abaixo." if m.transferencia else ""))
    return RedirectResponse(f"/movimentos/{m.id}?voltar=/contas/{c.id}/movimentos", status_code=303)


@router.post("/movimentos/{mov_id}/apagar")
async def apagar_manual(request: Request, mov_id: int, u: Utilizador = Depends(utilizador_atual),
                        db: Session = Depends(get_db)):
    m = _movimento_editavel(db, u, mov_id)
    form = await ler_form(request)
    if not e_manual(m):
        raise HTTPException(status_code=400, detail="Só se apagam movimentos inseridos à mão; os importados anulam-se pela importação.")
    if m.par_id:
        servicos.desligar(db, m)
    conta_id, conta = m.conta_id, m.conta
    auditar(db, u, "movimento_manual_apagado", f"{m.id}: {m.descricao_original} {m.montante}")
    db.delete(m)
    retificacoes.recalcular_contas(db, [conta])
    db.commit()
    flash(request, "Movimento apagado.")
    return RedirectResponse(_voltar_seguro(str(form.get("voltar", ""))) if form.get("voltar") else f"/contas/{conta_id}/movimentos",
                            status_code=303)


@router.get("/movimentos/{mov_id}")
def editar_form(request: Request, mov_id: int, voltar: str = "", u: Utilizador = Depends(utilizador_atual),
                db: Session = Depends(get_db)):
    m = db.get(Movimento, mov_id)
    if not m:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    papel = exigir_papel(db, u, m.conta.entidade_id, "ver")
    par = db.get(Movimento, m.par_id) if m.par_id else None
    if par and not papel_em(db, u, par.conta.entidade_id):
        par = None  # o outro lado está numa entidade que este utilizador não vê
    return render(request, "movimentos/editar.html", m=m, par=par, categorias=arvore_categorias(db),
                  manual=e_manual(m), candidatos=_candidatos(db, m) if not par and PAPEIS[papel] >= PAPEIS["editar"] else [],
                  etiquetas=db.scalars(select(Etiqueta).order_by(Etiqueta.nome)).all(),
                  bens=db.scalars(select(Bem).where(Bem.entidade_id == m.conta.entidade_id).order_by(Bem.tipo, Bem.nome)).all(),
                  via_verde=db.scalars(select(ViaVerde).where(ViaVerde.movimento_id == m.id)
                                       .order_by(ViaVerde.data, ViaVerde.hora)).all(),
                  pode_editar=PAPEIS[papel] >= PAPEIS["editar"], voltar=_voltar_seguro(voltar))


def _voltar_seguro(voltar: str) -> str:
    return voltar if voltar.startswith("/") and not voltar.startswith("//") else "/movimentos"


@router.post("/movimentos/{mov_id}")
async def editar(request: Request, mov_id: int, u: Utilizador = Depends(utilizador_atual),
                 db: Session = Depends(get_db)):
    m = _movimento_editavel(db, u, mov_id)
    form = await ler_form(request)
    if e_manual(m) and form.get("data"):
        dados, erros = _ler_movimento_manual(form, m.conta)
        if erros:
            flash(request, " ".join(erros), "erro")
            return RedirectResponse(f"/movimentos/{m.id}", status_code=303)
        for k, v in dados.items():
            setattr(m, k, v)
    nova_etiqueta = str(form.get("nova_etiqueta", "")).strip()[:50]
    ids = [int(x) for x in form.getlist("etiquetas") if str(x).isdigit()]
    if nova_etiqueta:
        et = db.scalar(select(Etiqueta).where(func.lower(Etiqueta.nome) == nova_etiqueta.lower()))
        if not et:
            et = Etiqueta(nome=nova_etiqueta)
            db.add(et)
            db.flush()
        ids.append(et.id)
    qualificar_manual(db, m, categoria_id=_int(form.get("categoria_id")), transferencia=form.get("transferencia") == "1",
                      excluido=form.get("excluido") == "1", etiquetas_ids=ids,
                      notas=str(form.get("notas", "")).strip()[:2000])
    retificacoes.recalcular_contas(db, [m.conta])
    db.commit()
    flash(request, "Movimento atualizado.")
    return RedirectResponse(_voltar_seguro(str(form.get("voltar", ""))), status_code=303)


def _ids_do_filtro(db: Session, u: Utilizador, form) -> list[int]:
    """Todos os movimentos (todas as páginas) que correspondem ao filtro enviado."""
    conta = db.get(Conta, _int(form.get("f_conta"))) if _int(form.get("f_conta")) else None
    entidade_id = conta.entidade_id if conta else _int(form.get("f_entidade"))
    if not entidade_id:
        raise HTTPException(status_code=400, detail="Filtro sem entidade.")
    exigir_papel(db, u, entidade_id, "editar")
    try:
        d_de, d_ate = parse_data(str(form.get("f_de", ""))), parse_data(str(form.get("f_ate", "")))
    except ValueError:
        raise HTTPException(status_code=400, detail="Datas do filtro inválidas.")
    tipo = str(form.get("f_tipo", ""))
    base = select(Movimento.id).join(Conta).where(Conta.entidade_id == entidade_id)
    corte = db.get(Entidade, entidade_id).data_inicio
    if corte and form.get("f_anteriores") != "1":
        base = base.where(Movimento.data_mov >= corte)
    return list(db.scalars(_filtrar(base, conta_id=conta.id if conta else None, categoria=_int(form.get("f_categoria")),
                                    etiqueta=_int(form.get("f_etiqueta")), tipo=tipo if tipo in TIPOS_FILTRO else "",
                                    texto=str(form.get("f_q", "")), d_de=d_de, d_ate=d_ate,
                                    vmin=_valor_ou_none(form.get("f_vmin")), vmax=_valor_ou_none(form.get("f_vmax")))))


def _valor_ou_none(texto) -> int | None:
    t = str(texto or "").strip()
    try:
        return abs(parse_montante(t)) if t else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Valor do filtro inválido.")


@router.post("/movimentos-lote")
async def lote(request: Request, u: Utilizador = Depends(utilizador_atual), db: Session = Depends(get_db)):
    """Ação em massa sobre os movimentos selecionados ou sobre todo o filtro."""
    form = await ler_form(request)
    voltar = _voltar_seguro(str(form.get("voltar", "")))
    acao = str(form.get("acao", ""))
    todo_filtro = form.get("ambito") == "filtro"
    ids = _ids_do_filtro(db, u, form) if todo_filtro else [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    if not ids or not acao:
        flash(request, "Escolhe os movimentos e a ação." if not todo_filtro else "O filtro não tem movimentos.", "erro")
        return RedirectResponse(voltar, status_code=303)
    movs = db.scalars(select(Movimento).where(Movimento.id.in_(ids))
                      .options(selectinload(Movimento.conta), selectinload(Movimento.etiquetas))).all()
    cache: dict[int, str | None] = {}
    for m in movs:
        e = m.conta.entidade_id
        if e not in cache:
            cache[e] = papel_em(db, u, e)
        if not cache[e] or PAPEIS[cache[e]] < PAPEIS["editar"]:
            raise HTTPException(status_code=403, detail="Sem permissão para esta operação.")
    tipo, _, valor = acao.partition(":")
    extra = ""
    if tipo == "ligar":
        # Exatamente 2 selecionados: liga-os e classifica o par de uma vez.
        if todo_filtro or len(movs) != 2:
            flash(request, "Para ligar, seleciona exatamente 2 movimentos.", "erro")
            return RedirectResponse(voltar, status_code=303)
        a, b = movs
        if a.conta.entidade_id != b.conta.entidade_id or (a.montante > 0) == (b.montante > 0):
            flash(request, "Só se ligam uma entrada e uma saída da mesma entidade.", "erro")
            return RedirectResponse(voltar, status_code=303)
        como = str(form.get("ligar_como", "T"))
        cat = db.get(Categoria, int(como)) if como.isdigit() else None
        if not (cat or como in ("T", "X", "D")):
            flash(request, "Escolhe como classificar o par.", "erro")
            return RedirectResponse(voltar, status_code=303)
        if como == "D":
            # Conta dividida: a entrada (a parte que os outros pagaram) fica na categoria da saída e
            # abate-lhe — nos relatórios conta só a tua parte.
            saida, entrada = (a, b) if a.montante < 0 else (b, a)
            cat = saida.categoria or db.scalar(select(Categoria).where(Categoria.nome == "Restaurantes e cafés"))
            if not cat:
                flash(request, "Classifica primeiro a saída (ex.: Restaurantes) para dividir a conta.", "erro")
                return RedirectResponse(voltar, status_code=303)
            servicos.ligar_manual(db, saida, entrada, transferencia=False)
            for m in (saida, entrada):
                qualificar_manual(db, m, categoria_id=cat.id)
            if not entrada.notas:
                entrada.notas = f"Parte de outros na conta dividida: {saida.descricao_original}"[:500]
            parte = -(saida.montante + entrada.montante)
            auditar(db, u, "conta_dividida", f"{saida.id} ↔ {entrada.id} ({cat.nome_completo})")
            db.commit()
            flash(request, f"Conta dividida em {cat.nome_completo}: pagaste {fmt_montante(-saida.montante)}, "
                           f"recebeste {fmt_montante(entrada.montante)} — a tua parte é {fmt_montante(parte)}.")
            return RedirectResponse(voltar, status_code=303)
        servicos.ligar_manual(db, a, b, transferencia=como == "T")
        for m in (a, b):
            if cat:
                qualificar_manual(db, m, categoria_id=cat.id)
            elif como == "X":
                qualificar_manual(db, m, excluido=True)
        oque = "transferência" if como == "T" else cat.nome_completo if cat else "excluídos dos relatórios"
        auditar(db, u, "movimentos_ligados", f"{a.id} ↔ {b.id} ({oque})")
        db.commit()
        flash(request, f"Movimentos ligados e classificados: {oque}.")
        return RedirectResponse(voltar, status_code=303)
    if tipo in ("repor_regras", "repor_limpar"):
        n = repor(db, movs, reaplicar_regras=tipo == "repor_regras")
        extra = f" Qualificação reposta; {n} voltaram a ser categorizados pelas regras." if tipo == "repor_regras"             else " Qualificação reposta: ficaram sem categoria."
    else:
        for m in movs:
            if tipo == "categoria":
                qualificar_manual(db, m, categoria_id=_int(valor))
            elif tipo == "etiqueta_mais":
                # Etiquetas não contam como qualificação manual: não bloqueiam as regras.
                et = db.get(Etiqueta, _int(valor) or 0)
                if et and et not in m.etiquetas:
                    m.etiquetas.append(et)
            elif tipo == "etiqueta_menos":
                m.etiquetas = [e for e in m.etiquetas if e.id != _int(valor)]
            elif tipo in ("transferencia", "nao_transferencia"):
                qualificar_manual(db, m, transferencia=tipo == "transferencia")
            elif tipo in ("excluir", "incluir"):
                qualificar_manual(db, m, excluido=tipo == "excluir")
            elif tipo == "bem":
                b = db.get(Bem, _int(valor) or 0)
                m.bem_id, m.bem_manual = (b.id if b and b.entidade_id == m.conta.entidade_id else None), True
            else:
                raise HTTPException(status_code=400, detail="Ação desconhecida.")
    auditar(db, u, "qualificacao_lote", f"{acao} em {len(movs)} movimentos" + (" (todo o filtro)" if todo_filtro else ""))
    db.commit()
    flash(request, f"{len(movs)} movimentos atualizados." + extra)
    return RedirectResponse(voltar, status_code=303)


@router.post("/movimentos/{mov_id}/desligar")
async def desligar(request: Request, mov_id: int, u: Utilizador = Depends(utilizador_atual),
                   db: Session = Depends(get_db)):
    m = _movimento_editavel(db, u, mov_id)
    form = await ler_form(request)
    outro = servicos.desligar(db, m)
    auditar(db, u, "movimentos_desligados", f"{m.id} ↔ {outro.id if outro else '—'}")
    db.commit()
    flash(request, "Ligação desfeita. A marca de transferência mantém-se; muda-a no movimento, se for preciso.")
    return RedirectResponse(_voltar_seguro(str(form.get("voltar", ""))) if form.get("voltar") else f"/movimentos/{m.id}",
                            status_code=303)


@router.post("/movimentos/{mov_id}/ligar")
async def ligar(request: Request, mov_id: int, u: Utilizador = Depends(utilizador_atual),
                db: Session = Depends(get_db)):
    """Liga à mão este movimento a outro (o outro lado da mesma transferência)."""
    m = _movimento_editavel(db, u, mov_id)
    form = await ler_form(request)
    outro = _movimento_editavel(db, u, _int(form.get("par_id")) or 0)
    if outro.id == m.id or outro.conta.entidade_id != m.conta.entidade_id or (outro.montante > 0) == (m.montante > 0):
        raise HTTPException(status_code=400, detail="Só se ligam uma entrada e uma saída da mesma entidade.")
    servicos.ligar_manual(db, m, outro)
    auditar(db, u, "movimentos_ligados", f"{m.id} ↔ {outro.id}")
    db.commit()
    flash(request, "Movimentos ligados e marcados como transferência.")
    return RedirectResponse(_voltar_seguro(str(form.get("voltar", ""))) if form.get("voltar") else f"/movimentos/{m.id}",
                            status_code=303)



@router.post("/contas/{conta_id}/retificar")
async def retificar(request: Request, conta_id: int, u: Utilizador = Depends(utilizador_atual),
                    db: Session = Depends(get_db)):
    """Saldo real da conta numa data (Certificados de Aforro, Investimento)."""
    c = db.get(Conta, conta_id)
    if not c:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    exigir_papel(db, u, c.entidade_id, "editar")
    if not retificacoes.retificavel(c):
        raise HTTPException(status_code=400, detail="A retificação de saldo só existe em contas de Aforro e de Investimento.")
    form = await ler_form(request)
    try:
        data = parse_data(str(form.get("data", "")))
        saldo = parse_montante(str(form.get("saldo", "")))
    except ValueError as e:
        flash(request, str(e), "erro")
        return RedirectResponse(f"/contas/{c.id}/movimentos", status_code=303)
    if not data or not str(form.get("saldo", "")).strip():
        flash(request, "Indica a data e o saldo real.", "erro")
        return RedirectResponse(f"/contas/{c.id}/movimentos", status_code=303)
    partida = form.get("partida") == "1" and investimentos.e_investimento(c)
    if partida and db.scalar(select(RetificacaoSaldo.id).where(RetificacaoSaldo.conta_id == c.id,
                                                              RetificacaoSaldo.partida)):
        flash(request, "Esta conta já tem um valor de partida (apaga-o primeiro para o mudar).", "erro")
        return RedirectResponse(f"/contas/{c.id}/movimentos", status_code=303)
    estado = retificacoes.criar(db, c, data, saldo, str(form.get("nota", "")).strip()[:500] or None, u.id,
                                partida=partida)
    auditar(db, u, "saldo_retificado", f"{c.id}: {data} → {saldo} (diferença {estado.diferenca})")
    db.commit()
    from ..formatos import fmt_montante
    if partida:
        flash(request, f"Valor de partida: {fmt_montante(saldo, c.moeda)} em {data:%d/%m/%Y}. "
                       f"{fmt_montante(estado.diferenca, c.moeda)} sem histórico contam como capital investido; o "
                       "ganho/perda passa a contar a partir daqui (retifica o valor de novo nos próximos meses).")
    else:
        flash(request, f"Saldo retificado para {fmt_montante(saldo, c.moeda)} em {data:%d/%m/%Y}. " + (
            f"Diferença por explicar com movimentos: {fmt_montante(estado.diferenca, c.moeda)}." if estado.diferenca
            else "Os movimentos já explicam este saldo."))
    return RedirectResponse(f"/contas/{c.id}/movimentos", status_code=303)


@router.post("/contas/{conta_id}/retificacoes/{ret_id}/apagar")
async def apagar_retificacao(request: Request, conta_id: int, ret_id: int, u: Utilizador = Depends(utilizador_atual),
                             db: Session = Depends(get_db)):
    from ..models import RetificacaoSaldo
    r = db.get(RetificacaoSaldo, ret_id)
    if not r or r.conta_id != conta_id:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    exigir_papel(db, u, r.conta.entidade_id, "editar")
    await ler_form(request)
    conta = r.conta
    auditar(db, u, "retificacao_apagada", f"{conta.id}: {r.data} {r.saldo}")
    db.delete(r)
    retificacoes.recalcular(db, conta)
    db.commit()
    flash(request, "Retificação apagada; o saldo volta a ser só o dos movimentos.")
    return RedirectResponse(f"/contas/{conta_id}/movimentos", status_code=303)
