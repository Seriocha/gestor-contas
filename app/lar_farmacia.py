"""Gestão do Lar › Farmácia Nova: a farmácia passa a ser paga diretamente (já não pela Santa Casa).

Tudo lido do Paperless (correspondente da farmácia) e da conta da herança:
  - FATURA (FT): uma por compra; conta o «Total Utente» (o resto é comparticipado). Fica em conta corrente até ser paga.
  - FATURA-RECIBO (FR): compra paga logo no balcão — nasce paga.
  - RECIBO (RC/RE/REC…): diz que faturas foram pagas (n.º e valor) e o total recebido.
  - PAGAMENTO: o débito na conta da herança (transferência, multibanco…). Pode pagar uma fatura ou juntar várias.

A ligação faz-se em três passos:
  1. recibo → faturas: pelos números que o recibo lista;
  2. recibo → débito no banco: mesmo valor, de 10 dias antes a 5 dias depois do recibo (o mais próximo; os débitos
     com o texto da farmácia na descrição primeiro);
  3. faturas ainda sem recibo → débitos da farmácia que sobram: a fatura com o mesmo valor ou as faturas (até 6) que,
     somadas, dão o valor do débito (as mais próximas primeiro) — «pago no banco, recibo em falta».
O resto fica à vista: faturas por pagar, recibos sem débito no banco, débitos à farmácia sem fatura nem recibo, recibos
que citam faturas que não estão no Paperless ou com valores que não batem.

O formato do recibo da farmácia ainda não foi visto num documento real: a leitura é tolerante (n.º, data, total e as
linhas «FT …» com o valor) e afina-se quando chegar o primeiro.
"""
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .formatos import fmt_montante
from .lar import TOLERANCIA, _VALOR, _data, _melhor, _simples, cent, ler_farmacia

DIAS_RECIBO_ANTES = 10        # o débito pode ser até 10 dias antes do recibo (o recibo emite-se depois de receber)
DIAS_RECIBO_DEPOIS = 5
DIAS_FATURA_ANTES = 75        # um pagamento sem recibo junta faturas até 75 dias antes
MAX_FATURAS = 6
PRAZO_PAGAMENTO = 30          # dias até uma fatura em aberto dar alerta
PRAZO_RECIBO = 15             # dias até um pagamento sem recibo dar alerta
TEXTO_BANCO = "farmacia"      # o que identifica, na descrição do débito, um pagamento à farmácia

_NUM_FT = r"F[TR]\s+[A-Z0-9]+/\d+"


def _norm_num(n: str) -> str:
    return re.sub(r"\s+", " ", (n or "").strip().upper())


@dataclass
class Fatura:
    pid: int
    numero: str                     # «FT U014/49113» ou «FR …»
    data: date
    total: int                      # cêntimos (Total Utente)
    itens: list = field(default_factory=list)
    no_ato: bool = False            # fatura-recibo: paga ao balcão
    recibo: "ReciboFarmacia | None" = None
    pagamento: "Pagamento | None" = None

    @property
    def mes(self) -> date:
        return self.data.replace(day=1)

    def estado(self) -> str:
        if self.no_ato:
            return "pago no ato"
        if self.recibo and self.recibo.movimento is not None:
            return "pago"
        if self.recibo:
            return "recibo sem débito"
        if self.pagamento:
            return "recibo em falta"
        return "por pagar"

    @property
    def paga(self) -> bool:
        return self.estado() != "por pagar"


@dataclass
class ReciboFarmacia:
    pid: int
    numero: str
    data: date
    linhas: list = field(default_factory=list)   # [(n.º da fatura, valor ou None)]
    total: int | None = None
    meio: str = ""                                # transferência, multibanco… (quando o recibo o diz)
    movimento: object | None = None

    @property
    def soma_linhas(self) -> int | None:
        if not self.linhas or any(v is None for _, v in self.linhas):
            return None
        return sum(v for _, v in self.linhas)


