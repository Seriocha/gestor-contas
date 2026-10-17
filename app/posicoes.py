"""Posições (fundos, títulos) das contas de investimento e de pensões.

Valor de cada posição = unidades de participação × cotação; valias = valor − valor de aquisição. O valor da
conta = soma das posições + liquidez (saldo contabilístico). Sempre que uma posição, a cotação ou a liquidez
mudam, o saldo real da conta é acertado com uma retificação nessa data («Valor das posições»), para o
património e o desempenho (valor vs. capital investido) seguirem o valor de mercado.
"""
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import retificacoes
from .models import Conta, Posicao, RetificacaoSaldo

TIPOS = {"INVESTIMENTO", "PENSOES"}
NOTA = "Valor das posições"
NOTA_PARTIDA = "Capital: valor de aquisição das posições + liquidez"
CATEGORIAS = ["Ações", "Obrigações", "Misto", "Imobiliário", "Monetário", "PPR", "Fundo de pensões", "Flexível",
              "Matérias-primas", "Outro"]


def tem_posicoes(conta: Conta | None) -> bool:
    return conta is not None and conta.tipo in TIPOS


@dataclass
class Resumo:
    posicoes: list
    valor: int
    aquisicao: int
    liquidez: int | None
    data: date | None

    @property
    def total(self) -> int:
        return self.valor + (self.liquidez or 0)

    @property
    def valias(self) -> int | None:
        return self.valor - self.aquisicao if self.aquisicao else None

    @property
    def valias_pct(self) -> float | None:
        return self.valias / self.aquisicao * 100 if self.aquisicao else None


def resumo(conta: Conta) -> Resumo:
    ps = [p for p in conta.posicoes if p.unidades]
    com_aquisicao = [p for p in ps if p.valor_aquisicao]
    return Resumo(conta.posicoes, sum(p.valor for p in ps),
                  sum(p.valor_aquisicao for p in com_aquisicao) if len(com_aquisicao) == len(ps) else 0,
                  conta.liquidez, max((p.data_cotacao for p in ps if p.data_cotacao), default=None))


def sincronizar(db: Session, conta: Conta, data: date, utilizador_id: int | None = None) -> None:
    """Acerta o saldo real da conta para o valor das posições + liquidez, na data dada."""
    if not conta.posicoes and conta.liquidez is None:
        return
    total = resumo(conta).total
    r = db.scalar(select(RetificacaoSaldo).where(RetificacaoSaldo.conta_id == conta.id, RetificacaoSaldo.data == data,
                                                 RetificacaoSaldo.nota == NOTA))
    if r:
        r.saldo = total
    else:
        db.add(RetificacaoSaldo(conta_id=conta.id, data=data, saldo=total, nota=NOTA, criado_por=utilizador_id))
    _capital_de_partida(db, conta, data, utilizador_id)
    db.flush()
    retificacoes.recalcular(db, conta)


def _capital_de_partida(db: Session, conta: Conta, data: date, utilizador_id: int | None) -> None:
    """Sem histórico de aplicações, o capital investido é o valor de aquisição das posições + liquidez: um
    «valor de partida» (gerido aqui) antes da 1.ª avaliação, para o ganho mostrado ser as valias."""
    from .models import Movimento
    r = resumo(conta)
    partidas = db.scalars(select(RetificacaoSaldo).where(RetificacaoSaldo.conta_id == conta.id,
                                                         RetificacaoSaldo.partida)).all()
    minha = next((p for p in partidas if p.nota == NOTA_PARTIDA), None)
    if partidas and not minha:
        return                                  # o utilizador indicou o seu valor de partida
    aplicacoes = db.scalar(select(Movimento.id).where(
        Movimento.conta_id == conta.id, Movimento.transferencia, Movimento.montante > 0,
        ~Movimento.hash_dedup.startswith("retif:")).limit(1))
    if aplicacoes or not r.aquisicao:
        if minha:
            db.delete(minha)
        return
    primeira = db.scalar(select(func.min(RetificacaoSaldo.data)).where(
        RetificacaoSaldo.conta_id == conta.id, ~RetificacaoSaldo.partida)) or data
    quando = conta.data_contrato if conta.data_contrato and conta.data_contrato < primeira else primeira - timedelta(days=1)
    capital = r.aquisicao + (conta.liquidez or 0)
    if minha:
        minha.saldo, minha.data = capital, quando
    else:
        db.add(RetificacaoSaldo(conta_id=conta.id, data=quando, saldo=capital, partida=True, nota=NOTA_PARTIDA,
                                criado_por=utilizador_id))
