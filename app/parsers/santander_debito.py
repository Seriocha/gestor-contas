"""Santander — cartão de débito: Consulta Movimentos Cartões Débito (.xls).

Formato observado (2026-10): ~8 linhas de cabeçalho (data, titular, «Consulta Movimentos Cartões Débito»,
«N.º Cartão XXXXXXXXXXXX1234», «Nome Favorito: …», «Listagem Movimentos»), depois
  Data Operação | Montante | Descritivo
com datas e montantes em texto (ponto decimal, sinal no valor), o mais recente primeiro. Sem saldo.
As compras («COMPRA TPA*1234 <comerciante>») já vêm no extrato da conta à ordem, mas lá sem o cartão: este
ficheiro não cria movimentos — liga cada compra ao débito da conta para se saber que cartão (pessoa) a fez.
"""
import re

from . import Extrato, MovimentoBruto, encontrar_cabecalho, normalizar_cabecalho, para_cent, para_data

COLUNAS = ["dataopera", "montante", "descritivo"]


class Parser:
    chave = "santander_debito"
    banco = "Santander (cartão de débito)"
    FICHA = {
        "extrato": "Cartão de débito (compras de cada cartão)",
        "ficheiro": "XLS (Excel 97)",
        "onde": "NetBanco › Cartões › Cartões de Débito › Consultar Movimentos › Exportar",
        "colunas": "Data Operação, Montante, Descritivo",
        "ordem": "Mais recente primeiro",
        "saldo": False,
        "destino": "Conta à ordem do cartão (não cria movimentos: identifica o cartão de cada compra)",
        "notas": "As compras já estão no extrato da conta; cada uma é ligada ao débito igual (mesmo valor, até 5 dias depois).",
    }

    @staticmethod
    def detetar(linhas) -> bool:
        achado = encontrar_cabecalho(linhas, COLUNAS)
        if achado is None:
            return False
        return any("movimentoscartoesd" in normalizar_cabecalho(l[0]) for l in linhas[:achado[0]] if l)

    @staticmethod
    def ler(linhas) -> Extrato:
        i, col = encontrar_cabecalho(linhas, COLUNAS)
        final = None
        for l in linhas[:i]:
            m = re.search(r"cart\w*\s+X*(\d{4})\b", str(l[0] if l else ""), re.IGNORECASE)
            if m:
                final = m.group(1)
                break
        movs = []
        for linha in linhas[i + 1:]:
            if not linha or not str(linha[col["dataopera"]]).strip():
                continue
            montante = para_cent(linha[col["montante"]])
            data = para_data(linha[col["dataopera"]])
            if montante is None or data is None:
                continue
            movs.append(MovimentoBruto(data_mov=data, data_valor=None, descricao=str(linha[col["descritivo"]]).strip(),
                                       montante=montante))
        return Extrato(parser=Parser.chave, banco=Parser.banco, movimentos=movs, conta_ref=final, debito_cartao=True)
