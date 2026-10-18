"""Cartões de débito: que cartão (pessoa) fez cada compra da conta à ordem.

O extrato da conta (Santander) traz as compras do cartão uma a uma, mas sem dizer de que cartão. O ficheiro de
movimentos de cada cartão diz: as suas linhas guardam-se em MovimentoCartao (não são movimentos novos) e cada uma
liga-se ao débito igual na conta — mesmo valor, lançado no próprio dia ou até DIAS_LANCAMENTO depois. A ligação
corre ao importar o ficheiro do cartão e também depois de cada extrato da conta (a ordem das importações não importa).
"""
import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CORES, CartaoDebito, Conta, Instituicao, Movimento, MovimentoCartao
from .parsers import Extrato

DIAS_LANCAMENTO = 5


def _palavras(texto: str) -> set[str]:
    t = re.sub(r"^\s*COMPRA\s+(TPA|ESTRANG)?\*?\d{4}\s*", "", (texto or "").upper())
    return {p for p in re.findall(r"[A-Z0-9]{3,}", t)}


def _hashes(extrato: Extrato) -> list[tuple[str, object]]:
    vistos: Counter = Counter()
    out = []
    for m in extrato.movimentos:
        chave = (m.data_mov, m.montante, m.descricao.strip())
        vistos[chave] += 1
        bruto = f"{m.data_mov.isoformat()}|{m.montante}|{m.descricao.strip()}|{vistos[chave]}"
        out.append((hashlib.sha256(bruto.encode()).hexdigest(), m))
    return out


def cartoes_da_conta(db: Session, conta_id: int) -> list[CartaoDebito]:
    return db.scalars(select(CartaoDebito).where(CartaoDebito.conta_id == conta_id).order_by(CartaoDebito.nome)).all()


def contas_possiveis(db: Session, extrato: Extrato) -> list[Conta]:
    """Contas à ordem do mesmo banco (as que já têm este cartão primeiro)."""
    chave = (extrato.parser or "").split("_")[0]
    contas = db.scalars(select(Conta).join(Instituicao).where(Conta.ativa, Conta.tipo == "ORDEM",
                                                               Instituicao.parser == chave)).all()
    com = {c.conta_id for c in db.scalars(select(CartaoDebito).where(CartaoDebito.final == (extrato.conta_ref or "")))}
    return sorted(contas, key=lambda c: (c.id not in com, c.nome))


def _emparelhar(linhas: list[tuple], movs: list[Movimento]) -> dict[int, Movimento]:
    """linhas: [(chave, data, montante, descrição)] → {chave: movimento}. Cada movimento serve uma só linha; entre
    vários candidatos ganha o lançado mais perto da data e, depois, o de descrição mais parecida."""
    por_valor = defaultdict(list)
    for m in movs:
        por_valor[m.montante].append(m)
    usados, out = set(), {}
    for chave, d, v, desc in sorted(linhas, key=lambda x: x[1]):
        pal = _palavras(desc)
        cands = [m for m in por_valor.get(v, []) if m.id not in usados and d <= m.data_mov <= d + timedelta(days=DIAS_LANCAMENTO)]
        if not cands:
            continue
        m = min(cands, key=lambda m: ((m.data_mov - d).days, -len(pal & _palavras(m.descricao_original))))
        usados.add(m.id)
        out[chave] = m
    return out


def _livres(db: Session, conta_id: int, de: date, ate: date) -> list[Movimento]:
    ligados = select(MovimentoCartao.movimento_id).where(MovimentoCartao.movimento_id.is_not(None))
    return db.scalars(select(Movimento).where(
        Movimento.conta_id == conta_id, Movimento.montante < 0, Movimento.data_mov.between(de, ate),
        Movimento.id.not_in(ligados))).all()


def ligar(db: Session, conta_id: int) -> int:
    """Liga as compras dos cartões da conta ainda sem débito aos débitos livres. Devolve quantas ligou."""
    soltas = db.scalars(select(MovimentoCartao).join(CartaoDebito).where(
        CartaoDebito.conta_id == conta_id, MovimentoCartao.movimento_id.is_(None))).all()
    if not soltas:
        return 0
    de, ate = min(s.data for s in soltas), max(s.data for s in soltas) + timedelta(days=DIAS_LANCAMENTO)
    pares = _emparelhar([(s.id, s.data, s.montante, s.descricao) for s in soltas], _livres(db, conta_id, de, ate))
    por_id = {s.id: s for s in soltas}
    for sid, m in pares.items():
        por_id[sid].movimento_id = m.id
    db.flush()
    return len(pares)


@dataclass
class Previa:
    conta: Conta
    final: str
    cartao: CartaoDebito | None
    n_lidas: int = 0
    n_novas: int = 0
    n_ligam: int = 0                      # das novas, quantas encontram o débito na conta
    sem_debito: list = field(default_factory=list)   # novas sem débito (ainda por lançar ou fora do extrato)


