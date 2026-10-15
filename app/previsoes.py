"""Receitas previstas: entradas recorrentes (ordenados, cartões refeição) que
ainda não chegaram no mês.

Cada ReceitaPrevista diz em que conta e em que dia do mês costuma entrar. Num
mês, conta como RECEBIDA quando há um crédito real nessa conta que corresponde:
  - ao padrão da descrição (ex.: VENCIMENTO), se houver padrão;
  - senão, à categoria escolhida (ex.: Salário);
  - senão, a um valor próximo (±15%) do esperado.
Enquanto não for recebida, o fluxo de caixa do mês soma-a como «prevista». O
valor, quando não é fixado, é a média dos últimos 3 créditos reais que
corresponderam.
"""
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import Categoria, Conta, Movimento, ReceitaPrevista
from .qualificacao import compilar
from .relatorios import fim_do_mes

CATEGORIA_REFEICAO = "Subsídio de alimentação"
TOLERANCIA = 0.15


@dataclass
class Estado:
    prev: ReceitaPrevista
    data: date                      # data prevista neste mês
    valor: int | None               # valor esperado (cêntimos)
    recebida: Movimento | None = None

    @property
    def pendente(self) -> bool:
        return self.recebida is None and bool(self.valor)


def categoria_refeicao(db: Session) -> Categoria:
    cat = db.scalar(select(Categoria).where(Categoria.nome == CATEGORIA_REFEICAO, Categoria.pai_id.is_(None)))
    if not cat:
        cat = Categoria(nome=CATEGORIA_REFEICAO, natureza="RECEITA")
        db.add(cat)
        db.flush()
    return cat


def corresponde(prev: ReceitaPrevista, m: Movimento, valor_ref: int | None) -> bool:
    if m.montante <= 0 or m.excluido or m.transferencia:
        return False
    if prev.padrao:
        texto = (m.descricao_norm or m.descricao_original or "").upper()
        return bool(compilar(prev.padrao, "contem").search(texto))
    if prev.categoria_id:
        return m.categoria_id == prev.categoria_id
    return bool(valor_ref) and abs(m.montante - valor_ref) <= valor_ref * TOLERANCIA


def _creditos(db: Session, conta_ids: list[int], ate: date) -> list[Movimento]:
    if not conta_ids:
        return []
    return db.scalars(select(Movimento).where(Movimento.conta_id.in_(conta_ids), Movimento.montante > 0,
                                              Movimento.data_mov <= ate)
                      .order_by(Movimento.data_mov.desc(), Movimento.id.desc())).all()


def valor_esperado(prev: ReceitaPrevista, creditos: list[Movimento], antes_de: date) -> int | None:
    """Valor fixado, ou média dos últimos 3 créditos reais que corresponderam."""
    if prev.valor:
        return prev.valor
    if not (prev.padrao or prev.categoria_id):
        return None
    ultimos = [m.montante for m in creditos
               if m.conta_id == prev.conta_id and m.data_mov < antes_de and corresponde(prev, m, None)][:3]
    return round(sum(ultimos) / len(ultimos)) if ultimos else None


def da_entidade(db: Session, entidade_id: int, so_ativas: bool = False) -> list[ReceitaPrevista]:
    q = (select(ReceitaPrevista).join(Conta).where(Conta.entidade_id == entidade_id)
         .options(selectinload(ReceitaPrevista.conta).selectinload(Conta.instituicao),
                  selectinload(ReceitaPrevista.categoria).selectinload(Categoria.pai))
         .order_by(ReceitaPrevista.dia, ReceitaPrevista.descricao))
    if so_ativas:
        q = q.where(ReceitaPrevista.ativa, Conta.ativa)
    return db.scalars(q).all()


def estado_mes(db: Session, entidade_id: int, mes: date, previsoes: list[ReceitaPrevista] | None = None) -> list[Estado]:
    """Estado de cada receita prevista ativa no mês de `mes`."""
    ini, fim = mes.replace(day=1), fim_do_mes(mes)
    previsoes = da_entidade(db, entidade_id, so_ativas=True) if previsoes is None else previsoes
    creditos = _creditos(db, sorted({p.conta_id for p in previsoes}), fim)
    usados: set[int] = set()
    out = []
    for p in previsoes:
        valor = valor_esperado(p, creditos, ini)
        e = Estado(p, date(ini.year, ini.month, min(p.dia, fim.day)), valor)
        # o crédito mais próximo do dia previsto, dentro do mês
        candidatos = [m for m in creditos if m.conta_id == p.conta_id and ini <= m.data_mov <= fim
                      and m.id not in usados and corresponde(p, m, valor)]
        if candidatos:
            e.recebida = min(candidatos, key=lambda m: abs((m.data_mov - e.data).days))
            usados.add(e.recebida.id)
        out.append(e)
    return out
