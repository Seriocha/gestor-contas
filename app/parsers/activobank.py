"""ActivoBank — Contas > Movimentos > exportar Excel (.xlsx).

Formato observado (2026-10): "HISTÓRICO DE CONTA NÚMERO <n.º>", linhas
Moeda / Tipo / Data de / Data até, depois
  Data Lanc. | Data Valor | Descrição | Valor | Saldo
com datas como datas Excel e o movimento mais antigo primeiro.
"""
import re

from . import Extrato, MovimentoBruto, encontrar_cabecalho, normalizar_cabecalho, para_cent, para_data

COLUNAS = ["datalanc", "datavalor", "descri", "valor", "saldo"]


class Parser:
    chave = "activobank"
    banco = "ActivoBank"
    FICHA = {
        "extrato": "Conta à ordem",
        "ficheiro": "XLSX",
        "onde": "ActivoBank › Contas › Movimentos › Exportar Excel",
        "colunas": "Data Lanc., Data Valor, Descrição, Valor, Saldo",
        "ordem": "Mais antigo primeiro",
        "saldo": True,
        "destino": "Conta à ordem (ActivoBank)",
        "notas": "N.º de conta lido do cabeçalho «HISTÓRICO DE CONTA NÚMERO …».",
    }

    @staticmethod
    def detetar(linhas) -> bool:
        return encontrar_cabecalho(linhas, COLUNAS) is not None

    @staticmethod
    def ler(linhas) -> Extrato:
        i, col = encontrar_cabecalho(linhas, COLUNAS)
        conta_ref, moeda = None, "EUR"
        for linha in linhas[:i]:
            primeira = str(linha[0] or "") if linha else ""
            m = re.search(r"CONTA\s+N\S*\s+(\d+)", primeira, re.I)
            if m:
                conta_ref = m.group(1)
            if normalizar_cabecalho(primeira) == "moeda" and len(linha) > 1 and linha[1]:
                moeda = str(linha[1]).strip().upper()
        movs = []
        for linha in linhas[i + 1:]:
            if not linha or linha[col["datalanc"]] in (None, ""):
                continue
            montante = para_cent(linha[col["valor"]])
            if montante is None:
                continue
            movs.append(MovimentoBruto(
                data_mov=para_data(linha[col["datalanc"]]),
                data_valor=para_data(linha[col["datavalor"]]),
                descricao=str(linha[col["descri"]]).strip(),
                montante=montante,
                saldo=para_cent(linha[col["saldo"]]),
            ))
        return Extrato(parser=Parser.chave, banco=Parser.banco, movimentos=movs, moeda=moeda, conta_ref=conta_ref)
