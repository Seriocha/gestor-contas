"""Saúde: leitura da exportação «Google Health» do Google Takeout e resumo por perfil.

Estrutura observada (2026-10, conta pt-PT; nomes das pastas e colunas em inglês):
  Takeout/Google Health/Physical Activity_GoogleData/<Medida>/<medida>[_AAAA-MM-DD].csv
      timestamp (UTC, «…Z»), valor(es), «data source»
  Takeout/Google Health/Health Fitness Data_GoogleData/UserSleeps_*.csv, UserSleepStages_*.csv, UserExercises_*.csv
Lê-se só o que interessa (o zip tem centenas de MB por causa das calorias minuto a minuto e das zonas cardíacas).

Decisões:
  - Dia = dia em Lisboa (os timestamps vêm em UTC; o sono e os treinos trazem o desvio horário).
  - Passos, distância e pisos chegam em duplicado por várias fontes (relógio, Health Sync, telemóvel, Fit): por dia,
    soma-se cada fonte e fica a MAIOR — somar as fontes contaria os mesmos passos 3 ou 4 vezes.
  - Valores diários (FC em repouso, VFC, SpO2, respiração, VO2 máx.): média do dia (várias fontes → média).
  - Peso e gordura: a última medição do dia. Altura: a mais recente (para o IMC).
  - Sono: cada noite aparece repetida (origens diferentes) e há sestas: fica o sono principal (o mais longo) que
    acaba nesse dia, com as fases (leve, profundo, REM, acordado) desse registo.
  - Treinos: um por exercise_id; valores do relógio (tracker_*) ou, se o treino foi registado à mão, os manuais.
Privacidade: nada disto vai para a IA nem para os registos; a auditoria só guarda contagens.
"""
import csv
import io
import statistics
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LISBOA = ZoneInfo("Europe/Lisbon")
PA = "Physical Activity_GoogleData/"
HF = "Health Fitness Data_GoogleData/"

# tipo → (rótulo, unidade, casas decimais)
TIPOS = {
    "passos": ("Passos", "", 0),
    "distancia_km": ("Distância", "km", 2),
    "pisos": ("Pisos", "", 0),
    "peso_kg": ("Peso", "kg", 1),
    "gordura_pct": ("Gordura corporal", "%", 1),
    "fc_repouso": ("FC em repouso", "bpm", 0),
    "vfc_ms": ("Variabilidade da FC", "ms", 0),
    "spo2": ("Oxigénio no sangue", "%", 1),
    "resp_rpm": ("Respiração", "rpm", 1),
    "vo2max": ("VO2 máx.", "", 1),
    "sono_min": ("Sono", "min", 0),
    "sono_cama_min": ("Tempo na cama", "min", 0),
    "sono_acordado_min": ("Acordado", "min", 0),
    "sono_profundo_min": ("Sono profundo", "min", 0),
    "sono_leve_min": ("Sono leve", "min", 0),
    "sono_rem_min": ("Sono REM", "min", 0),
}
# (pasta, coluna do valor, tipo, fator, agregação: soma_fonte_max | media | ultimo)
MEDIDAS = [
    ("Steps/", "steps", "passos", 1, "soma_fonte_max"),
    ("Distance/", "distance", "distancia_km", 0.001, "soma_fonte_max"),
    ("Floors/", "floors", "pisos", 1, "soma_fonte_max"),
    ("Weight/", "weight grams", "peso_kg", 0.001, "ultimo"),
    ("Body Fat/", "body fat percentage", "gordura_pct", 1, "ultimo"),
    ("Daily Resting Heart Rate/", "beats per minute", "fc_repouso", 1, "media"),
    ("Daily Heart Rate Variability/", "average heart rate variability milliseconds", "vfc_ms", 1, "media"),
    ("Daily Oxygen Saturation/", "average percentage", "spo2", 1, "media"),
    ("Daily Respiratory Rate/", "breaths per minute", "resp_rpm", 1, "media"),
    ("Daily VO2 Max/", "daily vo2 max value", "vo2max", 1, "media"),
]
FASES = {"DEEP": "sono_profundo_min", "LIGHT": "sono_leve_min", "REM": "sono_rem_min", "AWAKE": "sono_acordado_min"}


class ErroSaude(ValueError):
    pass


@dataclass
class Treino:
    externo_id: str
    inicio: datetime
    duracao_min: float | None
    atividade: str
    calorias: float | None = None
    passos: int | None = None
    distancia_km: float | None = None
    fc_media: float | None = None
    fc_maxima: float | None = None
    manual: bool = False


@dataclass
class Leitura:
    medidas: dict = field(default_factory=dict)        # (data, tipo) → (valor, fonte)
    treinos: list = field(default_factory=list)
    altura_cm: float | None = None
    ficheiros: int = 0

    def contagem(self) -> dict:
        c = defaultdict(int)
        for (_, tipo) in self.medidas:
            c[tipo] += 1
        return dict(c)

    @property
    def dias(self) -> list[date]:
        return sorted({d for d, _ in self.medidas})


