"""Eletricidade das casas, gerida na ficha do imóvel (Património › casa › Eletricidade).

Cada casa tem uma sequência de contratos (ContratoLuz), um por período: o comercializador (operador), quem gere o
contrato (ex.: a Manie, que muda de operador quando compensa — em qualquer mês) e os dados do contrato (tarifa, tipo
de preço, potência, CPE, referência). O contrato em vigor não tem fim; «Mudar de operador» fecha-o na véspera da data
da mudança e abre o seguinte, que herda o que não mudou (CPE, potência, tarifa, gestor).

Débitos: um débito no banco pertence a um contrato quando a descrição tem o texto do débito (por omissão, o nome do
operador) e a referência (se o contrato a tiver), com data entre o início do contrato e JANELA_FATURA dias depois do
fim — a última fatura de um operador chega depois de se mudar. Se dois contratos servem, ganha o que tem referência e
depois o que estava em vigor na data. Estes débitos vão para a casa (atribuição das despesas no Património) e para a
casa certa na comparação de fornecedores, mesmo que o operador mude a meio do ano.

Por período: total pago, meses com débitos, média por mês e, com o contador da E-Redes ligado à casa, os kWh e o
custo médio por kWh — para ver se cada mudança compensou.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, lazyload

from .models import Bem, ContadorEnergia, ContratoLuz, Conta, Movimento

GESTOR_OMISSAO = "Manie"
JANELA_FATURA = 45          # dias depois do fim do contrato em que ainda chegam débitos dele
HERDA = ("gestor", "tarifa", "preco", "potencia", "cpe")


def atual(b: Bem) -> ContratoLuz | None:
    return next((c for c in reversed(b.contratos_luz) if c.fim is None), None)


def _referencia(texto: str) -> str | None:
    from .fornecedores import referencia_de
    return referencia_de(texto)


def corresponde(c: ContratoLuz, m) -> bool:
    texto = (m.descricao_original or "").upper()
    if not c.texto or c.texto.upper() not in texto:
        return False
    if c.referencia and c.referencia != _referencia(texto):
        return False
    fim = (c.fim or date.max - timedelta(days=JANELA_FATURA)) + timedelta(days=JANELA_FATURA)
    return c.inicio <= m.data_mov <= fim


def contrato_do_debito(contratos: list[ContratoLuz], m) -> ContratoLuz | None:
    candidatos = [c for c in contratos if corresponde(c, m)]
    if len(candidatos) <= 1:
        return candidatos[0] if candidatos else None
    em_vigor = lambda c: c.inicio <= m.data_mov <= (c.fim or date.max)
    return sorted(candidatos, key=lambda c: (not c.referencia, not em_vigor(c), -c.inicio.toordinal()))[0]


def problemas(contratos: list[ContratoLuz]) -> list[str]:
    """Períodos que se sobrepõem, fins antes do início e mais do que um contrato em vigor."""
    out = []
    ordem = sorted(contratos, key=lambda c: c.inicio)
    for c in ordem:
        if c.fim and c.fim < c.inicio:
            out.append(f"{c.comercializador}: o fim ({c.fim:%d/%m/%Y}) é antes do início ({c.inicio:%d/%m/%Y}).")
    for a, b in zip(ordem, ordem[1:]):
        if a.fim is None or a.fim >= b.inicio:
            out.append(f"{a.comercializador} (desde {a.inicio:%d/%m/%Y}) sobrepõe-se a {b.comercializador} "
                       f"(desde {b.inicio:%d/%m/%Y}).")
    if sum(1 for c in contratos if c.fim is None) > 1:
        out.append("Há mais do que um contrato em vigor (sem fim).")
    return out


def mudar(db: Session, b: Bem, data: date, comercializador: str, **dados) -> ContratoLuz:
    """Muda de operador a partir de `data`: o contrato em vigor acaba na véspera e o novo herda o que não mudou."""
    anterior = atual(b)
    if anterior and data <= anterior.inicio:
        raise ValueError(f"A mudança tem de ser depois do início do contrato atual ({anterior.inicio:%d/%m/%Y}).")
    if any(c.fim and c.fim >= data for c in b.contratos_luz if c is not anterior):
        raise ValueError("Já há um contrato depois dessa data: corrige o histórico primeiro.")
    novo = ContratoLuz(bem=b, inicio=data, comercializador=comercializador.strip()[:60])
    for k in HERDA:
        v = dados.get(k)
        setattr(novo, k, v if v not in (None, "") else (getattr(anterior, k) if anterior else None))
    for k in ("referencia", "texto_debito", "motivo", "notas"):
        setattr(novo, k, dados.get(k) or None)      # a referência e o texto do débito mudam com o operador
    if anterior:
        anterior.fim = data - timedelta(days=1)
    db.add(novo)
    db.flush()
    return novo


# ------------------------------------------------------------------ débitos e consumo por período
@dataclass
class Periodo:
    contrato: ContratoLuz
    debitos: list = field(default_factory=list)       # [Movimento]
    kwh: int | None = None
    meses_kwh: int = 0

    @property
    def total(self) -> int:
        return -sum(m.montante for m in self.debitos)

    @property
    def meses(self) -> int:
        return len({(m.data_mov.year, m.data_mov.month) for m in self.debitos})

    @property
    def media_mes(self) -> int:
        return round(self.total / self.meses) if self.meses else 0

    @property
    def eur_kwh(self) -> float | None:
        """Custo médio por kWh (€) — pago no período ÷ consumo do período (aproximado: as faturas chegam depois)."""
        return self.total / 100 / self.kwh if self.kwh else None

    @property
    def dias(self) -> int:
        return ((self.contrato.fim or date.today()) - self.contrato.inicio).days + 1


def _debitos_candidatos(db: Session, entidade_id: int, contratos: list[ContratoLuz]) -> list[Movimento]:
    textos = {c.texto for c in contratos if c.texto}
    if not textos:
        return []
    return db.scalars(select(Movimento).join(Conta, Conta.id == Movimento.conta_id)
                      .where(Conta.entidade_id == entidade_id, Movimento.montante < 0, ~Movimento.excluido,
                             or_(*[Movimento.descricao_original.icontains(t, autoescape=True) for t in textos]))
                      .options(lazyload(Movimento.bem)).order_by(Movimento.data_mov)).all()


def periodos(db: Session, b: Bem) -> list[Periodo]:
    """Os contratos da casa (mais recente primeiro) com os débitos e o consumo de cada período."""
    contratos = list(b.contratos_luz)
    if not contratos:
        return []
    out = {c.id: Periodo(c) for c in contratos}
    for m in _debitos_candidatos(db, b.entidade_id, contratos):
        c = contrato_do_debito(contratos, m)
        if c:
            out[c.id].debitos.append(m)
    from . import eredes
    meses = defaultdict(int)
    for ct in db.scalars(select(ContadorEnergia).where(ContadorEnergia.bem_id == b.id)):
        for x in eredes.consumos(ct):
            if x.completo:
                meses[x.mes] += x.kwh
    for p in out.values():
        fim = p.contrato.fim or date.max
        dentro = [k for d, k in meses.items() if p.contrato.inicio.replace(day=1) <= d <= fim]
        if dentro:
            p.kwh, p.meses_kwh = sum(dentro), len(dentro)
    return sorted(out.values(), key=lambda p: p.contrato.inicio, reverse=True)


def debitos_por_bem(db: Session, entidade_id: int) -> dict[int, int]:
    """movimento.id → casa, pelos contratos de luz das casas da entidade (para a atribuição das despesas)."""
    contratos = db.scalars(select(ContratoLuz).join(Bem).where(Bem.entidade_id == entidade_id)).all()
    if not contratos:
        return {}
    return {m.id: c.bem_id for m in _debitos_candidatos(db, entidade_id, contratos)
            if (c := contrato_do_debito(contratos, m))}


# ------------------------------------------------------------------ histórico a partir dos débitos
@dataclass
class Sugestao:
    comercializador: str
    de: date
    ate: date
    n: int
    referencia: str | None


def sugerir(db: Session, b: Bem, hoje: date) -> list[Sugestao]:
    """O histórico de operadores que os débitos de luz desta casa mostram (para criar os contratos de uma vez):
    débitos de comercializadores de energia já atribuídos à casa (à mão, por regra ou por contrato de serviço)."""
    from . import fornecedores as fo
    from .models import ContratoServico
    servicos = db.scalars(select(ContratoServico).where(ContratoServico.bem_id == b.id)).all()
    movs = db.scalars(select(Movimento).join(Conta, Conta.id == Movimento.conta_id)
                      .where(Conta.entidade_id == b.entidade_id, Movimento.montante < 0, ~Movimento.excluido)
                      .options(lazyload(Movimento.bem)).order_by(Movimento.data_mov)).all()
    runs: list[Sugestao] = []
    for m in movs:
        nome = fo.fornecedor_de(m.descricao_original or "", [])
        if nome not in fo.ENERGIA:
            continue
        if m.bem_id != b.id and not any(fo.corresponde(s, m) for s in servicos):
            continue
        ref = fo.referencia_de((m.descricao_original or "").upper())
        if runs and runs[-1].comercializador == nome:
            runs[-1].ate, runs[-1].n = m.data_mov, runs[-1].n + 1
            runs[-1].referencia = runs[-1].referencia or ref
        else:
            runs.append(Sugestao(nome, m.data_mov, m.data_mov, 1, ref))
    return [r for r in runs if r.n >= 2 or r is (runs[-1] if runs else None)]


def criar_historico(db: Session, b: Bem, sugestoes: list[Sugestao], hoje: date, gestor: str | None = None) -> int:
    """Cria os contratos a partir das sugestões: cada um começa no mês anterior ao 1.º débito (paga-se o mês que
    passou) e acaba na véspera do seguinte; o último fica em vigor se tem débitos nos últimos 90 dias."""
    n = 0
    inicios = [(s.de.replace(day=1) - timedelta(days=1)).replace(day=1) for s in sugestoes]
    for i, s in enumerate(sugestoes):
        fim = inicios[i + 1] - timedelta(days=1) if i + 1 < len(sugestoes) else \
            (None if (hoje - s.ate).days <= 90 else s.ate)
        db.add(ContratoLuz(bem=b, comercializador=s.comercializador, inicio=inicios[i], fim=fim, referencia=s.referencia,
                           gestor=gestor if fim is None else None,
                           notas=f"Criado a partir de {s.n} débito(s) de {s.de:%d/%m/%Y} a {s.ate:%d/%m/%Y}."))
        n += 1
    db.flush()
    return n
