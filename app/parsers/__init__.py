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
    transferencia: bool = False  # o banco diz que é movimento entre contas próprias (ex.: carregamento Revolut)


@dataclass
class Extrato:
    parser: str
    banco: str
    movimentos: list[MovimentoBruto]  # SEMPRE em ordem cronológica
    moeda: str = "EUR"
    conta_ref: str | None = None      # n.º de conta, quando o ficheiro o traz
    avisos: list[str] = field(default_factory=list)
    cartao: bool = False              # extrato de cartão de crédito (não de conta à ordem)
    emprestimo: bool = False          # extrato de empréstimo (movimentos da conta do empréstimo)
    refeicao: bool = False            # extrato de cartão refeição
    debito_cartao: bool = False       # compras de um cartão de débito (conta_ref = 4 últimos dígitos): não cria movimentos
    # Extratos com várias subcontas (Revolut: (produto, moeda) → movimentos). `movimentos` é a principal.
    subcontas: dict | None = None
    # Deduplicação contra importações noutro formato (Revolut PDF vs CSV): mesmo valor e mesmo saldo
    # até N dias de diferença contam como o mesmo movimento. 0 = desligado.
    dedup_dias: int = 0
    # Extratos mensais (PDF consolidado do Santander): período e saldos do próprio extrato
    periodo: tuple | None = None
    saldo_inicial: int | None = None
    saldo_final: int | None = None
    # Extrato do ano fiscal (PDF consolidado da Revolut): saldos inicial/final de cada subconta segundo as páginas de
    # resumo ((produto, moeda) → {"inicial", "final"}) e os rendimentos/impostos do período [(secção, rótulo, cêntimos)]
    saldos_resumo: dict | None = None
    resumo_fiscal: list = field(default_factory=list)


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

def consertar_texto(valor):
    """Texto UTF-8 que foi lido como Latin-1/Windows-1252 («CONCLUÃ\\x8dDA», «cartÃ£o») → «CONCLUÍDA», «cartão».
    Só mexe quando aparecem as marcas típicas (Ã, Â, «â‚¬» = €) e a reconversão dá UTF-8 válido."""
    if not isinstance(valor, str) or ("Ã" not in valor and "Â" not in valor and "â‚" not in valor):
        return valor
    for codificacao in ("latin-1", "cp1252"):
        try:
            return valor.encode(codificacao).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return valor


def _csv_numa_coluna(linhas: list[list]) -> list[list] | None:
    """Excel com o CSV inteiro numa só célula por linha (exportação «Excel» da Revolut): separa as colunas."""
    preenchidas = [[c for c in l if c not in (None, "")] for l in linhas]
    preenchidas = [p for p in preenchidas if p]
    if not preenchidas or any(len(p) > 1 for p in preenchidas) or "," not in str(preenchidas[0][0]):
        return None
    return [linha for linha in csv.reader(str(p[0]) for p in preenchidas)]


def ler_linhas(nome: str, conteudo: bytes) -> list[list]:
    """Primeira folha de um .xls / .xlsx / .csv como lista de linhas."""
    if conteudo[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":  # OLE2 = .xls antigo
        import xlrd
        sh = xlrd.open_workbook(file_contents=conteudo).sheet_by_index(0)
        return [sh.row_values(r) for r in range(sh.nrows)]
    if conteudo[:2] == b"PK":  # zip = .xlsx
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
        linhas = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
        linhas = _csv_numa_coluna(linhas) or linhas
        # corrige célula a célula (uma linha inteira pode misturar texto estragado com caracteres bons)
        return [[consertar_texto(c) for c in l] for l in linhas]
    if nome.lower().endswith((".csv", ".txt")):
        for enc in ("utf-8-sig", "cp1252"):
            try:
                texto = conteudo.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        # Só se adivinha o separador; as aspas seguem a norma do CSV. O Sniffer, com descrições que
        # têm aspas e vírgulas (Revolut: '…subconta "Conta reforçada" relativos a Sep 10, 2026'),
        # escolhia mal as regras de aspas e baralhava as colunas.
        cabecalho = texto.split("\n", 1)[0]
        separador = max(";,\t", key=cabecalho.count)
        return [linha for linha in csv.reader(io.StringIO(texto), delimiter=separador)]
    raise FormatoDesconhecido("Tipo de ficheiro não suportado (usa .xls, .xlsx ou .csv).")


def todos() -> list:
    """Todos os parsers, pela ordem de deteção (o 1.º que reconhecer o ficheiro ganha)."""
    from . import activobank, big, creditoagricola, revolut, santander, santander_cartao, santander_emprestimo
    from . import revolut_consolidado, revolut_pdf, santander_debito, santander_pdf, santander_refeicao
    return [santander.Parser, santander_cartao.Parser, santander_emprestimo.Parser,
            santander_refeicao.Parser, santander_debito.Parser, santander_pdf.Parser, revolut_pdf.Parser, activobank.Parser,
            creditoagricola.Parser, big.Parser, revolut_consolidado.Parser, revolut.Parser]


def ler_extrato(nome: str, conteudo: bytes) -> Extrato:
    if conteudo[:5] == b"%PDF-":
        from . import revolut_pdf, santander_pdf
        extrato = santander_pdf.ler_pdf(conteudo)
        if extrato is not None:
            extrato.avisos += verificar_saldos(extrato.movimentos)
            return extrato
        extrato = revolut_pdf.ler_pdf(conteudo)
        if extrato is None:
            raise FormatoDesconhecido("PDF não reconhecido: importam-se o «Extrato Consolidado» mensal do Santander "
                                      "e o «Extrato personalizado» consolidado da Revolut.")
        for chave, movs in (extrato.subcontas or {}).items():
            extrato.avisos += [f"{chave[0]} {chave[1]}: {a}" for a in verificar_saldos(movs)]
        return extrato
    linhas = ler_linhas(nome, conteudo)
    for parser in todos():
        if parser.detetar(linhas):
            extrato = parser.ler(linhas)
            if extrato.subcontas:
                for chave, movs in extrato.subcontas.items():
                    extrato.subcontas[chave] = para_cronologico(movs)
                    extrato.avisos += [f"{chave[0]} {chave[1]}: {a}" for a in verificar_saldos(extrato.subcontas[chave])]
                extrato.movimentos = para_cronologico(extrato.movimentos)
            else:
                extrato.movimentos = para_cronologico(extrato.movimentos)
                extrato.avisos += verificar_saldos(extrato.movimentos)
            return extrato
    raise FormatoDesconhecido("Formato de extrato não reconhecido. Bancos suportados: Santander (conta, cartão de crédito, cartão refeição e empréstimo), ActivoBank, Crédito Agrícola, Banco BiG, Santander (extrato consolidado em PDF), Revolut (CSV ou PDF consolidado). Vê os formatos em Administração › Importadores.")
