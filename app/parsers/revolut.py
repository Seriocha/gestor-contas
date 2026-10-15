"""Revolut — extrato de conta (CSV), em português ou inglês.

Formato observado (2026-10, pt-PT):
  Tipo | Produto | Data de início | Data de Conclusão | Descrição | Montante | Comissão | Moeda | Estado | Saldo
  Pagamento com cartão | Atual | 2025-01-03 16:51:58 | 2025-01-04 12:36:11 | Amazon | -26.82 | 0.00 | EUR | CONCLUÍDA | 188.82
(em inglês: Type, Product, Started Date, Completed Date, Description, Amount, Fee, Currency, State, Balance).

- Cada (Produto, Moeda) é uma subconta com saldo próprio: «Atual» é a conta
  corrente, «Depósito»/«Savings» a poupança («Conta reforçada»). O ficheiro
  traz todas; a importação escolhe a da conta de destino (servicos.escolher_subconta).
- Saldo = saldo anterior + Montante − Comissão: a comissão vira um movimento
  à parte («Comissão Revolut …»), para contar como comissão bancária.
- Movimentos REVERTIDA/PENDENTE ficam de fora (não mexeram no saldo).
- Carregamentos, câmbios e transferências entre subcontas ou para a conta
  de investimento vêm marcados como transferência (não são receita nem despesa).
"""
import re
from collections import OrderedDict

from . import Extrato, MovimentoBruto, encontrar_cabecalho, para_cent, para_data

COLUNAS_PT = ["tipo", "produto", "datadeinicio", "datadeconclusao", "descri", "montante", "comissao", "moeda",
              "estado", "saldo"]
COLUNAS_EN = ["type", "product", "starteddate", "completeddate", "description", "amount", "fee", "currency",
              "state", "balance"]
CONCLUIDO = {"CONCLUÍDA", "CONCLUIDA", "COMPLETED"}
POUPANCA = {"DEPÓSITO", "DEPOSITO", "SAVINGS", "DEPOSIT"}

# Tipos que mudam dinheiro de sítio sem ser receita nem despesa.
TIPOS_TRANSFERENCIA = {"CARREGAMENTO", "TOPUP", "TOP-UP", "CÂMBIO", "CAMBIO", "EXCHANGE"}
# Transferências entre as subcontas do próprio titular / para investimento.
DESCRICAO_PROPRIA = re.compile(r"CONTA REFOR|SAVINGS|POUPAN|POCKET|CONTA DE INVESTIMENTO|INVESTMENT ACCOUNT|"
                               r"^PARA EUR |^DE EUR |^TO EUR |^FROM EUR ", re.IGNORECASE)


def _data(texto):
    t = str(texto or "").strip()
    return para_data(t[:10]) if t else None


class Parser:
    chave = "revolut"
    banco = "Revolut"

    @staticmethod
    def _colunas(linhas):
        return encontrar_cabecalho(linhas, COLUNAS_PT, 5) or encontrar_cabecalho(linhas, COLUNAS_EN, 5)

    @staticmethod
    def detetar(linhas) -> bool:
        return Parser._colunas(linhas) is not None

    @staticmethod
    def ler(linhas) -> Extrato:
        i, c = Parser._colunas(linhas)
        k = list(c.values())  # mesma ordem nas duas línguas
        TIPO, PROD, INI, FIM, DESC, MONT, COM, MOEDA, ESTADO, SALDO = k
        subcontas: OrderedDict = OrderedDict()
        ignorados = 0
        for linha in linhas[i + 1:]:
            if not linha or len(linha) <= SALDO or not str(linha[INI]).strip():
                continue
            if str(linha[ESTADO]).strip().upper() not in CONCLUIDO:
                ignorados += 1
                continue
            tipo = str(linha[TIPO]).strip()
            produto = "Depósito" if str(linha[PROD]).strip().upper() in POUPANCA else "Atual"
            moeda = str(linha[MOEDA]).strip().upper() or "EUR"
            desc = " ".join(str(linha[DESC]).split())
            montante = para_cent(linha[MONT]) or 0
            comissao = para_cent(linha[COM]) or 0
            saldo = para_cent(linha[SALDO])
            data, data_valor = _data(linha[INI]), _data(linha[FIM])
            transferencia = tipo.upper() in TIPOS_TRANSFERENCIA or (
                tipo.upper() in ("TRANSFERÊNCIA", "TRANSFERENCIA", "TRANSFER") and bool(DESCRICAO_PROPRIA.search(desc)))
            movs = subcontas.setdefault((produto, moeda), [])
            if montante:
                movs.append(MovimentoBruto(data, data_valor, desc, montante,
                                           None if saldo is None else saldo + comissao, transferencia=transferencia))
            if comissao:
                movs.append(MovimentoBruto(data, data_valor, f"Comissão Revolut ({tipo}): {desc}", -comissao, saldo))
        principal = subcontas.get(("Atual", "EUR")) or next(iter(subcontas.values()), [])
        extrato = Extrato(parser=Parser.chave, banco=Parser.banco, movimentos=principal, subcontas=dict(subcontas))
        if ignorados:
            extrato.avisos.append(f"{ignorados} movimento(s) revertidos ou pendentes foram ignorados (não mexeram no saldo).")
        return extrato
