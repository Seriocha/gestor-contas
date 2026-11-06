"""Recibos de vencimento no Paperless: que períodos (meses) já lá estão, quais faltam e quais estão repetidos.

Uma grelha ano × mês por empregador (o correspondente do documento), com os recibos do tipo «Recibo de vencimento»
visíveis com o token do dono (os recibos só são visíveis para ele):
  - o período vem do campo personalizado «Período» (AAAA-MM, gravado por app/recibos.py); sem ele, lê-se do texto
    (recibos.extrair) e assinala-se «do texto» — o recibo ainda não passou por «Gravar valores»;
  - um mês com um recibo fica «no Paperless», com a ligação ao documento;
  - um mês SEM recibo entre o primeiro e o último fica «em falta» (bem à vista);
  - um mês com vários recibos fica «repetido» quando dois têm o mesmo Líquido (ou não se sabe o Líquido: na dúvida
    mostra-se), e «vários» quando os Líquidos diferem (ex.: recibo de prémio ou de subsídio à parte);
  - os subsídios de férias e de Natal assinalam-se no mês do recibo que os paga (linha «Subsídio de férias/Natal»
    com importe na tabela de Abonos; o «subsídio de almoço férias» não conta).
Privacidade: só se guardam id, título, período, Líquido e os subsídios; nunca o texto.
"""
import re
from dataclasses import dataclass, field
from datetime import date

from . import paperless, recibos

SUBSIDIO = re.compile(r"\bsubs(?:idio)?\.? ?(?:de )?(natal|ferias)\b")
SEM_EMPREGADOR = "Sem correspondente"


def periodo_de(valor) -> str | None:
    """«2025-12», «2025-1», «12/2025» ou «12-2025» → «2025-12»; senão None."""
    txt = str(valor or "").strip()
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})", txt)
    if m:
        a, mes = int(m.group(1)), int(m.group(2))
    else:
        m = re.fullmatch(r"(\d{1,2})[-/.](\d{4})", txt)
        if not m:
            return None
        a, mes = int(m.group(2)), int(m.group(1))
    return f"{a}-{mes:02d}" if 1 <= mes <= 12 and 1990 <= a <= 2100 else None


def subsidios(texto: str) -> set[str]:
    """{«ferias», «natal»} pagos neste recibo: linhas «Subsídio de férias/Natal» com um importe positivo."""
    out = set()
    for l in recibos.normalizar(texto):
        m = SUBSIDIO.search(l)
        if m and any(not i.startswith("-") for i in re.findall(recibos.IMPORTE, l)):
            out.add(m.group(1))
    return out


@dataclass
class Doc:
    id: int
    titulo: str
    periodo: str | None
    do_texto: bool = False          # período lido do texto (o campo «Período» está vazio)
    liquido: int | None = None      # cêntimos
    subsidios: set = field(default_factory=set)

    @property
    def link(self) -> str:
        return f"{paperless.url_browser()}/documents/{self.id}/details"


@dataclass
class Mes:
    periodo: str
    docs: list = field(default_factory=list)
    estado: str = ""                # ok | repetido | varios | falta | "" (fora do intervalo)

    @property
    def subsidios(self) -> set:
        return set().union(*(d.subsidios for d in self.docs)) if self.docs else set()

    @property
    def siglas(self) -> str:
        """«SF», «SN» ou «SF SN»."""
        s = self.subsidios
        return " ".join(x for x, k in (("SF", "ferias"), ("SN", "natal")) if k in s)


@dataclass
class Mapa:
    empregador: str
    docs: list
    anos: list = field(default_factory=list)          # [(ano, [Mes × 12])], do mais recente para o mais antigo
    sem_periodo: list = field(default_factory=list)

    def _com(self, estado: str) -> list:
        return [m for _, meses in self.anos for m in meses if m.estado == estado]

    @property
    def em_falta(self) -> list:
        return sorted(self._com("falta"), key=lambda m: m.periodo)

    @property
    def repetidos(self) -> list:
        return sorted(self._com("repetido"), key=lambda m: m.periodo)

    @property
    def varios(self) -> list:
        return sorted(self._com("varios"), key=lambda m: m.periodo)

    @property
    def com_periodo(self) -> list:
        return sorted({d.periodo for d in self.docs if d.periodo})

    @property
    def primeiro(self) -> str | None:
        return self.com_periodo[0] if self.com_periodo else None

    @property
    def ultimo(self) -> str | None:
        return self.com_periodo[-1] if self.com_periodo else None

    @property
    def do_texto(self) -> int:
        return sum(1 for d in self.docs if d.do_texto)


