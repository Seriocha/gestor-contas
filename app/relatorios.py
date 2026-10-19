"""Cálculos dos relatórios e do painel.

Regras de contagem (iguais às do Sure):
- transferências entre contas próprias e movimentos excluídos não contam;
- com categoria, a natureza da categoria manda: numa categoria de despesa um
  crédito (ex.: devolução) reduz a despesa; numa de receita um débito reduz
  a receita;
- sem categoria, o sinal decide (entrada = receita, saída = despesa).
"""
import bisect
import calendar
import statistics
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .models import TIPOS_PASSIVO, Categoria, Conta, Entidade, Movimento

PASSIVOS = TIPOS_PASSIVO
SEM_CATEGORIA = "Sem categoria"


# ---------------------------------------------------------------- períodos

def fim_do_mes(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def somar_meses(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


@dataclass
class Periodo:
    chave: str
    ini: date
    fim: date
    rotulo: str

    @property
    def anterior(self) -> "Periodo":
        """Período anterior com a mesma duração (mês→mês, trimestre→trimestre)."""
        if self.chave in ("mes", "trimestre", "6meses", "ano"):
            meses = {"mes": 1, "trimestre": 3, "6meses": 6, "ano": 12}[self.chave]
            ini = somar_meses(self.ini, -meses)
            return Periodo(self.chave, ini, self.ini - timedelta(days=1), "período anterior")
        if self.chave == "ytd":
            ini = date(self.ini.year - 1, 1, 1)
            fim = self.fim.replace(year=self.fim.year - 1) if not (self.fim.month == 2 and self.fim.day == 29) \
                else date(self.fim.year - 1, 2, 28)
            return Periodo(self.chave, ini, fim, "mesmo período do ano anterior")
        dias = (self.fim - self.ini).days
        fim = self.ini - timedelta(days=1)
        return Periodo(self.chave, fim - timedelta(days=dias), fim, "período anterior")

    def meses(self) -> list[date]:
        """1.º dia de cada mês coberto."""
        out, d = [], self.ini.replace(day=1)
        while d <= self.fim:
            out.append(d)
            d = somar_meses(d, 1)
        return out


MESES_PT = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
PRESETS = {"mes": "Mês", "trimestre": "Trimestre", "6meses": "Últimos 6 meses", "ytd": "Ano até hoje",
           "ano": "Últimos 12 meses", "anos": "Vários anos", "personalizado": "Personalizado"}


@dataclass
class PeriodoAnos(Periodo):
    """Vários anos escolhidos (seguidos ou não): de 1 de janeiro do 1.º ao fim do último; os meses e os movimentos
    dos anos que não foram escolhidos ficam de fora."""
    anos: tuple = ()

    @property
    def anterior(self) -> "Periodo":
        # sem período de comparação (a comparação é entre os anos escolhidos); o fim serve ao património de partida
        return Periodo("anos", self.ini, self.ini - timedelta(days=1), "antes do 1.º ano")

    def meses(self) -> list[date]:
        return [m for m in super().meses() if m.year in self.anos]

    def inclui(self, d: date) -> bool:
        return d.year in self.anos


def periodo_anos(anos: list[int], ref: date) -> PeriodoAnos:
    anos = tuple(sorted(set(anos)))
    grupos = []                                  # anos seguidos juntos: 2017, 2018, 2019, 2021 → «2017–2019, 2021»
    for a in anos:
        if grupos and grupos[-1][-1] == a - 1:
            grupos[-1].append(a)
        else:
            grupos.append([a])
    rotulo = ", ".join(f"{g[0]}–{g[-1]}" if len(g) > 2 else ", ".join(map(str, g)) for g in grupos)
    fim = date(anos[-1], 12, 31)
    return PeriodoAnos("anos", date(anos[0], 1, 1), min(fim, ref) if anos[-1] == ref.year else fim, rotulo, anos)


def rotulo_mes(d: date) -> str:
    return f"{MESES_PT[d.month - 1]} {d.year % 100:02d}"


def periodo(chave: str, ref: date, de: date | None = None, ate: date | None = None) -> Periodo:
    """`ref` é o dia de referência (hoje, ou a data do último movimento)."""
    if chave == "personalizado" and de and ate and de <= ate:
        return Periodo(chave, de, ate, f"{de:%d/%m/%Y} a {ate:%d/%m/%Y}")
    if chave == "trimestre":
        ini = date(ref.year, 3 * ((ref.month - 1) // 3) + 1, 1)
        return Periodo(chave, ini, fim_do_mes(somar_meses(ini, 2)), f"{(ini.month - 1) // 3 + 1}.º trimestre {ini.year}")
    if chave == "6meses":
        ini = somar_meses(ref.replace(day=1), -5)
        return Periodo(chave, ini, fim_do_mes(ref), f"{rotulo_mes(ini)} a {rotulo_mes(ref)}")
    if chave == "ano":
        ini = somar_meses(ref.replace(day=1), -11)
        return Periodo(chave, ini, fim_do_mes(ref), f"{rotulo_mes(ini)} a {rotulo_mes(ref)}")
    if chave == "ytd":
        return Periodo(chave, date(ref.year, 1, 1), ref, f"1 jan a {ref:%d/%m/%Y}")
    ini = ref.replace(day=1)
    return Periodo("mes", ini, fim_do_mes(ref), f"{MESES_PT[ini.month - 1].capitalize()} {ini.year}")


# ---------------------------------------------------------------- movimentos

def contas_da_entidade(db: Session, entidade_id: int, so_ativas: bool = False) -> list[Conta]:
    q = select(Conta).where(Conta.entidade_id == entidade_id)
    if so_ativas:
        q = q.where(Conta.ativa)
    return db.scalars(q.options(selectinload(Conta.instituicao)).order_by(Conta.nome)).all()


def inicio_analise(db: Session, entidade_id: int) -> date | None:
    """Data a partir da qual os movimentos contam nas análises (None = todos)."""
    return db.scalar(select(Entidade.data_inicio).where(Entidade.id == entidade_id))


def ids_categoria(db: Session, categoria_id: int) -> list[int]:
    """A categoria e as suas subcategorias (escolher «Casa» inclui «Casa › Luz», …)."""
    ids, nivel = [categoria_id], [categoria_id]
    while nivel:
        nivel = list(db.scalars(select(Categoria.id).where(Categoria.pai_id.in_(nivel))))
        ids += nivel
    return ids


def movimentos_reportaveis(db: Session, entidade_id: int, ini: date, fim: date,
                           conta_ids: list[int] | None = None, categoria_ids: list[int] | None = None) -> list[Movimento]:
    corte = inicio_analise(db, entidade_id)
    if corte and ini < corte:
        ini = corte
    q = (select(Movimento).join(Conta)
         .where(Conta.entidade_id == entidade_id, Movimento.data_mov >= ini, Movimento.data_mov <= fim,
                ~Movimento.transferencia, ~Movimento.excluido)
         .options(selectinload(Movimento.categoria).selectinload(Categoria.pai)))
    if conta_ids:
        q = q.where(Movimento.conta_id.in_(conta_ids))
    if categoria_ids:
        q = q.where(Movimento.categoria_id.in_(categoria_ids))
    return db.scalars(q).all()


def classificar(m: Movimento) -> tuple[str, int]:
    """('RECEITA'|'DESPESA', valor em cêntimos com o sinal da natureza)."""
    if m.categoria:
        natureza = m.categoria.natureza
    else:
        natureza = "RECEITA" if m.montante > 0 else "DESPESA"
    return natureza, (m.montante if natureza == "RECEITA" else -m.montante)


@dataclass
class Linha:
    nome: str
    cor: str
    total: int = 0
    n: int = 0
    categoria_id: int | None = None
    filhas: dict = field(default_factory=dict)
    anterior: int = 0
    prevista: date | None = None   # receita prevista (simulada): data em que deve entrar

    @property
    def variacao(self) -> float | None:
        return None if not self.anterior else (self.total - self.anterior) / abs(self.anterior) * 100


@dataclass
class Resumo:
    receitas: int = 0
    despesas: int = 0
    n: int = 0
    por_categoria: dict = field(default_factory=lambda: {"RECEITA": {}, "DESPESA": {}})

    @property
    def poupanca(self) -> int:
        return self.receitas - self.despesas

    @property
    def taxa_poupanca(self) -> float | None:
        return None if self.receitas <= 0 else self.poupanca / self.receitas * 100


COR_NEUTRA = "#8a8a85"   # categorias sem cor própria e "Outras"
COR_SEM = "#b4b2a9"      # sem categoria


def resumir(movs: list[Movimento]) -> Resumo:
    r = Resumo()
    for m in movs:
        natureza, valor = classificar(m)
        r.n += 1
        if natureza == "RECEITA":
            r.receitas += valor
        else:
            r.despesas += valor
        grupo = r.por_categoria[natureza]
        cat = m.categoria
        raiz = cat.raiz if cat else None
        chave = raiz.id if raiz else 0
        linha = grupo.get(chave) or grupo.setdefault(
            chave, Linha(raiz.nome if raiz else SEM_CATEGORIA, raiz.cor_efetiva if raiz else COR_SEM,
                         categoria_id=raiz.id if raiz else None))
        linha.total += valor
        linha.n += 1
        if cat and cat.pai:
            sub = linha.filhas.get(cat.id) or linha.filhas.setdefault(
                cat.id, Linha(cat.nome, linha.cor, categoria_id=cat.id))
            sub.total += valor
            sub.n += 1
    return r


def linhas_ordenadas(grupo: dict, anterior: dict | None = None) -> list[Linha]:
    """Cópias ordenadas por valor (o resumo original fica intacto), com o
    valor do período anterior e as subcategorias já em lista."""
    out = []
    for l in sorted(grupo.values(), key=lambda l: -l.total):
        ant = anterior.get(l.categoria_id or 0) if anterior and not l.prevista else None
        out.append(replace(l, anterior=ant.total if ant else 0,
                           filhas=sorted(l.filhas.values(), key=lambda x: -x.total)))
    return out


def serie_mensal(movs: list[Movimento], p: Periodo) -> list[dict]:
    meses = {m: {"mes": m, "receitas": 0, "despesas": 0} for m in p.meses()}
    for mv in movs:
        natureza, valor = classificar(mv)
        alvo = meses.get(mv.data_mov.replace(day=1))
        if alvo:
            alvo["receitas" if natureza == "RECEITA" else "despesas"] += valor
    out = list(meses.values())
    for x in out:
        x["saldo"] = x["receitas"] - x["despesas"]
        x["rotulo"] = rotulo_mes(x["mes"])
    return out


def estatisticas_categorias(movs: list[Movimento], p: Periodo, natureza: str = "DESPESA") -> list[dict]:
    """Por categoria de topo: total mensal → média, mediana, mínimo, máximo."""
    meses = p.meses()
    valores: dict = defaultdict(lambda: defaultdict(int))
    info = {}
    for m in movs:
        nat, valor = classificar(m)
        if nat != natureza:
            continue
        raiz = m.categoria.raiz if m.categoria else None
        chave = raiz.id if raiz else 0
        info[chave] = (raiz.nome if raiz else SEM_CATEGORIA, raiz.cor_efetiva if raiz else COR_SEM, chave or None)
        valores[chave][m.data_mov.replace(day=1)] += valor
    out = []
    for chave, por_mes in valores.items():
        serie = [por_mes.get(m, 0) for m in meses]
        nome, cor, cid = info[chave]
        out.append({"nome": nome, "cor": cor, "categoria_id": cid, "serie": serie, "total": sum(serie),
                    "media": round(sum(serie) / len(serie)), "mediana": round(statistics.median(serie)),
                    "minimo": min(serie), "maximo": max(serie)})
    return sorted(out, key=lambda x: -x["total"])


def comparacao_anos(movs: list[Movimento], anos: tuple) -> dict:
    """Vários anos lado a lado: totais por ano e, por natureza, cada categoria de topo ano a ano (com a média)."""
    por_ano = {a: resumir([m for m in movs if m.data_mov.year == a]) for a in anos}
    categorias = {}
    for natureza in ("DESPESA", "RECEITA"):
        linhas = {}
        for a, r in por_ano.items():
            for chave, l in r.por_categoria[natureza].items():
                x = linhas.setdefault(chave, {"nome": l.nome, "cor": l.cor, "categoria_id": l.categoria_id,
                                              "valores": {b: 0 for b in anos}})
                x["valores"][a] = l.total
        for x in linhas.values():
            x["total"] = sum(x["valores"].values())
            x["media"] = round(x["total"] / len(anos))
        categorias[natureza] = sorted(linhas.values(), key=lambda x: -x["total"])
    n = len(anos)
    return {"anos": anos, "por_ano": por_ano, "categorias": categorias,
            "media": {"receitas": round(sum(r.receitas for r in por_ano.values()) / n),
                      "despesas": round(sum(r.despesas for r in por_ano.values()) / n),
                      "poupanca": round(sum(r.poupanca for r in por_ano.values()) / n)}}


# ---------------------------------------------------------------- saldos e património

class HistoricoConta:
    """Saldo de uma conta em qualquer data, a partir dos movimentos.

    Com saldo do banco: o último saldo_apos até à data; antes do 1.º
    movimento conhecido, o saldo antes desse movimento (saldo_apos − montante).
    Sem saldo do banco: saldo inicial + soma dos movimentos."""

    def __init__(self, db: Session, conta: Conta):
        self.conta = conta
        self.movs = db.execute(
            select(Movimento.data_mov, Movimento.montante, Movimento.saldo_apos)
            .where(Movimento.conta_id == conta.id)
            .order_by(Movimento.data_mov, Movimento.ordem, Movimento.id)).all()
        self.datas = [m.data_mov for m in self.movs]
        self.com_saldo = any(m.saldo_apos is not None for m in self.movs)
        acumulado, self.acumulados = 0, []
        for m in self.movs:
            if not conta.data_saldo_inicial or m.data_mov > conta.data_saldo_inicial:
                acumulado += m.montante
            self.acumulados.append(acumulado)

    def saldo_em(self, d: date) -> int | None:
        """None quando a conta ainda não existia nessa data (sem dados)."""
        i = bisect.bisect_right(self.datas, d)  # movimentos com data <= d: [0, i)
        if self.com_saldo:
            for j in range(i - 1, -1, -1):
                if self.movs[j].saldo_apos is not None:
                    return self.movs[j].saldo_apos
            for j in range(i, len(self.movs)):
                if self.movs[j].saldo_apos is not None:
                    return self.movs[j].saldo_apos - self.movs[j].montante
        if self.conta.data_saldo_inicial and d < self.conta.data_saldo_inicial and i == 0:
            return None
        if not self.movs and not self.conta.saldo_inicial:
            return None
        return self.conta.saldo_inicial + (self.acumulados[i - 1] if i else 0)


def patrimonio(db: Session, entidade_id: int, datas: list[date]) -> dict:
    """Ativos, passivos e património líquido (EUR) em cada data, e o detalhe
    por conta na última data."""
    contas = [c for c in contas_da_entidade(db, entidade_id) if c.moeda == "EUR"]
    hist = [HistoricoConta(db, c) for c in contas]
    serie = []
    for d in datas:
        ativos = passivos = 0
        for h in hist:
            s = h.saldo_em(d)
            if s is None:
                continue
            if h.conta.tipo in PASSIVOS:
                passivos += -s  # dívida do cartão vem negativa
            else:
                ativos += s
        serie.append({"data": d, "ativos": ativos, "passivos": passivos, "liquido": ativos - passivos})
    ultimo = datas[-1]
    detalhe = []
    for h in hist:
        s = h.saldo_em(ultimo)
        if s is not None and (h.conta.ativa or s):
            detalhe.append((h.conta, s))
    return {"serie": serie, "contas": detalhe}


@dataclass
class VariacaoConta:
    conta: Conta
    inicio: int
    fim: int
    movimentos: int          # soma dos movimentos registados no período

    @property
    def variacao(self) -> int:
        return self.fim - self.inicio

    @property
    def nao_explicado(self) -> int:
        """Parte da variação do saldo sem movimentos registados (ex.: falta o extrato desse período)."""
        return self.variacao - self.movimentos


@dataclass
class VariacaoPatrimonio:
    ini: date
    fim: date
    contas: list            # [VariacaoConta], só as que mudaram
    movimentos: list        # [Movimento] do período, maiores primeiro
    resumo: Resumo          # receitas e despesas (sem transferências nem excluídos)
    transferencias: int     # soma (absoluta) das transferências internas, que se anulam

    @property
    def variacao(self) -> int:
        return sum(c.variacao for c in self.contas)

    @property
    def nao_explicado(self) -> int:
        return sum(c.nao_explicado for c in self.contas)


def variacao_patrimonio(db: Session, entidade_id: int, ini: date, fim: date, limite: int = 200) -> VariacaoPatrimonio:
    """O que fez mudar o património líquido entre o fim de `ini − 1 dia` e `fim`.

    A variação de cada conta é a diferença de saldos (como no gráfico); os
    movimentos do período explicam-na, e o que sobra é «não explicado»
    (tipicamente um período sem extrato importado)."""
    from datetime import timedelta as _td
    corte = inicio_analise(db, entidade_id)
    if corte and ini < corte:
        ini = min(corte, fim)
    contas = [c for c in contas_da_entidade(db, entidade_id) if c.moeda == "EUR"]
    variacoes = []
    for c in contas:
        h = HistoricoConta(db, c)
        s_ini, s_fim = h.saldo_em(ini - _td(days=1)) or 0, h.saldo_em(fim) or 0
        soma = sum(m.montante for m in h.movs if ini <= m.data_mov <= fim and
                   (not c.data_saldo_inicial or m.data_mov > c.data_saldo_inicial or h.com_saldo))
        if s_ini or s_fim or soma:
            v = VariacaoConta(c, s_ini, s_fim, soma)
            if v.variacao or v.movimentos:
                variacoes.append(v)
    variacoes.sort(key=lambda v: -abs(v.variacao))
    ids = [c.id for c in contas]
    movs = db.scalars(select(Movimento).where(Movimento.conta_id.in_(ids), Movimento.data_mov >= ini,
                                              Movimento.data_mov <= fim)
                      .options(selectinload(Movimento.conta), selectinload(Movimento.categoria).selectinload(Categoria.pai))
                      .order_by(func.abs(Movimento.montante).desc(), Movimento.data_mov).limit(limite)).all()
    reportaveis = [m for m in movs if not m.transferencia and not m.excluido]
    return VariacaoPatrimonio(ini=ini, fim=fim, contas=variacoes, movimentos=movs, resumo=resumir(reportaveis),
                              transferencias=sum(abs(m.montante) for m in movs if m.transferencia) // 2)


COR_PREVISTA = "#9cc0ea"   # receitas previstas (ainda não recebidas)


def sankey(resumo: Resumo, max_categorias: int = 8, previstas: list[tuple[str, int]] | None = None) -> list[dict]:
    """Fluxos receitas → Orçamento → despesas (+ poupança/défice).
    `previstas` são entradas simuladas [(nome, cêntimos)], já somadas a resumo.receitas."""
    fluxos = []
    receitas = [l for l in linhas_ordenadas(resumo.por_categoria["RECEITA"]) if l.total > 0]
    despesas = [l for l in linhas_ordenadas(resumo.por_categoria["DESPESA"]) if l.total > 0]
    if len(despesas) > max_categorias:
        resto = sum(l.total for l in despesas[max_categorias - 1:])
        despesas = despesas[:max_categorias - 1] + [Linha("Outras", COR_NEUTRA, total=resto)]
    centro = "Rendimento"
    nomes_despesa = {l.nome for l in despesas}
    for l in receitas:
        # Um nó com o mesmo nome dos dois lados (ex.: "Sem categoria") criaria um ciclo.
        nome = f"{l.nome} (entradas)" if l.nome in nomes_despesa else l.nome
        fluxos.append({"from": nome, "to": centro, "flow": l.total / 100, "cor": "#2a78d6"})
    for nome, valor in previstas or []:
        if valor > 0:
            fluxos.append({"from": f"{nome} (previsto)", "to": centro, "flow": valor / 100, "cor": COR_PREVISTA})
    if resumo.poupanca < 0:
        fluxos.append({"from": "Défice (poupanças)", "to": centro, "flow": -resumo.poupanca / 100, "cor": "#e34948"})
    for l in despesas:
        fluxos.append({"from": centro, "to": l.nome, "flow": l.total / 100, "cor": l.cor})
    if resumo.poupanca > 0:
        fluxos.append({"from": centro, "to": "Poupança", "flow": resumo.poupanca / 100, "cor": "#1baf7a"})
    return fluxos
