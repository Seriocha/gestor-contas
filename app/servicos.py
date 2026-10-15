"""Regras de negócio da importação de movimentos e dos saldos das contas."""
import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Conta, Importacao, Movimento, Utilizador
from .parsers import Extrato
from . import emprestimos
from .qualificacao import aplicar_regras

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
    movs = [Movimento(
        conta_id=conta.id, importacao_id=imp.id, data_mov=l.data_mov, data_valor=l.data_valor,
        ordem=l.ordem, descricao_original=l.descricao[:500], descricao_norm=l.descricao_norm[:500],
        montante=l.montante, moeda=conta.moeda, saldo_apos=l.saldo, hash_dedup=l.hash_dedup,
    ) for l in novas]
    db.add_all(movs)
    # Nas contas de empréstimo a qualificação é própria (prestação/juros/encargos), não pelas regras gerais.
    imp.n_qualificados = aplicar_regras(db, movs) if conta.tipo != "EMPRESTIMO" else 0
    db.flush()
    imp.n_pagamentos_cartao = ligar_pagamentos_cartao(db, conta)
    if conta.tipo == "EMPRESTIMO":
        emprestimos.qualificar(db, conta, movs)
        db.flush()
    imp.emprestimo = emprestimos.processar(db, conta)

    msg = None
    primeiro = linhas[0] if linhas else None
    if (not tinha_movimentos and conta.saldo_inicial == 0 and conta.data_saldo_inicial is None
            and primeiro and primeiro.saldo is not None and not primeiro.duplicado):
        conta.saldo_inicial = primeiro.saldo - primeiro.montante
        conta.data_saldo_inicial = primeiro.data_mov - timedelta(days=1)
        msg = "Saldo inicial calculado a partir do extrato."
    return imp, msg


# ---------------------------------------------------------------- cartões de crédito

PAGAMENTO_CARTAO = re.compile(r"^(PAG[.\s]|PAGAMENTO\b|PGTO\b)", re.IGNORECASE)
JANELA_PAGAMENTO = timedelta(days=7)


def _marcar_transferencia(m: Movimento) -> bool:
    if m.transferencia or m.qualificado_por == "manual":
        return False
    m.transferencia = True
    m.qualificado_por = "regra"
    m.regra_id = None
    return True


def pagamentos_do_cartao(db: Session, cartao: Conta) -> list[Movimento]:
    """Créditos no cartão que são pagamentos (descrição "PAG…"/"PAGAMENTO…"),
    ou que já foram ligados a um débito."""
    return [m for m in db.scalars(select(Movimento).where(
        Movimento.conta_id == cartao.id, Movimento.montante > 0).order_by(Movimento.data_mov, Movimento.id))
        if m.par_id or PAGAMENTO_CARTAO.search(m.descricao_norm or m.descricao_original)]


def _pode_ligar(m: Movimento) -> bool:
    # Um movimento que alguém marcou à mão como "não é transferência" fica de fora.
    return not (m.qualificado_por == "manual" and not m.transferencia)


def ligar_pagamentos_cartao(db: Session, conta: Conta) -> int:
    """O pagamento de um cartão não é despesa (a despesa são as compras do
    cartão): marca como transferência o crédito "PAG…" no cartão e o débito
    do mesmo valor na conta que o paga (±7 dias, o mais próximo), e grava a
    ligação entre os dois (par_id). Corre ao importar tanto o cartão como a
    conta pagadora, por isso a ordem das importações não importa. Nunca muda
    a qualificação de movimentos tratados à mão. Devolve quantos movimentos
    foram marcados ou ligados."""
    if conta.tipo == "CARTAO_CREDITO":
        cartoes = [conta]
    else:
        cartoes = db.scalars(select(Conta).where(Conta.tipo == "CARTAO_CREDITO",
                                                 Conta.conta_pagamento_id == conta.id)).all()
    n = 0
    for cartao in cartoes:
        pagamentos = [m for m in pagamentos_do_cartao(db, cartao) if _pode_ligar(m)]
        if not pagamentos:
            continue
        livres = []
        if cartao.conta_pagamento_id:
            livres = [d for d in db.scalars(select(Movimento).where(
                Movimento.conta_id == cartao.conta_pagamento_id, Movimento.montante < 0, Movimento.par_id.is_(None),
                Movimento.data_mov >= pagamentos[0].data_mov - JANELA_PAGAMENTO,
                Movimento.data_mov <= pagamentos[-1].data_mov + JANELA_PAGAMENTO)) if _pode_ligar(d)]
        for pag in pagamentos:
            mudou = _marcar_transferencia(pag)
            if not pag.par_id:
                pares = [d for d in livres if d.montante == -pag.montante
                         and abs(d.data_mov - pag.data_mov) <= JANELA_PAGAMENTO]
                if pares:
                    par = min(pares, key=lambda d: (abs(d.data_mov - pag.data_mov), d.id))
                    livres.remove(par)
                    pag.par_id, par.par_id = par.id, pag.id
                    _marcar_transferencia(par)
                    mudou = True
            n += mudou
    return n


