"""Tarefas de importação do mês: que extrato tirar de cada banco, onde, e se já
foi feito neste ciclo.

O ciclo começa no dia `tarefas_dia_ini` (por omissão 25, depois dos ordenados)
e o lembrete (menu e painel) só aparece entre esse dia e `tarefas_dia_fim`
(por omissão 5 do mês seguinte) — configurável em Administração › Preferências.
Uma conta conta como «feita» se teve uma importação, um movimento inserido ou
uma retificação desde o início do ciclo.
"""
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from . import parsers
from .models import Conta, Importacao, Movimento
from .relatorios import fim_do_mes, somar_meses

# instituição.parser (ou nome) → (endereço, nome do serviço online)
SITES = {
    "santander": ("https://www.santander.pt", "NetBanco Particulares"),
    "activobank": ("https://www.activobank.pt", "Homebanking ActivoBank"),
    "big": ("https://www.bancobig.pt", "BiG Online"),
    "revolut": ("https://app.revolut.com", "Revolut (app ou web)"),
    "ca": ("https://www.creditoagricola.pt", "CA Online"),
    "igcp": ("https://www.aforronet.igcp.pt", "AforroNet (IGCP)"),
    "Caixa Geral de Depósitos": ("https://www.cgd.pt", "Caixadirecta"),
}
# Santander: o extrato depende do tipo de conta.
SANTANDER_POR_TIPO = {"CARTAO_CREDITO": "santander_cartao", "EMPRESTIMO": "santander_emprestimo",
                      "CARTAO_REFEICAO": "santander_refeicao"}
TIPOS_CONTA_CORRENTE = {"ORDEM", "POUPANCA", "CARTEIRA_DIGITAL"}
MANUAL = {
    "INVESTIMENTO": "Importa o extrato da conta de liquidez da carteira (BiG: movimentos da «FA …» em CSV) ou insere "
                    "aplicações/resgates com «+ Movimento»; depois acerta o valor total da carteira com «Retificar saldo».",
    "PENSOES": "Consulta o saldo do fundo de pensões / PPR no site da gestora e acerta com «Retificar saldo» "
               "(ou atualiza a cotação das posições).",
    "AFORRO": "Consulta o valor no AforroNet e acerta com «Retificar saldo» (juros entram como ganho).",
    "CARTAO_REFEICAO": "Sem exportação de movimentos: consulta o saldo na app do banco e acerta com «Retificar saldo».",
}


def _int(prefs: dict, chave: str, omissao: int) -> int:
    v = str(prefs.get(chave, ""))
    return int(v) if v.isdigit() and 1 <= int(v) <= 31 else omissao


def janela(prefs: dict) -> tuple[bool, int, int]:
    return prefs.get("tarefas_ativas", "1") == "1", _int(prefs, "tarefas_dia_ini", 25), _int(prefs, "tarefas_dia_fim", 5)


def na_janela(prefs: dict, hoje: date) -> bool:
    """O lembrete das tarefas deve aparecer hoje? (o intervalo pode passar para o mês seguinte)"""
    ativas, ini, fim = janela(prefs)
    if not ativas:
        return False
    d = hoje.day
    return ini <= d <= fim if ini <= fim else (d >= ini or d <= fim)


def inicio_ciclo(prefs: dict, hoje: date) -> date:
    """Último dia `tarefas_dia_ini` até hoje (ajustado aos meses curtos)."""
    _, ini, _ = janela(prefs)
    este = hoje.replace(day=min(ini, fim_do_mes(hoje).day))
    if este <= hoje:
        return este
    anterior = somar_meses(hoje.replace(day=1), -1)
    return anterior.replace(day=min(ini, fim_do_mes(anterior).day))


def parser_da_conta(c: Conta) -> str | None:
    base = c.instituicao.parser if c.instituicao else None
    if base == "santander":
        return SANTANDER_POR_TIPO.get(c.tipo) or ("santander" if c.tipo in TIPOS_CONTA_CORRENTE else None)
    if base in ("activobank", "big", "revolut", "ca") and c.tipo in TIPOS_CONTA_CORRENTE:
        return base
    return None


@dataclass
class Tarefa:
    conta: Conta
    ficha: dict | None          # FICHA do parser (onde exportar, formato…) ou None = atualização manual
    nota_manual: str | None
    ultima: datetime | None     # última atualização (importação ou movimento inserido)
    feita: bool


@dataclass
class Banco:
    nome: str
    site: str | None
    servico: str | None
    tarefas: list

    @property
    def por_fazer(self) -> int:
        return sum(1 for t in self.tarefas if not t.feita)


def do_ciclo(db: Session, entidade_id: int, inicio: date) -> list[Banco]:
    contas = db.scalars(select(Conta).where(Conta.entidade_id == entidade_id, Conta.ativa)
                        .options(selectinload(Conta.instituicao)).order_by(Conta.nome)).all()
    ids = [c.id for c in contas]
    ult_mov = dict(db.execute(select(Movimento.conta_id, func.max(Movimento.criado_em))
                              .where(Movimento.conta_id.in_(ids)).group_by(Movimento.conta_id)).all()) if ids else {}
    ult_imp = dict(db.execute(select(Importacao.conta_id, func.max(Importacao.criado_em))
                              .where(Importacao.conta_id.in_(ids), Importacao.estado == "confirmada")
                              .group_by(Importacao.conta_id)).all()) if ids else {}
    fichas = {p.chave: p.FICHA for p in parsers.todos()}
    corte = datetime.combine(inicio, datetime.min.time())
    bancos: dict[str, Banco] = {}
    for c in contas:
        chave = parser_da_conta(c)
        ficha = fichas.get(chave) if chave else None
        nota = None if ficha else MANUAL.get(c.tipo, "Sem importador para esta conta: insere os movimentos à mão ou acerta o saldo.")
        if c.tipo == "EMPRESTIMO" and not ficha:
            continue   # empréstimos de outros bancos: as prestações vêm na conta à ordem
        datas = [d for d in (ult_mov.get(c.id), ult_imp.get(c.id)) if d]
        ultima = max(datas) if datas else None
        inst = c.instituicao
        site = SITES.get(inst.parser or "") or SITES.get(inst.nome)
        b = bancos.get(inst.nome) or bancos.setdefault(
            inst.nome, Banco(inst.nome, site[0] if site else None, site[1] if site else None, []))
        b.tarefas.append(Tarefa(c, ficha, nota, ultima, bool(ultima and ultima >= corte)))
    return sorted(bancos.values(), key=lambda b: (b.por_fazer == 0, b.nome.lower()))
