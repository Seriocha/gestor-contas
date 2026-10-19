"""Meses importados: para cada conta, uma grelha anos × meses com os meses que têm extrato (verde) e os que faltam
(vermelho), entre o primeiro mês com dados e o mês atual.

Um mês conta como importado quando tem movimentos (importados ou à mão) ou quando cabe no período de uma importação
confirmada — um extrato mensal sem movimentos também cobre o mês. As retificações de saldo não contam.
"""
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Conta, Importacao, Movimento
from .relatorios import MESES_PT

# Ordem dos tipos no filtro: primeiro as contas à ordem (onde os extratos mensais importam mais)
ORDEM_TIPOS = ["ORDEM", "CARTAO_CREDITO", "CARTEIRA_DIGITAL", "CARTAO_REFEICAO", "POUPANCA", "EMPRESTIMO",
               "INVESTIMENTO", "AFORRO", "PENSOES"]


@dataclass
class Celula:
    ano: int
    mes: int
    estado: str          # ok | falta | curso (mês atual ainda sem dados) | fora (antes do 1.º mês ou no futuro)
    n: int = 0           # movimentos no mês

    @property
    def chave(self) -> str:
        return f"{self.ano}-{self.mes:02d}"


@dataclass
class Grelha:
    conta: Conta
    anos: list = field(default_factory=list)         # [(ano, [Celula × 12], n_ok, n_falta)]
    n_ok: int = 0
    n_falta: int = 0
    faltas: list = field(default_factory=list)       # textos «jan–dez 2020», «mar 2023»
    primeiro: date | None = None

    @property
    def completa(self) -> bool:
        return self.n_ok > 0 and self.n_falta == 0


def _meses_entre(a: date, b: date):
    ano, mes = a.year, a.month
    while (ano, mes) <= (b.year, b.month):
        yield ano, mes
        ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)


def _intervalos(faltas: list[tuple[int, int]]) -> list[str]:
    """[(2020,1)…(2020,12), (2023,3)] → ['jan–dez 2020', 'mar 2023'] (sequências seguidas juntas)."""
    out, grupo = [], []
    seguinte = lambda a, m: (a + 1, 1) if m == 12 else (a, m + 1)
    for am in faltas:
        if grupo and seguinte(*grupo[-1]) != am:
            out.append(grupo)
            grupo = []
        grupo.append(am)
    if grupo:
        out.append(grupo)
    textos = []
    for g in out:
        (a0, m0), (a1, m1) = g[0], g[-1]
        if len(g) == 1:
            textos.append(f"{MESES_PT[m0 - 1]} {a0}")
        elif a0 == a1:
            textos.append(f"{MESES_PT[m0 - 1]}–{MESES_PT[m1 - 1]} {a0}")
        else:
            textos.append(f"{MESES_PT[m0 - 1]} {a0} – {MESES_PT[m1 - 1]} {a1}")
    return textos


def calcular(db: Session, contas: list[Conta], hoje: date) -> list[Grelha]:
    if not contas:
        return []
    ids = [c.id for c in contas]
    por_mes = Counter()
    for cid, am, n in db.execute(
            select(Movimento.conta_id, func.strftime("%Y-%m", Movimento.data_mov), func.count())
            .where(Movimento.conta_id.in_(ids), ~Movimento.hash_dedup.like("retif:%"))
            .group_by(Movimento.conta_id, func.strftime("%Y-%m", Movimento.data_mov))):
        por_mes[(cid, am)] = n
    cobertos: dict[int, set] = {i: set() for i in ids}
    for cid, a, b in db.execute(select(Importacao.conta_id, Importacao.data_ini, Importacao.data_fim)
                                .where(Importacao.conta_id.in_(ids), Importacao.estado == "confirmada",
                                       Importacao.data_ini.is_not(None), Importacao.data_fim.is_not(None))):
        cobertos[cid].update(f"{y}-{m:02d}" for y, m in _meses_entre(a, b))

    grelhas = []
    for c in contas:
        com_dados = {am for (cid, am) in por_mes if cid == c.id} | cobertos[c.id]
        g = Grelha(c)
        if not com_dados:
            grelhas.append(g)
            continue
        a0, m0 = map(int, min(com_dados).split("-"))
        g.primeiro = date(a0, m0, 1)
        faltas = []
        for ano in range(a0, hoje.year + 1):
            celulas, ok, falta = [], 0, 0
            for mes in range(1, 13):
                chave = f"{ano}-{mes:02d}"
                n = por_mes.get((c.id, chave), 0)
                if (ano, mes) < (a0, m0) or (ano, mes) > (hoje.year, hoje.month):
                    estado = "fora"
                elif chave in com_dados:
                    estado = "ok"
                elif (ano, mes) == (hoje.year, hoje.month):
                    estado = "curso"         # o extrato deste mês ainda não saiu
                else:
                    estado = "falta"
                    faltas.append((ano, mes))
                ok += estado == "ok"
                falta += estado == "falta"
                celulas.append(Celula(ano, mes, estado, n))
            g.anos.append((ano, celulas, ok, falta))
            g.n_ok += ok
            g.n_falta += falta
        g.anos.reverse()                     # o ano mais recente em cima
        g.faltas = _intervalos(faltas)
        grelhas.append(g)
    return grelhas


def tipos_presentes(contas: list[Conta]) -> list[str]:
    presentes = {c.tipo for c in contas}
    return [t for t in ORDEM_TIPOS if t in presentes] + sorted(presentes - set(ORDEM_TIPOS))
