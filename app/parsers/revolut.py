"""Revolut — extrato de conta (CSV ou Excel), em português ou inglês.

Formato observado (2026-10, pt-PT):
  Tipo | Produto | Data de início | Data de Conclusão | Descrição | Montante | Comissão | Moeda | Estado | Saldo
  Pagamento com cartão | Atual | 2025-01-03 16:51:58 | 2025-01-04 12:36:11 | Amazon | -26.82 | 0.00 | EUR | CONCLUÍDA | 188.82
(em inglês: Type, Product, Started Date, Completed Date, Description, Amount, Fee, Currency, State, Balance).
A exportação «Excel» traz o CSV inteiro numa só coluna e parte do texto mal codificado
(«CONCLUÃ\\x8dDA») — parsers.ler_linhas separa as colunas e corrige o texto.

- Cada (Produto, Moeda) é uma subconta com saldo próprio:
    «Atual» (conta corrente, em várias moedas), «Depósito» (Conta reforçada),
    «Poupanças» (cofres), «Investimentos» (conta de ações, em USD).
- Saldo = saldo anterior + Montante − Comissão: a comissão vira um movimento
  à parte («Comissão Revolut …»), para contar como comissão bancária.
- Movimentos REVERTIDA/PENDENTE ficam de fora (não mexeram no saldo).
- Carregamentos, câmbios e transferências entre subcontas ou para a conta
  de investimento são transferências (não são receita nem despesa).
- Moedas estrangeiras: a taxa de cada câmbio entre o euro e a moeda (os dois lados têm a
  mesma data e hora) converte os movimentos dessa moeda — usa-se o câmbio mais próximo no
  tempo. A subconta («Todas as moedas», EUR) junta a conta Atual em euros com as outras moedas
  convertidas (a descrição guarda o valor original).
- Investimentos como carteira: compras e vendas de ações («Investing money», «Closing
  investment») são trocas dentro da carteira e não entram; dividendos, impostos e taxas de
  custódia são ganho/perda; câmbios para USD e «Revolut Trading Ltd» são aplicações/resgates.
  Há a subconta na moeda original e («Investimentos», EUR) convertida.
"""
import bisect
import re
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import date

from . import Extrato, MovimentoBruto, encontrar_cabecalho, para_cent, para_data

COLUNAS_PT = ["tipo", "produto", "datadeinicio", "datadeconclusao", "descri", "montante", "comissao", "moeda",
              "estado", "saldo"]
COLUNAS_EN = ["type", "product", "starteddate", "completeddate", "description", "amount", "fee", "currency",
              "state", "balance"]
CONCLUIDO = {"CONCLUÍDA", "CONCLUIDA", "COMPLETED"}
PRODUTOS = {"ATUAL": "Atual", "CURRENT": "Atual", "DEPÓSITO": "Depósito", "DEPOSITO": "Depósito",
            "SAVINGS": "Depósito", "DEPOSIT": "Depósito", "POUPANÇAS": "Poupanças", "POUPANCAS": "Poupanças",
            "POCKETS": "Poupanças", "INVESTIMENTOS": "Investimentos", "INVESTMENTS": "Investimentos"}

# Tipos que mudam dinheiro de sítio sem ser receita nem despesa.
TIPOS_TRANSFERENCIA = {"CARREGAMENTO", "TOPUP", "TOP-UP", "CÂMBIO", "CAMBIO", "EXCHANGE"}
TIPOS_TRANSFERENCIA_NOME = {"TRANSFERÊNCIA", "TRANSFERENCIA", "TRANSFER"}
# Transferências entre as subcontas do próprio titular / para investimento.
DESCRICAO_PROPRIA = re.compile(r"CONTA REFOR|SAVINGS|POUPAN|POCKET|SUBCONTA|BALANCE MIGRATION|CONTA DE INVESTIMENTO|"
                               r"INVESTMENT ACCOUNT|REVOLUT TRADING|^PARA EUR |^DE EUR |^TO EUR |^FROM EUR ", re.IGNORECASE)
# Investimentos: compra/venda de ações — trocas dentro da carteira.
TROCA_ACOES = re.compile(r"^(INVESTING MONEY|CLOSING INVESTMENT)", re.IGNORECASE)


