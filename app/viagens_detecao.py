"""Deteção de períodos de viagem a partir dos movimentos (análise cronológica dos gastos).

Cada dia ganha pontos pelos sinais de estar fora de casa:
  +3 compra presencial no estrangeiro (Santander «COMPRA ESTRANG*nnnn <comerciante>», conta noutra moeda,
     «(123,45 JPY)» na descrição), câmbio para outra moeda («Conversão cambial para KRW»), seguro de viagem diário
  +2 código postal fora das zonas de casa (as zonas postais que aparecem em muitos meses)
  +2 alojamento, transporte ou aluguer de carro (hotel, resort, Airbnb, companhia aérea, aeroporto, …)
  +1 comerciante raro (visto em 4 meses ou menos — os sítios de férias repetem-se de ano para ano)
  +1 comissão de compra em moeda estrangeira
  −1 cada compra num comerciante habitual (8 meses ou mais: o supermercado do bairro)
Compras online (Amazon, PayPal, assinaturas, …), transferências, débitos diretos e pagamentos ao Estado não contam.
Um dia está «fora» com 2+ pontos e um sinal forte (estrangeiro/longe) ou metade das compras em comerciantes raros.
Dias fora separados por 5 dias ou menos (sem um dia «em casa» pelo meio) formam um período; fica o período com 2+ dias e 8+ pontos (ou com 3+ sinais
de estrangeiro). As reservas (hotel, voo, …) até 120 dias antes aparecem junto do período.

A IA (opcional) recebe só as descrições dos comerciantes por dia relativo («dia 1», «dia 2»), as moedas e as zonas
postais — nunca valores, datas, contas ou nomes de pessoas — e devolve se parece viagem, o destino e um nome.

Fotografias (opcional, Immich com a chave de quem está a ver): os lugares das fotos tiradas no período (cidade e país do
EXIF) confirmam o período quando são no estrangeiro ou longe de casa, dão o destino à viagem nova e vão para a IA (só
«cidade, país» e quantas fotos — nunca nomes de ficheiros, datas ou coordenadas).
"""
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import Conta, Movimento, SugestaoIA, Viagem, ViagemDescartada
from .sugestoes import padrao_de

ONLINE = re.compile(r"WWW\.|\.COM\b|\.PT\b|\.ES\b|AMAZON|AMZN|PAYPAL|PAY PAL|GOOGLE|APPLE\.|ITUNES|NETFLIX|SPOTIFY|ALIEXPRESS|"
                    r"ANTHROPIC|CLAUDE|OPENAI|REVOLUT|MICROSOFT|STEAM|DISNEY|HBO|PRIME VIDEO|EBAY|SHEIN|TEMU|LINKEDIN|"
                    r"DROPBOX|ADOBE|ICLOUD|HPY\*|GENEANET|MYHERITAGE|STRIPE|GARMIN|PADDLE|FACEBK|UDEMY|1GLOBAL", re.I)
NAO_PRESENCIAL = re.compile(r"^(TRF|TRANSF|DD[ -]|D[EÉ]BITO DIRETO|COB\.REC|PAG SERVICOS|PAG ESTADO|PAGAMENTO AO ESTADO|"
                            r"PAGAMENTO HCD|IMPOSTO|IMP\.|JUROS|VC-|\d{7}-\d{2}-|CONSTIT|MB WAY|TRF MB WAY)", re.I)
# contas da casa e serviços (o .xls do Santander escreve-os sem «DD»/«DÉBITO DIRETO»): nunca são sinal de viagem
CONTAS_FIXAS = re.compile(r"VIA VERDE|^PAGAMENTO |SANTA CASA|ENERGY|\bEDP\b|GALP ENERGIA|VODAFONE|\bMEO\b|\bNOS\b|EPAL|SMAS|"
                          r"SEGURO(?! VIAGEM)|CONDOMINIO|MUNICIP|FINANCAS|AT - |SEG SOCIAL|PRESTA[CÇ][AÃ]O", re.I)
