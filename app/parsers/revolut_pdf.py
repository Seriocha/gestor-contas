"""Revolut — «Extrato personalizado» consolidado em PDF (pt).

Estrutura observada (2026-10):
  - páginas de resumo (uma por conta/moeda, poupanças, fundos, investimentos, cripto);
  - «Contas-correntes · Extratos de operações»: uma tabela por conta e moeda
    («Conta Pessoal (EUR)», «Conta Pessoal (KRW)», …, cofres «Poupança <nome> (EUR)»):
        Data | Descrição | Categoria | Dinheiro a entrar/sair | Saldo | Imposto retido | Outros impostos | Comissões
    A descrição pode continuar nas linhas seguintes. Nas moedas estrangeiras cada
    valor ocupa 3 linhas: o valor na moeda, a unidade («KRW») e o equivalente em
    euros calculado pela Revolut («-12,24€»);
  - «Savings Accounts · Transaction Statements»: a Conta reforçada («Poupança de
    Acesso Imediato») só com os recibos de juros («10/08 … 0,01€ … 0,01€» e «/2026» na linha seguinte);
  - fundos, investimentos, cripto, ouro: não são movimentos de conta — ignorados.
  - páginas de resumo: o saldo disponível inicial e final de cada conta (para conferir os movimentos e
    mostrar as contas sem movimentos) e, nas outras secções, os rendimentos e impostos do período
    (juros, dividendos, retornos dos fundos, vendas/mais-valias, staking) — o resumo fiscal do ano.

Decisões:
  - O saldo de cada linha não desconta impostos/comissões (já estão no valor), por
    isso não viram movimentos à parte.
  - Câmbios, carregamentos e envios para poupanças/investimento são transferências.
  - Subcontas: («Atual», moeda) na moeda original; («Atual», «EUR») só a conta em euros;
    («Todas as moedas», «EUR») = euros + as outras moedas convertidas pelo valor em
    euros do próprio extrato (a descrição guarda o valor original); («Poupança <nome>», «EUR»)
    para cada cofre; («Depósito», «EUR») os juros da Conta reforçada.

O parser trabalha sobre as palavras de cada página com a sua posição (x, topo),
para poder ser testado sem um PDF real (interpretar()); ler_pdf() usa o pdfplumber.
"""
import io
import re
from collections import OrderedDict
from datetime import date
from decimal import Decimal, InvalidOperation

from . import Extrato, MovimentoBruto

MESES = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6, "jul": 7, "ago": 8, "set": 9, "out": 10,
         "nov": 11, "dez": 12}
DATA_PT = re.compile(r"^(\d{1,2}) de ([a-zç]{3})\.? de (\d{4})$", re.IGNORECASE)
DATA_JUROS = re.compile(r"^(\d{2})/(\d{2})$")
SECCAO = re.compile(r"^(Conta Pessoal|Poupança .+?) \(([A-Z]{3})\)$")
TRANSFERENCIA_CATEGORIAS = {"CÂMBIO", "CAMBIO", "CARREGAR", "CARREGAMENTO"}
DESCRICAO_PROPRIA = re.compile(r"CONTA DE INVESTIMENTO|POUPAN|CONTA REFOR|SUBCONTA|COFRE|CARREGAMENTO DE", re.IGNORECASE)
# limites das colunas (pontos), medidos no cabeçalho; recalculados por página quando o cabeçalho aparece
COLUNAS = {"descricao": 125, "categoria": 257, "montante": 342, "saldo": 384, "imposto": 427, "outros": 469,
           "comissoes": 505}
TOTAIS = ("Total",)
PERIODO = re.compile(r"^(\d{1,2} de [a-zç]{3}\.? de \d{4}) - (\d{1,2} de [a-zç]{3}\.? de \d{4})$", re.IGNORECASE)
SALDO_RESUMO = re.compile(r"Saldo disponível (inicial|final) (-?[\d.,]+)")
# «Rótulo 12,34€» (vários por linha); só os rótulos de rendimentos/impostos contam para o resumo fiscal
PAR_FISCAL = re.compile(r"([A-Za-zÀ-ÿ&][A-Za-zÀ-ÿ&./ -]*?)\s+(-?[\d.]+,\d{2}€)")
ROTULO_FISCAL = re.compile(r"rendimento|retorno|imposto|dividend|tax|income|receita|cost basis|^base$|ganhos|pnl|"
                           r"comiss|staking|vendas|custo|valor total|valor bruto", re.IGNORECASE)
ROTULO_IGNORAR = re.compile(r"saldo|carregamentos|levantamentos|médio", re.IGNORECASE)
SECCOES_FISCAIS = {"Savings Accounts": "Conta reforçada (juros)", "Fundos Monetários Flexíveis": "Fundos Monetários Flexíveis",
                   "Investment Services": "Investimentos (ações)", "Cripto": "Cripto", "Bem": "Bens (metais preciosos)"}
