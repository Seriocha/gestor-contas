"""Revolut: limpar as compras repetidas na conta em euros e na conta da moeda.

Até à v0.91, o extrato em PDF punha cada operação noutra moeda em duas contas: na conta em euros, convertida
(«Coop (-12 CHF)», -13,03 €, sem saldo), e na conta da moeda («Coop», -12 CHF). Agora cada operação fica só na conta
da sua moeda, com o valor em euros guardado (Movimento.valor_eur).

Esta limpeza encontra os pares já gravados (mesma entidade, mesma data, mesmo texto e mesmo valor na moeda): passa o
valor em euros e o que foi tratado à mão (categoria, bem, notas, etiquetas, ligações) para o movimento da conta da
moeda, e apaga a cópia da conta em euros. As cópias de uma moeda sem conta no Gestor, ou sem par, ficam como estão:
sem elas essa despesa desaparecia.
"""
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from .models import Conta, Instituicao, Movimento

COPIA = re.compile(r"^(.*) \((-?[\d.]*\d(?:,\d+)?) ([A-Z]{3})\)$", re.S)
HERDA = ("categoria_id", "qualificado_por", "regra_id", "bem_id", "subscricao_id", "notas")
# as tabelas que apontam para um movimento (as linhas repetidas perdem-se com a cópia: UPDATE OR IGNORE)
LIGACOES = (("movimento", "par_id"), ("movimento_etiqueta", "movimento_id"), ("movimento_cartao", "movimento_id"),
            ("documento_ligado", "movimento_id"), ("item_recheio", "movimento_id"), ("via_verde", "movimento_id"),
            ("abastecimento", "movimento_id"), ("manutencao", "movimento_id"), ("viagem_rejeitado", "movimento_id"),
            ("viagem_confirmado", "movimento_id"), ("classificacao_documento", "movimento_id"))


@dataclass
class Previa:
    pares: list = field(default_factory=list)          # [(cópia em euros, movimento na moeda)]
    sem_conta: Counter = field(default_factory=Counter)  # moeda → cópias sem conta dessa moeda (ficam)
    sem_par: Counter = field(default_factory=Counter)    # moeda → cópias sem movimento igual na conta (ficam)

    @property
    def por_moeda(self) -> dict:
        return dict(Counter(m.moeda for _, m in self.pares))


def _nativo(texto: str) -> int | None:
    try:
        return int(Decimal(texto.replace(".", "").replace(",", ".")) * 100)
    except InvalidOperation:
        return None


def previa(db: Session) -> Previa:
    p = Previa()
    contas = db.scalars(select(Conta).join(Instituicao).where(Instituicao.parser == "revolut")).all()
    euros = [c.id for c in contas if c.moeda == "EUR"]
    if not euros:
        return p
    por_moeda = {(c.entidade_id, c.moeda): [] for c in contas if c.moeda != "EUR"}
    for c in contas:
        if c.moeda != "EUR":
            por_moeda[(c.entidade_id, c.moeda)].append(c.id)
    entidade = {c.id: c.entidade_id for c in contas}
    usados = set()
    for m in db.scalars(select(Movimento).where(Movimento.conta_id.in_(euros), Movimento.saldo_apos.is_(None))
                        .order_by(Movimento.data_mov, Movimento.id)):
        x = COPIA.match(m.descricao_original or "")
        if not x:
            continue
        desc, valor, moeda = x.group(1), _nativo(x.group(2)), x.group(3)
        ids = por_moeda.get((entidade[m.conta_id], moeda))
        if not ids:
            p.sem_conta[moeda] += 1
            continue
        par = next((o for o in db.scalars(select(Movimento).where(
            Movimento.conta_id.in_(ids), Movimento.data_mov == m.data_mov, Movimento.montante == valor,
            Movimento.descricao_original == desc)) if o.id not in usados), None)
        if par is None:
            p.sem_par[moeda] += 1
            continue
        usados.add(par.id)
        p.pares.append((m, par))
    return p


def aplicar(db: Session) -> int:
    """Junta cada par no movimento da conta da moeda e apaga a cópia. Devolve quantas cópias saíram."""
    pares = previa(db).pares
    for copia, nativo in pares:
        nativo.valor_eur = copia.montante
        if not nativo.qualificado_por == "manual" and (copia.qualificado_por == "manual" or not nativo.categoria_id):
            for campo in HERDA:
                novo = getattr(copia, campo)
                if novo is not None:
                    setattr(nativo, campo, novo)
            nativo.bem_manual = copia.bem_manual
        elif not nativo.notas:
            nativo.notas = copia.notas
        nativo.excluido = nativo.excluido or copia.excluido
    db.flush()
    for copia, nativo in pares:
        for tabela, coluna in LIGACOES:
            db.execute(text(f"UPDATE OR IGNORE {tabela} SET {coluna} = :novo WHERE {coluna} = :velho"),
                       {"novo": nativo.id, "velho": copia.id})
        if copia.par_id and not nativo.par_id:
            db.execute(text("UPDATE movimento SET par_id = :par WHERE id = :id"), {"par": copia.par_id, "id": nativo.id})
    ids = [c.id for c, _ in pares]
    for i in range(0, len(ids), 500):
        db.execute(delete(Movimento).where(Movimento.id.in_(ids[i:i + 500])))
    db.flush()
    db.expire_all()
    return len(ids)
