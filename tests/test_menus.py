"""Reorganização dos menus (v0.58): grupos do menu lateral, separadores das secções, contador «por validar» e
endereços antigos que continuam a funcionar."""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import AbastecimentoPendente, Bem
from tests.test_app import PW, _post


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    return c


def test_menu_em_grupos_e_contador(admin, app):
    p = admin.get("/inicio").text
    for grupo in ("Visão geral", "Dinheiro", "Património", "Dados", "Configuração"):
        assert f'<p class="menu-grupo">{grupo}</p>' in p
    for rotulo in ("Início", "Relatórios", "Movimentos", "Contas", "Subscrições", "Previsões", "Viagens", "Importar",
                   "Categorias e regras", "Administração"):
        assert f"<span>{rotulo}</span>" in p
    assert "<span>Painel</span>" not in p and "<span>Receitas previstas</span>" not in p and "Tarefas do mês</span>" not in p
    assert 'class="contador"' not in p
    with app.state.SessionLocal() as db:                       # um abastecimento por validar → contador em Importar
        db.add(Bem(entidade_id=1, tipo="carro", nome="Golf"))
        db.flush()
        from datetime import date
        db.add(AbastecimentoPendente(bem_id=1, data=date.today(), litros=40.0))
        db.commit()
    p = admin.get("/inicio").text
    assert '<span>Importar</span><span class="contador" title="1 por validar">1</span>' in p
    v = admin.get("/importar/validar").text
    assert "1</strong> abastecimento por validar" in v and "Extratos da pasta partilhada" in v
    assert 'Por validar <span class="contador">1</span>' in v


@pytest.mark.parametrize("url, separador", [
    ("/inicio", "Contas e fluxo"), ("/", "Resumo"),
    ("/movimentos", "Pareáveis ↔"), ("/movimentos-pares", "Lista"),
    ("/contas", "Cartões de débito"), ("/contas/cartoes", "Empréstimos"), ("/contas/emprestimos", "Saldos"),
    ("/patrimonio", "Recheio"), ("/patrimonio/recheio", "Via Verde"), ("/patrimonio/via-verde", "Abastecimentos"),
    ("/patrimonio/abastecimentos", "Bens"),
    ("/importar", "Por validar"), ("/importar/validar", "Histórico"), ("/importar/historico", "Carregar ficheiro"),
    ("/importar/tarefas", "Tarefas do mês"),
    ("/admin/utilizadores", "Preferências"), ("/admin/preferencias", "Backups e atividade"), ("/admin/backups", "Fuelio"),
    ("/admin/fuelio", "Cobertura"), ("/contas/cobertura", "Importadores"),
])
def test_separadores(admin, url, separador):
    r = admin.get(url)
    assert r.status_code == 200 and 'class="abas-pagina"' in r.text and separador in r.text, url


def test_enderecos_antigos(admin):
    for antigo, novo in (("/admin/tarefas", "/importar/validar"), ("/admin/recheio", "/patrimonio/recheio")):
        assert admin.get(antigo, follow_redirects=False).headers["location"] == novo
    assert "Que categorias são recheio" in admin.get("/patrimonio/recheio").text
    assert "Fazer backup agora" in admin.get("/admin/backups").text and "Atividade recente" in admin.get("/admin/backups").text
    assert "Guardar preferências" in admin.get("/admin/preferencias").text
    assert "Guardar preferências" not in admin.get("/admin/utilizadores").text