@dataclass
class Pagamento:
    """Uma linha da tabela «Pagamentos»: o que liga recibo, débito no banco e faturas."""
    movimento: object | None
    recibo: ReciboFarmacia | None
    faturas: list                                 # [Fatura]
    metodo: str                                   # recibo | mesmo valor | soma de faturas | sem recibo nem fatura

    @property
    def data(self) -> date:
        datas = [self.movimento.data_mov] if self.movimento is not None else []
        if self.recibo:
            datas.append(self.recibo.data)
        return min(datas or [f.data for f in self.faturas] or [date.min])

    @property
    def valor(self) -> int:
        if self.movimento is not None:
            return -self.movimento.montante
        if self.recibo and self.recibo.total is not None:
            return self.recibo.total
        return sum(f.total for f in self.faturas)

    @property
    def soma_faturas(self) -> int:
        return sum(f.total for f in self.faturas)

    @property
    def diferenca(self) -> int:
        return self.valor - self.soma_faturas

    def problemas(self) -> list[str]:
        out = []
        if self.recibo is None:
            out.append("recibo em falta" if self.faturas else "sem recibo nem fatura")
        if self.movimento is None:
            out.append("sem débito no banco")
        if self.recibo:
            em_falta = [n for n, _ in self.recibo.linhas if n not in {f.numero for f in self.faturas}]
            if em_falta:
                out.append("faturas que não estão no Paperless: " + ", ".join(em_falta))
            for n, v in self.recibo.linhas:
                f = next((f for f in self.faturas if f.numero == n), None)
                if f and v is not None and abs(v - f.total) > TOLERANCIA:
                    out.append(f"{n}: o recibo diz {fmt_montante(v)}, a fatura {fmt_montante(f.total)}")
            soma = self.recibo.soma_linhas
            if soma is not None and self.recibo.total is not None and abs(soma - self.recibo.total) > TOLERANCIA:
                out.append(f"as linhas do recibo somam {fmt_montante(soma)}, o total diz {fmt_montante(self.recibo.total)}")
        if self.faturas and not out and abs(self.diferenca) > TOLERANCIA:
            out.append(f"diferença de {fmt_montante(self.diferenca)} para as faturas")
        return out

    @property
    def ok(self) -> bool:
        return not self.problemas()


# ------------------------------------------------------------------ leitura
def ler_fatura(pid: int, texto: str) -> Fatura | None:
    """Fatura (FT, a crédito) ou fatura-recibo (FR, paga ao balcão) da farmácia."""
    f = ler_farmacia(pid, texto)
    if f:
        return Fatura(pid, _norm_num(f.numero), f.data, f.total, f.itens)
    t = texto or ""
    m = re.search(r"Fatura[\s-]*Recibo\s+n\.?\s*\S*\s*:?\s*(FR\s+[A-Z0-9]+/\d+)", t, re.I)
    if not m or not re.search(r"farm[aá]cia", t, re.I):
        return None
    d = _data(t[m.end():m.end() + 80]) or _data(t)
    tot = re.search(r"Total\s+Utente\s*\(Euros\)\s*:?\s*(" + _VALOR + ")", t, re.I) or \
        re.search(r"Total\s+a\s+pagar\s*(" + _VALOR + ")", t, re.I)
    if not d or not tot:
        return None
    return Fatura(pid, _norm_num(m.group(1)), d, cent(tot.group(1)), no_ato=True)