TRADUCOES = {"Gross income": "Juros brutos", "Net income": "Juros líquidos", "Dividends": "Dividendos",
             "Tax withheld on dividend": "Imposto retido nos dividendos", "Cost basis": "Custo de aquisição",
             "Base": "Custo de aquisição", "Ganhos e perdas brutos": "Mais/menos-valias brutas realizadas"}


class Parser:
    chave = "revolut_pdf"
    banco = "Revolut (PDF)"
    FICHA = {
        "extrato": "Conta corrente (todas as moedas), cofres e juros da Conta reforçada",
        "ficheiro": "PDF «Extrato personalizado» consolidado",
        "onde": "App Revolut › Perfil › Extratos › Extrato personalizado (todas as contas) › PDF",
        "colunas": "Data, Descrição, Categoria, Dinheiro a entrar/sair, Saldo, Imposto retido, Outros impostos, Comissões",
        "ordem": "Mais antigo primeiro, uma tabela por conta e moeda",
        "saldo": True,
        "destino": "Carteira digital (todas as moedas convertidas a EUR), conta numa moeda, Poupança (cofre ou juros); "
                   "ou todas as contas de uma vez («Importar todas as contas»)",
        "notas": "Moedas estrangeiras convertidas pelo valor em euros do próprio extrato; câmbios, carregamentos e "
                 "envios para poupança/investimento são transferências; a Conta reforçada só traz os juros. "
                 "Serve o extrato do ano fiscal (1 de janeiro até hoje, todas as contas): os saldos de cada conta "
                 "são conferidos com as páginas de resumo e os rendimentos e impostos (juros, dividendos, fundos, "
                 "vendas, staking) aparecem no resumo fiscal da pré-visualização.",
    }

    @staticmethod
    def detetar(linhas) -> bool:   # não lê folhas de cálculo: o PDF entra por ler_pdf()
        return False


# ---------------------------------------------------------------- utilitários

def _valor(texto: str) -> int | None:
    """'-1.234,56€' / '33.419' / '0,00 €' → cêntimos (ou unidades×100). None se não for número."""
    t = re.sub(r"[^\d,.\-]", "", texto or "")
    if not t or not re.search(r"\d", t):
        return None
    t = t.replace(".", "").replace(",", ".")
    try:
        return int((Decimal(t) * 100).quantize(Decimal("1")))
    except InvalidOperation:
        return None


def _data(texto: str) -> date | None:
    m = DATA_PT.match(texto.strip())
    if not m or m.group(2).lower()[:3] not in MESES:
        return None
    return date(int(m.group(3)), MESES[m.group(2).lower()[:3]], int(m.group(1)))


def _linhas(palavras: list[dict], tolerancia: float = 2.5) -> list[list[dict]]:
    """Agrupa as palavras de uma página em linhas (pelo topo), da esquerda para a direita."""
    out: list[list[dict]] = []
    for w in sorted(palavras, key=lambda w: (round(w["top"]), w["x0"])):
        if out and abs(out[-1][0]["top"] - w["top"]) <= tolerancia:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(l, key=lambda w: w["x0"]) for l in out]


def _texto(linha: list[dict]) -> str:
    return " ".join(w["text"] for w in linha)


def _coluna(x: float, col: dict) -> str:
    if x < col["descricao"] - 2:
        return "data"
    for nome in ("comissoes", "outros", "imposto", "saldo", "montante", "categoria", "descricao"):
        if x >= col[nome] - 2:
            return nome
    return "descricao"


def _colunas_do_cabecalho(linha: list[dict], col: dict) -> dict:
    novo = dict(col)
    for w in linha:
        t = w["text"].lower()
        if t.startswith("descri"):
            novo["descricao"] = w["x0"]
        elif t.startswith("categoria"):
            novo["categoria"] = w["x0"]
        elif t == "saldo":
            novo["saldo"] = w["x0"]
        elif t.startswith("comiss"):
            novo["comissoes"] = w["x0"] - 10
    return novo


# ---------------------------------------------------------------- interpretação

