"""Empréstimos (habitação): prestações, ligação ao débito, estimativas,
quadro previsional e acertos.

Modelo: a conta do empréstimo tem saldo = −capital em dívida. Cada prestação
é registada na conta do empréstimo como
  +P  «PRESTACAO <n> …»       P = capital + juros + encargos — transferência,
                              ligada (par_id) ao débito da prestação na conta à ordem
  −J  «JUROS PRESTACAO <n>»   despesa (Casa › Juros do empréstimo)
  −E  «ENCARGOS PRESTACAO <n>» despesa (Comissões bancárias), se houver
Efeito líquido na dívida: +P − J − E = capital amortizado.

Fonte principal: o extrato do empréstimo do banco (valores exatos, com o saldo
em dívida). Quando o débito da prestação chega antes desse extrato, a
prestação é ESTIMADA (juros = capital × TAN × dias/360, convenção dos
empréstimos à habitação) e marcada como estimativa; ao importar o extrato,
as estimativas da mesma prestação são substituídas pelos valores reais.
"""
import calendar
import math
import re
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Categoria, Conta, Emprestimo, Movimento
from .qualificacao import compilar

ESCALA_TAXA = 10_000_000  # taxa_e5 / ESCALA_TAXA = taxa anual (154895 → 0,0154895)
PADRAO_OMISSAO = "CRED. HABITACAO|CRED HABITACAO|CREDITO HABITACAO|CRÉDITO HABITAÇÃO|PRESTACAO|EMPRESTIMO"
ESTIMATIVA = "est:"   # prefixo do hash_dedup das prestações estimadas: est:<id do débito>:<P|J|E>
JANELA = timedelta(days=7)
_NUMERO = re.compile(r"^PRESTACAO (\d+)")


# ---------------------------------------------------------------- taxas

def parse_taxa(texto: str | None) -> int | None:
    """'1,54895' / '1.54895%' → 154895 (centésimas de milésima de ponto percentual)."""
    t = (texto or "").strip().replace("%", "").replace(" ", "").replace(",", ".")
    if not t:
        return None
    try:
        v = Decimal(t)
    except InvalidOperation:
        raise ValueError(f"Taxa inválida: {texto!r}")
    if v < 0 or v > 30:
        raise ValueError("A taxa tem de estar entre 0% e 30%.")
    return int((v * 100000).to_integral_value())


def fmt_taxa(e5: int | None) -> str:
    if e5 is None:
        return "—"
    return f"{Decimal(e5) / 100000:.5f}".rstrip("0").rstrip(".").replace(".", ",") + "%"


def taxa_para_input(e5: int | None) -> str:
    return "" if e5 is None else fmt_taxa(e5).rstrip("%")


# ---------------------------------------------------------------- cálculo

def prestacao(capital: int, taxa_e5: int, n: int) -> int:
    """Prestação constante (método francês), em cêntimos."""
    if capital <= 0:
        return 0
    if n <= 0:
        return capital
    i = taxa_e5 / ESCALA_TAXA / 12
    if i == 0:
        return math.ceil(capital / n)
    return round(capital * i / (1 - (1 + i) ** -n))


def juros(capital: int, taxa_e5: int, dias: int = 30) -> int:
    """Juros de um período (atual/360)."""
    return round(capital * taxa_e5 / ESCALA_TAXA * dias / 360)


