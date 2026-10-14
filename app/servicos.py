"""Regras de negócio da importação de movimentos e dos saldos das contas."""
import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Conta, Importacao, Movimento, Utilizador
from .parsers import Extrato

TAMANHO_MAX = 10 * 1024 * 1024


def normalizar_descricao(texto: str) -> str:
    """'COMPRA 6344 Nespresso  Linda-a-Velha' -> 'NESPRESSO LINDA-A-VELHA'."""
    t = re.sub(r"\s+", " ", texto or "").strip().upper()
    t = re.sub(r"^COMPRA \d{4} ", "", t)
    return t


# ---------------------------------------------------------------- ficheiros

def pasta_uploads(data_dir: Path) -> Path:
    p = data_dir / "uploads"
    p.mkdir(parents=True, exist_ok=True)
    return p


def guardar_upload(data_dir: Path, nome: str, conteudo: bytes) -> str:
    """Guarda o ficheiro original (auditoria e re-leitura na confirmação)."""
    sha = hashlib.sha256(conteudo).hexdigest()
    ext = Path(nome).suffix.lower()[:6] if Path(nome).suffix else ""
    destino = pasta_uploads(data_dir) / f"{sha}{ext}"
    if not destino.exists():
        destino.write_bytes(conteudo)
    return sha


def ler_upload(data_dir: Path, sha: str) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise FileNotFoundError(sha)
    for f in pasta_uploads(data_dir).glob(f"{sha}*"):
        return f.read_bytes()
    raise FileNotFoundError(sha)


# ---------------------------------------------------------------- importação

@dataclass
class LinhaPrevia:
    data_mov: object
    data_valor: object
    descricao: str
    descricao_norm: str
    montante: int
    saldo: int | None
    ordem: int
    hash_dedup: str
    duplicado: bool


def preparar(db: Session, conta: Conta, extrato: Extrato) -> list[LinhaPrevia]:
    """Calcula o hash de deduplicação de cada linha e marca as que já existem.

    Com saldo (Santander, ActivoBank) o par data+montante+saldo identifica o
    movimento mesmo quando há duas compras iguais no mesmo dia. Mas o saldo
    pode repetir-se (transferência, devolução e nova transferência igual no
    mesmo dia), por isso as repetições levam também o n.º de ocorrência no
    ficheiro. A 1.ª ocorrência fica sem sufixo, para manter os hashes já
    gravados pela v0.2.0. Sem saldo, o sufixo vai sempre."""
    ocorrencias: Counter = Counter()
    por_dia: Counter = Counter()
    linhas = []
    for m in extrato.movimentos:
        norm = normalizar_descricao(m.descricao)
        chave = (m.data_mov, m.montante, m.saldo, norm)
        ocorrencias[chave] += 1
        por_dia[m.data_mov] += 1
        bruto = f"{conta.id}|{m.data_mov.isoformat()}|{m.montante}|{'' if m.saldo is None else m.saldo}|{norm}"
        if m.saldo is None or ocorrencias[chave] > 1:
            bruto += f"|{ocorrencias[chave]}"
        linhas.append(LinhaPrevia(
            data_mov=m.data_mov, data_valor=m.data_valor, descricao=m.descricao, descricao_norm=norm,
            montante=m.montante, saldo=m.saldo, ordem=por_dia[m.data_mov],
            hash_dedup=hashlib.sha256(bruto.encode()).hexdigest(), duplicado=False,
        ))
    existentes = set(db.scalars(select(Movimento.hash_dedup).where(
        Movimento.conta_id == conta.id, Movimento.hash_dedup.in_([l.hash_dedup for l in linhas]))))
    for l in linhas:
        l.duplicado = l.hash_dedup in existentes
    return linhas


def confirmar(db: Session, conta: Conta, extrato: Extrato, nome: str, sha: str,
              u: Utilizador) -> tuple[Importacao, str | None]:
    """Grava os movimentos novos. Devolve a importação e, se aplicável, uma
    mensagem sobre o saldo inicial calculado automaticamente."""
    linhas = preparar(db, conta, extrato)
    novas = [l for l in linhas if not l.duplicado]
    tinha_movimentos = bool(db.scalar(select(func.count()).select_from(Movimento)
                                      .where(Movimento.conta_id == conta.id)))
    imp = Importacao(
        conta_id=conta.id, utilizador_id=u.id, ficheiro_nome=nome[:255], sha256=sha, parser=extrato.parser,
        data_ini=linhas[0].data_mov if linhas else None, data_fim=linhas[-1].data_mov if linhas else None,
        n_lidos=len(linhas), n_novos=len(novas), n_duplicados=len(linhas) - len(novas),
    )
    db.add(imp)
    db.flush()
    for l in novas:
        db.add(Movimento(
            conta_id=conta.id, importacao_id=imp.id, data_mov=l.data_mov, data_valor=l.data_valor,
            ordem=l.ordem, descricao_original=l.descricao[:500], descricao_norm=l.descricao_norm[:500],
            montante=l.montante, moeda=conta.moeda, saldo_apos=l.saldo, hash_dedup=l.hash_dedup,
        ))

    msg = None
    primeiro = linhas[0] if linhas else None
    if (not tinha_movimentos and conta.saldo_inicial == 0 and conta.data_saldo_inicial is None
            and primeiro and primeiro.saldo is not None and not primeiro.duplicado):
        conta.saldo_inicial = primeiro.saldo - primeiro.montante
        conta.data_saldo_inicial = primeiro.data_mov - timedelta(days=1)
        msg = "Saldo inicial calculado a partir do extrato."
    return imp, msg


def anular(db: Session, imp: Importacao) -> int:
    n = 0
    for m in db.scalars(select(Movimento).where(Movimento.importacao_id == imp.id)):
        db.delete(m)
        n += 1
    imp.estado = "anulada"
    return n


# ---------------------------------------------------------------- saldos

@dataclass
class Saldo:
    atual: int               # melhor estimativa: o saldo do banco, se houver
    calculado: int           # saldo inicial + soma dos movimentos
    do_banco: int | None     # saldo_apos do último movimento
    data: object             # data do último movimento
    n_movimentos: int

    @property
    def diferenca(self) -> int:
        return 0 if self.do_banco is None else self.do_banco - self.calculado


def saldo_conta(db: Session, conta: Conta) -> Saldo:
    q = select(func.count(), func.coalesce(func.sum(Movimento.montante), 0)).where(Movimento.conta_id == conta.id)
    if conta.data_saldo_inicial:
        q = q.where(Movimento.data_mov > conta.data_saldo_inicial)
    n, soma = db.execute(q).one()
    ultimo = db.scalar(select(Movimento).where(Movimento.conta_id == conta.id)
                       .order_by(Movimento.data_mov.desc(), Movimento.ordem.desc(), Movimento.id.desc()).limit(1))
    calculado = conta.saldo_inicial + soma
    do_banco = ultimo.saldo_apos if ultimo else None
    return Saldo(atual=do_banco if do_banco is not None else calculado, calculado=calculado,
                 do_banco=do_banco, data=ultimo.data_mov if ultimo else None, n_movimentos=n)