def interpretar(paginas: list[list[dict]]) -> Extrato:
    """paginas: lista de páginas, cada uma com as palavras ({text, x0, top}) — como o pdfplumber as dá."""
    modo, seccao, moeda = None, None, "EUR"
    col = dict(COLUNAS)
    linhas_mov: list[tuple[str, str, list[list[dict]], dict]] = []   # (seccao, moeda, linhas, colunas)
    juros: list[MovimentoBruto] = []
    atual: list[list[dict]] | None = None
    juros_pendente = None
    periodo = None
    saldos: dict = {}
    fiscal: list[tuple[str, str, int]] = []
    conta_resumo = None

    def fechar():
        nonlocal atual
        if atual and seccao:
            linhas_mov.append((seccao, moeda, atual, dict(col)))   # as colunas da tabela onde a operação está
        atual = None

    for palavras in paginas:
        for linha in _linhas(palavras):
            texto = _texto(linha)
            if periodo is None and (mp := PERIODO.match(texto)):
                periodo = (_data(mp.group(1)), _data(mp.group(2)))
            if texto.endswith(" Resumos"):          # «Contas-correntes Resumos», «Cripto Resumos», …
                fechar()
                nome_r = texto[:-len(" Resumos")].strip()
                modo, seccao = ("resumo_contas", None) if nome_r == "Contas-correntes" else ("resumo", nome_r)
                conta_resumo = None
                continue
            if modo == "resumo_contas" and "Extratos de opera" not in texto:
                m = SECCAO.match(texto)
                if m:
                    conta_resumo = ("Atual" if m.group(1) == "Conta Pessoal" else m.group(1), m.group(2))
                elif conta_resumo:
                    for tipo, val in SALDO_RESUMO.findall(texto):
                        saldos.setdefault(conta_resumo, {})[tipo] = _valor(val)
                continue
            if modo == "resumo" and not re.search(r"Extratos de opera|Transaction Statements", texto):
                nome_s = SECCOES_FISCAIS.get(seccao, seccao)
                for rot, val in PAR_FISCAL.findall(texto):
                    rot = rot.strip(" -/.")
                    v = _valor(val)
                    if not v or not ROTULO_FISCAL.search(rot) or ROTULO_IGNORAR.search(rot):
                        continue
                    rot = TRADUCOES.get(rot, rot)
                    if not any(s == nome_s and r == rot for s, r, _ in fiscal):
                        fiscal.append((nome_s, rot, v))
                continue
            if "Extratos de opera" in texto and "Contas-correntes" in texto:
                fechar()
                modo, seccao = "contas", None
                continue
            if "Transaction Statements" in texto and "Savings" in texto:
                fechar()
                modo, seccao = "juros", "Depósito"
                continue
            if re.search(r"(Cripto|Fundos|Investment|Bem|Ações|Commodit).*(Extrato|Resumo|statement)", texto) \
                    or texto.startswith("Transaction statement"):
                fechar()
                modo, seccao = None, None
                continue
            if modo is None:
                continue
            m = SECCAO.match(texto)
            if m and linha[0]["x0"] < 60:
                fechar()
                seccao = "Atual" if m.group(1) == "Conta Pessoal" else m.group(1)
                moeda = m.group(2)
                if modo == "juros":
                    seccao = "Depósito"
                continue
            if texto.startswith("Data ") and "Descri" in texto:
                if modo == "contas":   # a tabela de juros tem outro layout
                    col = _colunas_do_cabecalho(linha, col)
                continue
            if texto.startswith(TOTAIS) or texto.startswith(("Extrato personalizado", "Gerado a ")) \
                    or re.match(r"^\d{1,2} de [a-zç]{3}\. de \d{4} - ", texto):
                if texto.startswith(TOTAIS):
                    fechar()
                continue
            if modo == "juros":
                # «10/08 Pagamento de juros … 0,01€ 0,00€ 0,00€ 0,00 0,01€» e «/2026 …» na linha seguinte
                if DATA_JUROS.match(linha[0]["text"]) and linha[0]["x0"] < 60:
                    euros = [_valor(w["text"]) for w in linha if "€" in w["text"]]
                    juros_pendente = (linha[0]["text"], euros[-1] if euros else None)
                    continue
                if juros_pendente and re.match(r"^/\d{4}", linha[0]["text"]):
                    dm, liquido = juros_pendente
                    d, mes = (int(x) for x in dm.split("/"))
                    ano = int(linha[0]["text"][1:5])
                    if liquido:
                        juros.append(MovimentoBruto(date(ano, mes, d), None, "Juros Conta reforçada", liquido))
                    juros_pendente = None
                continue
            # contas-correntes: uma operação começa com a data na 1.ª coluna
            datas = [w for w in linha if _coluna(w["x0"], col) == "data"]
            if datas and _data(" ".join(w["text"] for w in datas)):
                fechar()
                atual = [linha]
            elif atual is not None:
                atual.append(linha)
    fechar()

    subcontas: OrderedDict = OrderedDict()
    for seccao_, moeda_, linhas, col_ in linhas_mov:
        mov = _operacao(linhas, moeda_, col_)
        if mov is None:
            continue
        nativo, euros = mov
        subcontas.setdefault((seccao_, moeda_), []).append(nativo)
        if seccao_ == "Atual":
            subcontas.setdefault(("Todas as moedas", "EUR"), []).append(euros)
    if juros:
        subcontas[("Depósito", "EUR")] = juros
    for chave, movs in subcontas.items():
        movs.sort(key=lambda m: m.data_mov)   # estável: mantém a ordem do extrato dentro do dia
    principal = subcontas.get(("Todas as moedas", "EUR")) or next(iter(subcontas.values()), [])
    ext = Extrato(parser=Parser.chave, banco=Parser.banco, movimentos=list(principal), subcontas=dict(subcontas))
    ext.dedup_dias = 4   # o PDF usa a data de conclusão; o CSV a de início (até ~3 dias antes)
    if periodo and all(periodo):
        ext.periodo = periodo
    ext.saldos_resumo = saldos or None
    ext.resumo_fiscal = fiscal
    ext.avisos += conferir_saldos(ext)
    return ext


