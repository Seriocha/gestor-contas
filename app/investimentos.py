"""Desempenho das contas de Investimento e de Certificados de Aforro.

Cada movimento destas contas é interpretado assim:
  transferência a entrar (vem de outra conta minha) → APLICAÇÃO (capital investido)
  transferência a sair                              → RESGATE  (capital retirado)
  qualquer outra entrada (valorização, juros, …)    → GANHO
  qualquer outra saída (desvalorização, comissões)  → PERDA
O acerto de uma retificação de saldo é também ganho/perda (diferença para o
valor real). O capital investido é o saldo inicial mais as aplicações menos os
resgates; ganho/perda acumulado = valor da conta − capital investido.

Extratos importados para estas contas (ex.: conta de liquidez da carteira de
fundos BiG «FA Agressiva») são lidos como carteira, não como conta à ordem:
  - «Subscrição de <fundo>» / «Resgate de <fundo>» trocam dinheiro por unidades
    dentro da carteira — não mudam o valor, não se importam;
  - o saldo do ficheiro é só a liquidez — ignora-se; o valor real entra por
    retificação de saldo (valor da carteira indicado pelo banco);
  - entradas/saídas de/para a conta de liquidez são aplicações/resgates
    (transferências); juros, rendimentos e impostos são ganho/perda.
"""
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .formatos import fmt_montante
from .models import Categoria, Conta, Movimento
from .relatorios import HistoricoConta, fim_do_mes, rotulo_mes, somar_meses

TIPOS = {"INVESTIMENTO", "AFORRO", "PENSOES"}
TROCA_INTERNA = re.compile(r"^(SUBSCRI[ÇC][ÃA]O|RESGATE)\s+DE\b", re.IGNORECASE)
TRANSF_CARTEIRA = re.compile(r"CONTA LIQUIDEZ|\bP/ A FA\b|\bDA FA\b", re.IGNORECASE)
CATEGORIA = "Rendimentos de investimento"   # categoria (receita) dos ganhos/perdas inseridos à mão
NATUREZAS = {"aplicacao": "Aplicação", "resgate": "Resgate", "ganho": "Ganho", "perda": "Perda"}
# (a natureza «partida» só existe nos acertos de retificação marcados como valor de partida)


def e_investimento(conta: Conta | None) -> bool:
    return conta is not None and conta.tipo in TIPOS


def natureza(m: Movimento) -> str:
    if (m.hash_dedup or "").startswith("retif:") and m.descricao_norm == "VALOR DE PARTIDA":
        return "partida"   # valor de partida: capital (sem histórico do que foi investido antes)
    if m.transferencia and not (m.hash_dedup or "").startswith("retif:"):
        return "aplicacao" if m.montante > 0 else "resgate"
    return "ganho" if m.montante > 0 else "perda"


def adaptar_extrato(extrato) -> list[str]:
    """Prepara um extrato para uma conta de investimento (ver o topo do módulo). Devolve avisos."""
    trocas = [m for m in extrato.movimentos if TROCA_INTERNA.search(m.descricao or "")]
    extrato.movimentos = [m for m in extrato.movimentos if not TROCA_INTERNA.search(m.descricao or "")]
    tinha_saldo = any(m.saldo is not None for m in extrato.movimentos)
    for m in extrato.movimentos:
        m.saldo = None
        if TRANSF_CARTEIRA.search(m.descricao or ""):
            m.transferencia = True
    avisos = []
    if trocas:
        subs = -sum(m.montante for m in trocas if m.montante < 0)
        resg = sum(m.montante for m in trocas if m.montante > 0)
        avisos.append(f"Conta de investimento: {len(trocas)} subscrições/resgates de fundos são trocas dentro da carteira "
                      f"(subscrições {fmt_montante(subs)}, resgates {fmt_montante(resg)}) e não são importados — não "
                      "mudam o valor da conta.")
    if tinha_saldo:
        avisos.append("O saldo do ficheiro é só a liquidez da carteira e foi ignorado: depois de importar, usa «Retificar "
                      "saldo» com o valor total da carteira indicado pelo banco.")
    return avisos


