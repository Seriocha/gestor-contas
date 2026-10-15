"""Santander — empréstimo: Consulta Movimentos Empréstimo (.xls).

Formato observado (2026-10): folha "Consulta Movimentos Empréstimo", ~6
linhas de cabeçalho, depois
  Data Movimento | Data Valor | Número Prestação | Descrição | Montante | Saldo em Dívida
com duas linhas por prestação (mais recente primeiro):
  25-09-2026 | 25-09-2026 | 356 | PRESTACAO - JUROS  | -28.26  | 0
  25-09-2026 | 25-09-2026 | 356 | PRESTACAO - CAPIT. | -578.22 | 21320.39

Na conta do empréstimo (saldo = −capital em dívida) cada prestação vira:
  +P  «PRESTACAO 356 …»   P = capital + juros + encargos — transferência,
                           liga ao débito da prestação na conta à ordem
  −J  «JUROS PRESTACAO 356» — despesa
  −E  «ENCARGOS PRESTACAO 356 …» — outras linhas da prestação (comissões, selo)
com o saldo após cada movimento calculado a partir do «Saldo em Dívida» do
banco, para o saldo da conta ser sempre o valor exato do banco.
"""
from collections import OrderedDict

from . import Extrato, MovimentoBruto, encontrar_cabecalho, para_cent, para_data
from ..formatos import fmt_montante

COLUNAS = ["datamovimento", "datavalor", "numeroprest", "descri", "montante", "saldoemdivida"]


class Parser:
    chave = "santander_emprestimo"
    banco = "Santander (empréstimo)"
    FICHA = {
        "extrato": "Empréstimo",
        "ficheiro": "XLS (Excel 97)",
        "onde": "NetBanco › Crédito › Consulta Movimentos Empréstimo › Exportar",
        "colunas": "Data Movimento, Data Valor, Número Prestação, Descrição, Montante, Saldo em Dívida",
        "ordem": "Mais recente primeiro",
        "saldo": True,
        "destino": "Empréstimo (habitação)",
        "notas": "Duas linhas por prestação (JUROS e CAPIT.): vira prestação (transferência ligada ao débito) + juros (despesa). Saldo = −capital em dívida.",
    }

    @staticmethod
    def detetar(linhas) -> bool:
        return encontrar_cabecalho(linhas, COLUNAS) is not None

    @staticmethod
    def ler(linhas) -> Extrato:
        i, col = encontrar_cabecalho(linhas, COLUNAS)
        prestacoes: OrderedDict = OrderedDict()
        avisos = []
        for linha in linhas[i + 1:]:
            if not linha or not str(linha[col["datamovimento"]]).strip():
                continue
            montante = para_cent(linha[col["montante"]])
            if montante is None:
                continue
            numero = str(linha[col["numeroprest"]]).strip().split(".")[0] or "?"
            p = prestacoes.setdefault(numero, {"data": para_data(linha[col["datamovimento"]]),
                                               "data_valor": para_data(linha[col["datavalor"]]),
                                               "juros": 0, "capital": 0, "encargos": [], "saldo": None})
            desc = " ".join(str(linha[col["descri"]]).split()).upper()
            valor = abs(montante)
            if "JUROS" in desc:
                p["juros"] += valor
            elif "CAPIT" in desc:
                p["capital"] += valor
                p["saldo"] = para_cent(linha[col["saldoemdivida"]])
            else:
                p["encargos"].append((desc, valor))

        movs = []
        for numero, p in reversed(prestacoes.items()):  # cronológico
            enc = sum(v for _, v in p["encargos"])
            total = p["capital"] + p["juros"] + enc
            if p["saldo"] is None:
                avisos.append(f"Prestação {numero}: sem linha de capital; saldo em dívida desconhecido.")
            final = -p["saldo"] if p["saldo"] is not None else None
            detalhe = f"capital {fmt_montante(p['capital'])} + juros {fmt_montante(p['juros'])}" + \
                (f" + encargos {fmt_montante(enc)}" if enc else "")
            movs.append(MovimentoBruto(p["data"], p["data_valor"], f"PRESTACAO {numero} ({detalhe})", total,
                                       None if final is None else final + p["juros"] + enc))
            if p["juros"]:
                movs.append(MovimentoBruto(p["data"], p["data_valor"], f"JUROS PRESTACAO {numero}", -p["juros"],
                                           None if final is None else final + enc))
            resto = enc
            for desc, v in p["encargos"]:
                resto -= v
                movs.append(MovimentoBruto(p["data"], p["data_valor"], f"ENCARGOS PRESTACAO {numero} {desc}", -v,
                                           None if final is None else final + resto))
        return Extrato(parser=Parser.chave, banco=Parser.banco, movimentos=movs, emprestimo=True, avisos=avisos)