# ---------------------------------------------------------------- utilitários
def _utc(txt: str) -> datetime | None:
    t = (txt or "").strip()
    if not t:
        return None
    try:
        d = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def dia_local(txt: str) -> date | None:
    d = _utc(txt)
    return d.astimezone(LISBOA).date() if d else None


def _com_desvio(txt: str, desvio: str) -> datetime | None:
    """Hora local a partir do instante UTC e do desvio do próprio registo («+01:00»); sem desvio, Lisboa."""
    d = _utc(txt)
    if not d:
        return None
    try:
        sinal = -1 if desvio.strip().startswith("-") else 1
        h, m = desvio.strip().lstrip("+-").split(":")
        return (d + sinal * timedelta(hours=int(h), minutes=int(m))).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return d.astimezone(LISBOA).replace(tzinfo=None)


def _num(txt) -> float | None:
    try:
        v = float(str(txt).strip())
    except (TypeError, ValueError):
        return None
    return v if v == v else None          # NaN fora


class _Zip:
    def __init__(self, ficheiro):
        try:
            self.z = zipfile.ZipFile(ficheiro)
        except zipfile.BadZipFile:
            raise ErroSaude("O ficheiro não é um .zip do Google Takeout.") from None
        nomes = [n for n in self.z.namelist() if "/Google Health/" in n or n.startswith("Google Health/")]
        if not nomes:
            raise ErroSaude("O .zip não tem a pasta «Google Health» (no Takeout, escolhe «Google Health» / Fit).")
        raiz = nomes[0][:nomes[0].index("Google Health/") + len("Google Health/")]
        self.raiz = raiz
        self.nomes = [n for n in nomes if n.endswith(".csv")]
        self.lidos = 0

    def csvs(self, prefixo: str):
        for n in sorted(x for x in self.nomes if x.startswith(self.raiz + prefixo)):
            self.lidos += 1
            with self.z.open(n) as fh:
                yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace"))


# ---------------------------------------------------------------- leitura
def ler_takeout(ficheiro) -> Leitura:
    """ficheiro: caminho ou objeto de ficheiro (seekable) com o .zip do Takeout."""
    z = _Zip(ficheiro)
    out = Leitura()
    for pasta, coluna, tipo, fator, agreg in MEDIDAS:
        por_fonte = defaultdict(lambda: defaultdict(float))     # dia → fonte → soma
        valores = defaultdict(list)                             # dia → [(instante, valor, fonte)]
        for r in z.csvs(PA + pasta):
            d = _utc(r.get("timestamp", ""))
            v = _num(r.get(coluna))
            if d is None or v is None:
                continue
            dia, fonte = d.astimezone(LISBOA).date(), (r.get("data source") or "").strip()
            if agreg == "soma_fonte_max":
                por_fonte[dia][fonte] += v
            else:
                valores[dia].append((d, v, fonte))
        for dia, fontes in por_fonte.items():
            fonte, soma = max(fontes.items(), key=lambda x: x[1])
            if soma > 0:
                out.medidas[(dia, tipo)] = (round(soma * fator, 3), fonte)
        for dia, vs in valores.items():
            if agreg == "ultimo":
                _, v, fonte = max(vs, key=lambda x: x[0])
            else:
                v, fonte = statistics.fmean(x[1] for x in vs), ", ".join(sorted({x[2] for x in vs if x[2]}))
            out.medidas[(dia, tipo)] = (round(v * fator, 3), fonte[:60] or None)
    # altura: a mais recente
    alturas = [(d, v) for r in z.csvs(PA + "Height/")
               if (d := _utc(r.get("timestamp", ""))) and (v := _num(r.get("height millimeters")))]
    if alturas:
        out.altura_cm = round(max(alturas)[1] / 10, 1)
    _ler_sono(z, out)
    _ler_treinos(z, out)
    out.ficheiros = z.lidos
    return out


