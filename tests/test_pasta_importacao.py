"""Extratos na pasta partilhada da NAS: leitura, deteção da conta, validação (importa e move) e rejeição.
Ficheiros fictícios gerados nos testes (nunca extratos reais no git)."""
import re
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import inicio, pasta_importacao as pi
from app.main import create_app
from app.models import Entidade, ImportacaoPendente, Movimento
from tests.test_app import PW, _csrf, _post
from tests.test_importacao import SANT, xls_santander, xlsx_activobank


@pytest.fixture
def pasta(tmp_path):
    raiz = tmp_path / "extratos"
    (raiz / "Santander").mkdir(parents=True)
    (raiz / "ActivoBank").mkdir()
    return raiz


@pytest.fixture
def app(tmp_path, pasta, monkeypatch):
    monkeypatch.setenv("GESTOR_IMPORT_DIR", str(pasta))
    return create_app(tmp_path / "dados", backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Santander ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _post(c, "/contas/nova?entidade=1", nome="Santander poupança", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _post(c, "/contas/nova?entidade=1", nome="ActivoBank", tipo="ORDEM", moeda="EUR", instituicao_id="2")
    # o extrato antigo já importado à mão na 1.ª conta (é por ele que a conta do novo é reconhecida)
    r = c.get("/importar")
    previa = c.post("/importar", data={"csrf": _csrf(r), "conta_id": "1"}, files={"ficheiro": ("antigo.xls", xls_santander(SANT))})
    sha = re.search(r'name="sha" value="([0-9a-f]+)"', previa.text).group(1)
    c.post("/importar/confirmar", data={"csrf": _csrf(previa), "conta_id": "1", "sha": sha, "nome": "antigo.xls"})
    return c


def _n_movs(app):
    with app.state.SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(Movimento))


def test_ler_validar_mover_e_rejeitar(admin, app, pasta):
    novo = [("14-10-2026", "Farmacia", -10.0, 990.0)] + SANT
    (pasta / "Santander" / "outubro.xls").write_bytes(xls_santander(novo))
    (pasta / "ActivoBank" / "mov.xlsx").write_bytes(xlsx_activobank([(datetime(2026, 10, 5), "Compra", -12.5, 87.5)]))
    (pasta / "Santander" / "lixo.csv").write_bytes(b"a;b\n1;2\n")
    (pasta / "Santander" / "importados").mkdir()
    (pasta / "Santander" / "importados" / "velho.xls").write_bytes(xls_santander(SANT))   # já tratado: ignorado
    antes = _n_movs(app)

    t = admin.get("/admin/tarefas")
    assert "Extratos da pasta partilhada" in t.text and "Ler a pasta agora" in t.text
    p = admin.post("/admin/pasta/ler", data={"csrf": _csrf(t)}).text
    assert "Pasta lida: 2 extratos novos por validar, 1 com erro." in p
    assert _n_movs(app) == antes                                     # nada importado sem validar
    with app.state.SessionLocal() as db:
        pend = {x.ficheiro: x for x in db.scalars(select(ImportacaoPendente))}
        sant, ab, lixo = pend["outubro.xls"], pend["mov.xlsx"], pend["lixo.csv"]
        assert (sant.conta_id, sant.n_lidos, sant.n_novos) == (1, 5, 1) and "já existente" in sant.deteccao
        assert ab.conta_id == 3 and "única conta" in ab.deteccao
        assert lixo.estado == "erro" and "não reconhecido" in lixo.erro
        assert "velho.xls" not in pend
        proximos, _ = inicio.agenda(db, [db.get(Entidade, 1)], app.state.prefs, date.today())
        assert any(i.titulo == "Validar 2 extratos" for i in proximos)
        sid, aid = sant.id, ab.id
    # ler outra vez não repete
    p = admin.post("/admin/pasta/ler", data={"csrf": _csrf(admin.get("/admin/tarefas"))}).text
    assert "0 extratos novos por validar, 3 já conhecidos" in p

    # validar: pré-visualização com a conta detetada, importa o novo e move o ficheiro
    v = admin.get(f"/admin/pasta/{sid}")
    assert v.status_code == 200 and "Validar e importar 1 movimentos" in v.text and "Extrato da pasta partilhada" in v.text
    sha = re.search(r'name="sha" value="([0-9a-f]+)"', v.text).group(1)
    r = admin.post("/importar/confirmar", follow_redirects=False,
                   data={"csrf": _csrf(v), "conta_id": "1", "sha": sha, "nome": "outubro.xls", "pendente": str(sid)})
    assert r.headers["location"] == "/admin/tarefas#pasta"
    assert "movido para importados/" in admin.get("/admin/tarefas").text
    assert _n_movs(app) == antes + 1
    hoje = date.today().isoformat()
    assert not (pasta / "Santander" / "outubro.xls").exists()
    assert (pasta / "Santander" / "importados" / f"{hoje}_outubro.xls").exists()
    with app.state.SessionLocal() as db:
        s = db.get(ImportacaoPendente, sid)
        assert s.estado == "validada" and s.importacao_id and s.caminho == f"Santander/importados/{hoje}_outubro.xls"
    assert "já não está por validar" in admin.get(f"/admin/pasta/{sid}", follow_redirects=True).text

    # escolher outra conta na pré-visualização
    v = admin.get(f"/admin/pasta/{aid}?conta=2")
    assert "Santander poupança" in v.text and 'selected>Família · Santander poupança' in v.text
    # rejeitar: fica na pasta, não volta a ser proposto
    admin.post(f"/admin/pasta/{aid}/rejeitar", data={"csrf": _csrf(admin.get("/admin/tarefas"))})
    assert (pasta / "ActivoBank" / "mov.xlsx").exists()
    with app.state.SessionLocal() as db:
        assert db.get(ImportacaoPendente, aid).estado == "rejeitada"
    admin.post("/admin/pasta/ler", data={"csrf": _csrf(admin.get("/admin/tarefas"))})
    with app.state.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ImportacaoPendente)) == 3


def test_intervalo_e_pasta_nao_configurada(tmp_path, monkeypatch):
    monkeypatch.delenv("GESTOR_IMPORT_DIR", raising=False)
    app = create_app(tmp_path, backups_automaticos=False)
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    t = c.get("/admin/tarefas").text
    assert "Pasta partilhada não configurada" in t and "Ler a pasta agora" not in t
    assert pi.estado_pasta(tmp_path / "nao-existe")[0] is False
    c.post("/admin/pasta/intervalo", data={"csrf": _csrf(c.get("/admin/tarefas")), "minutos": "180"})
    assert pi.intervalo_minutos(app.state.prefs) == 180
    c.post("/admin/pasta/intervalo", data={"csrf": _csrf(c.get("/admin/tarefas")), "minutos": "7"})
    assert pi.intervalo_minutos(app.state.prefs) == 180
