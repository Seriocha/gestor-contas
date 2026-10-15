"""Retificação do saldo de contas de Certificados de Aforro e de Investimento.

O utilizador indica o saldo real numa data (valor da carteira, valor dos
certificados). A diferença para o saldo que os movimentos explicam vira um
movimento de acerto, fora dos relatórios (não é receita nem despesa), para o
saldo e o património coincidirem com o valor real.

O acerto não fica congelado: é recalculado sempre que os movimentos da conta
mudam. Se mais tarde se inserirem os movimentos em falta (reforços, resgates,
juros, valorizações), a diferença por explicar encolhe; quando chega a zero o
movimento de acerto desaparece e o aviso da conta também.
"""
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .formatos import fmt_montante
from .models import TIPOS_RETIFICAVEIS, Conta, Movimento, RetificacaoSaldo

PREFIXO = "retif:"  # hash_dedup do movimento de acerto: retif:<id da retificação>
ORDEM_ACERTO = 999  # último movimento do dia


@dataclass
class EstadoRetificacao:
    retificacao: RetificacaoSaldo
    diferenca: int  # parte do saldo real ainda sem movimentos que a expliquem


def retificavel(conta: Conta) -> bool:
    return conta.tipo in TIPOS_RETIFICAVEIS


def _conta_no_saldo(conta: Conta, m: Movimento) -> bool:
    # Mesma regra do saldo calculado: o saldo inicial já inclui o que é anterior à sua data.
    return not conta.data_saldo_inicial or m.data_mov > conta.data_saldo_inicial


def recalcular(db: Session, conta: Conta) -> list[EstadoRetificacao]:
    """Atualiza os movimentos de acerto da conta e devolve o estado de cada retificação."""
    db.flush()
    todos = db.scalars(select(Movimento).where(Movimento.conta_id == conta.id)).all()
    acertos = {m.hash_dedup: m for m in todos if (m.hash_dedup or "").startswith(PREFIXO)}
    normais = [m for m in todos if not (m.hash_dedup or "").startswith(PREFIXO) and _conta_no_saldo(conta, m)]
    retificacoes = db.scalars(select(RetificacaoSaldo).where(RetificacaoSaldo.conta_id == conta.id)
                              .order_by(RetificacaoSaldo.data, RetificacaoSaldo.id)).all()
    estados, acertos_anteriores = [], 0
    for r in retificacoes:
        explicado = conta.saldo_inicial + sum(m.montante for m in normais if m.data_mov <= r.data) + acertos_anteriores
        diferenca = r.saldo - explicado
        chave = f"{PREFIXO}{r.id}"
        m = acertos.pop(chave, None)
        if diferenca:
            if m is None:
                m = Movimento(conta_id=conta.id, moeda=conta.moeda, hash_dedup=chave, excluido=True,
                              qualificado_por="manual", ordem=ORDEM_ACERTO)
                db.add(m)
            m.data_mov = r.data
            m.montante = diferenca
            m.descricao_original = f"Retificação de saldo para {fmt_montante(r.saldo, conta.moeda)} (valor real)"
            m.descricao_norm = "RETIFICACAO DE SALDO"
            m.notas = r.nota
        elif m is not None:
            db.delete(m)
        acertos_anteriores += diferenca
        estados.append(EstadoRetificacao(r, diferenca))
    for orfao in acertos.values():  # retificação apagada
        db.delete(orfao)
    db.flush()
    return estados


def recalcular_contas(db: Session, contas) -> None:
    for c in contas:
        if c is not None and retificavel(c):
            recalcular(db, c)


def avisos(db: Session, conta_ids=None) -> dict[int, dict]:
    """{conta_id: {"data": última data com diferença, "diferenca": soma das diferenças por explicar}}."""
    q = select(Movimento.conta_id, func.max(Movimento.data_mov), func.sum(Movimento.montante)).where(
        Movimento.hash_dedup.like(f"{PREFIXO}%"), Movimento.montante != 0).group_by(Movimento.conta_id)
    if conta_ids is not None:
        q = q.where(Movimento.conta_id.in_(list(conta_ids)))
    return {cid: {"data": d, "diferenca": soma} for cid, d, soma in db.execute(q)}


def criar(db: Session, conta: Conta, data: date, saldo: int, nota: str | None, utilizador_id: int | None) -> EstadoRetificacao:
    r = RetificacaoSaldo(conta_id=conta.id, data=data, saldo=saldo, nota=nota, criado_por=utilizador_id)
    db.add(r)
    db.flush()
    return next(e for e in recalcular(db, conta) if e.retificacao.id == r.id)


def estados(db: Session, conta: Conta) -> list[EstadoRetificacao]:
    """Leitura sem alterações: a diferença de cada retificação é o valor do seu movimento de acerto."""
    acertos = dict(db.execute(select(Movimento.hash_dedup, Movimento.montante).where(
        Movimento.conta_id == conta.id, Movimento.hash_dedup.like(f"{PREFIXO}%"))).all())
    return [EstadoRetificacao(r, acertos.get(f"{PREFIXO}{r.id}", 0))
            for r in db.scalars(select(RetificacaoSaldo).where(RetificacaoSaldo.conta_id == conta.id)
                                .order_by(RetificacaoSaldo.data.desc(), RetificacaoSaldo.id.desc()))]
