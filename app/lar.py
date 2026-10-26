"""Lar (Santa Casa da Misericórdia): conciliação mensal das faturas da farmácia, das notas de lançamento, das cartas
de aviso, dos recibos e dos pagamentos — tudo lido do Paperless (texto/OCR dos documentos).

Como funciona o circuito (confirmado pelo utilizador e pelos documentos reais):
  1. A farmácia (Farmácia Nova de Santo André) emite uma FATURA por cada compra, «Debitado em Conta» na conta corrente
     da utente; o valor que conta é o «Total Utente» (o resto é comparticipado pelo Estado).
  2. A Santa Casa paga a farmácia e cobra à utente numa NOTA DE LANÇAMENTO (NLD) «22 - Reembolso de Medicamentos»,
     duas vezes por mês (dia 15 e fim do mês) — em princípio a soma das faturas dessa quinzena. Há NLD de outros
     reembolsos (26 Consultas, 90 Cabeleireiro), que não vêm da farmácia.
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
JANELA_FARMACIA = 62    # dias antes da NLD em que se procuram faturas da farmácia
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


# ------------------------------------------------------------------ conciliação: farmácia ↔ NLD de medicamentos
@dataclass
class LinhaMedicamentos:
    nld: DocSantaCasa
    faturas: list
    metodo: str                     # quinzena | quinzena anterior | combinação | sem correspondência | sem faturas no Paperless
    diferenca: int
    validacao: str = ""             # regularizado | validado | por validar (ver conciliar)

    @property
    def soma(self) -> int:
        return sum(f.total for f in self.faturas)

    @property
    def ok(self) -> bool:
        return abs(self.diferenca) <= TOLERANCIA


def _quinzena(d: date, anterior: bool = False) -> tuple[date, date]:
    """A quinzena que acaba em `d` (1–15 ou 16–fim do mês) ou a anterior."""
    ini = d.replace(day=1) if d.day <= 15 else d.replace(day=16)
    fim = d.replace(day=15) if d.day <= 15 else (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    if anterior:
        fim = ini - timedelta(days=1)
        ini = fim.replace(day=1) if fim.day <= 15 else fim.replace(day=16)
    return ini, fim


def conciliar_medicamentos(faturas: list[FaturaFarmacia], nlds: list[DocSantaCasa]) -> tuple[list[LinhaMedicamentos], list[FaturaFarmacia]]:
    """Para cada NLD de medicamentos (por data): as faturas da quinzena; se não somam o valor, a quinzena anterior;
    senão a combinação de faturas ainda livres (até 62 dias antes) que soma exatamente o valor. Devolve
    (linhas, faturas que nenhuma NLD cobrou)."""
    livres = sorted([f for f in faturas if f.total > 0], key=lambda f: (f.data, f.numero))
    usadas: set[str] = set()
    linhas = []
    # antes da 1.ª fatura da farmácia no Paperless não há com que comparar (não é erro)
    inicio = _quinzena(min(f.data for f in faturas))[0] if faturas else None
    for n in sorted([n for n in nlds if n.tipo == "NLD" and n.medicamentos], key=lambda n: (n.data, n.numero)):
        alvo = n.medicamentos
        if inicio is None or n.data < inicio:
            linhas.append(LinhaMedicamentos(n, [], "sem faturas no Paperless", 0))
            continue
        cand = [f for f in livres if f.numero not in usadas and n.data - timedelta(days=JANELA_FARMACIA) < f.data <= n.data]
        escolha, metodo = None, "sem correspondência"
        for nome, anterior in (("quinzena", False), ("quinzena anterior", True)):
            ini, fim = _quinzena(n.data, anterior)
            grupo = [f for f in cand if ini <= f.data <= fim]
            if grupo and abs(sum(f.total for f in grupo) - alvo) <= TOLERANCIA:
                escolha, metodo = grupo, nome
                break
        if escolha is None:
            recentes = cand[-16:]                        # limite para a procura de combinações
            for k in range(1, len(recentes) + 1):
                achou = next((list(c) for c in combinations(recentes, k) if abs(sum(f.total for f in c) - alvo) <= TOLERANCIA), None)
                if achou:
                    escolha, metodo = achou, "combinação"
                    break
        if escolha is None:                              # mostra a quinzena para se ver a diferença
            ini, fim = _quinzena(n.data)
            escolha = [f for f in cand if ini <= f.data <= fim]
            linhas.append(LinhaMedicamentos(n, escolha, metodo, alvo - sum(f.total for f in escolha)))
            continue
        usadas.update(f.numero for f in escolha)
        linhas.append(LinhaMedicamentos(n, escolha, metodo, alvo - sum(f.total for f in escolha)))
    por_cobrar = [f for f in livres if f.numero not in usadas]
    return linhas, por_cobrar


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
        por[d.chave] = EstadoDoc(d.chave, d.tipo, d.data, [d.total], [], descricao=d.descricao, pid=d.pid)
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
    medicamentos: list
    por_cobrar: list
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
    docs, cartas, recibos = [], [], []
    for pid, t in santa_casa_docs:
        x = ler_santa_casa(pid, t)
        if isinstance(x, DocSantaCasa):
            docs.append(x)
        elif isinstance(x, Carta):
            cartas.append(x)
        elif isinstance(x, Recibo):
            recibos.append(x)
    docs = list({d.chave: d for d in docs}.values())
    cartas = list({(c.data, tuple(k for k, *_ in c.docs)): c for c in cartas}.values())
    recibos = list({(r.numero or r.pid): r for r in recibos}.values())
    linhas, por_cobrar = conciliar_medicamentos(faturas, docs)
    est, ultima = estados(docs, cartas, recibos)
    pagamentos = ligar_pagamentos(recibos, movimentos)
    for e in est:
        e.validacao = situacao(e.chave, e.data)
        e.validado = validados.get(e.chave)
    for l in linhas:
        l.validacao = situacao(l.nld.chave, l.nld.data)

    alertas = []
    for l in linhas:
        if not l.ok and l.validacao not in ("regularizado", "validado"):
            alertas.append(("medicamentos", f"NLD {l.nld.numero} de {l.nld.data:%d/%m/%Y}: {fmt_montante(l.nld.medicamentos)} cobrados, "
                                            f"faturas da farmácia somam {fmt_montante(l.soma)} (diferença {fmt_montante(l.diferenca)})"))
    ultima_nld = max((l.nld.data for l in linhas if l.metodo != "sem faturas no Paperless"), default=None)
    for f in por_cobrar:
        if validar_desde and f.data < validar_desde:
            continue
        if ultima_nld and f.data <= ultima_nld - timedelta(days=20):
            alertas.append(("farmacia", f"Fatura da farmácia {f.numero} de {f.data:%d/%m/%Y} ({fmt_montante(f.total)}) não aparece em nenhuma NLD"))
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
    return Conciliacao([l for l in linhas if no_intervalo(l.nld.data) or l.metodo == "sem faturas no Paperless"],
                       [f for f in por_cobrar if no_intervalo(f.data)], dentro, ultima,
                       [(r, m) for r, m in pagamentos if no_intervalo(r.data)],
                       sorted([c for c in cartas if no_intervalo(c.data)], key=lambda c: c.data, reverse=True),
                       alertas, len(faturas), len(docs) + len(cartas) + len(recibos), pago_ate, validar_desde,
                       sum(1 for e in dentro if e.validacao == "por validar"),
                       sorted(meses.values(), key=lambda m: m["mes"] or date.min, reverse=True), len(est) - len(dentro))
