"""Lar (Santa Casa da Misericórdia): conciliação mensal das faturas da farmácia, das notas de lançamento, das cartas
de aviso, dos recibos e dos pagamentos — tudo lido do Paperless (texto/OCR dos documentos).

Como funciona o circuito (confirmado pelo utilizador e pelos documentos reais):
  1. A farmácia (Farmácia Nova de Santo André) emite uma FATURA por cada compra, «Debitado em Conta» na conta corrente
     da utente; o valor que conta é o «Total Utente» (o resto é comparticipado pelo Estado).
  2. A Santa Casa paga a farmácia e cobra à utente num LANÇAMENTO: uma NOTA DE LANÇAMENTO (NLD) «22 - Reembolso de
     Medicamentos» ou uma FATURA (FT) que não é a mensalidade — no mês da fatura da farmácia ou no seguinte, com o
     mesmo valor (ou repartido por vários lançamentos, ou vários faturas juntas num). Há NLD de outros reembolsos
     (26 Consultas, 90 Cabeleireiro), que não vêm da farmácia.
  3. A mensalidade vem numa FATURA da Santa Casa (FT: 39 Mensalidade ERPI, 41 Comp. descendentes…).
  4. A CARTA INFORMATIVA/AVISO lista os documentos (NLD e FT) por pagar e o total.
  5. O RECIBO (REC) diz que documentos foram pagos e quando; o pagamento sai da conta da utente (Crédito Agrícola).

A conciliação aponta: NLD de medicamentos que não batem com as faturas da farmácia, faturas da farmácia ainda não
cobradas (ou esquecidas), documentos repetidos em várias cartas ou com valor alterado, e recibos sem movimento no banco.
Regularização: «pago até» (declarado pelo utilizador) dispensa os alertas de «por pagar»; os documentos a partir de
«validar desde» ficam «por validar» até serem validados um a um (tabela lar_validacao) — os anteriores ficam
«regularizados». Só os documentos por validar dão alertas.
"""
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import combinations

from .formatos import fmt_montante

TOLERANCIA = 1          # cêntimos
COD_MEDICAMENTOS = "22"


# ------------------------------------------------------------------ utilitários
def _simples(t: str) -> str:
    return unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode()


def cent(txt: str) -> int:
    """«1.339,41» / «1 339,41» / «1339,41» / «2,64 €» → cêntimos."""
    t = re.sub(r"[^\d,]", "", txt or "")
    if "," not in t:
        return int(t or 0) * 100
    inteiro, dec = t.rsplit(",", 1)
    return int(inteiro or 0) * 100 + int((dec + "0")[:2])


_VALOR = r"\d{1,3}(?:[ .]\d{3})*,\d{2}|\d+,\d{2}"


