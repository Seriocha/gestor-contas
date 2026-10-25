"""Saúde pelo telemóvel (app Android «Gestor Saúde» → Health Connect): emparelhamento por código, envio por token,
validação do que chega, reenvio sem duplicar, permissões e desligar. Dados sintéticos."""
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import saude_telemovel as st
from app.models import Auditoria, DispositivoSaude, MedidaSaude, Permissao, PerfilSaude, TreinoSaude, Utilizador
from app.seguranca import hash_password
from tests.test_app import PW, _csrf, _ids_entidades, _post, admin, app  # noqa: F401 (fixtures)

DIA = {"data": "2026-10-20", "passos": 8123, "distancia_km": 6.12, "peso_kg": 91.46, "sono_min": 412, "sono_profundo_min": 80,
       "fc_repouso": 55}
TREINO = {"id": "0f5e9c1a-1111-2222-3333-444455556666", "inicio": "2026-10-20T08:00:00+01:00", "duracao_min": 42.5,
          "atividade": "Corrida", "calorias": 410, "passos": 6000, "distancia_km": 6.0, "fc_media": 148, "fc_maxima": 171}


def test_converter_valida():
    leitura, ign = st.converter({"versao": 1, "dias": [DIA, {"data": "2030-01-01", "passos": 5},
                                                       {"data": "2026-10-21", "passos": -3, "peso_kg": "abc", "spo2": 97.5,
                                                        "x": 1}],
                                 "treinos": [TREINO, {"id": "", "inicio": "2026-10-20T08:00:00"}]}, hoje=date(2026, 10, 25))
    assert leitura.medidas[(date(2026, 10, 20), "passos")] == (8123, "Health Connect")
    assert leitura.medidas[(date(2026, 10, 20), "peso_kg")][0] == 91.46
    assert leitura.medidas[(date(2026, 10, 21), "spo2")][0] == 97.5
    assert (date(2026, 10, 21), "passos") not in leitura.medidas and ign == 4      # dia futuro, passos <0, peso texto, treino sem id
    t = leitura.treinos[0]
    assert t.externo_id == "hc:" + TREINO["id"] and t.inicio.hour == 8 and t.passos == 6000
    with pytest.raises(st.ErroEnvio):
        st.converter({"versao": 2})


@pytest.fixture
def perfil(admin, app):  # noqa: F811
    fam = _ids_entidades(app)["Família"]
    _post(admin, "/saude", "/saude/perfis", nome="Sérgio", entidade=str(fam), utilizador_id="1")
    with app.state.SessionLocal() as db:
        return db.scalar(select(PerfilSaude)).id


def _emparelhar(app, admin, perfil):  # noqa: F811
    tel = TestClient(app)
    r = tel.post("/api/saude/emparelhar").json()
    assert re.fullmatch(r"\d{6}", r["codigo"]) and r["aprovar"].endswith(f"/saude/telemovel?codigo={r['codigo']}")
    assert tel.post("/api/saude/emparelhar/estado", json={"segredo": r["segredo"]}).json()["estado"] == "espera"
    res = _post(admin, f"/saude/telemovel?codigo={r['codigo']}", "/saude/telemovel/aprovar", codigo=r["codigo"],
                perfil_id=str(perfil), nome="S24")
    assert res.status_code == 303, res.text
    e = tel.post("/api/saude/emparelhar/estado", json={"segredo": r["segredo"]}).json()
    assert e["estado"] == "aprovado" and e["perfil"] == "Sérgio"
    assert tel.post("/api/saude/emparelhar/estado", json={"segredo": r["segredo"]}).json()["estado"] == "expirado"
    return tel, {"Authorization": f"Bearer {e['token']}"}


