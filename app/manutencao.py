"""Manutenção dos carros: quilómetros, plano de manutenção (IA) e necessidade de oficina.

- **Quilómetros**: registos periódicos do conta-quilómetros (e os das idas à oficina). Dão a média de
  km por dia, usada para estimar os km de hoje e a data em que se chega ao próximo marco.
- **Plano**: a IA (Claude API, com pesquisa web quando a conta o permite) devolve o plano de
  manutenção do fabricante para a marca, modelo, ano, combustível e versão — operações com
  intervalo em km e/ou meses e custo estimado. Só se envia isso e os km atuais.
- **Estado**: para cada operação, a última vez que foi feita (idas à oficina registadas) mais o
  intervalo dá o próximo marco; «em atraso» se já passou (em km estimados ou em data), «em breve»
  se faltam menos de 1500 km ou 45 dias.
"""
import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.orm import Session

from . import ia
from .models import COMBUSTIVEIS, Bem, ItemPlano, RegistoKm

LEMBRETE_KM_DIAS = 30          # pedir nova leitura dos km quando a última tem mais do que isto
EM_BREVE_KM = 1500
EM_BREVE_DIAS = 45


# ------------------------------------------------------------------ quilómetros
def ultimo_registo(b: Bem) -> RegistoKm | None:
    return max(b.registos_km, key=lambda r: (r.data, r.km)) if b.registos_km else None


def km_por_dia(b: Bem) -> float | None:
    """Média entre a leitura mais antiga e a mais recente (precisa de pelo menos 30 dias entre elas)."""
    regs = sorted(b.registos_km, key=lambda r: (r.data, r.km))
    if len(regs) < 2 or (regs[-1].data - regs[0].data).days < 30:
        return None
    return max(0.0, (regs[-1].km - regs[0].km) / (regs[-1].data - regs[0].data).days)


def km_estimados(b: Bem, hoje: date) -> int | None:
    """Km de hoje: a última leitura mais a média diária desde então."""
    r = ultimo_registo(b)
    if not r:
        return b.km
    v = km_por_dia(b)
    return r.km + round(v * max(0, (hoje - r.data).days)) if v else r.km


def lembrar_km(b: Bem, hoje: date) -> bool:
    r = ultimo_registo(b)
    return not b.vendido and (r is None or (hoje - r.data).days > LEMBRETE_KM_DIAS)


def registar_km(db: Session, b: Bem, data: date, km: int, origem: str = "manual", nota: str | None = None) -> RegistoKm:
    """Grava a leitura e valida que não anda para trás face às leituras vizinhas."""
    antes = [r for r in b.registos_km if r.data <= data]
    depois = [r for r in b.registos_km if r.data > data]
    if antes and km < max(r.km for r in antes):
        raise ValueError(f"Os km não podem ser menos do que numa leitura anterior ({max(r.km for r in antes)} km).")
    if depois and km > min(r.km for r in depois):
        raise ValueError(f"Os km não podem ser mais do que numa leitura posterior ({min(r.km for r in depois)} km).")
    r = RegistoKm(bem_id=b.id, data=data, km=km, origem=origem, nota=nota)
    b.registos_km.append(r)
    ultimo = ultimo_registo(b)
    b.km = ultimo.km if ultimo else km
    db.flush()
    return r


# ------------------------------------------------------------------ estado do plano
def _somar_meses(d: date, n: int) -> date:
    m = d.month - 1 + n
    a, m = d.year + m // 12, m % 12 + 1
    return date(a, m, min(d.day, calendar.monthrange(a, m)[1]))


@dataclass
class EstadoItem:
    item: ItemPlano
    ultima_data: date | None        # última vez feito (None = sem registo)
    ultima_km: int | None
    prox_km: int | None
    prox_data: date | None
    falta_km: int | None
    falta_dias: int | None
    data_prevista: date | None      # quando se chega ao marco (pelo que vier primeiro: km ou data)
    estado: str                     # em atraso | em breve | ok | sem dados

    @property
    def sem_registo(self) -> bool:
        return self.ultima_data is None


