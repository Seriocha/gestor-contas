"""Revolut — extrato consolidado («consolidated-statement-v2_…_pt.xlsx»): o mesmo relatório do PDF «Extrato
personalizado», exportado em Excel (o CSV inteiro numa só coluna — parsers.ler_linhas separa as colunas).

Estrutura observada (2026-10, ficheiro de 2020):
  - resumos (uma secção por conta/moeda, poupanças, investimentos, cripto, bens) — ignorados;
  - «Contas-correntes Extratos de operações»: «Conta Pessoal (EUR)», «Conta Pessoal (USD)», cofres
    «Poupança <nome> (EUR)», cada um com «Extrato de operações» e a tabela
        Data | Descrição | Categoria | Dinheiro a entrar/sair | Saldo | Imposto retido | Outros impostos | Comissões
    («18 de jan. de 2020», «-12,34€»). Nas moedas estrangeiras cada coluna de valores vem em dobro: na moeda
    («12,00$») e em euros («10,80€»); o saldo só na moeda. Linha «Total» no fim;
  - «Savings Accounts Transaction Statements»: a Conta reforçada («Poupança de Acesso Imediato») só com os recibos
    de juros (Data | Descrição | TAE | TANB | Gross interest | Impostos retidos | Other taxes | Fees | Net interest);
  - investimentos, cripto, bens: não são movimentos de conta — ignorados.
A Revolut grava o texto com os acentos já perdidos («C�mbio», «Poupan�a F�rias»): as comparações
ignoram as letras acentuadas e as palavras habituais são repostas (REPOR).

Subcontas iguais às do PDF (revolut_pdf), para se poder importar um ou outro sem duplicar (dedup por valor e saldo).
"""
import re
import unicodedata
from collections import OrderedDict
from datetime import date

from . import Extrato, MovimentoBruto, para_data
from .revolut_pdf import DESCRICAO_PROPRIA, _data, _valor

ESTRAGADO = "�"
# palavras habituais com o acento perdido → como devem ser
REPOR = {w.replace("@", ESTRAGADO): c for w, c in (
    ("Poupan@a", "Poupança"), ("F@rias", "Férias"), ("C@mbio", "Câmbio"), ("Convers@o", "Conversão"),
    ("Transfer@ncia", "Transferência"), ("Dep@sito", "Depósito"), ("Cart@o", "Cartão"), ("Comiss@o", "Comissão"),
    ("Comiss@es", "Comissões"), ("Opera@@o", "Operação"), ("Opera@@es", "Operações"), ("Cr@dito", "Crédito"),
    ("D@bito", "Débito"), ("Pagamento autom@tico", "Pagamento automático"), ("Dispon@vel", "Disponível"),
    ("intrab@ncaria", "intrabancária"), ("Intrab@ncaria", "Intrabancária"), ("Emerg@ncia", "Emergência"),
    ("Viagem ao Jap@o", "Viagem ao Japão"), ("Jap@o", "Japão"), ("Su@@a", "Suíça"), ("Fran@a", "França"),
    ("Reembolso de comiss@o", "Reembolso de comissão"),
    ("numer@rio", "numerário"), ("Pa@s", "País"), ("Rendimento l@quido", "Rendimento líquido"))}
SECCAO = re.compile(r"^(Conta Pessoal|Poupan.a .+?)\s+\(([A-Z]{3})\)$")
JUROS_SECCAO = re.compile(r"^Poupan.a de Acesso Imediato\s+\(([A-Z]{3})\)$")
TRANSFERENCIA_CATEGORIAS = {"cmbio", "carregar", "carregamento", "exchange", "topup"}


def chave(texto) -> str:
    """Comparação que ignora acentos E letras estragadas: tira tudo o que não é ASCII, depois só [a-z0-9]."""
    t = "".join(ch for ch in str(texto or "") if ord(ch) < 128)
    return re.sub(r"[^a-z0-9]", "", t.lower())