def _estado(docs: list[Doc]) -> str:
    if len(docs) == 1:
        return "ok"
    liquidos = [d.liquido for d in docs]
    if None in liquidos or len(set(liquidos)) < len(liquidos):
        return "repetido"
    return "varios"


def _seguinte(p: str) -> str:
    a, m = int(p[:4]), int(p[5:])
    return f"{a + (m == 12)}-{m % 12 + 1:02d}"


def mapa(empregador: str, docs: list[Doc]) -> Mapa:
    """Grelha ano × mês: os anos do primeiro ao último recibo, todos os meses; «em falta» só entre o primeiro e o último."""
    out = Mapa(empregador, docs, sem_periodo=[d for d in docs if not d.periodo])
    por_mes: dict[str, list[Doc]] = {}
    for d in docs:
        if d.periodo:
            por_mes.setdefault(d.periodo, []).append(d)
    if not por_mes:
        return out
    primeiro, ultimo = min(por_mes), max(por_mes)
    dentro, p = set(), primeiro
    while p <= ultimo:
        dentro.add(p)
        p = _seguinte(p)
    for ano in range(int(ultimo[:4]), int(primeiro[:4]) - 1, -1):
        meses = []
        for m in range(1, 13):
            per = f"{ano}-{m:02d}"
            ds = sorted(por_mes.get(per, []), key=lambda d: d.id)
            meses.append(Mes(per, ds, _estado(ds) if ds else ("falta" if per in dentro else "")))
        out.anos.append((ano, meses))
    return out


def documentos(token: str, tipo: str = recibos.TIPO, correspondente: str | None = None) -> list[tuple[str, Doc]]:
    """(empregador, Doc) de cada recibo visível com o token (deste tipo de documento e, se indicado, deste empregador)."""
    tipo_id, corr_id = recibos.filtro(token, tipo, correspondente)
    campos = recibos._ids("custom_fields", token)
    c_periodo, c_liquido = campos.get(recibos.CAMPOS["periodo"]), campos.get(recibos.CAMPOS["liquido"])
    nomes = paperless._nomes("/api/correspondents/", token)
    out = []
    for d in recibos._documentos(tipo_id, token, corr_id, fields="id,title,content,custom_fields,correspondent"):
        valores = {c["field"]: c.get("value") for c in d.get("custom_fields") or []}
        texto = d.get("content") or ""
        per, do_texto = periodo_de(valores.get(c_periodo)), False
        liquido = recibos.de_monetario(valores.get(c_liquido)) if valores.get(c_liquido) not in (None, "") else None
        if not per or liquido is None:
            lido = recibos.extrair(texto)
            if not per and lido.periodo:
                per, do_texto = lido.periodo, True
            if liquido is None:
                liquido = lido.liquido
        emp = nomes.get(d.get("correspondent")) or SEM_EMPREGADOR
        out.append((emp, Doc(d["id"], d.get("title") or "", per, do_texto, liquido, subsidios(texto))))
    return out


def mapas(token: str, tipo: str = recibos.TIPO, correspondente: str | None = None) -> list[Mapa]:
    """Um mapa por empregador, por ordem alfabética (os sem correspondente no fim)."""
    grupos: dict[str, list[Doc]] = {}
    for emp, d in documentos(token, tipo, correspondente):
        grupos.setdefault(emp, []).append(d)
    return [mapa(e, grupos[e]) for e in sorted(grupos, key=lambda e: (e == SEM_EMPREGADOR, e.lower()))]


def meses_desde(periodo: str | None, hoje: date | None = None) -> int | None:
    """Quantos meses passaram do período até ao mês atual (0 = este mês)."""
    if not periodo:
        return None
    hoje = hoje or date.today()
    return (hoje.year - int(periodo[:4])) * 12 + hoje.month - int(periodo[5:])