def ler_recibo(pid: int, texto: str) -> ReciboFarmacia | None:
    """Recibo da farmácia: «Recibo n.º: RC U014/123», a data, as faturas pagas («FT U014/49113 … 2,64») e o total."""
    t = texto or ""
    if not re.search(r"farm[aá]cia", t, re.I) or re.search(r"Fatura[\s-]*Recibo", t, re.I):
        return None
    m = re.search(r"Recibo\s+n\.?\s*\S*\s*:?\s*([A-Z]{1,4}\s+[A-Z0-9]+/\d+)", t, re.I)
    if not m:
        return None
    d = _data(t[m.end():m.end() + 80]) or _data(t)
    if not d:
        return None
    r = ReciboFarmacia(pid, _norm_num(m.group(1)), d)
    vistos = set()
    for linha in t.splitlines():
        for ref in re.finditer(_NUM_FT, linha):
            n = _norm_num(ref.group(0))
            if n in vistos:
                continue
            vistos.add(n)
            depois = re.sub(r"\d{2}[-/]\d{2}[-/]\d{4}", " ", linha[ref.end():])   # as datas não são valores
            valores = re.findall(_VALOR, depois)
            r.linhas.append((n, cent(valores[-1]) if valores else None))
    totais = re.findall(r"Total(?:\s+(?:pago|recebido|a\s+pagar))?\s*(?:\(Euros\))?\s*:?\s*(" + _VALOR + ")", t, re.I)
    r.total = cent(totais[-1]) if totais else r.soma_linhas
    meio = re.search(r"(Transfer[eê]ncia|Multibanco|MB\s*WAY|Numer[aá]rio|Cart[aã]o|D[eé]bito\s+direto|Cheque)", t, re.I)
    r.meio = meio.group(1) if meio else ""
    return r


# ------------------------------------------------------------------ análise
@dataclass
class AnaliseFarmacia:
    faturas: list                    # [Fatura] do intervalo, mais recentes primeiro
    pagamentos: list                 # [Pagamento], mais recentes primeiro
    meses: list                      # [{mes, faturas, faturado, pago, em_aberto}]
    alertas: list                    # [texto]
    desde: date | None
    n_faturas: int = 0
    n_recibos: int = 0
    nao_lidos: list = field(default_factory=list)   # [(pid, início do texto)] da farmácia que não se leram
    n_antes: int = 0                                 # faturas de antes de «desde» (pagas pela Santa Casa)

    @property
    def em_aberto(self) -> int:
        return sum(f.total for f in self.faturas if not f.paga)

    @property
    def por_pagar(self) -> list:
        return [f for f in self.faturas if not f.paga]


def _da_farmacia(m, texto_banco: str) -> bool:
    return bool(texto_banco) and texto_banco in _simples(m.descricao_original or "").lower()


