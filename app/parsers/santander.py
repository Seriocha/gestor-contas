"""Santander — NetBanco > Saldos e movimentos > exportar (.xls).

Formato observado (2026-10): folha "Saldos e movimentos", ~6 linhas de
cabeçalho (data, nome do titular), depois
  Data Operação | Data valor | Descrição | Montante( EUR ) | Saldo Contabilístico( EUR )
com datas em texto dd-mm-aaaa e o movimento mais recente primeiro.
"""
import re

from . import Extrato, MovimentoBruto, encontrar_cabecalho, normalizar_cabecalho, para_cent, para_data

COLUNAS = ["dataopera", "datavalor", "descri", "montante", "saldo"]


class Parser:
    chave = "santander"
    banco = "Santander"
    FICHA = {
        "extrato": "Conta à ordem",
        "ficheiro": "XLS (Excel 97)",
        "onde": "NetBanco › Saldos e movimentos › Exportar",
        "colunas": "Data Operação, Data valor, Descrição, Montante( EUR ), Saldo Contabilístico( EUR )",
        "ordem": "Mais recente primeiro",
        "saldo": True,
        "destino": "Conta à ordem (Santander)",
        "notas": "Datas em texto dd-mm-aaaa. A moeda vem no título da coluna do montante.",
    }

    @staticmethod
    def detetar(linhas) -> bool:
        return encontrar_cabecalho(linhas, COLUNAS) is not None

    @staticmethod
    def ler(linhas) -> Extrato:
        i, col = encontrar_cabecalho(linhas, COLUNAS)
        cab = normalizar_cabecalho(linhas[i][col["montante"]])
        m = re.search(r"([a-z]{3})$", cab)
        moeda = m.group(1).upper() if m else "EUR"
        movs = []
        for linha in linhas[i + 1:]:
            if not linha or not str(linha[col["dataopera"]]).strip():
                continue
            montante = para_cent(linha[col["montante"]])
            if montante is None:
                continue
            movs.append(MovimentoBruto(
                data_mov=para_data(linha[col["dataopera"]]),
                data_valor=para_data(linha[col["datavalor"]]),
                descricao=str(linha[col["descri"]]).strip(),
                montante=montante,
                saldo=para_cent(linha[col["saldo"]]),
            ))
        return Extrato(parser=Parser.chave, banco=Parser.banco, movimentos=movs, moeda=moeda)
