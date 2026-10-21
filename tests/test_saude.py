"""Saúde: leitura da exportação «Google Health» do Takeout (fontes duplicadas, dias em Lisboa, sono repetido e sestas,
treinos), gravação idempotente, página com gráficos e permissões por entidade. Dados sintéticos com a estrutura do
ficheiro real — nenhum dado pessoal."""
import io
import zipfile
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import saude as sd
from app.main import create_app
from app.models import Auditoria, MedidaSaude, Permissao, PerfilSaude, TreinoSaude, Utilizador
from app.seguranca import hash_password
from tests.test_app import PW, _csrf, _post

R = "Takeout/Google Health/"
PA = R + "Physical Activity_GoogleData/"
HF = R + "Health Fitness Data_GoogleData/"


def _csv(cab, linhas):
    return "\n".join([cab] + [",".join(str(x) for x in l) for l in linhas]) + "\n"


def takeout(extra: dict | None = None) -> bytes:
    f = {
        # 10/10: o relógio e o Health Sync (cópia do relógio) contam os mesmos passos; o telemóvel conta menos.
        # 23:30Z de 10/10 ainda é 11/10 em Lisboa (UTC+1 no verão) → vai para o dia 11.
        PA + "Steps/steps_2026-10-01.csv": _csv("timestamp,steps,data source", [
            ("2026-10-10T08:00:00Z", 3000, "Garmin Connect Health Connect"), ("2026-10-10T12:00:00Z", 2000, "Garmin Connect Health Connect"),
            ("2026-10-10T08:00:00Z", 3000, "Health Sync Health Connect"), ("2026-10-10T12:00:00Z", 2000, "Health Sync Health Connect"),
            ("2026-10-10T09:00:00Z", 4100, "Phone Health Connect"),
            ("2026-10-10T23:30:00Z", 700, "Garmin Connect Health Connect"),
            ("2026-10-11T10:00:00Z", 1000, "Phone Health Connect")]),
        PA + "Distance/distance_2026-10-01.csv": _csv("timestamp,distance,data source", [
            ("2026-10-10T08:00:00Z", 2500.0, "Garmin Connect Health Connect"), ("2026-10-10T08:00:00Z", 2500.0, "Fit Health Connect")]),
        PA + "Weight/weight.csv": _csv("timestamp,weight grams,data source", [
            ("2026-10-10T07:00:00Z", 92000, "Fitdays Health Connect"), ("2026-10-10T19:00:00Z", 91500, "Fitdays Health Connect"),
            ("2026-09-01T07:00:00Z", 93500, "Fitdays Health Connect")]),
        PA + "Body Fat/body_fat_2026-10-01.csv": _csv("timestamp,body fat percentage,data source", [
            ("2026-10-10T19:00:00Z", 27.53, "Fitdays Health Connect")]),
        PA + "Height/height.csv": _csv("timestamp,height millimeters,data source", [
            ("2019-02-14T00:00:00Z", 1790, "Google Health App"), ("2026-06-13T09:29:53Z", 1800, "Google Health App")]),
        PA + "Daily Resting Heart Rate/daily_resting_heart_rate.csv": _csv("timestamp,beats per minute,data source", [
            ("2026-10-10T07:00:00Z", 54.0, "Garmin Connect Health Connect"), ("2026-10-10T07:00:00Z", 56.0, "Health Sync Health Connect")]),
        PA + "Daily Oxygen Saturation/daily_oxygen_saturation.csv": _csv(
            "timestamp,average percentage,lower bound percentage,upper bound percentage,baseline percentage,standard deviation percentage,data source",
            [("2026-10-09T23:00:00Z", 95.4, 90.1, 99.0, 0.0, 1.0, "Health Sync Health Connect")]),        # meia-noite de 10/10 em Lisboa
        PA + "Daily Heart Rate Variability/daily_heart_rate_variability.csv": _csv(
            "timestamp,average heart rate variability milliseconds,non rem heart rate beats per minute,entropy,deep sleep root mean square of successive differences milliseconds,data source",
            [("2026-10-10T05:00:00Z", 41.2, 50.0, 2.1, 44.0, "Health Sync Health Connect")]),
        PA + "Daily Respiratory Rate/daily_respiratory_rate.csv": _csv("timestamp,breaths per minute,data source", [
            ("2026-10-10T05:00:00Z", 16.4, "Health Sync Health Connect")]),
        # ficheiros enormes que não interessam: não devem ser lidos
        PA + "Calories/calories_2026-10-01.csv": "isto não é um csv válido para a app\n",
        HF + "UserSleeps_2026-10-01.csv": _csv(
            "sleep_id,sleep_type,minutes_in_sleep_period,minutes_after_wake_up,minutes_to_fall_asleep,minutes_asleep,minutes_awake,"
            "minutes_longest_awakening,minutes_to_persistent_sleep,start_utc_offset,sleep_start,end_utc_offset,sleep_end,data_source,"
            "algorithm_version,sleep_created,sleep_last_updated", [
                # a mesma noite duas vezes (origens diferentes) e uma sesta à tarde: fica a noite (S1)
                ("S1", "STAGES", 450, 0, 5, 400, 50, 9, 5, "+01:00", "2026-10-09T22:30:00Z", "+01:00", "2026-10-10T06:00:00Z", "PASSIVELY_MEASURED", "x", "", ""),
                ("S2", "STAGES", 450, 0, 5, 400, 50, 9, 5, "+01:00", "2026-10-09T22:30:00Z", "+01:00", "2026-10-10T06:00:00Z", "UNKNOWN", "x", "", ""),
                ("S3", "STAGES", 40, 0, 5, 35, 5, 2, 5, "+01:00", "2026-10-10T14:00:00Z", "+01:00", "2026-10-10T14:40:00Z", "UNKNOWN", "x", "", "")]),
        HF + "UserSleepStages_2026-10-01.csv": _csv(
            "sleep_id,sleep_stage_id,sleep_stage_type,start_utc_offset,sleep_stage_start,end_utc_offset,sleep_stage_end,data_source,algorithm_version,sleep_stage_created,sleep_stage_last_updated", [
                ("S1", "1", "LIGHT", "+01:00", "2026-10-09T22:30:00Z", "+01:00", "2026-10-10T01:30:00Z", "x", "x", "", ""),
                ("S1", "2", "DEEP", "+01:00", "2026-10-10T01:30:00Z", "+01:00", "2026-10-10T02:40:00Z", "x", "x", "", ""),
                ("S1", "3", "REM", "+01:00", "2026-10-10T02:40:00Z", "+01:00", "2026-10-10T03:30:00Z", "x", "x", "", ""),
                ("S1", "4", "AWAKE", "+01:00", "2026-10-10T03:30:00Z", "+01:00", "2026-10-10T03:45:00Z", "x", "x", "", ""),
                ("S2", "5", "LIGHT", "+01:00", "2026-10-09T22:30:00Z", "+01:00", "2026-10-10T06:00:00Z", "x", "x", "", "")]),
        HF + "UserExercises_2026-10-01.csv": _csv(
            "exercise_id,exercise_start,exercise_end,utc_offset,activity_name,log_type,tracker_total_calories,tracker_total_steps,"
            "tracker_total_distance_mm,tracker_avg_heart_rate,tracker_peak_heart_rate,manually_logged_total_calories,"
            "manually_logged_total_steps,manually_logged_total_distance_mm,deletion_reason", [
                ("E1", "2026-10-11T07:00:00Z", "2026-10-11T08:00:00Z", "+01:00", "Bike", "UNSPECIFIED", 610, 0, 25300000, 132, 160, "", "", "", "UNSPECIFIED"),
                ("E2", "2019-02-14T13:05:38Z", "2019-02-14T13:35:38Z", "+00:00", "Outdoor Walk", "MANUAL", "", "", "", "", "", 99, 3000, 2100000, "UNSPECIFIED"),
                ("E1", "2026-10-11T07:00:00Z", "2026-10-11T08:00:00Z", "+01:00", "Bike", "UNSPECIFIED", 610, 0, 25300000, 132, 160, "", "", "", "UNSPECIFIED")]),
        R + "Paired Devices/devices.csv": "x\n",
    }
    f.update(extra or {})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for nome, conteudo in f.items():
            z.writestr(nome, conteudo)
    return buf.getvalue()


