"""Consolidação: volta a correr as ligações e atribuições automáticas sobre os dados já gravados.

Cada importação já faz estes passos, mas só sobre o que existia nesse momento — dados registados
depois (uma matrícula, um carro, uma regra nova, o extrato do banco de um mês antigo) só entram
quando os passos voltam a correr. Todos os passos são idempotentes e nunca mexem no que foi
qualificado ou escolhido à mão.
"""
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import fuelio, patrimonio, retificacoes, servicos, viaverde
from .models import Abastecimento, Bem, Conta, Entidade, Movimento, ViaVerde, norm_matricula
from .qualificacao import aplicar_regras

PASSOS = {
    "regras": ("Regras de categorização", "aplica as regras aos movimentos ainda sem categoria (os tratados à mão ficam)"),
    "cartoes": ("Pagamentos de cartão", "liga o pagamento de cada cartão de crédito ao débito na conta que o paga"),
    "transferencias": ("Transferências entre contas", "liga os dois lados das transferências entre contas próprias"),
    "fuelio": ("Abastecimentos (Fuelio)", "liga os abastecimentos aos débitos do banco e passa-os para o carro certo"),
    "bens": ("Despesas das casas e carros", "revê a atribuição de cada movimento a um bem (padrões e «por omissão»)"),
    "via_verde": ("Via Verde", "liga as linhas da Via Verde aos débitos do banco; a repartição pelos carros segue as matrículas"),
    "retificacoes": ("Saldos retificados", "recalcula os acertos das contas de investimento, aforro e cartão refeição"),
}
SEM_REGRAS = {"EMPRESTIMO", "INVESTIMENTO", "AFORRO", "PENSOES"}


@dataclass
class Diagnostico:
    entidade: Entidade
    sem_categoria: int = 0
    cartoes_por_ligar: int = 0
    vv_por_ligar: int = 0
    vv_total_por_ligar: int = 0
    vv_sem_carro: list = field(default_factory=list)
    abastecimentos_por_ligar: int = 0
    bens: int = 0


def diagnostico(db: Session, ent: Entidade) -> Diagnostico:
    d = Diagnostico(ent)
    d.sem_categoria = db.scalar(select(func.count()).select_from(Movimento).join(Conta).where(
        Conta.entidade_id == ent.id, Conta.tipo.not_in(SEM_REGRAS), Movimento.categoria_id.is_(None),
        ~Movimento.transferencia, ~Movimento.excluido)) or 0
    d.cartoes_por_ligar = db.scalar(select(func.count()).select_from(Movimento).join(Conta).where(
        Conta.entidade_id == ent.id, Conta.tipo == "CARTAO_CREDITO", Movimento.montante > 0,
        Movimento.par_id.is_(None))) or 0
    soltas = db.scalars(select(ViaVerde).where(ViaVerde.entidade_id == ent.id, ViaVerde.movimento_id.is_(None))).all()
    d.vv_por_ligar, d.vv_total_por_ligar = len(soltas), sum(l.valor for l in soltas)
    carros = {norm_matricula(b.matricula) for b in db.scalars(select(Bem).where(Bem.entidade_id == ent.id,
                                                                                 Bem.matricula.is_not(None)))}
    d.vv_sem_carro = sorted({m for m in db.scalars(select(ViaVerde.matricula).where(ViaVerde.entidade_id == ent.id))}
                            - carros)
    d.abastecimentos_por_ligar = db.scalar(select(func.count()).select_from(Abastecimento).join(Bem).where(
        Bem.entidade_id == ent.id, Abastecimento.movimento_id.is_(None))) or 0
    d.bens = db.scalar(select(func.count()).select_from(Bem).where(Bem.entidade_id == ent.id)) or 0
    return d


def consolidar(db: Session, ent: Entidade, passos: set[str]) -> dict[str, int]:
    """Corre os passos escolhidos para a entidade. Devolve {passo: n.º de alterações}."""
    r = {}
    contas = db.scalars(select(Conta).where(Conta.entidade_id == ent.id)).all()
    if "regras" in passos:
        movs = db.scalars(select(Movimento).join(Conta).where(
            Conta.entidade_id == ent.id, Conta.tipo.not_in(SEM_REGRAS), Movimento.categoria_id.is_(None),
            ~Movimento.transferencia, ~Movimento.excluido)).all()
        r["regras"] = aplicar_regras(db, movs)
        db.flush()
    if "cartoes" in passos:
        r["cartoes"] = sum(servicos.ligar_pagamentos_cartao(db, c) for c in contas if c.tipo == "CARTAO_CREDITO")
        db.flush()
    if "transferencias" in passos:
        r["transferencias"] = servicos.ligar_transferencias(db, ent.id)
        db.flush()
    if "fuelio" in passos:
        r["fuelio"] = fuelio.ligar(db, ent.id)
    if "bens" in passos or "fuelio" in passos:
        n = patrimonio.atribuir(db, ent.id)
        if "bens" in passos:
            r["bens"] = n
    if "via_verde" in passos:
        r["via_verde"] = viaverde.ligar(db, ent.id)
    if "retificacoes" in passos:
        retificaveis = [c for c in contas if retificacoes.retificavel(c)]
        retificacoes.recalcular_contas(db, retificaveis)
        r["retificacoes"] = len(retificaveis)
    db.flush()
    return r
