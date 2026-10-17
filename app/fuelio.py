"""Fuelio: abastecimentos dos carros a partir do CSV de sincronização (vehicle-N-sync.csv, ou o ZIP).

O ficheiro tem secções «## Vehicle», «## Log», «## FavStations», «## TripLog»…; usa-se o veículo
(matrícula, marca, modelo) e o registo de abastecimentos (data, km, litros, depósito cheio, valor,
posto). De cada abastecimento:
- a leitura dos km entra nos registos de km do carro (média km/mês e previsão da manutenção);
- liga-se ao débito no banco (mesmo valor, ou até 0,20 € de diferença, de 1 dia antes a 5 dias
  depois) — o débito passa a Transportes › Combustível (se não foi classificado à mão) e fica
  atribuído a este carro, o que separa o combustível de cada carro (e da viatura da empresa);
- consumo (l/100 km) e custo por km entre depósitos cheios.
Do ficheiro não se guardam VIN, coordenadas, fotografias nem viagens.
"""
import csv
import io
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Abastecimento, Bem, Categoria, Conta, Movimento

TOLERANCIA = 20          # cêntimos de diferença aceites (valor mal escrito no Fuelio)
DIAS_ANTES, DIAS_DEPOIS = 1, 5


@dataclass
class Veiculo:
    nome: str = ""
    descricao: str = ""
    matricula: str = ""
    marca: str = ""
    modelo: str = ""
    ano: int | None = None


@dataclass
class Registo:
    data: date
    hora: str | None
    km: int | None
    litros: float
    cheio: bool
    valor: int | None
    preco_litro: float | None
    posto: str | None
    notas: str | None
    guid: str


@dataclass
class Ficheiro:
    veiculo: Veiculo = field(default_factory=Veiculo)
    registos: list = field(default_factory=list)


def _f(s) -> float | None:
    try:
        return float(str(s).replace(",", ".")) if str(s or "").strip() else None
    except ValueError:
        return None


def _seccoes(texto: str) -> dict[str, list[str]]:
    seccoes, atual = {}, None
    for l in texto.splitlines():
        t = l.strip().strip('"')
        if t.startswith("## "):
            atual = t[3:].strip()
            seccoes[atual] = []
        elif atual and l.strip():
            seccoes[atual].append(l)
    return seccoes


