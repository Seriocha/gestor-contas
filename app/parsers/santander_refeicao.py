"""Santander — cartão refeição: Consultar Movimentos de Cartão (.xls).

Formato observado (2026-10): folha "Consultar Movimentos de Cartão", ~8 linhas
de cabeçalho (data, titular, «N.º Cartão XXXXXXXXXXXX1234», «Listagem
Movimentos»), depois
  Data Operação | Data Valor | Descrição | Montante
com datas e montantes em texto (ponto decimal, sinal no valor: «-2.50»), o
mais recente primeiro. Não traz saldo. O carregamento mensal vem como
«SUB REFEICAO BST»; as compras como «COMPRA *1234 <comerciante>».
"""
import re

from . import Extrato, MovimentoBruto, encontrar_cabecalho, normalizar_cabecalho, para_cent, para_data

COLUNAS = ["dataopera", "datavalor", "descri", "montante"]


class Parser:
    chave = "santander_refeicao"
    banco = "Santander (cartão refeição)"
    FICHA = {
        "extrato": "Cartão refeição",
        "ficheiro": "XLS (Excel 97)",
        "onde": "NetBanco › Cartões › Consultar Movimentos de Cartão › Exportar",
        "colunas": "Data Operação, Data Valor, Descrição, Montante",
        "ordem": "Mais recente primeiro",
        "saldo": False,
        "destino": "Cartão refeição",
        "notas": "Montantes em texto com ponto decimal e sinal. O carregamento «SUB REFEICAO» é categorizado como Subsídio de alimentação.",
    }

    @staticmethod
    def detetar(linhas) -> bool:
        achado = encontrar_cabecalho(linhas, COLUNAS)
        if achado is None or len([v for v in linhas[achado[0]] if str(v).strip()]) != 4:
            return False
        return any("movimentosdecartao" in normalizar_cabecalho(l[0]) for l in linhas[:achado[0]] if l)

    @staticmethod
    def ler(linhas) -> Extrato:
        i, col = encontrar_cabecalho(linhas, COLUNAS)
        ref = None
        for l in linhas[:i]:
            m = re.search(r"cart\w*\s+(X*\d{4})\b", str(l[0] if l else ""), re.IGNORECASE)
            if m:
                ref = "cartão …" + m.group(1)[-4:]
                break
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
            ))
        return Extrato(parser=Parser.chave, banco=Parser.banco, movimentos=movs, conta_ref=ref, refeicao=True)