def test_emparelhar_enviar_e_reenviar(admin, app, perfil):  # noqa: F811
    tel, cab = _emparelhar(app, admin, perfil)
    assert tel.get("/api/saude/estado", headers=cab).json() == {"perfil": "Sérgio", "nome": "S24", "ultimo_dia": None,
                                                                "ultimo_envio": None}
    r = tel.post("/api/saude/enviar", headers=cab, json={"versao": 1, "dias": [DIA], "treinos": [TREINO]}).json()
    assert r["ok"] and r["dias"] == 1 and r["novos"] == 6 and r["treinos"] == 1
    # reenviar o mesmo dia com um valor corrigido: atualiza, não duplica
    r = tel.post("/api/saude/enviar", headers=cab, json={"versao": 1, "dias": [{**DIA, "passos": 9000}], "treinos": [TREINO]}).json()
    assert r["novos"] == 0 and r["atualizados"] == 1 and r["treinos"] == 0
    with app.state.SessionLocal() as db:
        assert db.scalar(select(MedidaSaude.valor).where(MedidaSaude.tipo == "passos")) == 9000
        assert len(db.scalars(select(TreinoSaude)).all()) == 1
        d = db.scalar(select(DispositivoSaude))
        assert d.ultimo_envio and "1 dias" in d.ultimo_resumo and len(d.token_hash) == 64
        aud = db.scalar(select(Auditoria.detalhe).where(Auditoria.detalhe.like("envio de%")))
        assert "9000" not in aud and "Sérgio" in aud                       # só contagens nos registos
    assert tel.get("/api/saude/estado", headers=cab).json()["ultimo_dia"] == "2026-10-20"
    assert "S24" in admin.get("/saude/telemovel").text and "Saúde › Telemóvel" in admin.get("/saude").text
    # sem token / token errado / formato errado
    assert tel.post("/api/saude/enviar", json={"versao": 1}).status_code == 401
    assert tel.post("/api/saude/enviar", headers={"Authorization": "Bearer x"}, json={"versao": 1}).status_code == 401
    assert tel.post("/api/saude/enviar", headers=cab, json={"versao": 9}).status_code == 400
    assert tel.post("/api/saude/enviar", headers=cab, content=b"nao json").status_code == 400


def test_codigo_errado_e_bloqueio(admin, app, perfil):  # noqa: F811
    tel = TestClient(app)
    codigo = tel.post("/api/saude/emparelhar").json()["codigo"]
    errado = "000000" if codigo != "000000" else "111111"
    for _ in range(5):
        assert _post(admin, "/saude/telemovel", "/saude/telemovel/aprovar", codigo=errado, perfil_id=str(perfil)).status_code == 400
    assert _post(admin, "/saude/telemovel", "/saude/telemovel/aprovar", codigo=codigo, perfil_id=str(perfil)).status_code == 429


def test_desligar_e_perder_acesso(admin, app, perfil):  # noqa: F811
    tel, cab = _emparelhar(app, admin, perfil)
    # outra pessoa só com «ver» na entidade não vê o perfil nesta página nem consegue desligar
    with app.state.SessionLocal() as db:
        ana = Utilizador(nome="Ana", login="ana", password_hash=hash_password(PW))
        db.add(ana)
        db.flush()
        db.add(Permissao(utilizador_id=ana.id, entidade_id=_ids_entidades(app)["Família"], papel="ver"))
        db.commit()
        did = db.scalar(select(DispositivoSaude.id))
    c2 = TestClient(app)
    _post(c2, "/login", login="ana", password=PW)
    r = c2.get("/saude/telemovel")
    assert "S24" not in r.text
    assert c2.post(f"/saude/telemovel/{did}/desligar", data={"csrf": _csrf(r)}).status_code == 404
    # quem aprovou muda a password: o telemóvel deixa de poder enviar
    novo = "outra-password-boa"
    _post(admin, "/perfil", atual=PW, password=novo, password2=novo)
    assert tel.post("/api/saude/enviar", headers=cab, json={"versao": 1, "dias": [DIA]}).status_code == 401


def test_desligar(admin, app, perfil):  # noqa: F811
    tel, cab = _emparelhar(app, admin, perfil)
    with app.state.SessionLocal() as db:
        did = db.scalar(select(DispositivoSaude.id))
    r = admin.get("/saude/telemovel")
    admin.post(f"/saude/telemovel/{did}/desligar", data={"csrf": _csrf(r)})
    assert tel.get("/api/saude/estado", headers=cab).status_code == 401