LEVANTAMENTO = re.compile(r"LEVANT|LEV\.? ?NUMERARIO|^LEV ATM|^\d{7} LEV", re.I)
ESTRANG = re.compile(r"COMPRA ESTRANG\*\d{4} (\S.+)", re.I)
MOEDA_NA_DESCRICAO = re.compile(r"\(-?[\d.,]+ ([A-Z]{3})\)\s*$")
CAMBIO = re.compile(r"CONVERS\S* CAMBIAL PARA ([A-Z]{3})", re.I)
SEGURO_VIAGEM = re.compile(r"SEGURO VIAGEM", re.I)
# comissões e imposto de selo de uma compra em moeda estrangeira («0.07-978-COMISSAO CPR.», «IMP.CPR. ESTR»)
COMISSAO_CAMBIO = re.compile(r"COMISS\S* (CAMB|CPR)|IMP\.CPR\. ESTR|S/COM\. CA|^\d+\.\d{2}-\d{3}-", re.I)
CODIGO_POSTAL = re.compile(r"(?<!\d)(\d{4})-\d{2,3}")      # às vezes colado ao nome: «MEDRON8200-609»
ALOJAMENTO_TRANSPORTE = re.compile(
    r"AIRBNB|BOOKING|HOTEL|HOSTEL|EXPEDIA|TRIP\.COM|EDREAMS|RYANAIR|EASYJET|TAP AIR|VUELING|IBERIA|LUFTHANSA|AIR FRANCE|"
    r"\bKLM\b|TRANSAVIA|WIZZ|HERTZ|\bAVIS\b|EUROPCAR|\bSIXT\b|AIRPORT|AEROPORT|RESORT|POUSADA|MARRIOTT|HILTON|\bIBIS\b|"
    r"NOVOTEL|TIVOLI|PESTANA|VILA GALE|GETYOURGUIDE|PORTWAY|DUTY FREE", re.I)
NAO_MOEDAS = {"EUR", "XAU", "XAG", "XPT", "XPD", "BTC", "ETH"}     # metais e cripto não são viagens

RARO, HABITUAL = 4, 8                 # meses distintos
DIAS_JUNTAR, DIAS_RESERVA = 5, 120


@dataclass
class Evidencia:
    mov: Movimento
    razoes: list


@dataclass
class Periodo:
    ini: date
    fim: date
    pontos: int = 0
    n_estrangeiro: int = 0
    n_longe: int = 0
    moedas: Counter = field(default_factory=Counter)
    zonas: Counter = field(default_factory=Counter)
    evidencias: list = field(default_factory=list)    # [Evidencia] dentro do período
    reservas: list = field(default_factory=list)      # [Evidencia] antes do período
    viagem: Viagem | None = None                      # já registada (sobrepõe-se)
    ia: dict | None = None                            # resposta da IA (cache)

    @property
    def dias(self) -> int:
        return (self.fim - self.ini).days + 1

    @property
    def chave(self) -> str:
        return f"{self.ini:%Y%m%d}-{self.fim:%Y%m%d}"

    @property
    def estrangeiro(self) -> bool:
        return self.n_estrangeiro >= 2

    @property
    def confianca(self) -> str:
        if self.n_estrangeiro >= 3 or self.pontos >= 25:
            return "alta"
        return "média" if self.pontos >= 12 or self.n_longe >= 2 else "baixa"

    @property
    def gasto(self) -> int:
        """Soma (positiva) das despesas que deram sinal — uma estimativa, não o total da viagem."""
        return -sum(e.mov.montante for e in self.evidencias if e.mov.montante < 0 and not e.mov.transferencia)

    @property
    def comerciantes(self) -> list[str]:
        vistos = []
        for e in self.evidencias:
            nome = _comerciante(e.mov)
            if nome and nome not in vistos:
                vistos.append(nome)
        return vistos

    @property
    def nome_sugerido(self) -> str:
        if self.ia and self.ia.get("nome"):
            return str(self.ia["nome"])[:100]
        return f"Viagem {self.ini:%d/%m/%Y}"


def _comerciante(m: Movimento) -> str:
    texto = m.descricao_original or ""
    e = ESTRANG.search(texto)
    if e:
        texto = e.group(1)
    texto = re.sub(r"^COMPRA \*?\d{4} ", "", texto.strip(), flags=re.I)
    return re.sub(r"\s+", " ", texto)[:40]


def _movimentos(db: Session, entidade_id: int, ini: date | None = None) -> list[Movimento]:
    q = (select(Movimento).join(Conta).where(Conta.entidade_id == entidade_id, Movimento.montante < 0, ~Movimento.excluido)
         .options(selectinload(Movimento.conta)).order_by(Movimento.data_mov, Movimento.ordem, Movimento.id))
    if ini:
        q = q.where(Movimento.data_mov >= ini)
    return db.scalars(q).all()