def previa(db: Session, conta: Conta, extrato: Extrato) -> Previa:
    final = extrato.conta_ref or "????"
    cartao = db.scalar(select(CartaoDebito).where(CartaoDebito.conta_id == conta.id, CartaoDebito.final == final))
    existentes = set(db.scalars(select(MovimentoCartao.hash).where(MovimentoCartao.cartao_id == cartao.id))) if cartao else set()
    novas = [(h, m) for h, m in _hashes(extrato) if h not in existentes]
    p = Previa(conta, final, cartao, len(extrato.movimentos), len(novas))
    if novas:
        de = min(m.data_mov for _, m in novas)
        ate = max(m.data_mov for _, m in novas) + timedelta(days=DIAS_LANCAMENTO)
        pares = _emparelhar([(h, m.data_mov, m.montante, m.descricao) for h, m in novas], _livres(db, conta.id, de, ate))
        p.n_ligam = len(pares)
        p.sem_debito = [m for h, m in novas if h not in pares]
    return p


def registar(db: Session, conta: Conta, extrato: Extrato) -> tuple[CartaoDebito, int, int]:
    """Guarda as compras novas do cartão (criando-o, se for o 1.º ficheiro) e liga-as. (cartão, novas, ligadas)."""
    final = extrato.conta_ref or "????"
    cartao = db.scalar(select(CartaoDebito).where(CartaoDebito.conta_id == conta.id, CartaoDebito.final == final))
    if not cartao:
        cartao = CartaoDebito(conta_id=conta.id, final=final, nome=f"Cartão …{final}",
                              cor=CORES[len(cartoes_da_conta(db, conta.id)) % len(CORES)])
        db.add(cartao)
        db.flush()
    existentes = set(db.scalars(select(MovimentoCartao.hash).where(MovimentoCartao.cartao_id == cartao.id)))
    novas = 0
    for h, m in _hashes(extrato):
        if h in existentes:
            continue
        db.add(MovimentoCartao(cartao_id=cartao.id, data=m.data_mov, montante=m.montante, descricao=m.descricao[:200], hash=h))
        novas += 1
    db.flush()
    return cartao, novas, ligar(db, conta.id)


def detetar_conta(db: Session, extrato: Extrato) -> tuple[Conta | None, str]:
    contas = contas_possiveis(db, extrato)
    final = extrato.conta_ref or "????"
    com = [c for c in contas if db.scalar(select(CartaoDebito.id).where(CartaoDebito.conta_id == c.id, CartaoDebito.final == final))]
    if len(com) == 1:
        return com[0], f"cartão …{final} já registado nesta conta"
    if len(contas) == 1:
        return contas[0], f"única conta à ordem do {extrato.banco.split(' (')[0]}"
    melhores = sorted(((previa(db, c, extrato).n_ligam, c) for c in contas), key=lambda x: -x[0])
    if melhores and melhores[0][0] and (len(melhores) == 1 or melhores[1][0] < melhores[0][0]):
        return melhores[0][1], f"{melhores[0][0]} compras com o débito nesta conta"
    return None, f"cartão …{final}: escolhe a conta à ordem"


def do_movimento(db: Session, ids: list[int]) -> dict[int, CartaoDebito]:
    """{movimento_id: cartão} para os movimentos dados (etiqueta na lista de movimentos)."""
    if not ids:
        return {}
    return {mid: c for mid, c in db.execute(select(MovimentoCartao.movimento_id, CartaoDebito)
                                            .join(CartaoDebito).where(MovimentoCartao.movimento_id.in_(ids)))}


@dataclass
class GastoCartao:
    cartao: CartaoDebito
    total: int = 0
    n: int = 0
    por_mes: dict = field(default_factory=dict)       # date (1.º do mês) → cêntimos (positivo)


def gastos(db: Session, entidade_id: int, de: date, ate: date) -> list[GastoCartao]:
    """Compras de cada cartão de débito da entidade no período (pelos débitos ligados na conta)."""
    linhas = db.execute(select(CartaoDebito, Movimento).join(MovimentoCartao, MovimentoCartao.cartao_id == CartaoDebito.id)
                        .join(Movimento, Movimento.id == MovimentoCartao.movimento_id).join(Conta, Conta.id == CartaoDebito.conta_id)
                        .where(Conta.entidade_id == entidade_id, Movimento.data_mov.between(de, ate), ~Movimento.excluido)).all()
    por = {}
    for c, m in linhas:
        g = por.setdefault(c.id, GastoCartao(c))
        g.total += -m.montante
        g.n += 1
        mes = m.data_mov.replace(day=1)
        g.por_mes[mes] = g.por_mes.get(mes, 0) - m.montante
    for c in db.scalars(select(CartaoDebito).join(Conta).where(Conta.entidade_id == entidade_id)):
        por.setdefault(c.id, GastoCartao(c))
    return sorted(por.values(), key=lambda g: -g.total)