# ------------------------------------------------------------------ todos os registos (paginados) e apagar no Gestor
def _povoar(app, pid, n_tel=60, n_take=10):
    from datetime import datetime, timedelta
    with app.state.SessionLocal() as db:
        for i in range(n_tel):
            db.add(MedidaSaude(perfil_id=pid, data=date(2026, 8, 1) + timedelta(days=i), tipo="passos", valor=1000 + i,
                               fonte="Health Connect"))
        for i in range(n_take):
            db.add(MedidaSaude(perfil_id=pid, data=date(2025, 1, 1) + timedelta(days=i), tipo="peso_kg", valor=90 + i / 10,
                               fonte="Fitdays Health Connect"))
        db.add(TreinoSaude(perfil_id=pid, externo_id="hc:abc", inicio=datetime(2026, 9, 1, 8), atividade="Corrida"))
        db.add(TreinoSaude(perfil_id=pid, externo_id="999", inicio=datetime(2025, 3, 1, 8), atividade="Caminhada"))
        db.commit()


def test_registos_paginados_e_filtros(admin, app, perfil):  # noqa: F811
    _povoar(app, perfil)
    r = admin.get(f"/saude/perfis/{perfil}/registos")
    assert "70 registos · página 1 de 2" in r.text and "seguinte" in r.text
    r = admin.get(f"/saude/perfis/{perfil}/registos?pagina=2")
    assert "página 2 de 2" in r.text and "anterior" in r.text
    r = admin.get(f"/saude/perfis/{perfil}/registos?origem=takeout")
    assert "10 registos" in r.text and "Fitdays" in r.text
    r = admin.get(f"/saude/perfis/{perfil}/registos?tipo=passos&de=2026-09-01&ate=2026-09-10")
    assert "10 registos" in r.text
    r = admin.get(f"/saude/perfis/{perfil}/registos?vista=treinos&origem=telemovel")
    assert "1 registo" in r.text and "Corrida" in r.text and "Caminhada" not in r.text
    assert "todos os registos" in admin.get("/saude").text


def test_apagar_dados_so_no_gestor(admin, app, perfil):  # noqa: F811
    _povoar(app, perfil)
    r = admin.get(f"/saude/perfis/{perfil}/registos")
    assert "Apagar dados de Sérgio" in r.text
    # sem a confirmação certa não apaga
    r = admin.post(f"/saude/perfis/{perfil}/apagar-dados", data={"csrf": _csrf(r), "origem": "telemovel", "confirmar": "x"},
                   follow_redirects=True)
    assert "escreve o nome do perfil" in r.text
    # só o que veio do telemóvel, num intervalo
    admin.post(f"/saude/perfis/{perfil}/apagar-dados", data={"csrf": _csrf(r), "origem": "telemovel", "de": "2026-08-01",
                                                             "ate": "2026-08-10", "confirmar": "sérgio"})
    with app.state.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(MedidaSaude)) == 60
        assert db.scalar(select(func.count()).select_from(TreinoSaude)) == 2
        assert "telemóvel" in db.scalar(select(Auditoria.detalhe).where(Auditoria.acao == "saude_apagar"))
    # tudo
    r = admin.get(f"/saude/perfis/{perfil}/registos")
    r = admin.post(f"/saude/perfis/{perfil}/apagar-dados", data={"csrf": _csrf(r), "origem": "", "confirmar": "Sérgio"},
                   follow_redirects=True)
    assert "Apagados do Gestor 60 valores e 2 treinos" in r.text
    with app.state.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(MedidaSaude)) == 0
        assert db.get(PerfilSaude, perfil).importado_em is None


def test_apagar_dados_so_administradores(admin, app, perfil):  # noqa: F811
    _povoar(app, perfil)
    with app.state.SessionLocal() as db:
        ana = Utilizador(nome="Ana", login="ana", password_hash=hash_password(PW))
        db.add(ana)
        db.flush()
        db.add(Permissao(utilizador_id=ana.id, entidade_id=_ids_entidades(app)["Família"], papel="editar"))
        db.commit()
    c2 = TestClient(app)
    _post(c2, "/login", login="ana", password=PW)
    r = c2.get(f"/saude/perfis/{perfil}/registos")
    assert r.status_code == 200 and "Apagar dados" not in r.text
    assert c2.post(f"/saude/perfis/{perfil}/apagar-dados", data={"csrf": _csrf(r), "confirmar": "Sérgio"}).status_code == 403