def _ler_sono(z: _Zip, out: Leitura) -> None:
    noites = {}                                                # dia de acordar → registo mais longo
    for r in z.csvs(HF + "UserSleeps"):
        fim = _com_desvio(r.get("sleep_end", ""), r.get("end_utc_offset", ""))
        dormido = _num(r.get("minutes_asleep"))
        if not fim or not dormido:
            continue
        dia = fim.date()
        if dia not in noites or dormido > noites[dia][0]:
            noites[dia] = (dormido, r)
    if not noites:
        return
    escolhidos = {r["sleep_id"]: dia for dia, (_, r) in noites.items()}
    fases = defaultdict(lambda: defaultdict(float))
    for r in z.csvs(HF + "UserSleepStages"):
        dia = escolhidos.get(r.get("sleep_id"))
        tipo = FASES.get((r.get("sleep_stage_type") or "").upper())
        a, b = _utc(r.get("sleep_stage_start", "")), _utc(r.get("sleep_stage_end", ""))
        if dia and tipo and a and b and b > a:
            fases[dia][tipo] += (b - a).total_seconds() / 60
    for dia, (dormido, r) in noites.items():
        out.medidas[(dia, "sono_min")] = (dormido, None)
        if (cama := _num(r.get("minutes_in_sleep_period"))):
            out.medidas[(dia, "sono_cama_min")] = (cama, None)
        if dia in fases:
            for tipo, minutos in fases[dia].items():
                out.medidas[(dia, tipo)] = (round(minutos), None)
        elif (acordado := _num(r.get("minutes_awake"))) is not None:
            out.medidas[(dia, "sono_acordado_min")] = (acordado, None)


def _ler_treinos(z: _Zip, out: Leitura) -> None:
    vistos = set()
    for r in z.csvs(HF + "UserExercises"):
        eid = (r.get("exercise_id") or "").strip()
        inicio = _com_desvio(r.get("exercise_start", ""), r.get("utc_offset", ""))
        if not eid or eid in vistos or not inicio:
            continue
        vistos.add(eid)
        a, b = _utc(r.get("exercise_start", "")), _utc(r.get("exercise_end", ""))

        def primeiro(*colunas):
            for c in colunas:
                v = _num(r.get(c))
                if v:
                    return v
            return None
        dist_mm = primeiro("summary_distance_mm", "tracker_total_distance_mm", "manually_logged_total_distance_mm")
        passos = primeiro("summary_steps", "tracker_total_steps", "manually_logged_total_steps")
        out.treinos.append(Treino(
            externo_id=eid[:40], inicio=inicio, duracao_min=round((b - a).total_seconds() / 60, 1) if a and b and b > a else None,
            atividade=(r.get("activity_name") or "Treino").strip()[:60],
            calorias=primeiro("summary_calories_kcal", "tracker_total_calories", "manually_logged_total_calories"),
            passos=int(passos) if passos else None, distancia_km=round(dist_mm / 1e6, 2) if dist_mm else None,
            fc_media=primeiro("summary_avg_heart_rate", "tracker_avg_heart_rate"),
            fc_maxima=primeiro("tracker_peak_heart_rate"),
            manual=(r.get("log_type") or "").upper() == "MANUAL"))


# ---------------------------------------------------------------- gravar (idempotente)
def gravar(db, perfil, leitura: Leitura) -> dict:
    """Insere ou atualiza as medidas (por dia e tipo) e os treinos (por id). Devolve {novas, alteradas, treinos}."""
    from sqlalchemy import select
    from .models import MedidaSaude, TreinoSaude
    existentes = {(m.data, m.tipo): m for m in db.scalars(select(MedidaSaude).where(MedidaSaude.perfil_id == perfil.id))}
    novas = alteradas = 0
    for (dia, tipo), (valor, fonte) in leitura.medidas.items():
        m = existentes.get((dia, tipo))
        if m is None:
            db.add(MedidaSaude(perfil_id=perfil.id, data=dia, tipo=tipo, valor=valor, fonte=fonte))
            novas += 1
        elif abs(m.valor - valor) > 1e-9 or m.fonte != fonte:
            m.valor, m.fonte = valor, fonte
            alteradas += 1
    treinos = {t.externo_id: t for t in db.scalars(select(TreinoSaude).where(TreinoSaude.perfil_id == perfil.id))}
    n_treinos = 0
    for t in leitura.treinos:
        dados = {k: getattr(t, k) for k in ("inicio", "duracao_min", "atividade", "calorias", "passos", "distancia_km",
                                            "fc_media", "fc_maxima", "manual")}
        if t.externo_id in treinos:
            for k, v in dados.items():
                setattr(treinos[t.externo_id], k, v)
        else:
            db.add(TreinoSaude(perfil_id=perfil.id, externo_id=t.externo_id, **dados))
            n_treinos += 1
    if leitura.altura_cm:
        perfil.altura_cm = leitura.altura_cm
    perfil.importado_em = datetime.now()
    if leitura.dias:
        perfil.importado_ate = leitura.dias[-1]
    return {"novas": novas, "alteradas": alteradas, "treinos": n_treinos}


# ---------------------------------------------------------------- resumo para a página
PERIODOS = {"30": ("30 dias", 30), "90": ("90 dias", 90), "365": ("1 ano", 365), "tudo": ("Tudo", None)}


def _serie(medidas: dict, tipo: str, dias: list[date]) -> list:
    return [medidas.get((d, tipo)) for d in dias]


