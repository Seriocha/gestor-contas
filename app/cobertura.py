"""Cobertura dos dados: de quando a quando há movimentos em cada conta, e a
data de início da análise sugerida.

Enquanto não houver extratos antigos de todas as contas, os meses em que só
algumas têm movimentos dão relatórios enganadores (receitas sem as despesas do
cartão, etc.). A sugestão é o 1.º dia do mês seguinte ao primeiro movimento da
conta que começa mais tarde — a partir daí todas as contas de fluxo têm dados.
Contas poupança, de investimento, aforro e empréstimo não entram na sugestão (os seus
movimentos são esporádicos ou manuais, ou a conta foi aberta mais tarde).
"""
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .models import Conta, Movimento
from .relatorios import MESES_PT, somar_meses

TIPOS_FLUXO = {"ORDEM", "CARTAO_CREDITO", "CARTEIRA_DIGITAL", "CARTAO_REFEICAO"}


@dataclass
class Linha:
    conta: Conta
    primeiro: date | None
    ultimo: date | None
    n: int
    conta_para_sugestao: bool
    # posições (%) no cronograma
    esq: float = 0
    larg: float = 0


@dataclass
class Cobertura:
    linhas: list
    ini: date                 # início do eixo
    fim: date                 # fim do eixo
    sugestao: date | None
    limitada_por: Linha | None
    marcas: list              # [(%, rótulo)]

    def pct(self, d: date | None) -> float | None:
        if d is None:
            return None
        total = max((self.fim - self.ini).days, 1)
        return min(max((d - self.ini).days / total * 100, 0), 100)


def _marcas(ini: date, fim: date, pct) -> list[tuple[float, str]]:
    meses = (fim.year - ini.year) * 12 + fim.month - ini.month
    passo = 1 if meses <= 14 else 3 if meses <= 40 else 12
    d = ini.replace(day=1)
    if passo == 3:
        d = date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)
    elif passo == 12:
        d = date(d.year, 1, 1)
    out = []
    while d <= fim:
        if d >= ini:
            rot = str(d.year) if passo == 12 else f"{MESES_PT[d.month - 1]} {d:%y}"
            out.append((pct(d), rot))
        d = somar_meses(d, passo)
    return out


def calcular(db: Session, entidade_id: int, hoje: date) -> Cobertura:
    contas = db.scalars(select(Conta).where(Conta.entidade_id == entidade_id, Conta.ativa)
                        .options(selectinload(Conta.instituicao)).order_by(Conta.nome)).all()
    ids = [c.id for c in contas]
    extremos = {cid: (a, b, n) for cid, a, b, n in db.execute(
        select(Movimento.conta_id, func.min(Movimento.data_mov), func.max(Movimento.data_mov), func.count())
        .where(Movimento.conta_id.in_(ids), ~Movimento.hash_dedup.like("retif:%"))
        .group_by(Movimento.conta_id))} if ids else {}
    linhas = []
    for c in contas:
        a, b, n = extremos.get(c.id, (None, None, 0))
        linhas.append(Linha(c, a, b, n, c.tipo in TIPOS_FLUXO and n > 0))
    linhas.sort(key=lambda l: (l.primeiro is None, l.primeiro or hoje, l.conta.nome.lower()))
    candidatas = [l for l in linhas if l.conta_para_sugestao]
    limitada = max(candidatas, key=lambda l: l.primeiro) if candidatas else None
    sugestao = None
    if limitada:
        p = limitada.primeiro
        sugestao = p if p.day == 1 else somar_meses(p.replace(day=1), 1)
    datas = [l.primeiro for l in linhas if l.primeiro]
    ini = min(datas).replace(day=1) if datas else hoje.replace(day=1)
    fim = max([hoje] + [l.ultimo for l in linhas if l.ultimo])
    cob = Cobertura(linhas, ini, fim, sugestao, limitada, [])
    for l in linhas:
        if l.primeiro:
            l.esq = cob.pct(l.primeiro)
            l.larg = max(cob.pct(l.ultimo) - l.esq, 0.6)
    cob.marcas = _marcas(ini, fim, cob.pct)
    return cob