def test_leitura():
    L = sd.ler_takeout(io.BytesIO(takeout()))
    m = {k: v[0] for k, v in L.medidas.items()}
    d10, d11 = date(2026, 10, 10), date(2026, 10, 11)
    assert m[(d10, "passos")] == 5000                 # a fonte maior (Garmin 5000 = Health Sync), não a soma (14 100)
    assert m[(d11, "passos")] == 1000                 # 23:30Z de 10/10 é 11/10 em Lisboa: 700 do relógio < 1000 do telemóvel
    assert L.medidas[(d10, "passos")][1] in ("Garmin Connect Health Connect", "Health Sync Health Connect")
    assert m[(d10, "distancia_km")] == 2.5
    assert m[(d10, "peso_kg")] == 91.5 and m[(date(2026, 9, 1), "peso_kg")] == 93.5   # a última medição do dia
    assert m[(d10, "gordura_pct")] == 27.53 and L.altura_cm == 180.0
    assert m[(d10, "fc_repouso")] == 55.0             # média das duas fontes
    assert m[(d10, "spo2")] == 95.4 and m[(d10, "vfc_ms")] == 41.2 and m[(d10, "resp_rpm")] == 16.4
    # sono: a noite (não a sesta), uma vez, com as fases do registo escolhido
    assert m[(d10, "sono_min")] == 400 and m[(d10, "sono_cama_min")] == 450
    assert (m[(d10, "sono_leve_min")], m[(d10, "sono_profundo_min")], m[(d10, "sono_rem_min")], m[(d10, "sono_acordado_min")]) == (180, 70, 50, 15)
    # treinos: um por id; os manuais usam os valores registados à mão
    t = {x.externo_id: x for x in L.treinos}
    assert len(L.treinos) == 2
    assert (t["E1"].inicio, t["E1"].duracao_min, t["E1"].distancia_km, t["E1"].calorias, t["E1"].fc_media) == \
        (datetime(2026, 10, 11, 8, 0), 60.0, 25.3, 610, 132)
    assert t["E2"].manual and t["E2"].passos == 3000 and t["E2"].distancia_km == 2.1