def resumo(db, perfil, periodo: str = "90", hoje: date | None = None) -> dict:
    """KPIs (últimos valores e médias) e séries para os gráficos. Períodos longos agrupam por semana ou mês."""
    from sqlalchemy import func, select
    from .models import MedidaSaude, TreinoSaude
    hoje = hoje or date.today()
    _, n = PERIODOS.get(periodo, PERIODOS["90"])
    q = select(MedidaSaude.data, MedidaSaude.tipo, MedidaSaude.valor).where(MedidaSaude.perfil_id == perfil.id)
    todas = db.execute(q).all()
    if not todas:
        return {"vazio": True}
    ultimo_dia = max(d for d, _, _ in todas)
    fim = min(hoje, ultimo_dia)              # o período acaba no último dia com dados
    ini = fim - timedelta(days=n - 1) if n else min(d for d, _, _ in todas)
    medidas = {(d, t): v for d, t, v in todas}

    def ultimo(tipo):
        vs = [(d, v) for (d, t), v in medidas.items() if t == tipo]
        return max(vs) if vs else (None, None)

    def media(tipo, dias):
        vs = [v for (d, t), v in medidas.items() if t == tipo and fim - timedelta(days=dias - 1) <= d <= fim]
        return statistics.fmean(vs) if vs else None

    kpis = {}
    d_peso, peso = ultimo("peso_kg")
    if peso:
        antes = [(d, v) for (d, t), v in medidas.items() if t == "peso_kg" and d <= d_peso - timedelta(days=28)]
        kpis["peso"] = {"valor": peso, "data": d_peso, "delta": peso - max(antes)[1] if antes else None}
        if perfil.altura_cm:
            kpis["imc"] = peso / (perfil.altura_cm / 100) ** 2
    d_g, gordura = ultimo("gordura_pct")
    if gordura:
        kpis["gordura"] = {"valor": gordura, "data": d_g}
    for tipo, chave in (("passos", "passos"), ("sono_min", "sono"), ("fc_repouso", "fc"), ("vfc_ms", "vfc"),
                        ("spo2", "spo2"), ("resp_rpm", "resp"), ("pisos", "pisos")):
        m7, m30 = media(tipo, 7), media(tipo, 30)
        if m7 is not None or m30 is not None:
            kpis[chave] = {"m7": m7, "m30": m30}

    # eixo: dia a dia até 120 dias; semanas até 2 anos; meses depois
    total = (fim - ini).days + 1
    agrupar = "dia" if total <= 120 else ("semana" if total <= 731 else "mes")
    dias = [ini + timedelta(days=i) for i in range(total)]

    def chave_grupo(d):
        if agrupar == "dia":
            return d
        if agrupar == "semana":
            return d - timedelta(days=d.weekday())
        return d.replace(day=1)
    grupos = []
    for d in dias:
        k = chave_grupo(d)
        if not grupos or grupos[-1][0] != k:
            grupos.append((k, []))
        grupos[-1][1].append(d)

    def serie(tipo, casas=1):
        out = []
        for _, ds in grupos:
            vs = [medidas[(d, tipo)] for d in ds if (d, tipo) in medidas]
            out.append(round(statistics.fmean(vs), casas) if vs else None)
        return out
    fmt = {"dia": "%d/%m", "semana": "%d/%m/%y", "mes": "%m/%Y"}[agrupar]
    graficos = {
        "rotulos": [k.strftime(fmt) for k, _ in grupos], "agrupar": agrupar,
        "peso": serie("peso_kg"), "gordura": serie("gordura_pct"),
        "passos": serie("passos", 0), "distancia": serie("distancia_km", 2),
        "sono": {t: [None if v is None else round(v / 60, 2) for v in serie(t, 1)]
                 for t in ("sono_profundo_min", "sono_leve_min", "sono_rem_min", "sono_acordado_min", "sono_min")},
        "fc": serie("fc_repouso"), "vfc": serie("vfc_ms"), "spo2": serie("spo2"), "resp": serie("resp_rpm"),
    }
    tem = {k: any(v is not None for v in graficos[k]) for k in ("peso", "gordura", "passos", "fc", "vfc", "spo2", "resp")}
    tem["sono"] = any(v is not None for v in graficos["sono"]["sono_min"])
    treinos = db.scalars(select(TreinoSaude).where(TreinoSaude.perfil_id == perfil.id)
                         .order_by(TreinoSaude.inicio.desc()).limit(25)).all()
    n_treinos = db.scalar(select(func.count()).select_from(TreinoSaude).where(TreinoSaude.perfil_id == perfil.id))
    return {"vazio": False, "kpis": kpis, "graficos": graficos, "tem": tem, "ini": ini, "fim": fim, "agrupar": agrupar,
            "treinos": treinos, "n_treinos": n_treinos, "primeiro": min(d for d, _, _ in todas), "ultimo": ultimo_dia}
