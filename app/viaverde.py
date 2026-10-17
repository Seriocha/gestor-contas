"""Via Verde: extrato mensal (CSV, ou ZIP com CSV) de portagens, parques e estacionamento.

O débito «Via Verde» no banco é a soma das linhas com a mesma data de pagamento (debitada 0–7 dias
depois); as linhas sem data de pagamento (ex.: parque + taxa de mobilidade) são pagas à parte, em
conjunto por dia. Cada grupo fica ligado ao seu débito, e cada linha vai para o carro pela matrícula —
assim um débito que junta dois carros reparte-se pelos dois nos custos de cada carro.

Do cabeçalho do extrato só se usa o n.º de contrato: nome, NIF e morada não são guardados.
"""
import csv
import hashlib
import io
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Bem, Conta, Movimento, ViaVerde, norm_matricula

DIAS_DEBITO = 7          # o débito no banco chega até 7 dias depois da data de pagamento


def _sem_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).upper().strip()


def _texto(conteudo: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return conteudo.decode(enc)
        except UnicodeDecodeError:
            continue
    return conteudo.decode("latin-1", errors="replace")


def _data(s: str) -> date | None:
    s = (s or "").strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _centimos(s: str) -> int | None:
    s = (s or "").strip().replace(".", "").replace(",", ".") if "," in (s or "") else (s or "").strip()
    try:
        return round(float(s) * 100)
    except ValueError:
        return None


@dataclass
class Linha:
    contrato: str | None
    matricula: str
    data: date
    hora: str | None
    entrada: str | None
    saida: str | None
    valor: int
    operador: str | None
    servico: str | None
    documento: str | None
    data_pagamento: date | None

    @property
    def hash(self) -> str:
        chave = "|".join(str(x or "") for x in (self.contrato, self.matricula, self.data, self.hora, self.entrada,
                                                  self.saida, self.valor, self.documento))
        return hashlib.sha1(chave.encode()).hexdigest()


@dataclass
class Extrato:
    linhas: list = field(default_factory=list)
    ficheiros: int = 0
    ignoradas: int = 0


COLUNAS = {"MATRICULA": "matricula", "DATA ENTRADA": "data_entrada", "HORA ENTRADA": "hora_entrada",
           "ENTRADA": "entrada", "DATA SAIDA": "data_saida", "HORA SAIDA": "hora_saida", "SAIDA": "saida",
           "VALOR": "valor", "OPERADOR": "operador", "SERVICO": "servico", "NUMERO DOCUMENTO": "documento",
           "DATA PAGAMENTO": "data_pagamento"}


def ler_csv(texto: str, ext: Extrato) -> None:
    linhas = texto.splitlines()
    contrato = None
    cab = None
    for i, l in enumerate(linhas):
        partes = l.split(";")
        chave = _sem_acentos(partes[0])
        if chave.startswith("CONTRATO") and len(partes) > 1:
            contrato = partes[1].strip()[:20] or None
        if chave == "IDENTIFICADOR":
            cab = i
            break
    if cab is None:
        raise ValueError("Não parece um extrato Via Verde (falta a linha de cabeçalho «IDENTIFICADOR;MATRÍCULA;…»).")
    nomes = [_sem_acentos(c) for c in linhas[cab].split(";")]
    idx = {COLUNAS[n]: i for i, n in enumerate(nomes) if n in COLUNAS}
    if not {"matricula", "data_saida", "valor"} <= set(idx):
        raise ValueError("O extrato Via Verde não tem as colunas esperadas (matrícula, data de saída, valor).")
    ext.ficheiros += 1
    for row in csv.reader(linhas[cab + 1:], delimiter=";"):
        g = lambda k: row[idx[k]].strip() if k in idx and idx[k] < len(row) else ""
        data, valor, mat = _data(g("data_saida")) or _data(g("data_entrada")), _centimos(g("valor")), norm_matricula(g("matricula"))
        if not row or not mat or not data or valor is None:
            if any(c.strip() for c in row):
                ext.ignoradas += 1
            continue
        entrada = g("entrada") or None
        ext.linhas.append(Linha(contrato, mat[:12], data, (g("hora_saida") or g("hora_entrada"))[:5] or None,
                                entrada[:80] if entrada else None, (g("saida") or "")[:80] or None, valor,
                                g("operador")[:80] or None, g("servico")[:60] or None, g("documento")[:40] or None,
                                _data(g("data_pagamento"))))


def ler(nome: str, conteudo: bytes, ext: Extrato | None = None) -> Extrato:
    """Lê um CSV ou um ZIP (com um ou mais CSV) da Via Verde."""
    ext = ext or Extrato()
    if conteudo[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(conteudo)) as zf:
            for n in sorted(zf.namelist()):
                if n.lower().endswith(".csv"):
                    ler_csv(_texto(zf.read(n)), ext)
    else:
        ler_csv(_texto(conteudo), ext)
    return ext


# ------------------------------------------------------------------ gravar e ligar aos débitos
def importar(db: Session, entidade_id: int, ext: Extrato) -> tuple[int, int]:
    """Grava as linhas novas (as repetidas são ignoradas). Devolve (novas, repetidas)."""
    existentes = set(db.scalars(select(ViaVerde.hash).where(ViaVerde.entidade_id == entidade_id)))
    novas = rep = 0
    for l in ext.linhas:
        h = l.hash
        if h in existentes:
            rep += 1
            continue
        existentes.add(h)
        db.add(ViaVerde(entidade_id=entidade_id, contrato=l.contrato, matricula=l.matricula, data=l.data, hora=l.hora,
                        entrada=l.entrada, saida=l.saida, valor=l.valor, operador=l.operador, servico=l.servico,
                        documento=l.documento, data_pagamento=l.data_pagamento, hash=h))
        novas += 1
    db.flush()
    return novas, rep


def _debitos(db: Session, entidade_id: int) -> list[Movimento]:
    return db.scalars(select(Movimento).join(Conta).where(
        Conta.entidade_id == entidade_id, Movimento.montante < 0,
        Movimento.descricao_norm.contains("VIA VERDE"))).all()


def ligar(db: Session, entidade_id: int) -> int:
    """Liga cada grupo de linhas ainda sem débito ao débito «Via Verde» do mesmo valor no banco.
    Grupo = mesma data de pagamento; sem data de pagamento, o mesmo dia. Devolve quantos grupos ligou."""
    soltas = db.scalars(select(ViaVerde).where(ViaVerde.entidade_id == entidade_id,
                                               ViaVerde.movimento_id.is_(None))).all()
    grupos = defaultdict(list)
    for l in soltas:
        grupos[("pag", l.data_pagamento) if l.data_pagamento else ("dia", l.data)].append(l)
    usados = set(db.scalars(select(ViaVerde.movimento_id).where(ViaVerde.entidade_id == entidade_id,
                                                               ViaVerde.movimento_id.is_not(None))))
    debitos = [m for m in _debitos(db, entidade_id) if m.id not in usados]
    n = 0
    for (_, ref), linhas in sorted(grupos.items(), key=lambda g: g[0][1]):
        total = sum(l.valor for l in linhas)
        cand = [m for m in debitos if -m.montante == total and ref <= m.data_mov <= ref + timedelta(days=DIAS_DEBITO)]
        if not cand:
            continue
        m = min(cand, key=lambda m: (m.data_mov - ref).days)
        debitos.remove(m)
        for l in linhas:
            l.movimento_id = m.id
        n += 1
    db.flush()
    return n


# ------------------------------------------------------------------ repartição pelos carros
def bens_por_matricula(db: Session, entidade_id: int) -> dict[str, Bem]:
    return {norm_matricula(b.matricula): b for b in db.scalars(select(Bem).where(
        Bem.entidade_id == entidade_id, Bem.tipo == "carro", Bem.matricula.is_not(None))) if b.matricula}


def partes(db: Session, entidade_id: int, movs: list[Movimento]) -> dict[int, dict]:
    """Para os débitos com detalhe Via Verde: {movimento_id: {bem_id|None: valor positivo}}.
    Matrículas sem carro ficam no bem do próprio movimento (ou sem bem)."""
    ids = [m.id for m in movs]
    if not ids:
        return {}
    linhas = db.scalars(select(ViaVerde).where(ViaVerde.movimento_id.in_(ids))).all()
    if not linhas:
        return {}
    carros = bens_por_matricula(db, entidade_id)
    por_mov = {m.id: m for m in movs}
    r: dict[int, dict] = defaultdict(lambda: defaultdict(int))
    for l in linhas:
        b = carros.get(l.matricula)
        r[l.movimento_id][b.id if b else por_mov[l.movimento_id].bem_id] += l.valor
    return {k: dict(v) for k, v in r.items()}


def do_carro(db: Session, b: Bem, ini: date, fim: date) -> list[ViaVerde]:
    if not b.matricula:
        return []
    return db.scalars(select(ViaVerde).where(ViaVerde.entidade_id == b.entidade_id,
                                             ViaVerde.matricula == norm_matricula(b.matricula),
                                             ViaVerde.data >= ini, ViaVerde.data <= fim)
                      .order_by(ViaVerde.data.desc(), ViaVerde.hora.desc())).all()


def resumo(db: Session, entidade_id: int) -> dict:
    linhas = db.scalars(select(ViaVerde).where(ViaVerde.entidade_id == entidade_id)).all()
    carros = bens_por_matricula(db, entidade_id)
    por_mat = defaultdict(lambda: [0, 0])
    for l in linhas:
        por_mat[l.matricula][0] += 1
        por_mat[l.matricula][1] += l.valor
    soltas = [l for l in linhas if not l.movimento_id]
    return {
        "n": len(linhas), "total": sum(l.valor for l in linhas),
        "de": min((l.data for l in linhas), default=None), "ate": max((l.data for l in linhas), default=None),
        "matriculas": sorted(((m, carros.get(m), n, v) for m, (n, v) in por_mat.items()), key=lambda x: -x[3]),
        "sem_carro": [m for m in por_mat if m not in carros],
        "soltas": len(soltas), "soltas_total": sum(l.valor for l in soltas),
        "ligadas_debitos": len({l.movimento_id for l in linhas if l.movimento_id}),
    }
