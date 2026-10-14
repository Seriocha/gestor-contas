"""Leitura de extratos bancários.

Cada parser recebe as linhas da 1.ª folha (lista de listas de valores) e
diz se reconhece o formato (`detetar`) e como o ler (`ler`). Para juntar um
banco novo: criar `<banco>.py` com uma classe Parser e acrescentá-la a
PARSERS.
"""
import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from ..formatos import parse_data, parse_montante


class FormatoDesconhecido(ValueError):
    pass


@dataclass
class MovimentoBruto:
    data_mov: date
    data_valor: date | None
    descricao: str
    montante: int            # cêntimos
    saldo: int | None = None  # saldo após o movimento, se o banco o der


@dataclass
class Extrato:
    parser: str
    banco: str
    movimentos: list[MovimentoBruto]  # SEMPRE em ordem cronológica
    moeda: str = "EUR"
    conta_ref: str | None = None      # n.º de conta, quando o ficheiro o traz
    avisos: list[str] = field(default_factory=list)
    cartao: bool = False              # extrato de cartão de crédito (não de conta à ordem)


# ---------------------------------------------------------------- utilitários

def normalizar_cabecalho(valor) -> str:
    """'Data Operação' -> 'dataoperacao'."""
    t = unicodedata.normalize("NFKD", str(valor or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", t.lower())


def para_data(valor) -> date | None:
    if valor in (None, ""):
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    return parse_data(str(valor))


def para_cent(valor) -> int | None:
    if valor in (None, ""):
        return None
    if isinstance(valor, (int, float)):
        return int((Decimal(str(valor)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return parse_montante(str(valor))


def encontrar_cabecalho(linhas: list[list], obrigatorios: list[str], max_linhas: int = 30):
    """Procura a linha cujo conjunto de colunas normalizadas começa por cada
    prefixo pedido. Devolve (índice, {prefixo: coluna}) ou None."""
    for i, linha in enumerate(linhas[:max_linhas]):
        norm = [normalizar_cabecalho(v) for v in linha]
        mapa = {}
        for p in obrigatorios:
            col = next((j for j, n in enumerate(norm) if n.startswith(p) and j not in mapa.values()), None)
            if col is None:
                break
            mapa[p] = col
        else:
            return i, mapa
    return None


def para_cronologico(movs: list[MovimentoBruto]) -> list[MovimentoBruto]:
    """Os bancos exportam por ordem crescente ou decrescente; uniformiza para
    crescente mantendo a ordem relativa dentro do mesmo dia."""
    if len(movs) > 1 and movs[0].data_mov > movs[-1].data_mov:
        return list(reversed(movs))
    return movs


def verificar_saldos(movs: list[MovimentoBruto]) -> list[str]:
    """Confirma que saldo_anterior + montante = saldo em cada linha."""
    avisos = []
    for a, b in zip(movs, movs[1:]):
        if a.saldo is not None and b.saldo is not None and a.saldo + b.montante != b.saldo:
            avisos.append(f"Saldo não bate certo em {b.data_mov:%d-%m-%Y} «{b.descricao}».")
    return avisos


# ---------------------------------------------------------------- leitura de ficheiros

def ler_linhas(nome: str, conteudo: bytes) -> list[list]:
    """Primeira folha de um .xls / .xlsx / .csv como lista de linhas."""
    if conteudo[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":  # OLE2 = .xls antigo
        import xlrd
        sh = xlrd.open_workbook(file_contents=conteudo).sheet_by_index(0)
        return [sh.row_values(r) for r in range(sh.nrows)]
    if conteudo[:2] == b"PK":  # zip = .xlsx
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
        return [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    if nome.lower().endswith((".csv", ".txt")):
        for enc in ("utf-8-sig", "cp1252"):
            try:
                texto = conteudo.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        dialeto = csv.Sniffer().sniff(texto[:4096], delimiters=";,\t")
        return [linha for linha in csv.reader(io.StringIO(texto), dialeto)]
    raise FormatoDesconhecido("Tipo de ficheiro não suportado (usa .xls, .xlsx ou .csv).")


def ler_extrato(nome: str, conteudo: bytes) -> Extrato:
    from . import activobank, creditoagricola, santander, santander_cartao
    linhas = ler_linhas(nome, conteudo)
    for parser in (santander.Parser, santander_cartao.Parser, activobank.Parser, creditoagricola.Parser):
        if parser.detetar(linhas):
            extrato = parser.ler(linhas)
            extrato.movimentos = para_cronologico(extrato.movimentos)
            extrato.avisos += verificar_saldos(extrato.movimentos)
            return extrato
    raise FormatoDesconhecido("Formato de extrato não reconhecido. Bancos suportados: Santander (conta e cartão), ActivoBank, Crédito Agrícola.")
