"""Santander — cartão de crédito: Consulta Movimentos Extracto (.xls).

Formato observado (2026-10): folha "Consulta Movimentos Extracto", ~6 linhas
de cabeçalho (data, titular), depois
  Movimento | Data | Descrição | Estado | Montante
mas cada linha tem uma coluna a mais, sem título, com C/D (crédito/débito)
antes do valor, que vem sempre positivo:
  1 | 06-07-2026 | PAG.TRANS.BANCARIA | EXTRACTADO | C | 209.36
Não traz saldo.

Sinal: D (compra) → negativo (aumenta a dívida); C (pagamento, devolução)
→ positivo. Só se importam movimentos EXTRACTADO: os que ainda estão em
curso podem mudar de data/descrição quando forem extratados e criariam
duplicados.

Formato novo (2026-10): folha "Consulta Movimentos Cartão Créd(ito)", cabeçalho
com «Nº Cartão XXXXXXXXXXXX1234», depois
  Data Operação | Descrição | (sem título) | Montante (EUR)
com o montante já com sinal (compras −, pagamentos +) e o histórico todo, sem
estado. Os carregamentos do Revolut pagos com o cartão («… Revolut**1234* IRL»)
ficam como transferência (o gasto aparece no Revolut).
"""
import re

from . import Extrato, MovimentoBruto, encontrar_cabecalho, normalizar_cabecalho, para_cent, para_data

COLUNAS = ["movimento", "data", "descri", "estado", "montante"]
COLUNAS_LISTA = ["dataopera", "descri", "montante"]
ESTADO_FINAL = "EXTRACTADO"
CARREGAMENTO_REVOLUT = re.compile(r"REVOLUT", re.IGNORECASE)


def _formato(linhas) -> str | None:
    titulos = [normalizar_cabecalho(l[0]) for l in linhas[:8] if l]
    if encontrar_cabecalho(linhas, COLUNAS) is not None and any("extracto" in t for t in titulos):
        return "extracto"
    if encontrar_cabecalho(linhas, COLUNAS_LISTA) is not None and any("movimentoscartaocred" in t for t in titulos):
        return "lista"
    return None


def _ref_cartao(linhas) -> str | None:
    for l in linhas[:10]:
        m = re.search(r"cart\w*\s+(X*\d{4})\b", str(l[0] if l else ""), re.IGNORECASE)
        if m:
            return "cartão …" + m.group(1)[-4:]
    return None


def _sinal_e_valor(linha, inicio: int):
    """Procura, a partir da coluna Estado, o indicador C/D e o 1.º valor numérico."""
    sinal, valor = None, None
    for v in linha[inicio:]:
        txt = str(v).strip().upper() if v is not None else ""
        if txt in ("C", "D") and sinal is None:
            sinal = txt
        elif txt and valor is None:
            try:
                valor = para_cent(v)
            except ValueError:
                continue
    return sinal, valor


class Parser:
    chave = "santander_cartao"
    banco = "Santander (cartão de crédito)"
    FICHA = {
        "extrato": "Cartão de crédito",
        "ficheiro": "XLS (Excel 97)",
        "onde": "NetBanco › Cartões › Consulta Movimentos Extracto (ou Consulta Movimentos Cartão Crédito) › Exportar",
        "colunas": "Movimento, Data, Descrição, Estado, (C/D), Montante — ou Data Operação, Descrição, Montante (EUR)",
        "ordem": "Indiferente (a app ordena por data)",
        "saldo": False,
        "destino": "Cartão de crédito",
        "notas": "Extracto: valores positivos, sinal pela coluna C/D, só EXTRACTADO. Consulta Movimentos Cartão "
                 "Crédito: montante com sinal, histórico completo. O pagamento («PAG…») liga-se ao débito na conta "
                 "pagadora; carregamentos do Revolut ficam como transferência.",
    }

    @staticmethod
    def detetar(linhas) -> bool:
        return _formato(linhas) is not None

    @staticmethod
    def ler(linhas) -> Extrato:
        if _formato(linhas) == "lista":
            return Parser._ler_lista(linhas)
        i, col = encontrar_cabecalho(linhas, COLUNAS)
        movs, em_curso = [], 0
        for linha in linhas[i + 1:]:
            if not linha or not str(linha[col["data"]]).strip():
                continue
            estado = str(linha[col["estado"]] or "").strip().upper()
            sinal, valor = _sinal_e_valor(linha, col["estado"] + 1)
            if valor is None:
                continue
            if estado and estado != ESTADO_FINAL:
                em_curso += 1
                continue
            # Sem indicador C/D, um valor negativo já vem com o sinal certo; positivo = compra.
            if sinal == "C":
                montante = abs(valor)
            elif sinal == "D" or valor > 0:
                montante = -abs(valor)
            else:
                montante = valor
            movs.append(MovimentoBruto(
                data_mov=para_data(linha[col["data"]]), data_valor=None,
                descricao=str(linha[col["descri"]]).strip(), montante=montante))
        extrato = Extrato(parser=Parser.chave, banco=Parser.banco, movimentos=movs, cartao=True,
                          conta_ref=_ref_cartao(linhas))
        if em_curso:
            extrato.avisos.append(f"{em_curso} movimento(s) ainda em curso (não extratados) foram ignorados; "
                                  "serão importados quando constarem de um extrato.")
        return extrato

    @staticmethod
    def _ler_lista(linhas) -> Extrato:
        i, col = encontrar_cabecalho(linhas, COLUNAS_LISTA)
        movs = []
        for linha in linhas[i + 1:]:
            if not linha or not str(linha[col["dataopera"]]).strip():
                continue
            montante = para_cent(linha[col["montante"]])
            if montante is None:
                continue
            descricao = str(linha[col["descri"]]).strip()
            movs.append(MovimentoBruto(
                data_mov=para_data(linha[col["dataopera"]]), data_valor=None, descricao=descricao, montante=montante,
                transferencia=montante < 0 and bool(CARREGAMENTO_REVOLUT.search(descricao))))
        return Extrato(parser=Parser.chave, banco=Parser.banco, movimentos=movs, cartao=True,
                       conta_ref=_ref_cartao(linhas))