def desligar(db: Session, m: Movimento) -> Movimento | None:
    """Desfaz a ligação nos dois lados (não muda a marca de transferência)."""
    outro = db.get(Movimento, m.par_id) if m.par_id else None
    m.par_id = None
    if outro and outro.par_id == m.id:
        outro.par_id = None
    return outro


@dataclass
class PagamentosCartao:
    cartao: Conta
    pares: list            # [(pagamento no cartão, débito na conta pagadora)]
    sem_par: list          # [(pagamento, [débitos candidatos com o mesmo valor, ±30 dias])]


def relatorio_pagamentos_cartao(db: Session, entidade_id: int) -> list[PagamentosCartao]:
    out = []
    cartoes = db.scalars(select(Conta).where(Conta.entidade_id == entidade_id, Conta.tipo == "CARTAO_CREDITO")
                         .order_by(Conta.nome)).all()
    for cartao in cartoes:
        pares, sem_par = [], []
        for pag in sorted(pagamentos_do_cartao(db, cartao), key=lambda m: m.data_mov, reverse=True):
            if pag.par_id:
                pares.append((pag, db.get(Movimento, pag.par_id)))
                continue
            candidatos = []
            if cartao.conta_pagamento_id:
                candidatos = db.scalars(select(Movimento).where(
                    Movimento.conta_id == cartao.conta_pagamento_id, Movimento.montante == -pag.montante,
                    Movimento.par_id.is_(None),
                    Movimento.data_mov.between(pag.data_mov - timedelta(days=30), pag.data_mov + timedelta(days=30)))
                    .order_by(Movimento.data_mov)).all()
            sem_par.append((pag, candidatos))
        out.append(PagamentosCartao(cartao, pares, sem_par))
    return out


def ligar_manual(db: Session, a: Movimento, b: Movimento) -> None:
    """Liga à mão dois movimentos (ex.: pagamento fora da janela de 7 dias)."""
    for m in (a, b):
        if m.par_id:
            desligar(db, m)
    a.par_id, b.par_id = b.id, a.id
    for m in (a, b):
        m.transferencia = True
        m.qualificado_por = "manual"
        m.regra_id = None


def _dia_no_mes(ano: int, mes: int, dia: int):
    import calendar
    return date(ano, mes, min(dia, calendar.monthrange(ano, mes)[1]))


@dataclass
class ResumoCartao:
    divida: int                     # valor em dívida (positivo)
    ultimo_fecho: date | None
    proximo_fecho: date | None
    proximo_pagamento: date | None
    gastos_ciclo: int               # compras desde o último fecho
    pagamentos_ciclo: int


def resumo_cartao(db: Session, conta: Conta, hoje: date) -> ResumoCartao:
    divida = -saldo_conta(db, conta).atual
    ultimo = proximo = pagamento = None
    if conta.dia_fecho:
        este = _dia_no_mes(hoje.year, hoje.month, conta.dia_fecho)
        if este <= hoje:
            ultimo = este
            seg = (hoje.year + hoje.month // 12, hoje.month % 12 + 1)
            proximo = _dia_no_mes(seg[0], seg[1], conta.dia_fecho)
        else:
            ant = (hoje.year - (hoje.month == 1), (hoje.month - 2) % 12 + 1)
            ultimo = _dia_no_mes(ant[0], ant[1], conta.dia_fecho)
            proximo = este
        if conta.dia_pagamento:
            # O pagamento do último extrato: o 1.º dia de pagamento a seguir ao fecho.
            pagamento = _dia_no_mes(ultimo.year, ultimo.month, conta.dia_pagamento)
            if pagamento <= ultimo:
                seg = (ultimo.year + ultimo.month // 12, ultimo.month % 12 + 1)
                pagamento = _dia_no_mes(seg[0], seg[1], conta.dia_pagamento)
    q = select(Movimento).where(Movimento.conta_id == conta.id)
    if ultimo:
        q = q.where(Movimento.data_mov > ultimo)
    movs = db.scalars(q).all() if ultimo else []
    return ResumoCartao(
        divida=divida, ultimo_fecho=ultimo, proximo_fecho=proximo, proximo_pagamento=pagamento,
        gastos_ciclo=-sum(m.montante for m in movs if m.montante < 0),
        pagamentos_ciclo=sum(m.montante for m in movs if m.montante > 0 and m.transferencia),
    )


def anular(db: Session, imp: Importacao) -> int:
    n = 0
    for m in db.scalars(select(Movimento).where(Movimento.importacao_id == imp.id)):
        emprestimos.apagar_estimativa(db, m.id)  # prestação estimada a partir deste débito
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