def _base(b: Bem) -> tuple[date | None, int | None]:
    """Sem registo de oficina, conta-se do início: carro novo (data de compra ou ano) a 0 km."""
    if b.data_aquisicao:
        return b.data_aquisicao, (0 if not b.ano or b.data_aquisicao.year <= b.ano + 1 else None)
    return (date(b.ano, 1, 1), 0) if b.ano else (None, None)


def estado_itens(b: Bem, hoje: date) -> list[EstadoItem]:
    km_hoje = km_estimados(b, hoje)
    ritmo = km_por_dia(b)
    feitos = {}                     # nome do item (minúsculas) → (data, km) da última vez
    for m in sorted(b.manutencoes, key=lambda m: (m.data, m.km or 0)):
        for nome in m.lista_itens:
            feitos[nome.strip().lower()] = (m.data, m.km)
    base_data, base_km = _base(b)
    linhas = []
    for it in b.plano:
        ultima = feitos.get(it.item.strip().lower())
        ud, uk = ultima if ultima else (None, None)
        de_data, de_km = (ud, uk) if ultima else (base_data, base_km)
        prox_km = de_km + it.intervalo_km if it.intervalo_km and de_km is not None else None
        prox_data = _somar_meses(de_data, it.intervalo_meses) if it.intervalo_meses and de_data else None
        falta_km = prox_km - km_hoje if prox_km is not None and km_hoje is not None else None
        falta_dias = (prox_data - hoje).days if prox_data else None
        previstas = [d for d in (prox_data, hoje + timedelta(days=round(falta_km / ritmo))
                                 if falta_km is not None and ritmo and falta_km > 0 else None) if d]
        if falta_km is not None and falta_km <= 0:
            previstas.append(hoje)
        if falta_km is None and falta_dias is None:
            estado = "sem dados"
        elif (falta_km is not None and falta_km <= 0) or (falta_dias is not None and falta_dias <= 0):
            estado = "em atraso"
        elif (falta_km is not None and falta_km <= EM_BREVE_KM) or (falta_dias is not None and falta_dias <= EM_BREVE_DIAS):
            estado = "em breve"
        else:
            estado = "ok"
        linhas.append(EstadoItem(it, ud, uk, prox_km, prox_data, falta_km, falta_dias,
                                 min(previstas) if previstas else None, estado))
    ordem = {"em atraso": 0, "em breve": 1, "ok": 2, "sem dados": 3}
    return sorted(linhas, key=lambda e: (ordem[e.estado], e.data_prevista or date.max))


@dataclass
class Resumo:
    km_hoje: int | None
    km_dia: float | None
    lembrar_km: bool
    itens: list
    atraso: list
    breve: list

    @property
    def precisa_oficina(self) -> bool:
        return bool(self.atraso)

    @property
    def proxima(self) -> "EstadoItem | None":
        return next((e for e in self.itens if e.data_prevista), None)


def resumo(b: Bem, hoje: date) -> Resumo:
    itens = estado_itens(b, hoje)
    return Resumo(km_estimados(b, hoje), km_por_dia(b), lembrar_km(b, hoje), itens,
                  [e for e in itens if e.estado == "em atraso"], [e for e in itens if e.estado == "em breve"])


# ------------------------------------------------------------------ plano por IA
SISTEMA = """És um mecânico/consultor automóvel em Portugal. Dás o plano de manutenção periódica \
recomendado pelo fabricante para o carro descrito (revisões, óleo e filtros, correia/corrente de \
distribuição, velas ou filtro de partículas conforme o motor, travões, pneus, líquidos, bateria, \
inspeção periódica obrigatória em Portugal, etc.). Se tiveres a ferramenta de pesquisa na web, usa-a \
para confirmar os intervalos deste modelo e motor. Usa intervalos realistas; se o fabricante indicar \
km e tempo, dá os dois («o que ocorrer primeiro»). Custos estimados em euros, numa oficina independente \
em Portugal. Não inventes: se não souberes um intervalo, deixa-o null.
Responde APENAS com um objeto JSON: {"itens": [{"item": nome curto em português, "intervalo_km": inteiro \
ou null, "intervalo_meses": inteiro ou null, "custo_min": inteiro ou null, "custo_max": inteiro ou null, \
"notas": texto curto ou null}], "notas": 1 a 3 frases sobre o modelo (pontos fracos conhecidos, \
correia vs. corrente…)}."""


