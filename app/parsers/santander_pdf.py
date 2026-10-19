"""Santander — «Extrato Consolidado» mensal em PDF (EXTCON<AAAAMM…><conta>.pdf).

Estrutura observada (2016-01 a 2026-09, sempre igual na parte que interessa):
  - cabeçalho de cada página: «EXTRATO Nº 239 CONTA Nº 0003.15452857020 PERÍODO DE 2026-09-01 A 2026-09-30 …»;
  - «IBAN: PT50…» na 1.ª página;
  - «Detalhe de Movimentos da Conta à Ordem» → «Saldo Inicial EUR 3.463,06», depois uma linha por movimento
        01-09 01-09 COMPRA *1264 FARMACIA HIGIENICA -21,12 3.228,20
    (data mov. e data valor dd-mm sem ano, descrição, valor com sinal, saldo após) — a tabela pode partir-se entre
    páginas («Continua» / «Continuação» + cabeçalho repetido) — e termina em «Saldo Contabilístico Final EUR …»;
  - a seguir: contas poupança, depósitos a prazo, empréstimo, agenda — não são importados aqui (o empréstimo tem o
    seu próprio extrato; os depósitos são contas manuais).

O texto vem do PDF (não é digitalizado), por isso não há OCR. interpretar() trabalha sobre o texto e pode ser testado
sem um PDF; ler_pdf() usa o pdfplumber.
"""
import io
import re
from datetime import date

from . import Extrato, MovimentoBruto

PERIODO = re.compile(r"PER[IÍ]ODO DE (\d{4})-(\d{2})-(\d{2}) A (\d{4})-(\d{2})-(\d{2})")
IBAN = re.compile(r"IBAN:\s*(PT\d{2}[\d ]{21,30})")
INICIO = re.compile(r"Detalhe de Movimentos da Conta [àa] Ordem", re.IGNORECASE)
MOEDA = re.compile(r"^Moeda:\s*([A-Z]{3})")
SALDO_INICIAL = re.compile(r"^Saldo Inicial ([A-Z]{3}) (-?[\d.]+,\d{2})$")
SALDO_FINAL = re.compile(r"^Saldo Contabil[ií]stico Final ([A-Z]{3}) (-?[\d.]+,\d{2})$")
LINHA = re.compile(r"^(\d{2})-(\d{2}) (\d{2})-(\d{2}) (.+?) (-?[\d.]+,\d{2}) (-?[\d.]+,\d{2})$")
# Linhas da tabela que não são movimentos (quebra de página, cabeçalho repetido)
IGNORAR = re.compile(r"^(Data|Mov Valor Descritivo.*|EXTRATO Nº .*|<<<.*)$")


def _cent(txt: str) -> int:
    neg = txt.startswith("-")
    v = int(txt.lstrip("-").replace(".", "").replace(",", ""))
    return -v if neg else v


def _data(dia: str, mes: str, ini: date, fim: date) -> date:
    """dd-mm sem ano → a data dentro do período do extrato (o período pode atravessar o fim do ano)."""
    m, d = int(mes), int(dia)
    ano = fim.year if m <= fim.month or ini.year == fim.year else ini.year
    try:
        return date(ano, m, d)
    except ValueError:            # 29-02 num ano que não é bissexto: não deve acontecer, mas não rebenta
        return date(ano, m, 28)


def e_consolidado(texto: str) -> bool:
    return "Extrato Consolidado" in texto and "SANTANDER" in texto.upper() and bool(PERIODO.search(texto))