@dataclass
class _Registo:
    produto: str
    moeda: str
    tipo: str
    inicio: str           # «Data de início» completa (data e hora): emparelha os dois lados de um câmbio
    data: date
    data_valor: date | None
    desc: str
    montante: int
    comissao: int
    saldo: int | None
    transferencia: bool


def _data(texto):
    t = str(texto or "").strip()
    return para_data(t[:10]) if t else None


def _produto(texto: str) -> str:
    t = str(texto or "").strip()
    return PRODUTOS.get(t.upper(), t or "Atual")


def _fmt_original(cent: int, moeda: str) -> str:
    """-2061200, KRW → '-20.612 KRW'; -1224, USD → '-12,24 USD'."""
    inteiro = abs(cent) // 100
    dec = abs(cent) % 100
    txt = f"{inteiro:,}".replace(",", ".") + (f",{dec:02d}" if dec else "")
    return f"{'-' if cent < 0 else ''}{txt} {moeda}"


class Taxas:
    """Euros por unidade de cada moeda, a partir dos câmbios do próprio extrato."""

    def __init__(self, registos: list[_Registo]):
        grupos = defaultdict(list)
        for r in registos:
            if r.tipo.upper() in ("CÂMBIO", "CAMBIO", "EXCHANGE") and r.montante:
                grupos[r.inicio].append(r)
        pontos = defaultdict(list)
        for g in grupos.values():
            eur = [r for r in g if r.moeda == "EUR"]
            outros = [r for r in g if r.moeda != "EUR"]
            if len(eur) == 1 and len(outros) == 1 and (eur[0].montante > 0) != (outros[0].montante > 0):
                o = outros[0]
                pontos[o.moeda].append((o.data.toordinal(), abs(eur[0].montante) / abs(o.montante)))
        self.pontos = {m: sorted(p) for m, p in pontos.items()}

    def em_euros(self, cent: int, moeda: str, d: date) -> int | None:
        p = self.pontos.get(moeda)
        if not p:
            return None
        i = bisect.bisect_left(p, (d.toordinal(), 0))
        vizinhos = [p[j] for j in (i - 1, i) if 0 <= j < len(p)]
        _, taxa = min(vizinhos, key=lambda x: abs(x[0] - d.toordinal()))
        return round(cent * taxa)