def ler(nome: str, conteudo: bytes) -> Ficheiro:
    if conteudo[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(conteudo)) as zf:
            csvs = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if len(csvs) != 1:
                raise ValueError("O ZIP do Fuelio deve ter um ficheiro CSV (um por carro).")
            conteudo = zf.read(csvs[0])
    texto = conteudo.decode("utf-8-sig", errors="replace")
    sec = _seccoes(texto)
    if "Log" not in sec:
        raise ValueError("Não parece um ficheiro do Fuelio (falta a secção «## Log»).")
    f = Ficheiro()
    v = next(csv.DictReader(sec.get("Vehicle", [])), None)
    if v:
        ano = _f(v.get("Year"))
        f.veiculo = Veiculo((v.get("Name") or "").strip(), (v.get("Description") or "").strip(),
                            (v.get("Plate") or "").strip(), (v.get("Make") or "").strip(),
                            (v.get("Model") or "").strip(), int(ano) if ano and 1900 < ano < 2100 else None)
        if v.get("DistUnit", "0") not in ("0", "") or v.get("FuelUnit", "0") not in ("0", ""):
            raise ValueError("O carro no Fuelio não está em km e litros — muda as unidades na app antes de exportar.")
    for r in csv.DictReader(sec["Log"]):
        try:
            quando = datetime.strptime((r.get("Data") or "").strip()[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            try:
                quando = datetime.strptime((r.get("Data") or "").strip()[:10], "%Y-%m-%d")
            except ValueError:
                continue
        litros = _f(r.get("Fuel (litres)"))
        if not litros:
            continue
        valor, km = _f(r.get("Price (optional)")), _f(r.get("Odo (km)"))
        guid = (r.get("guid") or r.get("UniqueId") or f"{quando:%Y%m%d%H%M}-{km}").strip()[:64]
        f.registos.append(Registo(quando.date(), quando.strftime("%H:%M"), int(round(km)) if km else None, litros,
                                  (r.get("Full") or "1").strip() == "1", round(valor * 100) if valor else None,
                                  _f(r.get("VolumePrice")), (r.get("City (optional)") or "").strip()[:120] or None,
                                  (r.get("Notes (optional)") or "").strip()[:300] or None, guid))
    f.registos.sort(key=lambda x: (x.data, x.hora or "", x.km or 0))
    return f


# ------------------------------------------------------------------ gravar
def importar(db: Session, b: Bem, f: Ficheiro) -> tuple[int, int, int]:
    """Grava/atualiza os abastecimentos (pelo guid do Fuelio) e as leituras de km.
    Devolve (novos, atualizados, leituras de km novas)."""
    from . import manutencao as mt
    existentes = {a.guid: a for a in db.scalars(select(Abastecimento).where(Abastecimento.bem_id == b.id))}
    novos = atual = leituras = 0
    for r in f.registos:
        dados = dict(data=r.data, hora=r.hora, km=r.km, litros=r.litros, cheio=r.cheio, valor=r.valor,
                     preco_litro=r.preco_litro, posto=r.posto, notas=r.notas)
        a = existentes.get(r.guid)
        if a:
            if any(getattr(a, k) != v for k, v in dados.items()):
                if a.valor != r.valor or a.data != r.data:
                    a.movimento_id = None          # mudou o valor/data: volta a procurar o débito
                for k, v in dados.items():
                    setattr(a, k, v)
                atual += 1
        else:
            db.add(Abastecimento(bem_id=b.id, guid=r.guid, **dados))
            novos += 1
        if r.km and not any(x.data == r.data and x.km == r.km for x in b.registos_km):
            try:
                mt.registar_km(db, b, r.data, r.km, origem="fuelio", nota=(r.posto or "")[:200] or None)
                leituras += 1
            except ValueError:
                pass                                   # contradiz uma leitura escrita à mão: fica a manual
    # completa a ficha do carro com o que o Fuelio sabe (só campos vazios)
    v = f.veiculo
    if not b.marca and (v.marca or v.nome):
        b.marca = (v.marca or v.nome)[:60]
    if not b.modelo:
        modelo = v.modelo if v.modelo and v.modelo != (v.marca or v.nome) else v.descricao
        b.modelo = modelo[:60] or None
    if not b.ano and v.ano:
        b.ano = v.ano
    db.flush()
    return novos, atual, leituras


# ------------------------------------------------------------------ ligação aos débitos do banco
def _categoria_combustivel(db: Session) -> Categoria | None:
    return db.scalar(select(Categoria).where(Categoria.nome == "Combustível"))


def _parece_combustivel(m: Movimento) -> int:
    """0 = já é Combustível, 1 = sem categoria ou de Transportes, 2 = outra categoria (ex.: um restaurante do mesmo valor)."""
    c = m.categoria
    if c is None:
        return 1
    if c.nome == "Combustível":
        return 0
    return 1 if (c.pai.nome if c.pai else c.nome) == "Transportes" else 2


def ligar(db: Session, entidade_id: int) -> int:
    """Liga cada abastecimento sem débito ao débito do banco do mesmo valor (ou a ≤ 0,20 €), de 1 dia antes
    a 5 dias depois; com vários candidatos, primeiro o que parece combustível, depois o de valor mais próximo e o
    mais perto na data.
    O débito passa a Combustível (se não foi classificado à mão). Devolve quantos ligou."""
    soltos = db.scalars(select(Abastecimento).join(Bem).where(
        Bem.entidade_id == entidade_id, Abastecimento.movimento_id.is_(None), Abastecimento.valor.is_not(None))
        .order_by(Abastecimento.data)).all()
    if not soltos:
        return 0
    usados = set(db.scalars(select(Abastecimento.movimento_id).join(Bem).where(
        Bem.entidade_id == entidade_id, Abastecimento.movimento_id.is_not(None))))
    ini = min(a.data for a in soltos) - timedelta(days=DIAS_ANTES)
    fim = max(a.data for a in soltos) + timedelta(days=DIAS_DEPOIS)
    debitos = [m for m in db.scalars(select(Movimento).join(Conta).where(
        Conta.entidade_id == entidade_id, Movimento.montante < 0, ~Movimento.transferencia,
        Movimento.data_mov >= ini, Movimento.data_mov <= fim)) if m.id not in usados]
    cat = _categoria_combustivel(db)
    n = 0
    for a in soltos:
        cand = [m for m in debitos if abs(-m.montante - a.valor) <= TOLERANCIA
                and a.data - timedelta(days=DIAS_ANTES) <= m.data_mov <= a.data + timedelta(days=DIAS_DEPOIS)]
        if not cand:
            continue
        m = min(cand, key=lambda m: (_parece_combustivel(m), abs(-m.montante - a.valor), abs((m.data_mov - a.data).days)))
        debitos.remove(m)
        a.movimento_id = m.id
        if cat and m.qualificado_por != "manual":
            m.categoria_id, m.qualificado_por = cat.id, "regra"
        n += 1
    db.flush()
    return n


def bens_dos_debitos(db: Session, entidade_id: int) -> dict[int, int]:
    """{movimento_id: bem_id} dos débitos ligados a um abastecimento (manda na atribuição ao carro)."""
    return dict(db.execute(select(Abastecimento.movimento_id, Abastecimento.bem_id).join(Bem).where(
        Bem.entidade_id == entidade_id, Abastecimento.movimento_id.is_not(None))).all())


# ------------------------------------------------------------------ consumo
@dataclass
class Consumo:
    abastecimentos: list
    litros: float = 0.0
    valor: int = 0
    l100: float | None = None          # entre o 1.º e o último depósito cheio
    custo_km: float | None = None      # euros por km, idem
    preco_medio: float | None = None
    km: int | None = None
    ligados: int = 0


def consumo(db: Session, b: Bem, ini: date, fim: date) -> Consumo:
    from .relatorios import inicio_analise
    corte = inicio_analise(db, b.entidade_id)
    if corte and corte > ini:
        ini = corte                      # cobertura dos dados
    todos = db.scalars(select(Abastecimento).where(Abastecimento.bem_id == b.id)
                       .order_by(Abastecimento.data.desc(), Abastecimento.hora.desc())).all()
    periodo = [a for a in todos if ini <= a.data <= fim]
    c = Consumo(periodo, sum(a.litros for a in periodo), sum(a.valor or 0 for a in periodo))
    c.ligados = sum(1 for a in periodo if a.movimento_id)
    if c.litros:
        c.preco_medio = c.valor / 100 / c.litros
    # Consumo pelo método do depósito cheio: do 1.º cheio ao último cheio, conta tudo o que se meteu depois do 1.º.
    cron = sorted((a for a in periodo if a.km is not None), key=lambda a: (a.data, a.hora or "", a.km))
    cheios = [i for i, a in enumerate(cron) if a.cheio]
    if len(cheios) >= 2:
        i, j = cheios[0], cheios[-1]
        dist = cron[j].km - cron[i].km
        if dist > 0:
            litros = sum(a.litros for a in cron[i + 1:j + 1])
            valor = sum(a.valor or 0 for a in cron[i + 1:j + 1])
            c.l100, c.custo_km, c.km = litros / dist * 100, valor / 100 / dist, dist
    return c
