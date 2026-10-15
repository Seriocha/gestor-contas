"""Crédito Agrícola — Consulta actividade > exportar Excel (.xlsx).

Formato observado (2026-09): folha "Conta_<n.º>", linhas "Operação :" e
"Número de Conta :", depois
  Data Movimento | Data Valor | Descrição | Valor | Saldo após movimento | Tipo |
  Nome do Ordenante | NIB/IBAN/Conta do Ordenante | Nome do Beneficiário |
  Descritivo1 | Descritivo2 | Referencia
com datas em texto dd/mm/aaaa, valores em texto "-1.366,96 €", o movimento
mais recente primeiro, entidades HTML nas descrições ("&amp;") e uma linha
final "Data de impressão: …".
"""
import html
import re

from . import Extrato, MovimentoBruto, encontrar_cabecalho, normalizar_cabecalho, para_cent, para_data

COLUNAS = ["datamovimento", "datavalor", "descri", "valor", "saldo"]
_DATA = re.compile(r"^\d{2}/\d{2}/\d{4}$")


def _texto(valor) -> str:
    return html.unescape(re.sub(r"\s+", " ", str(valor or "")).strip())


class Parser:
    chave = "ca"
    banco = "Crédito Agrícola"
    FICHA = {
        "extrato": "Conta à ordem",
        "ficheiro": "XLSX",
        "onde": "CA Online › Consulta actividade › Exportar Excel",
        "colunas": "Data Movimento, Data Valor, Descrição, Valor, Saldo após movimento, Tipo, Ordenante, Beneficiário, Descritivo1/2, Referência",
        "ordem": "Mais recente primeiro",
        "saldo": True,
        "destino": "Conta à ordem (Crédito Agrícola)",
        "notas": "Valores e datas em texto («-1.366,96 €», dd/mm/aaaa). O Descritivo1 junta-se à descrição.",
    }

    @staticmethod
    def detetar(linhas) -> bool:
        return encontrar_cabecalho(linhas, COLUNAS) is not None

    @staticmethod
    def ler(linhas) -> Extrato:
        i, col = encontrar_cabecalho(linhas, COLUNAS)
        cab = [normalizar_cabecalho(v) for v in linhas[i]]
        descritivo = next((j for j, n in enumerate(cab) if n == "descritivo1"), None)

        conta_ref = None
        for linha in linhas[:i]:
            if linha and normalizar_cabecalho(linha[0]).startswith("numerodeconta") and len(linha) > 1:
                conta_ref = str(linha[1] or "").strip() or None

        movs = []
        for linha in linhas[i + 1:]:
            data = str(linha[col["datamovimento"]] or "").strip() if linha else ""
            if not _DATA.match(data):  # linhas vazias e rodapé "Data de impressão"
                continue
            montante = para_cent(linha[col["valor"]])
            if montante is None:
                continue
            descricao = _texto(linha[col["descri"]])
            # O descritivo (ex.: "Condominio 4 Esq 1 Semestre") acrescenta informação
            # que não está na descrição; nos débitos diretos é só uma cópia.
            extra = _texto(linha[descritivo]) if descritivo is not None and descritivo < len(linha) else ""
            if extra and extra.upper() not in descricao.upper():
                descricao = f"{descricao} — {extra}"
            movs.append(MovimentoBruto(
                data_mov=para_data(data),
                data_valor=para_data(linha[col["datavalor"]]),
                descricao=descricao,
                montante=montante,
                saldo=para_cent(linha[col["saldo"]]),
            ))
        return Extrato(parser=Parser.chave, banco=Parser.banco, movimentos=movs, conta_ref=conta_ref)