def _mes_seguinte(d: date, dia: int | None) -> date:
    ano, mes = (d.year + d.month // 12, d.month % 12 + 1)
    dia = dia or d.day
    return date(ano, mes, min(dia, calendar.monthrange(ano, mes)[1]))


@dataclass
class LinhaQuadro:
    n: int
    data: date
    prestacao: int
    juros: int
    capital: int
    encargos: int
    em_divida: int


def quadro(capital: int, taxa_e5: int, n_restantes: int, desde: date, dia: int | None,
           encargos: int = 0, meses: int | None = 12, primeiro_numero: int = 1) -> list[LinhaQuadro]:
    """Quadro previsional à taxa atual (as revisões da taxa mudam-no)."""
    out = []
    p = prestacao(capital, taxa_e5, n_restantes)
    d = desde
    for k in range(1, n_restantes + 1):
        if capital <= 0 or (meses and k > meses):
            break
        seguinte = _mes_seguinte(d, dia)
        j = juros(capital, taxa_e5, (seguinte - d).days)
        amort = min(capital, max(0, p - j)) if k < n_restantes else capital
        capital -= amort
        out.append(LinhaQuadro(primeiro_numero + k - 1, seguinte, amort + j, j, amort, encargos, capital))
        d = seguinte
    return out


# ---------------------------------------------------------------- estado

def _tipo(m: Movimento) -> str | None:
    n = (m.descricao_norm or "").upper()
    if n.startswith("PRESTACAO"):
        return "P"
    if n.startswith("JUROS"):
        return "J"
    if n.startswith("ENCARGOS"):
        return "E"
    return None


def movimentos(db: Session, conta: Conta) -> list[Movimento]:
    return db.scalars(select(Movimento).where(Movimento.conta_id == conta.id)
                      .order_by(Movimento.data_mov, Movimento.ordem, Movimento.id)).all()


def capital_em_divida(db: Session, conta: Conta) -> int:
    from .servicos import saldo_conta  # import local: servicos importa este módulo
    return -saldo_conta(db, conta).atual


def ultima_prestacao(db: Session, conta: Conta) -> tuple[int | None, date | None]:
    """(n.º, data) da última prestação registada (real ou estimada)."""
    numero, data = conta.emprestimo.ultima_prestacao_n if conta.emprestimo else None, None
    for m in movimentos(db, conta):
        if _tipo(m) == "P":
            data = m.data_valor or m.data_mov
            achado = _NUMERO.match(m.descricao_norm or "")
            numero = int(achado.group(1)) if achado else (numero or 0) + 1
    return numero, data


@dataclass
class Resumo:
    capital: int
    taxa_e5: int
    ultima_n: int | None
    ultima_data: date | None
    n_restantes: int
    prestacao_estimada: int
    fim_previsto: date | None
    juros_ano: int
    capital_ano: int
    encargos_ano: int
    n_estimadas: int


def resumo(db: Session, conta: Conta, hoje: date) -> Resumo:
    e = conta.emprestimo
    capital = capital_em_divida(db, conta)
    ultima_n, ultima_data = ultima_prestacao(db, conta)
    n = max(0, e.prazo_meses - (ultima_n or 0)) if ultima_n is not None else e.prazo_meses
    q = quadro(capital, e.taxa_e5, n, ultima_data or hoje, conta.dia_pagamento, e.encargos, meses=None)
    juros_ano = prest_ano = enc_ano = estimadas = 0
    for m in movimentos(db, conta):
        t = _tipo(m)
        if (m.hash_dedup or "").startswith(ESTIMATIVA) and t == "P":
            estimadas += 1
        if m.data_mov.year != hoje.year:
            continue
        if t == "P":
            prest_ano += m.montante
        elif t == "J":
            juros_ano += -m.montante
        elif t == "E":
            enc_ano += -m.montante
    return Resumo(capital=capital, taxa_e5=e.taxa_e5, ultima_n=ultima_n, ultima_data=ultima_data, n_restantes=n,
                  prestacao_estimada=prestacao(capital, e.taxa_e5, n) + e.encargos,
                  fim_previsto=q[-1].data if q else None, juros_ano=juros_ano,
                  capital_ano=prest_ano - juros_ano - enc_ano, encargos_ano=enc_ano, n_estimadas=estimadas)


# ---------------------------------------------------------------- categorias

def _categoria(db: Session, nome: str, pai: str | None) -> Categoria:
    """Garante a categoria (as bases criadas antes da v0.8 não a têm)."""
    raiz = None
    if pai:
        raiz = db.scalar(select(Categoria).where(Categoria.nome == pai, Categoria.pai_id.is_(None)))
        if not raiz:
            raiz = Categoria(nome=pai, natureza="DESPESA")
            db.add(raiz)
            db.flush()
    q = select(Categoria).where(Categoria.nome == nome)
    q = q.where(Categoria.pai_id == raiz.id) if raiz else q.where(Categoria.pai_id.is_(None))
    cat = db.scalar(q)
    if not cat:
        cat = Categoria(nome=nome, natureza="DESPESA", pai=raiz)
        db.add(cat)
        db.flush()
    return cat


def qualificar(db: Session, conta: Conta, movs) -> None:
    """Prestação = transferência; juros e encargos = despesa (exceto se tratados à mão)."""
    cat_j = cat_e = None
    for m in movs:
        if m.qualificado_por == "manual":
            continue
        t = _tipo(m)
        if t == "P":
            m.transferencia, m.categoria_id = True, None
        elif t == "J":
            cat_j = cat_j or _categoria(db, "Juros do empréstimo", "Casa")
            m.categoria_id, m.transferencia = cat_j.id, False
        elif t == "E":
            cat_e = cat_e or _categoria(db, "Comissões bancárias", None)
            m.categoria_id, m.transferencia = cat_e.id, False
        else:
            continue
        m.qualificado_por, m.regra_id = "regra", None


# ---------------------------------------------------------------- ligação e estimativas

def _emprestimos_de(db: Session, conta: Conta) -> list[Conta]:
    if conta.tipo == "EMPRESTIMO":
        return [conta] if conta.emprestimo else []
    return [c for c in db.scalars(select(Conta).where(Conta.tipo == "EMPRESTIMO", Conta.conta_pagamento_id == conta.id))
            if c.emprestimo]


def _livre(d: Movimento) -> bool:
    return d.par_id is None and not (d.qualificado_por == "manual" and not d.transferencia)


def _e_debito_de_prestacao(emp: Conta, d: Movimento, estimada: int) -> bool:
    padrao = (emp.emprestimo.padrao_prestacao or "").strip()
    if padrao:
        return bool(compilar(padrao, "contem").search((d.descricao_norm or d.descricao_original).upper()))
    if not estimada or abs(-d.montante - estimada) > estimada * 0.10:
        return False
    return not emp.dia_pagamento or abs(d.data_mov.day - emp.dia_pagamento) <= 5


def apagar_estimativa(db: Session, debito_id: int) -> int:
    movs = db.scalars(select(Movimento).where(Movimento.hash_dedup.like(f"{ESTIMATIVA}{debito_id}:%"))).all()
    for m in movs:
        db.delete(m)
    return len(movs)


def substituir_estimativas(db: Session, emp: Conta) -> int:
    """Remove as prestações estimadas que já têm a prestação real (do extrato do
    banco) com o mesmo valor ±7 dias; o débito fica livre para se ligar à real."""
    movs = movimentos(db, emp)
    reais = [m for m in movs if _tipo(m) == "P" and not (m.hash_dedup or "").startswith(ESTIMATIVA)]
    n = 0
    for est in [m for m in movs if _tipo(m) == "P" and (m.hash_dedup or "").startswith(ESTIMATIVA)]:
        if any(abs(r.data_mov - est.data_mov) <= JANELA for r in reais):
            debito = db.get(Movimento, est.par_id) if est.par_id else None
            if debito:
                debito.par_id = None
            apagar_estimativa(db, int(est.hash_dedup.split(":")[1]))
            n += 1
    db.flush()
    return n


def ligar_prestacoes(db: Session, emp: Conta) -> int:
    """Liga cada prestação real (+P) ao débito com o mesmo valor (±7 dias) na conta de débito."""
    if not emp.conta_pagamento_id:
        return 0
    prestacoes = [m for m in movimentos(db, emp) if _tipo(m) == "P" and m.par_id is None]
    if not prestacoes:
        return 0
    debitos = [d for d in db.scalars(select(Movimento).where(
        Movimento.conta_id == emp.conta_pagamento_id, Movimento.montante < 0,
        Movimento.data_mov >= prestacoes[0].data_mov - JANELA, Movimento.data_mov <= prestacoes[-1].data_mov + JANELA))
        if _livre(d)]
    n = 0
    for p in prestacoes:
        pares = [d for d in debitos if d.montante == -p.montante and abs(d.data_mov - p.data_mov) <= JANELA]
        if pares:
            d = min(pares, key=lambda x: (abs(x.data_mov - p.data_mov), x.id))
            debitos.remove(d)
            p.par_id, d.par_id = d.id, p.id
            if d.qualificado_por != "manual":
                d.transferencia, d.qualificado_por, d.regra_id = True, "regra", None
            n += 1
    db.flush()
    return n


def estimar_prestacoes(db: Session, emp: Conta) -> int:
    """Débitos de prestação sem prestação real correspondente → prestação estimada."""
    e = emp.emprestimo
    if not emp.conta_pagamento_id:
        return 0
    _, ultima_data = ultima_prestacao(db, emp)
    q = select(Movimento).where(Movimento.conta_id == emp.conta_pagamento_id, Movimento.montante < 0,
                                Movimento.par_id.is_(None))
    limite = max(filter(None, [emp.data_saldo_inicial, ultima_data]), default=None)
    if limite:
        q = q.where(Movimento.data_mov > limite + JANELA)  # anteriores: cobertos pelo extrato ou pelo capital inicial
    n = 0
    for d in db.scalars(q.order_by(Movimento.data_mov, Movimento.ordem, Movimento.id)).all():
        if not _livre(d):
            continue
        capital = capital_em_divida(db, emp)
        numero, anterior = ultima_prestacao(db, emp)
        restantes = max(1, e.prazo_meses - (numero or 0))
        if capital <= 0 or not _e_debito_de_prestacao(emp, d, prestacao(capital, e.taxa_e5, restantes) + e.encargos):
            continue
        p = -d.montante
        dias = (d.data_mov - anterior).days if anterior else 30
        j = min(juros(capital, e.taxa_e5, dias if 20 <= dias <= 40 else 30), p)
        enc = min(e.encargos, p - j)
        num = (numero or 0) + 1
        base = dict(conta_id=emp.id, data_mov=d.data_mov, data_valor=d.data_valor or d.data_mov, moeda=emp.moeda)
        saldo = -capital
        linhas = [("P", f"PRESTACAO {num} (estimada: débito em {d.conta.nome})", p, True)]
        if j:
            linhas.append(("J", f"JUROS PRESTACAO {num} (estimados)", -j, False))
        if enc:
            linhas.append(("E", f"ENCARGOS PRESTACAO {num} (estimados)", -enc, False))
        criados = []
        for ordem, (tipo, desc, valor, trf) in enumerate(linhas, start=1):
            saldo += valor
            m = Movimento(**base, ordem=ordem, descricao_original=desc, descricao_norm=desc.upper(), montante=valor,
                          saldo_apos=saldo, transferencia=trf, hash_dedup=f"{ESTIMATIVA}{d.id}:{tipo}")
            db.add(m)
            criados.append(m)
        db.flush()
        criados[0].par_id, d.par_id = d.id, criados[0].id
        if d.qualificado_por != "manual":
            d.transferencia, d.qualificado_por, d.regra_id = True, "regra", None
        qualificar(db, emp, criados)
        db.flush()
        n += 1
    return n


def processar(db: Session, conta: Conta) -> dict:
    """Corre depois de qualquer importação (conta do empréstimo ou conta de débito)."""
    total = {"substituidas": 0, "ligadas": 0, "estimadas": 0}
    for emp in _emprestimos_de(db, conta):
        total["substituidas"] += substituir_estimativas(db, emp)
        total["ligadas"] += ligar_prestacoes(db, emp)
        total["estimadas"] += estimar_prestacoes(db, emp)
    return total


def acertar_capital(db: Session, conta: Conta, capital_real: int, data: date) -> int:
    """Alinha o capital em dívida com o valor do banco (movimento fora dos relatórios)."""
    diferenca = capital_em_divida(db, conta) - capital_real
    if diferenca:
        db.add(Movimento(conta_id=conta.id, data_mov=data, ordem=9, moeda=conta.moeda,
                         descricao_original="Acerto do capital em dívida (valor do banco)",
                         descricao_norm="ACERTO CAPITAL", montante=diferenca, saldo_apos=-capital_real,
                         excluido=True, qualificado_por="manual", hash_dedup=f"acerto:{conta.id}:{uuid.uuid4().hex}"))
    return diferenca