class Parser:
    chave = "revolut"
    banco = "Revolut"
    FICHA = {
        "extrato": "Conta corrente (todas as moedas), cofres, Conta reforçada e Investimentos",
        "ficheiro": "CSV ou Excel (pt ou en)",
        "onde": "App Revolut › Conta › Extrato › Excel/CSV",
        "colunas": "Tipo, Produto, Data de início, Data de Conclusão, Descrição, Montante, Comissão, Moeda, Estado, Saldo",
        "ordem": "Mais antigo primeiro",
        "saldo": True,
        "destino": "Carteira digital (todas as moedas convertidas a EUR), conta numa moeda, Poupança (Conta "
                   "reforçada ou cofres) e Investimento (ações, como carteira)",
        "notas": "O Excel da Revolut traz o CSV numa só coluna e texto mal codificado: a app corrige. Comissões à "
                 "parte; revertidos ignorados; carregamentos e câmbios são transferências; outras moedas "
                 "convertidas pelos câmbios do próprio extrato; compras/vendas de ações não entram.",
    }

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
        registos, ignorados = [], 0
        for linha in linhas[i + 1:]:
            if not linha or len(linha) <= SALDO or not str(linha[INI]).strip():
                continue
            if str(linha[ESTADO]).strip().upper() not in CONCLUIDO:
                ignorados += 1
                continue
            tipo = str(linha[TIPO]).strip()
            produto = _produto(linha[PROD])
            desc = " ".join(str(linha[DESC]).split())
            transferencia = tipo.upper() in TIPOS_TRANSFERENCIA or (
                tipo.upper() in TIPOS_TRANSFERENCIA_NOME and bool(DESCRICAO_PROPRIA.search(desc)))
            if produto == "Investimentos" and tipo.upper() in TIPOS_TRANSFERENCIA_NOME:
                transferencia = True   # entradas/saídas da conta de ações: aplicações/resgates
            registos.append(_Registo(
                produto=produto, moeda=str(linha[MOEDA]).strip().upper() or "EUR", tipo=tipo,
                inicio=str(linha[INI]).strip(), data=_data(linha[INI]), data_valor=_data(linha[FIM]), desc=desc,
                montante=para_cent(linha[MONT]) or 0, comissao=para_cent(linha[COM]) or 0,
                saldo=para_cent(linha[SALDO]), transferencia=transferencia))

        subcontas: OrderedDict = OrderedDict()
        trocas = 0
        for r in registos:
            if r.produto == "Investimentos" and TROCA_ACOES.search(r.desc):
                trocas += 1
                continue
            movs = subcontas.setdefault((r.produto, r.moeda), [])
            saldo = None if r.produto == "Investimentos" else r.saldo   # sem as trocas, a cadeia de saldos não fecha
            if r.montante:
                movs.append(MovimentoBruto(r.data, r.data_valor, r.desc, r.montante,
                                           None if saldo is None else saldo + r.comissao, transferencia=r.transferencia))
            if r.comissao:
                movs.append(MovimentoBruto(r.data, r.data_valor, f"Comissão Revolut ({r.tipo}): {r.desc}", -r.comissao,
                                           saldo))

        # Outras moedas convertidas a euros pelos câmbios do próprio extrato
        taxas = Taxas(registos)
        sem_taxa = set()
        todas, invest_eur = [], []
        for r in registos:
            if r.produto not in ("Atual", "Investimentos") or (r.produto == "Investimentos" and TROCA_ACOES.search(r.desc)):
                continue
            destino = todas if r.produto == "Atual" else invest_eur
            if r.moeda == "EUR":
                if r.produto == "Atual":
                    if r.montante:
                        destino.append(MovimentoBruto(r.data, r.data_valor, r.desc, r.montante,
                                                      None if r.saldo is None else r.saldo + r.comissao,
                                                      transferencia=r.transferencia))
                    if r.comissao:
                        destino.append(MovimentoBruto(r.data, r.data_valor, f"Comissão Revolut ({r.tipo}): {r.desc}",
                                                      -r.comissao, r.saldo))
                else:
                    if r.montante:
                        destino.append(MovimentoBruto(r.data, r.data_valor, r.desc, r.montante,
                                                      transferencia=r.transferencia))
                    if r.comissao:
                        destino.append(MovimentoBruto(r.data, r.data_valor, f"Comissão Revolut ({r.tipo}): {r.desc}",
                                                      -r.comissao))
                continue
            for valor, desc, transf in ((r.montante, r.desc, r.transferencia),
                                        (-r.comissao, f"Comissão Revolut ({r.tipo}): {r.desc}", False)):
                if not valor:
                    continue
                eur = taxas.em_euros(valor, r.moeda, r.data)
                if eur is None:
                    sem_taxa.add(r.moeda)
                    continue
                destino.append(MovimentoBruto(r.data, r.data_valor, f"{desc} ({_fmt_original(valor, r.moeda)})", eur,
                                              None, transferencia=transf))
        if any(m != "EUR" for (p, m) in subcontas if p == "Atual"):
            subcontas[("Todas as moedas", "EUR")] = todas
        if invest_eur and ("Investimentos", "EUR") not in subcontas:
            subcontas[("Investimentos", "EUR")] = invest_eur

        principal = subcontas.get(("Atual", "EUR")) or next(iter(subcontas.values()), [])
        extrato = Extrato(parser=Parser.chave, banco=Parser.banco, movimentos=principal, subcontas=dict(subcontas))
        if ignorados:
            extrato.avisos.append(f"{ignorados} movimento(s) revertidos ou pendentes foram ignorados (não mexeram no saldo).")
        if trocas:
            extrato.avisos.append(f"{trocas} compra(s)/venda(s) de ações são trocas dentro da carteira de investimentos "
                                  "e não entram como movimentos.")
        if sem_taxa:
            extrato.avisos.append("Sem câmbio com o euro no extrato para " + ", ".join(sorted(sem_taxa))
                                  + ": esses movimentos não entram na conversão para euros.")
        return extrato
