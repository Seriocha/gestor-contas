"""Leituras dos contadores de eletricidade exportadas do Balcão Digital da E-Redes (Excel «Leituras_<CPE>_….xlsx»).

Formato observado (2026-10): folha «Leituras»; no topo «Morada», «CPE», «Número do contador»; depois o cabeçalho
  Data da Leitura | Tipo de Leitura | Origem | Estado | (Energia consumida) | <períodos: Vazio, Ponta, Cheias …>
com uma linha por dia (mais recente primeiro), as datas em texto dd/mm/aaaa e os valores ACUMULADOS do contador (kWh,
em texto). O consumo de um dia é a diferença entre duas leituras seguidas; conta no dia da leitura anterior.

A E-Redes não tem API para clientes (o Balcão Digital usa uma API interna com reCAPTCHA no login): os consumos entram
por este Excel, exportado à mão no Balcão Digital (Consumos › exportar). Importar o mesmo período outra vez atualiza.
"""
import io
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Bem, ContadorEnergia, LeituraEnergia


class FormatoInvalido(ValueError):
    pass


def _norm(t) -> str:
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class Ficheiro:
    cpe: str
    morada: str | None
    numero: str | None
    periodos: list
    leituras: list = field(default_factory=list)         # [(date, {periodo: kWh}, tipo)]


def ler(conteudo: bytes) -> Ficheiro:
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
    except Exception as e:
        raise FormatoInvalido(f"Não é um ficheiro Excel ({e}).") from e
    linhas = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    topo = {_norm(r[0]): r[1] for r in linhas[:10] if r and len(r) > 1 and r[0]}
    cpe = str(topo.get("cpe") or "").strip().upper()
    cab = next((i for i, r in enumerate(linhas[:30]) if r and _norm(r[0]).startswith("data da leitura")), None)
    if not cpe or cab is None:
        raise FormatoInvalido("Não parece o Excel de leituras da E-Redes (falta o CPE ou a «Data da Leitura»).")
    cabecalho = linhas[cab]
    # os períodos (Vazio, Ponta, Cheias / Fora de vazio / Simples) são as colunas com nome depois da 5.ª
    cols = [(j, str(v).strip()) for j, v in enumerate(cabecalho) if j >= 4 and v and str(v).strip()]
    if not cols:
        raise FormatoInvalido("Não encontrei as colunas dos períodos (Vazio, Ponta, Cheias…).")
    f = Ficheiro(cpe, str(topo.get("morada") or "").strip() or None, str(topo.get("numero do contador") or "").strip() or None,
                 [n for _, n in cols])
    for r in linhas[cab + 1:]:
        if not r or not r[0]:
            continue
        try:
            d = r[0] if isinstance(r[0], date) else datetime.strptime(str(r[0]).strip(), "%d/%m/%Y").date()
        except ValueError:
            continue
        if isinstance(d, datetime):
            d = d.date()
        if len(r) > 4 and r[4] and "injet" in _norm(r[4]):       # autoconsumo: energia injetada na rede — fica de fora
            continue
        valores = {}
        for j, nome in cols:
            v = r[j] if j < len(r) else None
            try:
                valores[nome] = int(float(str(v).replace(",", ".").strip()))
            except (TypeError, ValueError):
                continue
        if valores:
            f.leituras.append((d, valores, str(r[1]).strip() if len(r) > 1 and r[1] else None))
    if not f.leituras:
        raise FormatoInvalido("O ficheiro não tem leituras.")
    return f


def casa_provavel(db: Session, entidade_id: int, morada: str | None) -> Bem | None:
    """A casa cujo nome ou localização aparece na morada do contador (ex.: «Póvoa» em «… POVOA DE SANTA IRIA»)."""
    m = _norm(morada)
    if not m:
        return None
    for b in db.scalars(select(Bem).where(Bem.entidade_id == entidade_id, Bem.tipo == "casa")):
        for t in (b.nome, b.localizacao):
            palavras = [p for p in re.findall(r"[a-z]{4,}", _norm(t)) if p not in ("casa", "rua", "praia")]
            if palavras and any(p in m for p in palavras):
                return b
    return None


@dataclass
class Importacao:
    contador: ContadorEnergia
    novas: int
    atualizadas: int
    de: date
    ate: date