def detetar(db: Session, entidade_id: int, desde: date | None = None) -> list[Periodo]:
    movs = _movimentos(db, entidade_id)
    if not movs:
        return []
    padroes, meses_padrao, meses_zona, meses = {}, defaultdict(set), defaultdict(set), set()
    for m in movs:
        mes = (m.data_mov.year, m.data_mov.month)
        meses.add(mes)
        p = padroes[m.id] = padrao_de(m.descricao_norm or m.descricao_original or "")
        meses_padrao[p].add(mes)
        cp = CODIGO_POSTAL.search(m.descricao_original or "")
        if cp:
            meses_zona[cp.group(1)[:2]].add(mes)
    zonas_casa = {z for z, ms in meses_zona.items() if len(ms) >= max(6, 0.15 * len(meses))}

    contagem: dict[date, Counter] = defaultdict(Counter)
    sinais: dict[date, list] = defaultdict(list)
    reservas: list[Evidencia] = []
    for m in movs:
        texto = m.descricao_original or ""
        norm = (m.descricao_norm or texto).upper()
        c = contagem[m.data_mov]
        cambio = CAMBIO.search(texto)
        if cambio and cambio.group(1).upper() not in NAO_MOEDAS:
            c["estr"] += 1
            c["moeda:" + cambio.group(1).upper()] += 1
            sinais[m.data_mov].append(Evidencia(m, [f"câmbio para {cambio.group(1).upper()}"]))
            continue
        if SEGURO_VIAGEM.search(texto):
            c["estr"] += 1
            sinais[m.data_mov].append(Evidencia(m, ["seguro de viagem"]))
            continue
        if m.transferencia:
            continue
        if COMISSAO_CAMBIO.search(norm):
            c["comissao"] += 1
            continue
        online = bool(ONLINE.search(norm))
        viagem = bool(ALOJAMENTO_TRANSPORTE.search(norm))
        if online:
            if viagem:
                reservas.append(Evidencia(m, ["reserva online"]))
            continue
        if NAO_PRESENCIAL.search(norm) or CONTAS_FIXAS.search(norm):
            continue
        razoes = []
        moeda = None
        md = MOEDA_NA_DESCRICAO.search(texto)
        if m.conta.moeda != "EUR":
            moeda = m.conta.moeda
        elif md and md.group(1) not in NAO_MOEDAS:
            moeda = md.group(1)
        if LEVANTAMENTO.search(norm) and not moeda and not ESTRANG.search(texto):
            continue                     # levantamento em euros: pode ser em qualquer lado
        if ESTRANG.search(texto) or moeda:
            c["estr"] += 1
            razoes.append(f"no estrangeiro{' (' + moeda + ')' if moeda else ''}")
            if moeda:
                c["moeda:" + moeda] += 1
        cp = CODIGO_POSTAL.search(texto)
        if cp and cp.group(1)[:2] not in zonas_casa:
            c["longe"] += 1
            c["zona:" + cp.group(1)[:2]] += 1
            razoes.append(f"código postal {cp.group(0)}")
        if viagem:
            c["viagem"] += 1
            razoes.append("alojamento / transporte")
        n_meses = len(meses_padrao[padroes[m.id]])
        c["compras"] += 1
        if n_meses <= RARO:
            c["raro"] += 1
            razoes.append("comerciante pouco habitual")
        elif n_meses >= HABITUAL:
            c["habitual"] += 1
        if razoes:
            sinais[m.data_mov].append(Evidencia(m, razoes))

    def pontos(c: Counter) -> int:
        return 3 * c["estr"] + 2 * c["longe"] + 2 * c["viagem"] + c["raro"] + c["comissao"] - c["habitual"]

    fora = sorted(d for d, c in contagem.items()
                  if pontos(c) >= 2 and (c["estr"] or c["longe"] or c["viagem"] or 2 * c["raro"] >= max(c["compras"], 1)))
    def em_casa(d: date) -> bool:
        """Dia só com compras nos sítios habituais (o supermercado do bairro, sem nada raro nem longe)."""
        c = contagem.get(d)
        return bool(c and c["habitual"] and not (c["raro"] or c["estr"] or c["longe"] or c["viagem"]))

    grupos: list[list[date]] = []
    for d in fora:
        # junta com os dias fora anteriores se o intervalo é curto e, pelo meio, não houve um dia em casa
        if grupos and (d - grupos[-1][-1]).days <= DIAS_JUNTAR and not any(
                em_casa(grupos[-1][-1] + timedelta(days=i)) for i in range(1, (d - grupos[-1][-1]).days)):
            grupos[-1].append(d)
        else:
            grupos.append([d])

    periodos = []
    for g in grupos:
        p = Periodo(g[0], g[-1])
        # todos os dias entre a partida e o regresso (também os de sinais fracos, ex.: um restaurante sem código postal)
        for d in sorted(x for x in contagem if p.ini <= x <= p.fim):
            c = contagem[d]
            p.pontos += pontos(c)
            p.n_estrangeiro += c["estr"]
            p.n_longe += c["longe"]
            for k, n in c.items():
                if k.startswith("moeda:"):
                    p.moedas[k[6:]] += n
                elif k.startswith("zona:"):
                    p.zonas[k[5:]] += n
            p.evidencias += sinais[d]
        if (p.dias >= 2 and p.pontos >= 8) or p.n_estrangeiro >= 3:
            periodos.append(p)
    # cada reserva (hotel, voo… pago online) vai para a 1.ª viagem que começa depois dela, até 120 dias
    for r in reservas:
        p = next((p for p in periodos if r.mov.data_mov < p.ini <= r.mov.data_mov + timedelta(days=DIAS_RESERVA)), None)
        if p:
            p.reservas.append(r)
    if desde:
        periodos = [p for p in periodos if p.fim >= desde]

    # já registadas e descartadas
    viagens = db.scalars(select(Viagem).where(Viagem.entidade_id == entidade_id)).all()
    descartadas = db.scalars(select(ViagemDescartada).where(ViagemDescartada.entidade_id == entidade_id)).all()
    out = []
    for p in periodos:
        if any(x.data_ini <= p.fim and x.data_fim >= p.ini for x in descartadas):
            continue
        p.viagem = next((v for v in viagens if v.data_ini <= p.fim + timedelta(days=1) and v.data_fim >= p.ini - timedelta(days=1)), None)
        out.append(p)
    _juntar_ia(db, out)
    return sorted(out, key=lambda p: p.ini, reverse=True)


