"""Comparar fornecedores de um serviço da casa (luz, gás, água, telecomunicações) ano a ano — por casa.

Os débitos do serviço são os da categoria escolhida (com as subcategorias) e, além desses, os que têm na descrição um
dos textos extra (ex.: «G9» — um fornecedor novo cujos débitos ainda não têm categoria). Cada débito é atribuído a um
fornecedor pelo nome conhecido na descrição (EDP, G9, Galp, Endesa, …) ou pelo texto extra que o apanhou.

Casas: cada débito vai para a casa do 1.º contrato (ContratoServico) que lhe corresponde — o texto do fornecedor, a
referência (o n.º no fim da descrição, ex.: 60230660104) e, se o fornecedor cobrar duas casas com a mesma referência
(a G9 faz isso), o intervalo de dias do mês. Os débitos sem contrato ficam «por atribuir», com propostas de contratos
(por fornecedor e referência; com 2 débitos por mês, divididos pelo dia do mês).

Mostra, por casa e por ano: o total, o n.º de débitos e a média por mês com dados, por fornecedor; as mudanças de
fornecedor e, para cada uma, os 12 meses antes contra os 12 meses depois. Sem os kWh das faturas, a comparação é do que
se pagou (preço × consumo): um ano mais quente ou mais frio pesa.
"""
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, lazyload, selectinload

from .models import Bem, Categoria, Conta, ContratoServico, Movimento
from .relatorios import ids_categoria, somar_meses
from .sugestoes import padrao_de

# Comercializadores portugueses (luz e gás) e operadores — o nome que aparece no extrato
CONHECIDOS = [("EDP", r"\bEDP\b"), ("G9", r"\bG9\b"), ("Galp", r"GALP"), ("Endesa", r"ENDESA"), ("Iberdrola", r"IBERDROLA"),
              ("Goldenergy", r"GOLD ?ENERGY"), ("Luzboa", r"LUZBOA"), ("Repsol", r"REPSOL"), ("SU Eletricidade", r"SU ELETRICIDADE"),
              ("Plenitude", r"PLENITUDE|\bENI\b"), ("Coopérnico", r"COOPERNICO"), ("MEO Energia", r"MEO ENERGIA"),
              ("Muon", r"\bMUON\b"), ("YES Energy", r"YES ENERGY"), ("Ezu", r"\bEZU\b"), ("Audax", r"AUDAX"),
              ("Vodafone", r"VODAFONE"), ("MEO", r"\bMEO\b"), ("NOS", r"\bNOS\b"), ("Digi", r"\bDIGI\b"),
              ("EPAL", r"\bEPAL\b"), ("SMAS", r"\bSMAS\b"), ("Águas", r"AGUAS? D")]

ENERGIA = {"EDP", "G9", "Galp", "Endesa", "Iberdrola", "Goldenergy", "Luzboa", "Repsol", "SU Eletricidade", "Plenitude",
           "Coopérnico", "MEO Energia", "Muon", "YES Energy", "Ezu", "Audax"}
TELECOM = {"Vodafone", "MEO", "NOS", "Digi"}
AGUA = {"EPAL", "SMAS", "Águas"}
POR_ATRIBUIR = "Por atribuir"
REFERENCIA = re.compile(r"(\d{8,})\D*$")


def mesmo_servico(nome: str, fornecedores: list[str]) -> bool:
    """O fornecedor sugerido é do mesmo tipo de serviço que os já analisados (energia, telecomunicações, água)."""
    for grupo in (ENERGIA, TELECOM, AGUA):
        if any(f in grupo for f in fornecedores):
            return nome in grupo
    return True


def fornecedor_de(texto: str, extras: list[str]) -> str:
    t = (texto or "").upper()
    for nome, rx in CONHECIDOS:
        if re.search(rx, t):
            return nome
    for e in extras:
        if e.upper() in t:
            return e
    p = padrao_de(t)
    return p.split(" ")[0].title() if p else "Outro"


def referencia_de(texto: str) -> str | None:
    """O n.º do contrato/cliente no fim da descrição do débito direto («…-CO-60230660104» → 60230660104)."""
    m = REFERENCIA.search(texto or "")
    return m.group(1) if m else None