def interpretar(texto: str) -> Extrato:
    p = PERIODO.search(texto)
    ini = date(int(p.group(1)), int(p.group(2)), int(p.group(3)))
    fim = date(int(p.group(4)), int(p.group(5)), int(p.group(6)))
    iban = IBAN.search(texto)
    extrato = Extrato(parser="santander_pdf", banco="Santander (PDF)", movimentos=[],
                      conta_ref=re.sub(r"\s", "", iban.group(1)) if iban else None,
                      dedup_dias=1)   # o mesmo movimento já importado pelo .xls (descrição escrita de outra forma)
    extrato.periodo = (ini, fim)
    dentro, pausa, saldo_ini, saldo_fim, estranhas = False, False, None, None, []
    for linha in (l.strip() for l in texto.splitlines()):
        if not dentro:
            if INICIO.search(linha) and saldo_fim is None:
                dentro = True
            continue
        # «Continua» … rodapé da página (balcão, gestor, nota) … «Continuação» na página seguinte
        if linha == "Continua":
            pausa = True
        elif linha == "Continuação":
            pausa = False
        if pausa or linha == "Continuação" or not linha or IGNORAR.match(linha):
            continue
        if m := MOEDA.match(linha):
            extrato.moeda = m.group(1)
        elif m := SALDO_INICIAL.match(linha):
            saldo_ini = _cent(m.group(2))
        elif m := SALDO_FINAL.match(linha):
            saldo_fim = _cent(m.group(2))
            dentro = False
        elif m := LINHA.match(linha):
            extrato.movimentos.append(MovimentoBruto(
                data_mov=_data(m.group(1), m.group(2), ini, fim), data_valor=_data(m.group(3), m.group(4), ini, fim),
                descricao=re.sub(r"\s+", " ", m.group(5)).strip(), montante=_cent(m.group(6)), saldo=_cent(m.group(7))))
        else:
            estranhas.append(linha)
    extrato.saldo_inicial, extrato.saldo_final = saldo_ini, saldo_fim
    if saldo_ini is not None and saldo_fim is not None:
        soma = saldo_ini + sum(m.montante for m in extrato.movimentos)
        if soma != saldo_fim:
            extrato.avisos.append(f"Saldo inicial + movimentos ({soma / 100:.2f}) não dá o saldo final "
                                  f"({saldo_fim / 100:.2f}): pode faltar alguma linha.")
    elif saldo_fim is None:
        extrato.avisos.append("Não encontrei o fim da tabela de movimentos da conta à ordem.")
    if estranhas:
        extrato.avisos.append(f"{len(estranhas)} linha(s) na tabela de movimentos não foram percebidas, "
                              f"ex.: «{estranhas[0][:80]}».")
    if not extrato.movimentos and saldo_ini is not None:
        extrato.avisos.append(f"Sem movimentos de {ini:%d/%m/%Y} a {fim:%d/%m/%Y}.")
    return extrato


def texto_pdf(conteudo: bytes) -> str:
    import pdfplumber
    # sem o texto ao alto da margem (dados do banco rodados 90°), que o pdfplumber mete no meio das linhas
    direito = lambda o: o.get("object_type") != "char" or o.get("upright", True)
    with pdfplumber.open(io.BytesIO(conteudo)) as pdf:
        return "\n".join(f"<<<PAGINA {i + 1}>>>\n" + (pg.filter(direito).extract_text() or "")
                         for i, pg in enumerate(pdf.pages))


def ler_pdf(conteudo: bytes, texto: str | None = None) -> Extrato | None:
    """None se o PDF não for um extrato consolidado do Santander."""
    if texto is None:
        try:
            texto = texto_pdf(conteudo)
        except Exception:         # PDF que o pdfplumber não abre: não é um extrato do Santander (os outros tentam)
            return None
    return interpretar(texto) if e_consolidado(texto) else None


class Parser:
    chave = "santander_pdf"
    banco = "Santander (PDF)"
    FICHA = {
        "extrato": "Conta à ordem — histórico mensal",
        "ficheiro": "PDF «Extrato Consolidado» (EXTCON….pdf)",
        "onde": "NetBanco › Documentos › Extratos (um PDF por mês); ou o Paperless, se lá estiverem",
        "colunas": "Data Mov., Data Valor, Descritivo do Movimento, Valor, Saldo",
        "ordem": "Mais antigo primeiro",
        "saldo": True,
        "destino": "Conta à ordem (Santander)",
        "notas": "Lê só a conta à ordem (poupanças, depósitos e empréstimo ficam de fora). Movimentos já importados "
                 "pelo .xls não se repetem (mesmo valor e saldo). Confere saldo inicial + movimentos = saldo final.",
    }

    @staticmethod
    def detetar(linhas) -> bool:   # não lê folhas de cálculo: o PDF entra por ler_pdf()
        return False