# ------------------------------------------------------------------ fotografias (Immich, opcional)
PAIS_CASA = "Portugal"
CIDADES_CASA_OMISSAO = "Lisboa, Lisbon, Loures, Odivelas, Amadora, Oeiras, Sintra, Cascais, Almada, Seixal, Vila Franca de Xira"


def cidades_casa() -> set[str]:
    """Cidades (do EXIF das fotos) que são «casa»: GESTOR_CIDADES_CASA no .env, separadas por vírgulas."""
    txt = os.environ.get("GESTOR_CIDADES_CASA") or CIDADES_CASA_OMISSAO
    return {c.strip().casefold() for c in txt.split(",") if c.strip()}


@dataclass
class FotosPeriodo:
    total: int = 0
    lugares: list = field(default_factory=list)       # [(«Albufeira, Portugal», n)] dos mais fotografados
    destino: str | None = None                        # a cidade mais fotografada fora de casa
    pais: str | None = None
    estrangeiro: list = field(default_factory=list)   # países (≠ Portugal) com fotos
    confirmada: bool = False

    @property
    def motivo(self) -> str:
        if self.estrangeiro:
            return "fotos em " + ", ".join(self.estrangeiro[:3])
        return f"fotos longe de casa ({self.destino})" if self.destino else ""


def resumo_fotos(f) -> FotosPeriodo:
    """Lê as fotos de um período (immich.Fotos): lugares, destino provável e se confirmam que é uma viagem.

    Confirmam quando há fotos noutro país, ou quando metade (ou mais) das fotos com cidade são fora das cidades de casa."""
    casa = cidades_casa()
    r = FotosPeriodo(total=f.total, lugares=f.lugares)
    r.estrangeiro = [p for p in f.paises if p.casefold() != PAIS_CASA.casefold()]
    com_cidade = [x for x in f.fotos if x.cidade]
    fora = [x for x in com_cidade if x.cidade.casefold() not in casa or (x.pais and x.pais in r.estrangeiro)]
    # destino: o lugar mais fotografado fora de casa (no estrangeiro, de preferência)
    alvo = [x for x in fora if x.pais in r.estrangeiro] or fora
    if alvo:
        (r.destino, r.pais), _ = Counter((x.cidade, x.pais) for x in alvo).most_common(1)[0]
    elif r.estrangeiro:
        r.pais = r.estrangeiro[0]
    r.confirmada = bool(r.estrangeiro) or (bool(fora) and 2 * len(fora) >= len(com_cidade))
    return r