# ------------------------------------------------------------------ resumo de um conjunto de débitos
@dataclass
class Ano:
    ano: int
    por_fornecedor: dict = field(default_factory=dict)      # nome → (n, total)
    meses: set = field(default_factory=set)

    @property
    def total(self) -> int:
        return sum(t for _, t in self.por_fornecedor.values())

    @property
    def n(self) -> int:
        return sum(n for n, _ in self.por_fornecedor.values())

    @property
    def media_mes(self) -> int:
        return round(self.total / len(self.meses)) if self.meses else 0


@dataclass
class Mudanca:
    data: date              # 1.º débito do fornecedor novo
    de: str
    para: str
    antes: int = 0          # total dos 12 meses antes
    depois: int = 0         # total dos 12 meses depois (ou menos, se ainda não passaram)
    meses_antes: int = 0
    meses_depois: int = 0

    @property
    def media_antes(self) -> int:
        return round(self.antes / self.meses_antes) if self.meses_antes else 0

    @property
    def media_depois(self) -> int:
        return round(self.depois / self.meses_depois) if self.meses_depois else 0

    @property
    def variacao(self) -> float | None:
        return (self.media_depois - self.media_antes) / self.media_antes * 100 if self.media_antes else None


@dataclass
class Resumo:
    """Os débitos de uma casa (ou de todas) resumidos por ano, mês e fornecedor."""
    nome: str
    bem: Bem | None = None
    movimentos: list = field(default_factory=list)          # [(Movimento, fornecedor)]
    anos: list = field(default_factory=list)                # [Ano] do mais antigo para o mais recente
    fornecedores: list = field(default_factory=list)        # por ordem de aparecimento
    mudancas: list = field(default_factory=list)
    mensal: list = field(default_factory=list)              # [(date 1.º dia, {fornecedor: total})]
    anos_sem_dados: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(a.total for a in self.anos)

    def ano(self, n: int) -> Ano | None:
        return next((a for a in self.anos if a.ano == n), None)


def resumir(nome: str, itens: list, hoje: date, bem: Bem | None = None) -> Resumo:
    r = Resumo(nome, bem, list(itens))
    if not itens:
        return r
    anos: dict[int, Ano] = {}
    mensal: dict[date, Counter] = defaultdict(Counter)
    for m, f in itens:
        ano = anos.setdefault(m.data_mov.year, Ano(m.data_mov.year))
        n, t = ano.por_fornecedor.get(f, (0, 0))
        ano.por_fornecedor[f] = (n + 1, t - m.montante)
        ano.meses.add(m.data_mov.month)
        mensal[m.data_mov.replace(day=1)][f] -= m.montante
        if f not in r.fornecedores:
            r.fornecedores.append(f)
    r.anos = [anos[x] for x in sorted(anos)]
    r.anos_sem_dados = [x for x in range(min(anos), max(anos) + 1) if x not in anos]
    d, fim = min(mensal), max(mensal)
    while d <= fim:
        r.mensal.append((d, dict(mensal.get(d, {}))))
        d = somar_meses(d, 1)

    # mudanças: o fornecedor principal de cada mês (por valor) muda e mantém-se nos meses seguintes
    principal = [(d, max(v, key=v.get)) for d, v in r.mensal if v]
    atual = principal[0][1]
    for i, (d, f) in enumerate(principal):
        if f == atual:
            continue
        seguintes = [x for _, x in principal[i:i + 3]]
        if seguintes.count(f) >= min(2, len(seguintes)):     # não é um débito avulso
            primeiro = min(m.data_mov for m, x in itens if x == f and m.data_mov >= somar_meses(d, -1))
            r.mudancas.append(Mudanca(primeiro, atual, f))
            atual = f
    for mu in r.mudancas:
        ini = mu.data.replace(day=1)
        antes = [v for d, v in r.mensal if somar_meses(ini, -12) <= d < ini]
        depois = [v for d, v in r.mensal if ini <= d < somar_meses(ini, 12) and d <= hoje]
        mu.antes, mu.meses_antes = sum(sum(v.values()) for v in antes), sum(1 for v in antes if v)
        mu.depois, mu.meses_depois = sum(sum(v.values()) for v in depois), sum(1 for v in depois if v)
    return r


