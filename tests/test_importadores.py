"""Estado dos importadores (Administração › Importadores): por omissão, a Revolut só entra pelo PDF consolidado."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import tarefas
from app.main import create_app
from app.parsers import ImportadorInativo, ler_extrato
from tests.test_app import PW, _csrf, _post
from tests.test_importacao import _importar

CSV = ("Tipo,Produto,Data de início,Data de Conclusão,Descrição,Montante,Comissão,Moeda,Estado,Saldo\n"
       "Pagamento com cartão,Atual,2026-03-28 12:00:00,2026-03-30 08:00:00,Coop,-45.29,0.00,EUR,CONCLUÍDA,954.26\n").encode()


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Revolut", tipo="CARTEIRA_DIGITAL", moeda="EUR", instituicao_id="5")
    return c


def _estado(admin, chave, ativo):
    f = admin.get("/admin/importadores")
    return admin.post("/admin/importadores/estado", data={"csrf": _csrf(f), "chave": chave, "ativo": ativo},
                      follow_redirects=True)


def test_csv_revolut_recusado_e_reativado(admin):
    f = admin.get("/importar")
    r = admin.post("/importar", data={"csrf": _csrf(f), "conta_id": "1"}, files={"ficheiro": ("revolut.csv", CSV)})
    assert r.status_code == 400 and "está inativo" in r.text and "Usa: Revolut (PDF)" in r.text
    assert "Importar 1 movimentos" in _importar_ativo(admin)


def _importar_ativo(admin):
    r = _estado(admin, "revolut", "1")
    assert "ativo" in r.text
    previa, _ = _importar(admin, "revolut.csv", CSV, conta_id="1")
    return previa.text


def test_pagina_e_estado(admin, app):
    r = admin.get("/admin/importadores")
    assert "Estado" in r.text and "Inativar" in r.text and "Ativar" in r.text and "inativo" in r.text
    r = _estado(admin, "santander", "0")
    assert app.state.prefs["importadores_inativos"] == "revolut,revolut_consolidado,santander"
    assert "Importador «Santander»" in r.text and "inativo" in r.text
    _estado(admin, "santander", "1")
    assert app.state.prefs["importadores_inativos"] == "revolut,revolut_consolidado"
    f = admin.get("/admin/importadores")
    assert admin.post("/admin/importadores/estado", data={"csrf": _csrf(f), "chave": "x", "ativo": "1"}).status_code == 404


def test_so_administradores(admin):
    _post(admin, "/admin/utilizadores/novo", nome="Ana", login="ana", password=PW, password2=PW)
    c = TestClient(admin.app)
    _post(c, "/login", login="ana", password=PW)
    assert c.get("/admin/importadores").status_code == 403


def test_tarefas_indicam_o_pdf(admin, app):
    with app.state.SessionLocal() as db:
        t = tarefas.do_ciclo(db, 1, date(2026, 10, 1))[0].tarefas[0]
        assert "PDF" in t.ficha["ficheiro"]
    _estado(admin, "revolut_pdf", "0")
    with app.state.SessionLocal() as db:
        t = tarefas.do_ciclo(db, 1, date(2026, 10, 1))[0].tarefas[0]
        assert t.ficha is None and "está inativo" in t.nota_manual


def test_leitura_direta():
    with pytest.raises(ImportadorInativo, match="está inativo"):
        ler_extrato("revolut.csv", CSV, {"revolut"})
    assert ler_extrato("revolut.csv", CSV).parser == "revolut"
