"""Comparar fornecedores de um serviço da casa (luz, gás, água, telecomunicações) ano a ano.

Os débitos do serviço são os da categoria escolhida (com as subcategorias) e, além desses, os que têm na descrição um
dos textos extra (ex.: «G9» — um fornecedor novo cujos débitos ainda não têm categoria). Cada débito é atribuído a um
fornecedor pelo nome conhecido na descrição (EDP, G9, Galp, Endesa, …) ou pelo texto extra que o apanhou.

Mostra, por ano: o total, o n.º de débitos e a média por mês com dados, por fornecedor; as mudanças de fornecedor
(quando o débito passa de um para outro) e, para cada mudança, os 12 meses antes contra os 12 meses depois.
Sem os kWh das faturas, a comparação é do que se pagou (preço × consumo): um ano mais quente ou mais frio pesa.
"""
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import Categoria, Conta, Movimento
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


def mesmo_servico(nome: str, fornecedores: list[str]) -> bool:
    """O fornecedor sugerido é do mesmo tipo de serviço que os já analisados (energia, telecomunicações, água)."""
    for grupo in (ENERGIA, TELECOM, AGUA):
        if any(f in grupo for f in fornecedores):
            return nome in grupo
    return True


@dataclass
class Ano:
    ano: int
    por_fornecedor: dict = field(default_factory=dict)      # nome → [n, total]
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

    @property
    def principal(self) -> str | None:
        return max(self.por_fornecedor, key=lambda f: self.por_fornecedor[f][1]) if self.por_fornecedor else None


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
class Analise:
    categoria: Categoria | None
    extras: list
    movimentos: list = field(default_factory=list)          # [(Movimento, fornecedor)]
    anos: list = field(default_factory=list)                # [Ano] do mais antigo para o mais recente
    fornecedores: list = field(default_factory=list)        # por ordem de aparecimento
    mudancas: list = field(default_factory=list)
    sem_categoria: list = field(default_factory=list)       # apanhados pelos textos extra, sem categoria
    mensal: list = field(default_factory=list)              # [(date 1.º dia, {fornecedor: total})]
    anos_sem_dados: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(a.total for a in self.anos)


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


def analisar(db: Session, entidade_id: int, categoria_id: int | None, extras: list[str], hoje: date) -> Analise:
    cat = db.get(Categoria, categoria_id) if categoria_id else None
    extras = [e.strip() for e in extras if e.strip()]
    a = Analise(cat, extras)
    if not cat and not extras:
        return a
    cat_ids = set(ids_categoria(db, cat.id)) if cat else set()
    q = (select(Movimento).join(Conta).where(Conta.entidade_id == entidade_id, Movimento.montante < 0,
                                             ~Movimento.excluido, ~Movimento.transferencia)
         .options(selectinload(Movimento.conta), selectinload(Movimento.categoria)).order_by(Movimento.data_mov))
    rx_extras = [re.compile(re.escape(e.upper())) for e in extras]
    for m in db.scalars(q):
        texto = (m.descricao_original or "").upper()
        na_categoria = m.categoria_id in cat_ids
        extra = any(rx.search(texto) for rx in rx_extras)
        if not (na_categoria or extra):
            continue
        if extra and not na_categoria and m.categoria_id and cat_ids:
            continue                     # o texto apanhou um movimento doutra categoria (ex.: «G9» num talão)
        f = fornecedor_de(texto, extras)
        a.movimentos.append((m, f))
        if extra and not m.categoria_id:
            a.sem_categoria.append(m)
    if not a.movimentos:
        return a

    anos: dict[int, Ano] = {}
    mensal: dict[date, Counter] = defaultdict(Counter)
    for m, f in a.movimentos:
        ano = anos.setdefault(m.data_mov.year, Ano(m.data_mov.year))
        n, t = ano.por_fornecedor.get(f, (0, 0))
        ano.por_fornecedor[f] = (n + 1, t - m.montante)
        ano.meses.add(m.data_mov.month)
        mensal[m.data_mov.replace(day=1)][f] -= m.montante
        if f not in a.fornecedores:
            a.fornecedores.append(f)
    primeiro, ultimo = min(anos), max(anos)
    a.anos = [anos[x] for x in sorted(anos)]
    a.anos_sem_dados = [x for x in range(primeiro, ultimo + 1) if x not in anos]
    d, fim = min(mensal), max(mensal)
    while d <= fim:
        a.mensal.append((d, dict(mensal.get(d, {}))))
        d = somar_meses(d, 1)

    # mudanças: o fornecedor principal de cada mês (por valor) muda e mantém-se nos meses seguintes
    principal = [(d, max(v, key=v.get)) for d, v in a.mensal if v]
    atual = principal[0][1]
    for i, (d, f) in enumerate(principal):
        if f == atual:
            continue
        seguintes = [x for _, x in principal[i:i + 3]]
        if seguintes.count(f) >= min(2, len(seguintes)):     # não é um débito avulso
            primeiro_deb = min(m.data_mov for m, x in a.movimentos if x == f and m.data_mov >= somar_meses(d, -1))
            a.mudancas.append(Mudanca(primeiro_deb, atual, f))
            atual = f
    for mu in a.mudancas:
        ini = mu.data.replace(day=1)
        antes = [(d, v) for d, v in a.mensal if somar_meses(ini, -12) <= d < ini]
        depois = [(d, v) for d, v in a.mensal if ini <= d < somar_meses(ini, 12) and d <= hoje]
        mu.antes, mu.meses_antes = sum(sum(v.values()) for _, v in antes), sum(1 for _, v in antes if v)
        mu.depois, mu.meses_depois = sum(sum(v.values()) for _, v in depois), sum(1 for _, v in depois if v)
    return a
