"""Banco BiG — movimentos da conta à ordem (CSV).

Formato observado (2026-10):
  "Movimento*","Data Valor","Data Mov.","Natureza","Moeda","Original","EUR","Contabilistico*"
  "Juros 5512487550 Juros 5512487550","01-04-2026","01-04-2026","Crédito","EUR"," ","1,60","201,60"
- A descrição vem duplicada: «X X», ou a 1.ª metade truncada com «..» e a 2.ª
  completa («TRF … +ANA CRISTINA .. TRF … +ANA CRISTINA ESTEVES MELO»). Fica a completa.
- «EUR» é o valor com sinal; «Contabilistico*» o saldo após o movimento;
  «Natureza» (Crédito/Débito) é redundante; «Original» é o valor noutra moeda.
- Mais recente primeiro; datas dd-mm-aaaa; usa-se a «Data Mov.».
- Aplicações em fundos («Transf. da Conta Liquidez p/ a FA …») e o resgate da
  poupança («Levantamento SuperConta») são transferências entre contas próprias.
"""
import re

from . import Extrato, MovimentoBruto, encontrar_cabecalho, para_cent, para_data

COLUNAS = ["movimento", "datavalor", "datamov", "natureza", "moeda", "original", "eur", "contabilistico"]
TRANSFERENCIA_PROPRIA = re.compile(r"^TRANSF\. DA CONTA LIQUIDEZ|P/ A FA\b|^LEVANTAMENTO SUPERCONTA", re.IGNORECASE)


def descricao_sem_duplicado(texto: str) -> str:
    """'A A' → 'A'; 'TRF … +ANA .. TRF … +ANA ESTEVES MELO' → a 2.ª (completa)."""
    t = " ".join(str(texto or "").split())
    # «início truncado.. texto completo» (com ou sem espaço antes dos «..»)
    m = re.match(r"^(.*?)\s?\.\.\s(.*)$", t)
    if m and m.group(2).startswith(m.group(1).rstrip()):
        return m.group(2)
    # «A A»: as duas metades, separadas por um espaço, são iguais
    meio = len(t) // 2
    if len(t) % 2 == 1 and t[meio] == " " and t[:meio] == t[meio + 1:]:
        return t[:meio]
    return t


class Parser:
    chave = "big"
    banco = "Banco BiG"
    FICHA = {
        "extrato": "Conta à ordem",
        "ficheiro": "CSV (UTF-8, vírgulas)",
        "onde": "BiG online › Contas › Movimentos › Exportar",
        "colunas": "Movimento*, Data Valor, Data Mov., Natureza, Moeda, Original, EUR, Contabilistico*",
        "ordem": "Mais recente primeiro",
        "saldo": True,
        "destino": "Conta à ordem (Banco BiG)",
        "notas": "Descrição duplicada (fica a completa). Aplicações em fundos e «Levantamento SuperConta» "
                 "marcadas como transferência.",
    }

    @staticmethod
    def detetar(linhas) -> bool:
        return encontrar_cabecalho(linhas, COLUNAS, 5) is not None

    @staticmethod
    def ler(linhas) -> Extrato:
        i, col = encontrar_cabecalho(linhas, COLUNAS, 5)
        movs = []
        for linha in linhas[i + 1:]:
            if not linha or len(linha) <= col["contabilistico"] or not str(linha[col["datamov"]]).strip():
                continue
            montante = para_cent(linha[col["eur"]])
            if montante is None:
                continue
            descricao = descricao_sem_duplicado(linha[col["movimento"]])
            movs.append(MovimentoBruto(
                data_mov=para_data(linha[col["datamov"]]), data_valor=para_data(linha[col["datavalor"]]),
                descricao=descricao, montante=montante, saldo=para_cent(linha[col["contabilistico"]]),
                transferencia=bool(TRANSFERENCIA_PROPRIA.search(descricao)),
            ))
        moeda = next((str(l[col["moeda"]]).strip().upper() for l in linhas[i + 1:]
                      if l and len(l) > col["moeda"] and str(l[col["moeda"]]).strip()), "EUR")
        return Extrato(parser=Parser.chave, banco=Parser.banco, movimentos=movs, moeda=moeda)