def repor(texto: str) -> str:
    t = " ".join(str(texto or "").split())
    if ESTRAGADO not in t:
        return t
    for errado, certo in sorted(REPOR.items(), key=lambda x: -len(x[0])):
        t = t.replace(errado, certo)
    return t


def _data_linha(v) -> date | None:
    if isinstance(v, date):
        return para_data(v)
    t = str(v or "").strip()
    return _data(t) or (para_data(t) if re.match(r"^\d{1,4}[-/]\d{1,2}[-/]\d{1,4}$", t) else None)


def _limpa(linha) -> list:
    out = list(linha or [])
    while out and out[-1] in ("", None):
        out.pop()
    return out


class Parser:
    chave = "revolut_consolidado"
    banco = "Revolut (extrato consolidado)"
    FICHA = {
        "extrato": "Conta corrente (todas as moedas), cofres e juros da Conta reforçada",
        "ficheiro": "Excel «consolidated-statement-v2_…_pt.xlsx» (o mesmo relatório do PDF «Extrato personalizado»)",
        "onde": "App Revolut › Perfil › Extratos › Extrato personalizado (todas as contas) › Excel",
        "colunas": "Data, Descrição, Categoria, Dinheiro a entrar/sair, Saldo, Imposto retido, Outros impostos, Comissões",
        "ordem": "Mais antigo primeiro, uma tabela por conta e moeda",
        "saldo": True,
        "destino": "Carteira digital (todas as moedas convertidas a EUR), conta numa moeda, Poupança (cofre ou juros)",
        "notas": "Mesmas subcontas que o PDF (importar um ou outro não duplica). A Revolut grava o texto sem alguns "
                 "acentos: a app repõe os habituais. Outras moedas convertidas pelo valor em euros do próprio extrato.",
    }

    @staticmethod
    def detetar(linhas) -> bool:
        primeiras = [chave(l[0]) for l in linhas[:400] if l and l[0] not in ("", None)]
        return any(p.startswith("contascorrentesextratosdeopera") for p in primeiras) and \
            any(p.startswith("contascorrentesresumos") for p in primeiras[:5])

    @staticmethod
    def ler(linhas) -> Extrato:
        modo, seccao, moeda = None, None, "EUR"
        cols: dict = {}
        subcontas: OrderedDict = OrderedDict()
        totais = []                            # (secção, moeda, total do extrato) para conferir a soma
        for bruta in linhas:
            linha = _limpa(bruta)
            if not linha:
                continue
            k = chave(linha[0])
            if k.startswith("contascorrentesextratosdeopera"):
                modo, seccao = "contas", None
                continue
            if k.startswith("savingsaccountstransactionstatement"):
                modo, seccao = "juros", None
                continue
            if k.endswith("resumos") or ("extratosdeopera" in k and not k.startswith("contascorrentes")) \
                    or k.startswith(("investmentservices", "cripto", "bem")):
                modo, seccao = None, None
                continue
            if modo is None:
                continue
            texto = repor(linha[0])
            if len(linha) == 1:
                m = JUROS_SECCAO.match(texto) if modo == "juros" else SECCAO.match(texto)
                if m:
                    if modo == "juros":
                        seccao, moeda = "Depósito", m.group(1)
                    else:
                        seccao = "Atual" if m.group(1) == "Conta Pessoal" else m.group(1)
                        moeda = m.group(2)
                    cols = {}
                elif texto.startswith("---"):
                    seccao = None
                continue
            if k == "data":
                cols = Parser._colunas(linha)
                continue
            if seccao is None or not cols:
                continue
            d = _data_linha(linha[0])
            if d is None:
                if k == "total" and modo == "contas" and cols.get("montante"):
                    totais.append((seccao, moeda, Parser._total(linha, moeda, cols)))
                continue                       # «Total» e afins
            if modo == "juros":
                liquido = _valor(str(linha[cols["liquido"]])) if cols.get("liquido") is not None \
                    and cols["liquido"] < len(linha) else None
                if liquido:
                    subcontas.setdefault(("Depósito", moeda), []).append(
                        MovimentoBruto(d, None, "Juros Conta reforçada", liquido))
                continue
            nativo, euros = Parser._operacao(linha, d, moeda, cols)
            if nativo is None:
                continue
            subcontas.setdefault((seccao, moeda), []).append(nativo)
            if seccao == "Atual":
                subcontas.setdefault(("Todas as moedas", "EUR"), []).append(euros)
        for movs in subcontas.values():
            movs.sort(key=lambda m: m.data_mov)    # estável: mantém a ordem do extrato dentro do dia
        principal = subcontas.get(("Todas as moedas", "EUR")) or next(iter(subcontas.values()), [])
        ext = Extrato(parser=Parser.chave, banco=Parser.banco, movimentos=list(principal), subcontas=dict(subcontas))
        for seccao_, moeda_, total in totais:
            soma = sum(m.montante for m in subcontas.get((seccao_, moeda_), []))
            if total is not None and total != soma:
                ext.avisos.append(f"{seccao_} {moeda_}: a soma dos movimentos ({soma / 100:.2f}) não bate com o "
                                  f"Total do extrato ({total / 100:.2f}).")
        ext.dedup_dias = 4      # como o PDF: data de conclusão; o CSV usa a de início (até ~3 dias antes)
        return ext

    @staticmethod
    def _colunas(cab: list) -> dict:
        """Índices pelo cabeçalho; nas moedas estrangeiras «Dinheiro a entrar/sair» aparece duas vezes."""
        ks = [chave(c) for c in cab]
        def todos(prefixo):
            return [i for i, c in enumerate(ks) if c.startswith(prefixo)]
        return {"descricao": next(iter(todos("descri")), 1), "categoria": next(iter(todos("categoria")), None),
                "montante": todos("dinheiroaentrar"), "saldo": todos("saldo"),
                "liquido": next(iter(todos("netinterest") or todos("juroslquido") or todos("juros")), None)}

    @staticmethod
    def _total(linha: list, moeda: str, cols: dict) -> int | None:
        euro = moeda == "EUR"
        for i in cols["montante"]:
            t = str(linha[i]) if i < len(linha) and linha[i] not in (None, "") else ""
            if t and ("€" in t) == euro and _valor(t) is not None:
                return _valor(t)
        return None

    @staticmethod
    def _operacao(linha: list, d: date, moeda: str, cols: dict):
        def celula(i):
            return str(linha[i]) if i is not None and i < len(linha) and linha[i] not in (None, "") else ""
        def escolher(indices, euro: bool):
            for i in indices:
                t = celula(i)
                if t and ("€" in t) == euro and _valor(t) is not None:
                    return _valor(t)
            return None
        categoria = repor(celula(cols["categoria"]))
        descricao = repor(celula(cols["descricao"])) or f"{categoria or 'Movimento'} (sem descrição)"
        if moeda == "EUR":
            montante, saldo = escolher(cols["montante"], True), escolher(cols["saldo"], True)
            montante_eur = montante
        else:
            montante, saldo = escolher(cols["montante"], False), escolher(cols["saldo"], False)
            montante_eur = escolher(cols["montante"], True)
        if montante is None:
            return None, None
        transferencia = chave(categoria) in TRANSFERENCIA_CATEGORIAS or bool(DESCRICAO_PROPRIA.search(descricao))
        nativo = MovimentoBruto(d, None, descricao, montante, saldo, transferencia=transferencia)
        if moeda == "EUR":
            euros = MovimentoBruto(d, None, descricao, montante, saldo, transferencia=transferencia)
        else:
            original = f"{montante / 100:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".").rstrip("0").rstrip(",")
            euros = MovimentoBruto(d, None, f"{descricao} ({original} {moeda})",
                                   montante_eur if montante_eur is not None else 0, None, transferencia=transferencia)
        return nativo, euros