def test_nao_e_takeout():
    with pytest.raises(sd.ErroSaude, match="Google Health"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("Takeout/Drive/x.txt", "x")
        sd.ler_takeout(io.BytesIO(buf.getvalue()))
    with pytest.raises(sd.ErroSaude, match=r"\.zip"):
        sd.ler_takeout(io.BytesIO(b"nao e um zip"))


# ------------------------------------------------------------------ app
@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    return c


def _importar(c, pid, conteudo):
    return c.post(f"/saude/perfis/{pid}/importar", data={"csrf": _csrf(c.get("/saude?entidade=1"))},
                  files={"ficheiro": ("takeout-20261021.zip", conteudo, "application/zip")}, follow_redirects=True)


def test_criar_importar_e_reimportar(admin, app):
    assert "Perfil de saúde" in admin.get("/inicio").text                     # menu
    r = admin.get("/saude?entidade=1")
    assert "Google Health" in r.text and "Novo perfil" in r.text
    r = _post(admin, "/saude?entidade=1", "/saude/perfis", entidade="1", nome="Sérgio", utilizador_id="1")
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        pid = db.scalar(select(PerfilSaude.id))
    r = _importar(admin, pid, takeout())
    assert "Importados" in r.text and "2 treinos novos" in r.text
    with app.state.SessionLocal() as db:
        n = db.scalar(select(func.count()).select_from(MedidaSaude))
        p = db.get(PerfilSaude, pid)
        assert n > 15 and p.altura_cm == 180.0 and p.importado_ate == date(2026, 10, 11)
        aud = db.scalar(select(Auditoria).where(Auditoria.acao == "saude_importacao")).detalhe
        assert "dias" in aud and "91" not in aud and "5000" not in aud                # só contagens, nada de valores
    # reimportar o mesmo: nada novo; com um dia novo: só esse
    r = _importar(admin, pid, takeout())
    assert "0 valores novos, 0 atualizados, 0 treinos novos" in r.text
    novo = takeout({PA + "Weight/weight.csv": _csv("timestamp,weight grams,data source", [
        ("2026-10-10T19:00:00Z", 91500, "Fitdays Health Connect"), ("2026-10-12T07:00:00Z", 91000, "Fitdays Health Connect"),
        ("2019-02-14T08:00:00Z", 95000, "Google Health App")])})
    r = _importar(admin, pid, novo)
    assert "2 valores novos" in r.text
    with app.state.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(TreinoSaude)) == 2
    # a página: KPIs e gráficos
    t = admin.get(f"/saude?entidade=1&perfil={pid}&periodo=30").text
    assert "91,0 kg" in t and "IMC 28,1" in t and 'data-saude="sono"' in t and 'data-saude="passos"' in t
    assert 'id="dados-saude"' in t and "Bike" in t and "25,30 km" in t
    t = admin.get(f"/saude?entidade=1&perfil={pid}&periodo=tudo").text
    assert "Médias por mês" in t                                                     # 2019 → 2026: agrupado por mês