def categoria_rendimentos(db: Session) -> Categoria:
    cat = db.scalar(select(Categoria).where(Categoria.nome == CATEGORIA, Categoria.pai_id.is_(None)))
    if not cat:
        cat = Categoria(nome=CATEGORIA, natureza="RECEITA")
        db.add(cat)
        db.flush()
    return cat


@dataclass
class Mes:
    mes: date
    aplicacoes: int = 0
    resgates: int = 0
    resultado: int = 0      # ganhos − perdas
    valor_fim: int = 0
    capital_fim: int = 0


@dataclass
class Desempenho:
    valor: int
    capital: int
    resultado_ano: int
    aplicado_total: int
    resgatado_total: int
    partida: int = 0          # valor de partida (capital sem histórico)
    data_partida: date | None = None
    serie: dict = field(default_factory=dict)   # para o gráfico
    meses: list = field(default_factory=list)   # mais recente primeiro

    @property
    def resultado(self) -> int:
        return self.valor - self.capital

    @property
    def rentabilidade(self) -> float | None:
        return None if self.capital <= 0 else self.resultado / self.capital * 100


def desempenho(db: Session, conta: Conta, hoje: date, max_meses: int = 36) -> Desempenho | None:
    movs = db.scalars(select(Movimento).where(Movimento.conta_id == conta.id)
                      .order_by(Movimento.data_mov, Movimento.ordem, Movimento.id)).all()
    if not movs and not conta.saldo_inicial:
        return None
    hist = HistoricoConta(db, conta)

    def conta_no_saldo(m):
        return not conta.data_saldo_inicial or m.data_mov > conta.data_saldo_inicial

    contados = [m for m in movs if conta_no_saldo(m)]
    inicio = conta.data_saldo_inicial or (contados[0].data_mov if contados else hoje)
    fim = max(hoje, contados[-1].data_mov) if contados else hoje
    primeiro_mes = max(inicio.replace(day=1), somar_meses(fim.replace(day=1), -(max_meses - 1)))

    meses, d = [], primeiro_mes
    while d <= fim:
        meses.append(Mes(d))
        d = somar_meses(d, 1)
    por_mes = {x.mes: x for x in meses}

    capital, aplicado, resgatado, resultado_ano = conta.saldo_inicial, 0, 0, 0
    partida, data_partida = 0, None
    capital_antes = {}  # capital até ao fim de cada mês
    for m in contados:
        n = natureza(m)
        if n == "partida":
            capital += m.montante
            partida += m.montante
            data_partida = m.data_mov
        elif n == "aplicacao":
            capital += m.montante
            aplicado += m.montante
        elif n == "resgate":
            capital += m.montante
            resgatado += -m.montante
        elif m.data_mov.year == hoje.year:
            resultado_ano += m.montante
        x = por_mes.get(m.data_mov.replace(day=1))
        if x:
            if n in ("aplicacao", "partida"):
                x.aplicacoes += m.montante
            elif n == "resgate":
                x.resgates += -m.montante
            else:
                x.resultado += m.montante
        capital_antes[m.data_mov.replace(day=1)] = capital
    # capital e valor no fim de cada mês
    cap = conta.saldo_inicial
    for m in contados:
        if m.data_mov.replace(day=1) < primeiro_mes and natureza(m) in ("aplicacao", "resgate", "partida"):
            cap += m.montante
    for x in meses:
        cap = capital_antes.get(x.mes, cap)
        x.capital_fim = cap
        corte = min(fim_do_mes(x.mes), fim)
        x.valor_fim = hist.saldo_em(corte) or 0

    valor = hist.saldo_em(fim) or 0
    serie = {"rotulos": [rotulo_mes(x.mes) for x in meses],
             "valor": [x.valor_fim / 100 for x in meses],
             "capital": [x.capital_fim / 100 for x in meses]}
    return Desempenho(valor=valor, capital=capital, resultado_ano=resultado_ano, aplicado_total=aplicado,
                      resgatado_total=resgatado, partida=partida, data_partida=data_partida,
                      serie=serie, meses=list(reversed(meses)))