def descricao_carro(b: Bem, hoje: date) -> str:
    linhas = ["Automóvel: " + " ".join(x for x in (b.marca, b.modelo) if x)]
    if b.ano:
        linhas.append(f"Ano de fabrico: {b.ano}")
    if b.combustivel:
        linhas.append(f"Combustível: {COMBUSTIVEIS.get(b.combustivel, b.combustivel)}")
    if b.extras:
        linhas.append(f"Versão / motor: {b.extras}")
    km = km_estimados(b, hoje)
    if km is not None:
        linhas.append(f"Quilómetros atuais: {km}")
    return "\n".join(linhas)


def falta_para_plano(b: Bem) -> list[str]:
    return [n for n, v in (("marca", b.marca), ("modelo", b.modelo), ("ano", b.ano)) if not v]


def _int(v, minimo=1, maximo=10**7) -> int | None:
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return n if minimo <= n <= maximo else None


def gerar_plano(db: Session, b: Bem, hoje: date) -> int:
    """Pede à IA o plano de manutenção e substitui o atual. As idas à oficina ligam-se aos itens pelo nome
    — os itens com o mesmo nome mantêm-se (e o histórico continua a contar)."""
    from .patrimonio import PESQUISA_WEB, _objeto_json
    if not ia.disponivel():
        raise ia.ErroIA("A chave da Claude API não está configurada (ANTHROPIC_API_KEY).")
    falta = falta_para_plano(b)
    if falta:
        raise ia.ErroIA("Para pedir o plano, indica: " + ", ".join(falta) + ".")
    mensagem = f"Data de hoje: {hoje.strftime('%d/%m/%Y')}\n\n{descricao_carro(b, hoje)}"
    try:
        texto = ia._chamar(SISTEMA, mensagem, ferramentas=PESQUISA_WEB)
    except ia.ErroIA as e:
        if "400" not in str(e) and "web_search" not in str(e):
            raise
        texto = ia._chamar(SISTEMA, mensagem)
    d = _objeto_json(texto)
    novos = []
    for i, x in enumerate(d.get("itens") or []):
        if not isinstance(x, dict) or not str(x.get("item") or "").strip():
            continue
        km, meses = _int(x.get("intervalo_km"), 500), _int(x.get("intervalo_meses"), 1, 240)
        if not km and not meses:
            continue
        cmin, cmax = _int(x.get("custo_min"), 0), _int(x.get("custo_max"), 0)
        novos.append(dict(item=str(x["item"]).strip()[:120], intervalo_km=km, intervalo_meses=meses,
                          custo_min=cmin * 100 if cmin is not None else None,
                          custo_max=cmax * 100 if cmax is not None and (cmin is None or cmax >= cmin) else None,
                          notas=str(x.get("notas") or "")[:300] or None, ordem=i))
    if not novos:
        raise ia.ErroIA("A IA não devolveu operações de manutenção.")
    atuais = {it.item.strip().lower(): it for it in b.plano}
    vistos = set()
    for n in novos:
        chave = n["item"].lower()
        if chave in vistos:
            continue
        vistos.add(chave)
        it = atuais.get(chave)
        if it:
            for k, v in n.items():
                setattr(it, k, v)
        else:
            b.plano.append(ItemPlano(bem_id=b.id, **n))
    for chave, it in atuais.items():
        if chave not in vistos:
            b.plano.remove(it)
    b.plano_notas = str(d.get("notas") or "")[:1500] or None
    b.plano_em, b.plano_modelo = hoje, ia.modelo()
    db.flush()
    return len(vistos)