def conferir_saldos(ext: Extrato) -> list[str]:
    """Os movimentos de cada conta batem com o saldo inicial e final das páginas de resumo?"""
    avisos = []
    for (prod, moeda), s in (ext.saldos_resumo or {}).items():
        movs = (ext.subcontas or {}).get((prod, moeda)) or []
        com_saldo = [m for m in movs if m.saldo is not None]
        if not com_saldo:
            if s.get("final"):
                avisos.append(f"{prod} {moeda}: o resumo indica saldo final {_fmt(s['final'])} {moeda}, mas não há movimentos.")
            continue
        inicial = com_saldo[0].saldo - com_saldo[0].montante
        if s.get("inicial") is not None and inicial != s["inicial"]:
            avisos.append(f"{prod} {moeda}: saldo inicial pelos movimentos {_fmt(inicial)} ≠ {_fmt(s['inicial'])} do resumo.")
        if s.get("final") is not None and com_saldo[-1].saldo != s["final"]:
            avisos.append(f"{prod} {moeda}: saldo final pelos movimentos {_fmt(com_saldo[-1].saldo)} ≠ "
                          f"{_fmt(s['final'])} do resumo.")
    return avisos


def _fmt(cent: int) -> str:
    return f"{cent / 100:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")


def _operacao(linhas: list[list[dict]], moeda: str, col: dict):
    """(movimento na moeda original, movimento em euros) de uma operação."""
    partes = {k: [] for k in ("data", "descricao", "categoria", "montante", "saldo", "imposto", "outros", "comissoes")}
    for linha in linhas:
        for w in linha:
            partes[_coluna(w["x0"], col)].append(w["text"])
    data = _data(" ".join(partes["data"][:5]))
    if not data:
        return None
    categoria = " ".join(partes["categoria"]).strip()
    descricao = " ".join(partes["descricao"]).strip() or f"{categoria or 'Movimento'} (sem descrição)"
    def primeiro(tokens, euro: bool):
        for t in tokens:
            if ("€" in t) == euro and _valor(t) is not None:
                return _valor(t)
        return None
    if moeda == "EUR":
        montante, saldo = primeiro(partes["montante"], True), primeiro(partes["saldo"], True)
        montante_eur, saldo_eur = montante, saldo
    else:
        montante, saldo = primeiro(partes["montante"], False), primeiro(partes["saldo"], False)
        montante_eur, saldo_eur = primeiro(partes["montante"], True), primeiro(partes["saldo"], True)
    if montante is None:
        return None
    transferencia = categoria.upper() in TRANSFERENCIA_CATEGORIAS or bool(DESCRICAO_PROPRIA.search(descricao))
    nativo = MovimentoBruto(data, None, descricao, montante, saldo, transferencia=transferencia)
    if moeda == "EUR":
        euros = MovimentoBruto(data, None, descricao, montante, saldo, transferencia=transferencia)
    else:
        original = f"{montante / 100:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".").rstrip("0").rstrip(",")
        euros = MovimentoBruto(data, None, f"{descricao} ({original} {moeda})",
                               montante_eur if montante_eur is not None else 0, None, transferencia=transferencia)
    return nativo, euros


def ler_pdf(conteudo: bytes) -> Extrato | None:
    """None se o PDF não for um extrato consolidado da Revolut."""
    import pdfplumber
    with pdfplumber.open(io.BytesIO(conteudo)) as pdf:
        primeira = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
        if "Extrato personalizado" not in primeira or "Revolut" not in primeira:
            return None
        paginas = [[{"text": w["text"], "x0": w["x0"], "top": w["top"]} for w in p.extract_words()] for p in pdf.pages]
    return interpretar(paginas)