def analisar(docs: list, movimentos: list, desde: date | None = None, texto_banco: str = TEXTO_BANCO,
             hoje: date | None = None) -> AnaliseFarmacia:
    """`docs`: [(id, texto[, criado])] do correspondente da farmácia no Paperless. `movimentos`: débitos da conta da
    herança. `desde`: dia em que se passou a pagar a farmácia diretamente (as faturas anteriores são da Santa Casa)."""
    hoje = hoje or date.today()
    texto_banco = _simples(texto_banco or "").lower().strip()
    faturas, recibos, nao_lidos = {}, {}, []
    for item in docs:
        pid, t = item[0], item[1]
        r = ler_recibo(pid, t)
        if r:
            recibos.setdefault(r.numero, r)              # a 2.ª via não duplica
            continue
        f = ler_fatura(pid, t)
        if f:
            faturas.setdefault(f.numero, f)
            continue
        linhas = [l.strip() for l in (t or "").splitlines() if l.strip()]
        nao_lidos.append((pid, " · ".join(linhas[:3])[:160] or "(sem texto: o OCR falhou?)"))
    todas = list(faturas.values())
    fat = sorted([f for f in todas if f.total > 0 and (desde is None or f.data >= desde)], key=lambda f: (f.data, f.numero))
    n_antes = len(todas) - len(fat)
    por_num = {f.numero: f for f in todas}          # um recibo pode pagar ainda uma fatura de antes de «desde»
    recs = sorted([r for r in recibos.values() if desde is None or r.data >= desde], key=lambda r: r.data)
    inicio_mov = desde - timedelta(days=DIAS_RECIBO_ANTES) if desde else None
    movs = [m for m in movimentos if m.montante < 0 and (inicio_mov is None or m.data_mov >= inicio_mov)]
    usados: set[int] = set()
    pagamentos = []

    # 1) e 2) cada recibo: as faturas que lista e o débito com o mesmo valor
    for r in recs:
        fs = [por_num[n] for n, _ in r.linhas if n in por_num]
        for f in fs:
            f.recibo = r
        if r.total:
            cand = [m for m in movs if m.id not in usados and -m.montante == r.total
                    and r.data - timedelta(days=DIAS_RECIBO_ANTES) <= m.data_mov <= r.data + timedelta(days=DIAS_RECIBO_DEPOIS)]
            cand.sort(key=lambda m: (not _da_farmacia(m, texto_banco), abs((m.data_mov - r.data).days)))
            if cand:
                r.movimento = cand[0]
                usados.add(cand[0].id)
        p = Pagamento(r.movimento, r, fs, "recibo")
        for f in fs:
            f.pagamento = p
        pagamentos.append(p)

    # 3) os débitos da farmácia que sobram ↔ faturas ainda sem recibo (uma com o mesmo valor ou a soma de várias)
    for m in sorted([m for m in movs if m.id not in usados and _da_farmacia(m, texto_banco)], key=lambda m: m.data_mov):
        livres = [f for f in fat if not f.no_ato and f.recibo is None and f.pagamento is None
                  and m.data_mov - timedelta(days=DIAS_FATURA_ANTES) <= f.data <= m.data_mov + timedelta(days=3)]
        livres = sorted(livres, key=lambda f: abs((m.data_mov - f.data).days))[:14]
        achou = _melhor([(f, f.total, f.data) for f in livres], -m.montante, m.data_mov, MAX_FATURAS)
        usados.add(m.id)
        if achou:
            p = Pagamento(m, None, sorted(achou, key=lambda f: f.data), "mesmo valor" if len(achou) == 1 else "soma de faturas")
            for f in achou:
                f.pagamento = p
        else:
            p = Pagamento(m, None, [], "sem recibo nem fatura")
        pagamentos.append(p)

    # alertas
    alertas = []
    for f in fat:
        dias = (hoje - f.data).days
        if not f.paga and dias > PRAZO_PAGAMENTO:
            alertas.append(f"Fatura {f.numero} de {f.data:%d/%m/%Y} ({fmt_montante(f.total)}) por pagar há {dias} dias")
    for p in pagamentos:
        if p.recibo:
            r = p.recibo
            for prob in p.problemas():
                if prob == "sem débito no banco":
                    if (hoje - r.data).days > DIAS_RECIBO_DEPOIS:
                        alertas.append(f"Recibo {r.numero} de {r.data:%d/%m/%Y} ({fmt_montante(r.total or 0)}) sem débito igual na conta")
                elif prob != "recibo em falta":
                    alertas.append(f"Recibo {r.numero}: {prob}")
        elif p.faturas:
            if (hoje - p.movimento.data_mov).days > PRAZO_RECIBO:
                alertas.append(f"Pagamento de {p.movimento.data_mov:%d/%m/%Y} ({fmt_montante(p.valor)}) sem recibo da farmácia "
                               f"({', '.join(f.numero for f in p.faturas)})")
        else:
            alertas.append(f"Pagamento à farmácia de {p.movimento.data_mov:%d/%m/%Y} ({fmt_montante(p.valor)}) "
                           "sem fatura nem recibo com esse valor")

    meses = {}
    for f in fat:
        x = meses.setdefault(f.mes, {"mes": f.mes, "faturas": 0, "faturado": 0, "pago": 0, "em_aberto": 0})
        x["faturas"] += 1
        x["faturado"] += f.total
        x["pago" if f.paga else "em_aberto"] += f.total
    return AnaliseFarmacia(
        sorted(fat, key=lambda f: (f.data, f.numero), reverse=True),
        sorted(pagamentos, key=lambda p: p.data, reverse=True),
        sorted(meses.values(), key=lambda x: x["mes"], reverse=True),
        alertas, desde, len(fat), len(recs), nao_lidos, n_antes)
