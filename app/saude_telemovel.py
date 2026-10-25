"""Saúde pelo telemóvel: a app Android «Gestor Saúde» lê o Health Connect e envia ao Gestor (/api/saude/enviar).

Formato do envio (JSON, versão 1) — o telemóvel já manda os valores por dia (dia local), porque o Health Connect
junta ele próprio as fontes repetidas (relógio, Health Sync, telemóvel):
  {"versao": 1, "app": "1.0",
   "dias": [{"data": "2026-10-24", "passos": 8123, "distancia_km": 6.1, "peso_kg": 91.5, "sono_min": 412, ...}],
   "treinos": [{"id": "<id do Health Connect>", "inicio": "2026-10-24T08:00:00+01:00", "duracao_min": 42.5,
                "atividade": "Corrida", "calorias": 410, "passos": 6000, "distancia_km": 6.0,
                "fc_media": 148, "fc_maxima": 171}]}
As chaves de cada dia são os tipos de app/saude.py (TIPOS). Reenviar o mesmo dia substitui o valor (saude.gravar);
os treinos ficam pelo id do Health Connect («hc:<id>»), por isso também não duplicam.
Emparelhamento: igual ao da TV (app/tv.py) — a app pede um código, alguém com «editar» aprova-o em Saúde › Telemóvel
e escolhe o perfil; a app recebe um token próprio que só serve para enviar dados para esse perfil.
"""
import math
from datetime import date, datetime

from . import saude as sd

FONTE = "Health Connect"
MAX_DIAS = 800
MAX_TREINOS = 2000
# limites de plausibilidade: um valor fora disto é ignorado (erro de leitura, unidade trocada…)
LIMITES = {
    "passos": (0, 200_000), "distancia_km": (0, 400), "pisos": (0, 2_000), "peso_kg": (20, 400), "gordura_pct": (2, 75),
    "fc_repouso": (20, 200), "vfc_ms": (1, 400), "spo2": (50, 100), "resp_rpm": (3, 60), "vo2max": (5, 100),
    "sono_min": (0, 1_440), "sono_cama_min": (0, 1_440), "sono_acordado_min": (0, 1_440), "sono_profundo_min": (0, 1_440),
    "sono_leve_min": (0, 1_440), "sono_rem_min": (0, 1_440),
}


class ErroEnvio(ValueError):
    pass


def _num(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def converter(dados: dict, hoje: date | None = None) -> tuple[sd.Leitura, int]:
    """(Leitura pronta para saude.gravar, n.º de valores ignorados). Valida tudo: o que vem do telemóvel não é de
    confiança."""
    if not isinstance(dados, dict) or dados.get("versao") != 1:
        raise ErroEnvio("Formato desconhecido (versão do envio). Atualiza a app Gestor Saúde.")
    dias, treinos = dados.get("dias") or [], dados.get("treinos") or []
    if not isinstance(dias, list) or not isinstance(treinos, list):
        raise ErroEnvio("Formato inválido.")
    if len(dias) > MAX_DIAS or len(treinos) > MAX_TREINOS:
        raise ErroEnvio(f"Envio grande demais (máximo {MAX_DIAS} dias e {MAX_TREINOS} treinos de cada vez).")
    hoje = hoje or date.today()
    out, ignorados = sd.Leitura(), 0
    for d in dias:
        try:
            dia = date.fromisoformat(str(d.get("data"))[:10])
        except (AttributeError, ValueError):
            ignorados += 1
            continue
        if dia > hoje or dia.year < 2000:
            ignorados += 1
            continue
        for tipo, (lo, hi) in LIMITES.items():
            if tipo not in d:
                continue
            v = _num(d[tipo])
            if v is None or not lo <= v <= hi:
                ignorados += 1
                continue
            casas = sd.TIPOS[tipo][2]
            out.medidas[(dia, tipo)] = (round(v, casas + 1) if casas else round(v), FONTE)
    for t in treinos:
        if not isinstance(t, dict):
            ignorados += 1
            continue
        tid = str(t.get("id") or "").strip()[:36]
        try:
            inicio = datetime.fromisoformat(str(t.get("inicio")))
        except ValueError:
            ignorados += 1
            continue
        if not tid:
            ignorados += 1
            continue
        if inicio.tzinfo:                              # guarda-se a hora local de Lisboa, como no Takeout
            inicio = inicio.astimezone(sd.LISBOA).replace(tzinfo=None)
        if inicio.date() > hoje:
            ignorados += 1
            continue

        def n(k, lo=0.0, hi=1e6):
            v = _num(t.get(k))
            return v if v is not None and lo <= v <= hi else None
        passos = n("passos", 0, 200_000)
        out.treinos.append(sd.Treino(
            externo_id=f"hc:{tid}", inicio=inicio, duracao_min=n("duracao_min", 0, 2_880),
            atividade=(str(t.get("atividade") or "Exercício").strip() or "Exercício")[:60],
            calorias=n("calorias", 0, 20_000), passos=int(passos) if passos is not None else None,
            distancia_km=n("distancia_km", 0, 400), fc_media=n("fc_media", 20, 250), fc_maxima=n("fc_maxima", 20, 250)))
    return out, ignorados
