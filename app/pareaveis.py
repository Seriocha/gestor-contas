"""Movimentos pareáveis: débitos numa conta e créditos noutra, do mesmo valor,
no mesmo dia ou em dias próximos, ainda sem par — sugestões de ligação para
tratar à mão (a ligação automática ao importar continua igual).

Cada sugestão tem uma confiança:
  +2 os dois lados já marcados como transferência (+1 se só um)
  +1 descrição com pista de transferência (TRF, TRANSF, REVOLUT, MB WAY…)
  +1 no mesmo dia
  −1 se um dos lados já tem categoria de despesa/receita
Quando um movimento aparece em mais do que uma sugestão (ex.: duas transferências
iguais no mesmo dia), a sugestão é marcada como ambígua — ligar uma resolve as outras.
"""
import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased, selectinload

from .models import Conta, Movimento, ParIgnorado

PISTA = re.compile(r"\bTRF\b|TRANSF|REVOLUT|MB ?WAY|CARREGAMENTO|DEPOSITO|DEPÓSITO|LEVANTAMENTO|\bP/|SEPA", re.I)
DIAS = {0: "no mesmo dia", 1: "até 1 dia", 3: "até 3 dias", 5: "até 5 dias", 7: "até 7 dias", 10: "até 10 dias"}


@dataclass
class Par:
    debito: Movimento
    credito: Movimento
    dias: int
    pontos: int = 0
    concorrentes: int = 0   # outras sugestões com um dos mesmos movimentos

    @property
    def chave(self) -> str:
        return f"{self.debito.id}-{self.credito.id}"

    @property
    def confianca(self) -> str:
        return "alta" if self.pontos >= 2 else "média" if self.pontos >= 1 else "baixa"


def _pontuar(p: Par) -> int:
    pts = 2 if (p.debito.transferencia and p.credito.transferencia) else 1 if (p.debito.transferencia or p.credito.transferencia) else 0
    if PISTA.search(p.debito.descricao_original or "") or PISTA.search(p.credito.descricao_original or ""):
        pts += 1
    if p.dias == 0:
        pts += 1
    # Um lado já classificado como despesa/receita (ex.: «Casa › Condomínio») é sinal de que não é o mesmo dinheiro.
    if any(m.categoria_id and not m.transferencia for m in (p.debito, p.credito)):
        pts -= 1
    return pts


def candidatos(db: Session, entidade_id: int, dias: int = 3, desde: date | None = None, conta_id: int | None = None,
               valor_min: int | None = None, limite: int = 300) -> tuple[list[Par], int]:
    """(sugestões, total encontrado antes do limite)."""
    d, c = aliased(Movimento), aliased(Movimento)
    cd, cc = aliased(Conta), aliased(Conta)
    diferenca = func.abs(func.julianday(c.data_mov) - func.julianday(d.data_mov))
    q = (select(d, c, diferenca).join(cd, cd.id == d.conta_id).join(c, and_(
        c.montante == -d.montante, c.conta_id != d.conta_id, c.par_id.is_(None))).join(cc, cc.id == c.conta_id)
         .where(cd.entidade_id == entidade_id, cc.entidade_id == entidade_id, d.montante < 0, d.par_id.is_(None),
                diferenca <= dias)
         .where(~select(ParIgnorado.id).where(ParIgnorado.debito_id == d.id, ParIgnorado.credito_id == c.id).exists()))
    if desde:
        q = q.where(d.data_mov >= desde, c.data_mov >= desde)
    if conta_id:
        q = q.where(or_(d.conta_id == conta_id, c.conta_id == conta_id))
    if valor_min:
        q = q.where(d.montante <= -valor_min)
    linhas = db.execute(q.options(selectinload(d.conta), selectinload(c.conta))
                        .order_by(d.data_mov.desc(), func.abs(d.montante).desc())).all()
    pares = [Par(deb, cred, int(round(dif))) for deb, cred, dif in linhas]
    por_mov: dict[int, int] = {}
    for p in pares:
        por_mov[p.debito.id] = por_mov.get(p.debito.id, 0) + 1
        por_mov[p.credito.id] = por_mov.get(p.credito.id, 0) + 1
    for p in pares:
        p.concorrentes = por_mov[p.debito.id] + por_mov[p.credito.id] - 2
        p.pontos = _pontuar(p)
    pares.sort(key=lambda p: (-p.pontos, p.dias, -p.debito.data_mov.toordinal()))
    return pares[:limite], len(pares)