def importar(db: Session, entidade_id: int, conteudo: bytes) -> Importacao:
    f = ler(conteudo)
    c = db.scalar(select(ContadorEnergia).where(ContadorEnergia.entidade_id == entidade_id, ContadorEnergia.cpe == f.cpe))
    if not c:
        c = ContadorEnergia(entidade_id=entidade_id, cpe=f.cpe)
        db.add(c)
        db.flush()
    c.morada, c.numero = f.morada or c.morada, f.numero or c.numero
    if c.bem_id is None:
        b = casa_provavel(db, entidade_id, c.morada)
        c.bem_id = b.id if b else None
    existentes = {l.data: l for l in db.scalars(select(LeituraEnergia).where(LeituraEnergia.contador_id == c.id))}
    novas = atualizadas = 0
    for d, valores, tipo in f.leituras:
        txt = json.dumps(valores, ensure_ascii=False, sort_keys=True)
        if d in existentes:
            if existentes[d].valores != txt:
                existentes[d].valores, existentes[d].tipo = txt, tipo
                atualizadas += 1
        else:
            db.add(LeituraEnergia(contador_id=c.id, data=d, valores=txt, tipo=tipo))
            novas += 1
    db.flush()
    db.refresh(c)
    datas = [d for d, _, _ in f.leituras]
    return Importacao(c, novas, atualizadas, min(datas), max(datas))


# ------------------------------------------------------------------ consumo por mês
@dataclass
class Mes:
    mes: date                         # 1.º dia
    kwh: int = 0
    por_periodo: dict = field(default_factory=dict)
    dias: int = 0                     # dias com consumo conhecido

    @property
    def dias_mes(self) -> int:
        seguinte = date(self.mes.year + (self.mes.month == 12), self.mes.month % 12 + 1, 1)
        return (seguinte - self.mes).days

    @property
    def completo(self) -> bool:
        return self.dias >= self.dias_mes - 1


def consumos(contador: ContadorEnergia) -> list[Mes]:
    """Consumo de cada mês pelas diferenças entre leituras seguidas (o consumo entre duas leituras conta nos dias
    entre elas, repartido por igual quando há dias sem leitura)."""
    leituras = [(l.data, json.loads(l.valores)) for l in contador.leituras]
    meses: dict[date, Mes] = {}
    for (d0, v0), (d1, v1) in zip(leituras, leituras[1:]):
        n = (d1 - d0).days
        if n <= 0:
            continue
        dif = {p: v1.get(p, 0) - v0.get(p, 0) for p in v1 if p in v0}
        if any(x < 0 for x in dif.values()):            # troca de contador / acerto: o intervalo não conta
            continue
        for i in range(n):
            dia = d0 + timedelta(days=i)
            m = meses.setdefault(dia.replace(day=1), Mes(dia.replace(day=1)))
            m.dias += 1
            for p, x in dif.items():
                m.por_periodo[p] = m.por_periodo.get(p, 0) + x / n
    for m in meses.values():
        m.por_periodo = {p: round(x) for p, x in m.por_periodo.items()}
        m.kwh = sum(m.por_periodo.values())
    return [meses[k] for k in sorted(meses)]


@dataclass
class LinhaConsumo:
    mes: Mes
    pago_mes: int                     # débitos no mês do consumo (cêntimos)
    pago_seguinte: int                # débitos no mês seguinte — normalmente a fatura deste consumo

    @property
    def eur_kwh(self) -> float | None:
        return self.pago_seguinte / 100 / self.mes.kwh if self.mes.kwh and self.pago_seguinte else None


def correlacionar(meses: list[Mes], mensal: list) -> list[LinhaConsumo]:
    """Junta o consumo de cada mês ao que se pagou nesse mês e no seguinte (mensal: [(1.º dia, {fornecedor: cêntimos})])."""
    pago = {d: sum(v.values()) for d, v in mensal}
    out = []
    for m in meses:
        seguinte = date(m.mes.year + (m.mes.month == 12), m.mes.month % 12 + 1, 1)
        out.append(LinhaConsumo(m, pago.get(m.mes, 0), pago.get(seguinte, 0)))
    return out