# ------------------------------------------------------------------ IA (opcional)
SISTEMA_VIAGEM = """Recebes os gastos presenciais de um período em que uma família portuguesa (casa na zona de Lisboa) \
parece ter estado fora de casa: por cada dia relativo («dia 1», «dia 2», …), as descrições curtas dos comerciantes tal \
como aparecem no extrato bancário, mais as moedas estrangeiras usadas e as zonas postais portuguesas (2 primeiros \
dígitos do código postal) fora de casa e, se houver, os lugares (cidade, país) das fotografias tiradas nesses dias, \
com quantas fotos em cada um — um sinal forte do destino. Diz se é uma viagem (férias, escapadinha ou trabalho) ou não (ex.: compras \
online, deslocações do dia a dia), o destino provável (cidade/região), o país e um nome curto para a viagem \
(ex.: «Algarve — Albufeira 2026», «Japão e Coreia 2026»). Não inventes: se não houver pistas do destino, deixa-o a null.
Responde só com um objeto JSON: {"viagem": true|false, "destino": str|null, "pais": str|null, "nome": str|null, \
"motivo": "lazer"|"trabalho"|"outro", "confianca": 0..1, "explicacao": str (1–2 frases)}."""


def _mensagem_ia(p: Periodo, lugares: list | None = None) -> str:
    """O que a IA recebe. `lugares` ([(«cidade, país», n)] das fotos) fica de fora da chave da cache (chave_ia)."""
    por_dia = defaultdict(list)
    for e in p.evidencias:
        nome = _comerciante(e.mov)
        if nome and nome not in por_dia[e.mov.data_mov]:
            por_dia[e.mov.data_mov].append(nome)
    linhas = [f"Duração: {p.dias} dias"]
    for d in sorted(por_dia):
        linhas.append(f"dia {(d - p.ini).days + 1}: " + "; ".join(por_dia[d][:12]))
    if p.reservas:
        linhas.append("Reservas antes da partida: " + "; ".join(sorted({_comerciante(r.mov) for r in p.reservas})[:8]))
    if p.moedas:
        linhas.append("Moedas estrangeiras: " + ", ".join(p.moedas))
    if p.zonas:
        linhas.append("Zonas postais fora de casa: " + ", ".join(sorted(p.zonas)))
    if lugares:
        linhas.append("Lugares das fotografias: " + "; ".join(f"{lugar} ({n})" for lugar, n in lugares[:8]))
    return "\n".join(linhas)


def chave_ia(p: Periodo) -> str:
    # sem os lugares das fotos: a página encontra a resposta guardada sem ter de perguntar ao Immich
    return "viagem|" + hashlib.sha1(_mensagem_ia(p).encode()).hexdigest()


def _juntar_ia(db: Session, periodos: list[Periodo]) -> None:
    chaves = {chave_ia(p): p for p in periodos}
    for s in db.scalars(select(SugestaoIA).where(SugestaoIA.chave.in_(list(chaves)))):
        try:
            chaves[s.chave].ia = json.loads(s.detalhe or "{}")
        except json.JSONDecodeError:
            pass


def analisar_com_ia(db: Session, p: Periodo, lugares: list | None = None) -> dict:
    """Pergunta à IA se o período é uma viagem e para onde (com os lugares das fotos, se houver). Guarda a resposta na
    cache (SugestaoIA)."""
    from . import ia
    if not ia.disponivel():
        raise ia.ErroIA("A chave da Claude API não está configurada (ANTHROPIC_API_KEY).")
    texto = ia._chamar(SISTEMA_VIAGEM, _mensagem_ia(p, lugares))
    ini, fim = texto.find("{"), texto.rfind("}")
    try:
        d = json.loads(texto[ini:fim + 1]) if 0 <= ini < fim else None
    except json.JSONDecodeError:
        d = None
    if not isinstance(d, dict):
        raise ia.ErroIA("A resposta da IA não trouxe a análise.")
    try:
        conf = max(0.0, min(1.0, float(d.get("confianca"))))
    except (TypeError, ValueError):
        conf = None
    r = {"viagem": bool(d.get("viagem")), "destino": (str(d.get("destino") or "")[:120] or None),
         "pais": (str(d.get("pais") or "")[:80] or None), "nome": (str(d.get("nome") or "")[:100] or None),
         "motivo": d.get("motivo") if d.get("motivo") in ("lazer", "trabalho", "outro") else "lazer",
         "confianca": conf, "explicacao": str(d.get("explicacao") or "")[:500] or None}
    chave = chave_ia(p)
    s = db.scalar(select(SugestaoIA).where(SugestaoIA.chave == chave)) or SugestaoIA(chave=chave)
    s.comerciante, s.atividade, s.confianca, s.modelo = r["nome"], r["destino"], conf, ia.modelo()
    s.detalhe = json.dumps(r, ensure_ascii=False)
    db.add(s)
    db.flush()
    p.ia = r
    return r