def _data(txt: str) -> date | None:
    m = re.search(r"(\d{2})[-/](\d{2})[-/](\d{4})", txt or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


# «NLD NLDUT12026/221», «FT FTUT12025/2291», «FT FTUT1/2025 3780», «NLDUT12025 / 00000765»
_DOC = re.compile(r"\b(NLD|FT)\s+(?:NLDUT1|FTUT1)\s*/?\s*(20\d{2})\s*/?\s*0*(\d+)")


def chave(tipo: str, ano: int, numero: int) -> str:
    return f"{tipo} {ano}/{numero}"


# ------------------------------------------------------------------ documentos
@dataclass
class FaturaFarmacia:
    pid: int
    numero: str
    data: date
    total: int                      # cêntimos, a parte da utente
    debitado: bool = False
    itens: list = field(default_factory=list)   # [(produto, líquido)]


@dataclass
class DocSantaCasa:                  # NLD ou FT da Santa Casa
    pid: int
    tipo: str                       # NLD | FT
    ano: int
    numero: int
    data: date
    total: int
    linhas: list = field(default_factory=list)  # [(código, descrição, valor)]

    @property
    def chave(self) -> str:
        return chave(self.tipo, self.ano, self.numero)

    @property
    def medicamentos(self) -> int:
        return sum(v for c, _, v in self.linhas if c == COD_MEDICAMENTOS)

    @property
    def descricao(self) -> str:
        return ", ".join(sorted({d for _, d, _ in self.linhas})) or self.tipo


@dataclass
class Carta:
    pid: int
    data: date
    docs: list = field(default_factory=list)    # [(chave, tipo, data, valor, por_pagar)]
    total: int | None = None


@dataclass
class Recibo:
    pid: int
    numero: str
    data: date
    docs: list = field(default_factory=list)    # [(chave, valor)]
    total: int | None = None
    pagamento: str = ""


def ler_farmacia(pid: int, texto: str) -> FaturaFarmacia | None:
    t = texto or ""
    m = re.search(r"Fatura\s+n\.?\s*\S*\s*:?\s*(FT\s+[A-Z0-9]+/\d+)", t, re.I)
    if not m or not re.search(r"farm[aá]cia", t, re.I):
        return None
    d = _data(t[m.end():m.end() + 80]) or _data(t)
    tot = re.search(r"Total\s+Utente\s*\(Euros\)\s*:?\s*(" + _VALOR + ")", t, re.I) or \
        re.search(r"Total\s+a\s+pagar\s*(" + _VALOR + ")", t, re.I)
    if not d or not tot:
        return None
    itens = []
    for nome, liq in re.findall(r"^([^\n]+?)\n\d+%\s+\d+\s+(?:" + _VALOR + r")\s*€?.*?(" + _VALOR + r")\s*€?\s*$", t, re.M):
        itens.append((nome.strip(), cent(liq)))
    return FaturaFarmacia(pid, re.sub(r"\s+", " ", m.group(1)), d, cent(tot.group(1)),
                          bool(re.search(r"Debitado\s+em\s+Conta", t, re.I)), itens)


def ler_santa_casa(pid: int, texto: str):
    """DocSantaCasa (NLD/FT), Carta, Recibo ou None (anulados, outros)."""
    t = texto or ""
    s = _simples(t)
    if "DOC. ANULADO" in t.upper():
        return None
    if re.search(r"Carta\s+Informativa", s, re.I):
        c = Carta(pid, _data(re.search(r"Data:?\s*(\d{2}-\d{2}-\d{4})", t).group(1)) if re.search(r"Data:?\s*\d{2}-\d{2}-\d{4}", t) else None)
        for m in re.finditer(r"(\d{2}-\d{2}-\d{4})\s+(NLD|FT)\s+\S+?\s*/\s*0*(\d+)\s+(" + _VALOR + r")\s*€?\s+(" + _VALOR + r")", t):
            ano = int(m.group(1)[-4:])
            serie = re.search(r"(?:NLDUT1|FTUT1)(20\d{2})", t[m.start():m.end()])
            ano = int(serie.group(1)) if serie else ano
            c.docs.append((chave(m.group(2), ano, int(m.group(3))), m.group(2), _data(m.group(1)), cent(m.group(4)), cent(m.group(5))))
        tot = re.search(r"Total\s+por\s+Pagar\s*:?\s*(" + _VALOR + ")", s, re.I)
        c.total = cent(tot.group(1)) if tot else None
        return c if c.data else None
    if re.search(r"Recebimento\s+n", s, re.I):
        num = re.search(r"REC\s+(\S+)", t)
        r = Recibo(pid, num.group(1) if num else "", _data(re.search(r"Data\s+(\d{2}-\d{2}-\d{4})", t).group(1))
                   if re.search(r"Data\s+\d{2}-\d{2}-\d{4}", t) else None)
        for m in re.finditer(r"^(NLD|FT)\s+(?:NLDUT1|FTUT1)\s*/?\s*(20\d{2})\s*/?\s*0*(\d+)\s+[CD]\s+(" + _VALOR + ")", t, re.M):
            r.docs.append((chave(m.group(1), int(m.group(2)), int(m.group(3))), cent(m.group(4))))
        tot = re.search(r"TOTAL\s+(\d[\d .]*,\d{2})", t)
        r.total = cent(tot.group(1)) if tot else (sum(v for _, v in r.docs) or None)
        pag = re.search(r"Tipo\s+Pagamento:\s*([^\n]+)", s)
        r.pagamento = pag.group(1).strip() if pag else ""
        return r if r.data else None
    m = _DOC.search(t)
    if not m or not re.search(r"Nota\s+Lan|Factura\s+n|Fatura\s+n", s, re.I):
        return None
    doc = DocSantaCasa(pid, m.group(1), int(m.group(2)), int(m.group(3)),
                       _data(re.search(r"Data\s+(\d{2}-\d{2}-\d{4})", t).group(1)) if re.search(r"Data\s+\d{2}-\d{2}-\d{4}", t) else None, 0)
    vistos = set()
    for cod, desc, valor in re.findall(r"^(\d{1,3})\s+-\s+(.+?)\s+(?:\d+[,.]\d{2}\s+)?(?:\d+\s+)?(" + _VALOR + r")(?:\s+[\d.,]+)*\s*$", t, re.M):
        k = (cod, desc.strip(), valor)
        if k in vistos:                 # o PDF repete às vezes a mesma linha
            continue
        vistos.add(k)
        doc.linhas.append((cod, re.sub(r"\s+\d+$", "", desc.strip()), cent(valor)))
    tot = re.search(r"(?:SUB TOTAL\s+[\d.,]+\s+IVA\s+[\d.,]+\s+)?\bTOTAL\s*:?\s*(" + _VALOR + ")", t, re.I)
    doc.total = cent(tot.group(1)) if tot else sum(v for _, _, v in doc.linhas)
    return doc if doc.data else None


# ------------------------------------------------------------------ ligação: faturas da farmácia ↔ lançamentos da Santa Casa
CODIGOS_FARMACIA = {COD_MEDICAMENTOS}       # NLD «22 - Reembolso de Medicamentos»
CODIGOS_MENSALIDADE = {"39", "41"}          # FT «39 Mensalidade ERPI», «41 Comp. Descend. Lar»
MENSALIDADE_MINIMA = 30000                  # FT sem PDF (só pela carta) a partir deste valor: é mensalidade
DIAS_ANTES = 3                              # um lançamento pode ter data até 3 dias antes da fatura (datas de emissão)
MAX_COMBINACAO = 4


@dataclass
class Ligacao:
    """Faturas da farmácia ↔ lançamentos da Santa Casa (NLD ou FT) que as cobram."""
    faturas: list                   # [FaturaFarmacia]
    lancamentos: list               # [EstadoDoc]
    metodo: str                     # mesmo valor | soma de lançamentos | soma de faturas | sem lançamento | sem fatura

    @property
    def soma_faturas(self) -> int:
        return sum(f.total for f in self.faturas)

    @property
    def soma_lancamentos(self) -> int:
        return sum(e.valor for e in self.lancamentos)

    @property
    def diferenca(self) -> int:
        return self.soma_lancamentos - self.soma_faturas

    @property
    def ok(self) -> bool:
        return bool(self.faturas and self.lancamentos) and abs(self.diferenca) <= TOLERANCIA

    @property
    def data(self) -> date:
        return min([f.data for f in self.faturas] + [e.data for e in self.lancamentos if e.data])

    @property
    def prazo(self) -> date | None:
        """Até quando a Santa Casa pode lançar a fatura da farmácia (fim do mês seguinte)."""
        return _fim_mes_seguinte(min(f.data for f in self.faturas)) if self.faturas else None


def _fim_mes_seguinte(d: date) -> date:
    seguinte = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (seguinte.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def lancamento_farmacia(e) -> bool:
    """Um lançamento da Santa Casa que pode cobrar a farmácia: NLD de medicamentos ou FT que não é a mensalidade.
    Sem o PDF (só pela carta) não se sabe o código: NLD conta sempre; FT conta se for abaixo de 300 €."""
    if e.tipo == "NLD":
        return not e.codigos or bool(e.codigos & CODIGOS_FARMACIA)
    if e.tipo == "FT":
        if e.codigos:
            return not (e.codigos & CODIGOS_MENSALIDADE)
        return 0 < e.valor < MENSALIDADE_MINIMA
    return False


def _melhor(candidatos: list, alvo: int, ref: date, maximo: int):
    """A combinação (1..maximo elementos) que soma `alvo`, com o menor número de elementos e, nesse número, a mais
    próxima de `ref` no tempo. Os candidatos são (objeto, valor, data)."""
    for k in range(1, maximo + 1):
        melhor, dist = None, None
        for c in combinations(candidatos, k):
            if abs(sum(v for _, v, _ in c) - alvo) <= TOLERANCIA:
                d = sum(abs((dd - ref).days) for _, _, dd in c)
                if dist is None or d < dist:
                    melhor, dist = [o for o, _, _ in c], d
        if melhor:
            return melhor
    return None


def ligar_farmacia(faturas: list[FaturaFarmacia], estados_docs: list) -> tuple[list[Ligacao], int]:
    """Parte de cada fatura da farmácia e procura, pelo valor, os lançamentos da Santa Casa (NLD ou FT, também os que
    só se conhecem pela carta) desde 3 dias antes da fatura até ao fim do mês seguinte: o lançamento com o mesmo valor
    ou a combinação de até 4 lançamentos que soma o valor (os mais próximos primeiro). Depois, para os lançamentos que
    sobram, a combinação de faturas que dá o seu valor (uma NLD por quinzena). Devolve (ligações, n.º de lançamentos
    de antes da 1.ª fatura no Paperless — não há com que comparar)."""
    livres_f = sorted([f for f in faturas if f.total > 0], key=lambda f: (f.data, f.numero))
    inicio = min((f.data for f in livres_f), default=None)
    lanc = [e for e in estados_docs if e.data and lancamento_farmacia(e)]
    antigas = [e for e in lanc if inicio is None or e.data < inicio - timedelta(days=DIAS_ANTES)]
    lanc = [e for e in lanc if e not in antigas]
    usados_l: set[str] = set()
    usadas_f: set[str] = set()
    ligacoes = []
    for f in livres_f:                                       # 1) fatura → lançamento(s) pelo valor
        cand = [e for e in lanc if e.chave not in usados_l
                and f.data - timedelta(days=DIAS_ANTES) <= e.data <= _fim_mes_seguinte(f.data)]
        cand = sorted(cand, key=lambda e: abs((e.data - f.data).days))[:12]
        achou = _melhor([(e, e.valor, e.data) for e in cand], f.total, f.data, MAX_COMBINACAO)
        if achou:
            usados_l.update(e.chave for e in achou)
            usadas_f.add(f.numero)
            ligacoes.append(Ligacao([f], sorted(achou, key=lambda e: e.data), "mesmo valor" if len(achou) == 1 else "soma de lançamentos"))
    for e in sorted(lanc, key=lambda e: e.data):            # 2) lançamento que junta várias faturas
        if e.chave in usados_l:
            continue
        cand = [f for f in livres_f if f.numero not in usadas_f and e.data - timedelta(days=62) <= f.data <= e.data + timedelta(days=DIAS_ANTES)]
        achou = _melhor([(f, f.total, f.data) for f in cand[-14:]], e.valor, e.data, 6)
        if achou and len(achou) > 1:
            usados_l.add(e.chave)
            usadas_f.update(f.numero for f in achou)
            ligacoes.append(Ligacao(sorted(achou, key=lambda f: f.data), [e], "soma de faturas"))
    for f in livres_f:
        if f.numero not in usadas_f:
            ligacoes.append(Ligacao([f], [], "sem lançamento"))
    for e in lanc:
        if e.chave not in usados_l:
            ligacoes.append(Ligacao([], [e], "sem fatura"))
    return sorted(ligacoes, key=lambda l: l.data, reverse=True), len(antigas)


# ------------------------------------------------------------------ cartas, recibos e pagamentos
@dataclass
class EstadoDoc:
    chave: str
    tipo: str
    data: date | None
    valores: list                   # valores vistos (cartas e documento)
    cartas: list                    # datas das cartas em que aparece
    recibo: Recibo | None = None
    descricao: str = ""
    pid: int | None = None
    validacao: str = ""             # regularizado | validado | por validar
    validado: dict | None = None    # {em, por, nota} quando validado
    codigos: set = field(default_factory=set)   # códigos das linhas (só com o PDF): 22 medicamentos, 39 mensalidade…

    def estado(self, ultima_carta: date | None, pago_ate: date | None = None) -> str:
        if self.recibo:
            return "pago"
        if pago_ate and self.data and self.data <= pago_ate:
            return "pago (declarado)"
        if ultima_carta and self.cartas and self.cartas[-1] == ultima_carta:
            return "por pagar"
        if self.cartas:
            return "saiu das cartas"
        return "sem carta"

    @property
    def valor(self) -> int:
        return self.valores[-1] if self.valores else 0

    @property
    def tem_pdf(self) -> bool:
        """O próprio documento (NLD/FT) está no Paperless — senão só se sabe dele pelas cartas ou pelos recibos."""
        return self.pid is not None

    @property
    def mes(self) -> date | None:
        return self.data.replace(day=1) if self.data else None

    def falta(self, pago_ate: date | None = None) -> list[str]:
        """Porque é que este documento não «casa» por completo: o que falta no Paperless ou o que não bate."""
        out = []
        if not self.tem_pdf:
            out.append("PDF em falta")
        if not self.cartas and not self.recibo:
            out.append("sem carta nem recibo")
        if not self.recibo:
            out.append(f"recibo em falta (pago até {pago_ate:%d/%m})" if pago_ate and self.data and self.data <= pago_ate
                       else "recibo em falta")
        if len(self.valores) > 1:
            out.append("valor mudou: " + " → ".join(fmt_montante(v) for v in self.valores))
        return out


def estados(docs: list[DocSantaCasa], cartas: list[Carta], recibos: list[Recibo]) -> tuple[list[EstadoDoc], date | None]:
    por = {}
    for d in docs:
        por[d.chave] = EstadoDoc(d.chave, d.tipo, d.data, [d.total], [], descricao=d.descricao, pid=d.pid,
                                 codigos={c for c, _, _ in d.linhas})
    for c in sorted(cartas, key=lambda c: c.data):
        for k, tipo, dd, valor, _ in c.docs:
            e = por.setdefault(k, EstadoDoc(k, tipo, dd, [], []))
            if not e.valores or e.valores[-1] != valor:
                e.valores.append(valor)
            if c.data not in e.cartas:
                e.cartas.append(c.data)
    for r in sorted(recibos, key=lambda r: r.data):
        for k, valor in r.docs:
            e = por.setdefault(k, EstadoDoc(k, k.split()[0], r.data, [valor], []))
            e.recibo = r
    ultima = max((c.data for c in cartas), default=None)
    return sorted(por.values(), key=lambda e: (e.data or date.min, e.chave), reverse=True), ultima


def ligar_pagamentos(recibos: list[Recibo], movimentos: list) -> list[tuple[Recibo, object | None]]:
    """Cada recibo ↔ o débito de valor igual na conta da utente entre 10 dias antes e 3 dias depois do recibo."""
    livres = list(movimentos)
    out = []
    for r in sorted(recibos, key=lambda r: r.data, reverse=True):
        m = next((m for m in livres if r.total and m.montante == -r.total
                  and r.data - timedelta(days=10) <= m.data_mov <= r.data + timedelta(days=3)), None)
        if m:
            livres.remove(m)
        out.append((r, m))
    return out


# ------------------------------------------------------------------ tudo junto
@dataclass
class Conciliacao:
    farmacia: list                  # [Ligacao] faturas da farmácia ↔ lançamentos da Santa Casa
    n_antigas: int                  # lançamentos de antes da 1.ª fatura da farmácia no Paperless
    estados: list
    ultima_carta: date | None
    pagamentos: list
    cartas: list
    alertas: list
    n_faturas: int
    n_docs: int
    pago_ate: date | None = None
    validar_desde: date | None = None
    n_por_validar: int = 0
    meses: list = field(default_factory=list)     # [{mes, docs, total, por_validar}] do intervalo em análise
    n_fora: int = 0                                # documentos de antes do intervalo (escondidos)
    nao_reconhecidos: list = field(default_factory=list)   # [(pid, início do texto)] da Santa Casa que não se leram
    n_anulados: int = 0


def conciliar(farmacia_docs: list[tuple[int, str]], santa_casa_docs: list[tuple[int, str]], movimentos: list,
              hoje: date | None = None, pago_ate: date | None = None, validar_desde: date | None = None,
              validados: dict | None = None) -> Conciliacao:
    """`pago_ate`: tudo até essa data está pago (declarado pelo utilizador) — não há alertas de «por pagar».
    `validar_desde`: os documentos anteriores ficam «regularizados»; os seguintes ficam «por validar» até o utilizador
    os validar um a um (`validados`: chave → {em, por, nota}); documentos regularizados ou validados não dão alertas."""
    hoje = hoje or date.today()
    validados = validados or {}

    def situacao(chave_doc: str, d: date | None) -> str:
        if chave_doc in validados:
            return "validado"
        if validar_desde is None:
            return ""                    # sem regularização configurada: tudo como antes, com alertas
        if d and d < validar_desde:
            return "regularizado"
        return "por validar"
    faturas = [f for pid, t in farmacia_docs if (f := ler_farmacia(pid, t))]
    vistos_f = {}
    for f in faturas:                                 # o mesmo documento importado duas vezes
        vistos_f.setdefault(f.numero, f)
    faturas = list(vistos_f.values())
    docs, cartas, recibos, nao_reconhecidos, n_anulados = [], [], [], [], 0
    for pid, t in santa_casa_docs:
        x = ler_santa_casa(pid, t)
        if isinstance(x, DocSantaCasa):
            docs.append(x)
        elif isinstance(x, Carta):
            cartas.append(x)
        elif isinstance(x, Recibo):
            recibos.append(x)
        elif "DOC. ANULADO" in (t or "").upper():
            n_anulados += 1
        else:                                   # nem NLD, nem fatura, nem carta, nem recibo: ver no Paperless porquê
            linhas = [l.strip() for l in (t or "").splitlines() if l.strip()]
            nao_reconhecidos.append((pid, " · ".join(linhas[:3])[:160] or "(sem texto: o OCR falhou?)"))
    docs = list({d.chave: d for d in docs}.values())
    cartas = list({(c.data, tuple(k for k, *_ in c.docs)): c for c in cartas}.values())
    recibos = list({(r.numero or r.pid): r for r in recibos}.values())
    est, ultima = estados(docs, cartas, recibos)
    pagamentos = ligar_pagamentos(recibos, movimentos)
    for e in est:
        e.validacao = situacao(e.chave, e.data)
        e.validado = validados.get(e.chave)
    ligacoes, n_antigas = ligar_farmacia(faturas, est)

    alertas = []
    for l in ligacoes:
        if l.metodo == "sem lançamento":
            f = l.faturas[0]
            # só depois de passar o prazo em que a Santa Casa a podia lançar (fim do mês seguinte)
            if (validar_desde is None or f.data >= validar_desde) and _fim_mes_seguinte(f.data) < hoje:
                alertas.append(("farmacia", f"Fatura da farmácia {f.numero} de {f.data:%d/%m/%Y} ({fmt_montante(f.total)}) "
                                            "sem lançamento da Santa Casa com esse valor"))
        elif l.metodo == "sem fatura":
            e = l.lancamentos[0]
            if e.validacao not in ("regularizado", "validado"):
                alertas.append(("farmacia", f"{e.chave} de {e.data:%d/%m/%Y} ({fmt_montante(e.valor)}) sem fatura da farmácia "
                                            "com esse valor no Paperless"))
    for e in est:
        if e.validacao in ("regularizado", "validado"):
            continue
        if len(e.valores) > 1:
            alertas.append(("carta", f"{e.chave}: o valor mudou entre cartas ({' → '.join(fmt_montante(v) for v in e.valores)})"))
        if e.estado(ultima, pago_ate) == "por pagar" and e.data and (hoje - e.data).days > 45:
            alertas.append(("carta", f"{e.chave} de {e.data:%d/%m/%Y} ({fmt_montante(e.valor)}) continua por pagar há {(hoje - e.data).days} dias"))
    for c in cartas:
        if validar_desde and c.data < validar_desde:
            continue
        soma = sum(p for *_, p in c.docs)
        if c.total is not None and abs(soma - c.total) > TOLERANCIA:
            alertas.append(("carta", f"Carta de {c.data:%d/%m/%Y}: as linhas somam {fmt_montante(soma)}, o total diz {fmt_montante(c.total)}"))
    if movimentos:
        for r, m in pagamentos:
            regularizado = (validar_desde is not None and r.data < validar_desde) or \
                (bool(r.docs) and all(k in validados for k, _ in r.docs))
            if m is None and not regularizado and r.data >= hoje - timedelta(days=400):
                alertas.append(("pagamento", f"Recibo {r.numero} de {r.data:%d/%m/%Y} ({fmt_montante(r.total or 0)}) sem débito igual na conta"))
    # só o intervalo em análise (a partir de «validar desde»): o resto está regularizado e fica escondido
    def no_intervalo(d: date | None) -> bool:
        return validar_desde is None or (d is not None and d >= validar_desde)
    dentro = [e for e in est if no_intervalo(e.data)]
    meses = {}
    for e in dentro:
        m = meses.setdefault(e.mes, {"mes": e.mes, "docs": [], "total": 0, "por_validar": 0})
        m["docs"].append(e)
        m["total"] += e.valor
        m["por_validar"] += e.validacao == "por validar"
    return Conciliacao([l for l in ligacoes if no_intervalo(l.data)], n_antigas, dentro, ultima,
                       [(r, m) for r, m in pagamentos if no_intervalo(r.data)],
                       sorted([c for c in cartas if no_intervalo(c.data)], key=lambda c: c.data, reverse=True),
                       alertas, len(faturas), len(docs) + len(cartas) + len(recibos), pago_ate, validar_desde,
                       sum(1 for e in dentro if e.validacao == "por validar"),
                       sorted(meses.values(), key=lambda m: m["mes"] or date.min, reverse=True), len(est) - len(dentro),
                       nao_reconhecidos, n_anulados)