def test_ficheiro_errado(admin, app):
    _post(admin, "/saude?entidade=1", "/saude/perfis", entidade="1", nome="Ana")
    r = _importar(admin, 1, b"isto nao e um zip")
    assert "não é um .zip do Google Takeout" in r.text


def test_permissoes_por_entidade(admin, app):
    _post(admin, "/saude?entidade=1", "/saude/perfis", entidade="1", nome="Sérgio")
    _importar(admin, 1, takeout())
    with app.state.SessionLocal() as db:
        db.add_all([Utilizador(nome="Ana", login="ana", password_hash=hash_password(PW)),
                    Utilizador(nome="Rui", login="rui", password_hash=hash_password(PW))])
        db.flush()
        db.add_all([Permissao(utilizador_id=2, entidade_id=1, papel="ver"), Permissao(utilizador_id=3, entidade_id=2, papel="editar")])
        db.commit()
    ana, rui = TestClient(app), TestClient(app)
    _post(ana, "/login", login="ana", password=PW)
    _post(rui, "/login", login="rui", password=PW)
    # quem vê a Família vê o perfil, mas não importa nem gere perfis
    t = ana.get("/saude?entidade=1").text
    assert "Sérgio" in t and 'data-saude="peso"' in t and "Novo perfil" not in t and "/saude/perfis/1/importar" not in t
    r = ana.post("/saude/perfis/1/importar", data={"csrf": _csrf(ana.get("/saude"))},
                 files={"ficheiro": ("t.zip", takeout(), "application/zip")})
    assert r.status_code == 403
    # quem não tem acesso à Família não sabe que existe
    assert rui.get("/saude?entidade=1").status_code == 404
    t = rui.get("/saude?entidade=2").text
    assert "Sérgio" not in t and ">Ana<" not in t and ">Rui<" in t          # só as contas com acesso à Mãe
    assert rui.post("/saude/perfis/1/apagar", data={"csrf": _csrf(rui.get("/saude"))}).status_code == 404


def test_modulo_desligado_e_apagar(admin, app):
    from app.models import Entidade
    _post(admin, "/saude?entidade=1", "/saude/perfis", entidade="1", nome="Sérgio")
    _importar(admin, 1, takeout())
    r = _post(admin, "/saude?entidade=1", "/saude/perfis/1/apagar")
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(MedidaSaude)) == 0
        db.get(Entidade, 1).modulos = "imoveis,viagens"
        db.commit()
    assert "não usa saúde" in admin.get("/saude?entidade=1").text