# ------------------------------------------------------------------ casas: contratos e propostas
def corresponde(c: ContratoServico, m: Movimento) -> bool:
    texto = (m.descricao_original or "").upper()
    if c.texto.upper() not in texto:
        return False
    if c.referencia and c.referencia != referencia_de(texto):
        return False
    if c.dia_de and m.data_mov.day < c.dia_de:
        return False
    if c.dia_ate and m.data_mov.day > c.dia_ate:
        return False
    return True


@dataclass
class Proposta:
    """Um contrato proposto a partir dos débitos por atribuir (o utilizador escolhe a casa)."""
    fornecedor: str
    referencia: str | None
    dia_de: int | None
    dia_ate: int | None
    movimentos: list = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.movimentos)

    @property
    def itens(self) -> list:
        """[(Movimento, fornecedor)] — como nas outras listas de débitos."""
        return [(m, self.fornecedor) for m in self.movimentos]

    @property
    def media(self) -> int:
        return round(-sum(m.montante for m in self.movimentos) / self.n) if self.n else 0

    @property
    def dia_tipico(self) -> int:
        return round(statistics.median(m.data_mov.day for m in self.movimentos)) if self.movimentos else 0

    @property
    def de(self) -> date:
        return min(m.data_mov for m in self.movimentos)

    @property
    def ate(self) -> date:
        return max(m.data_mov for m in self.movimentos)


def _dividir_pelo_dia(movs: list[Movimento]) -> list[tuple[int | None, int | None, list]]:
    """Com 2 débitos por mês (duas casas, a mesma referência), separa-os pela maior falta entre os dias do mês."""
    por_mes = Counter((m.data_mov.year, m.data_mov.month) for m in movs)
    if not por_mes or statistics.median(por_mes.values()) < 2:
        return [(None, None, movs)]
    dias = sorted({m.data_mov.day for m in movs})
    if len(dias) < 2:
        return [(None, None, movs)]
    falta, corte = max((b - a, a) for a, b in zip(dias, dias[1:]))
    if falta < 3:
        return [(None, None, movs)]
    return [(1, corte, [m for m in movs if m.data_mov.day <= corte]),
            (corte + 1, 31, [m for m in movs if m.data_mov.day > corte])]


def propostas(itens: list) -> list[Proposta]:
    grupos = defaultdict(list)
    for m, f in itens:
        grupos[(f, referencia_de(m.descricao_original))].append(m)
    out = []
    for (f, ref), movs in grupos.items():
        for de, ate, parte in _dividir_pelo_dia(movs):
            if parte:
                out.append(Proposta(f, ref, de, ate, parte))
    return sorted(out, key=lambda p: (p.fornecedor, -p.media))


# ------------------------------------------------------------------ análise
@dataclass
class Analise:
    categoria: Categoria | None
    extras: list
    geral: Resumo | None = None
    casas: list = field(default_factory=list)                # [Resumo] uma por casa (com contratos), + «Por atribuir»
    contratos: list = field(default_factory=list)
    propostas: list = field(default_factory=list)
    sem_categoria: list = field(default_factory=list)       # apanhados pelos textos extra, sem categoria
    # movimento.id → (nome da casa | None, «à mão» | «contrato» | None) — para a lista dos débitos
    atribuicao: dict = field(default_factory=dict)

    @property
    def movimentos(self) -> list:
        return self.geral.movimentos if self.geral else []

    @property
    def fornecedores(self) -> list:
        return self.geral.fornecedores if self.geral else []

    # compatibilidade com o resumo geral
    @property
    def anos(self) -> list:
        return self.geral.anos if self.geral else []

    @property
    def mudancas(self) -> list:
        return self.geral.mudancas if self.geral else []

    @property
    def mensal(self) -> list:
        return self.geral.mensal if self.geral else []

    @property
    def anos_sem_dados(self) -> list:
        return self.geral.anos_sem_dados if self.geral else []


def recolher(db: Session, entidade_id: int, cat: Categoria | None, extras: list[str]) -> tuple[list, list]:
    """[(Movimento, fornecedor)] do serviço, e os que só os textos extra apanharam sem categoria."""
    if not cat and not extras:
        return [], []
    cat_ids = set(ids_categoria(db, cat.id)) if cat else set()
    q = (select(Movimento).join(Conta).where(Conta.entidade_id == entidade_id, Movimento.montante < 0,
                                             ~Movimento.excluido, ~Movimento.transferencia)
         .options(selectinload(Movimento.conta), selectinload(Movimento.categoria), lazyload(Movimento.bem))
         .order_by(Movimento.data_mov))
    if all(e.isascii() for e in extras):
        # pré-filtro na base (o LIKE do SQLite ignora maiúsculas só em ASCII; com acentos filtra-se tudo em Python)
        q = q.where(or_(Movimento.categoria_id.in_(cat_ids),
                        *[Movimento.descricao_original.icontains(e, autoescape=True) for e in extras]))
    rx_extras = [re.compile(re.escape(e.upper())) for e in extras]
    itens, sem_categoria = [], []
    for m in db.scalars(q):
        texto = (m.descricao_original or "").upper()
        na_categoria = m.categoria_id in cat_ids
        extra = any(rx.search(texto) for rx in rx_extras)
        if not (na_categoria or extra):
            continue
        if extra and not na_categoria and m.categoria_id and cat_ids:
            continue                     # o texto apanhou um movimento doutra categoria (ex.: «G9» num talão)
        itens.append((m, fornecedor_de(texto, extras)))
        if extra and not m.categoria_id:
            sem_categoria.append(m)
    return itens, sem_categoria


def analisar(db: Session, entidade_id: int, categoria_id: int | None, extras: list[str], hoje: date) -> Analise:
    cat = db.get(Categoria, categoria_id) if categoria_id else None
    extras = [e.strip() for e in extras if e.strip()]
    a = Analise(cat, extras)
    itens, a.sem_categoria = recolher(db, entidade_id, cat, extras)
    a.geral = resumir("Todas as casas", itens, hoje)
    # os contratos com referência primeiro: um contrato só pelo texto e pelos dias não lhes tira os débitos
    a.contratos = sorted(db.scalars(select(ContratoServico).where(ContratoServico.entidade_id == entidade_id)
                                    .options(selectinload(ContratoServico.bem))).all(),
                         key=lambda c: (c.referencia is None, c.dia_de is None, c.id))
    # casas atribuídas à mão a um débito (Movimento.bem_id com bem_manual): ganham aos contratos
    casas_mao = {b.id: b for b in db.scalars(select(Bem).where(Bem.entidade_id == entidade_id, Bem.tipo == "casa"))}
    a_mao = {m.id: casas_mao[m.bem_id] for m, _ in itens if m.bem_manual and m.bem_id in casas_mao}
    if not itens or (not a.contratos and not a_mao):
        a.propostas = propostas(itens) if itens else []
        a.atribuicao = {m.id: (None, None) for m, _ in itens}
        return a
    por_casa, por_atribuir = defaultdict(list), []
    bens = {c.bem_id: c.bem for c in a.contratos} | {b.id: b for b in a_mao.values()}
    for m, f in itens:
        if m.id in a_mao:
            por_casa[a_mao[m.id].id].append((m, f))
            a.atribuicao[m.id] = (a_mao[m.id].nome, "à mão")
            continue
        c = next((c for c in a.contratos if corresponde(c, m)), None)
        (por_casa[c.bem_id] if c else por_atribuir).append((m, f))
        a.atribuicao[m.id] = (c.bem.nome, "contrato") if c else (None, None)
    # a casa com mais gasto primeiro (normalmente a casa onde se vive)
    a.casas = sorted((resumir(bens[b].nome, por_casa[b], hoje, bens[b]) for b in por_casa), key=lambda r: -r.total)
    if por_atribuir:
        a.casas.append(resumir(POR_ATRIBUIR, por_atribuir, hoje))
        a.propostas = propostas(por_atribuir)
    return a
