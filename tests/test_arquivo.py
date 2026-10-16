"""Arquivo organizado dos extratos importados e pasta _Entrada (importar sem upload)."""
import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import arquivo
from app.main import create_app
from app.models import Importacao
from tests.test_app import PW, _csrf, _post
from tests.test_importacao import SANT, _importar, xls_santander

CAMINHO = "Família/Santander/Santander ordem/2026/2026-10-10_a_2026-10-13_Santander ordem.xls"


def test_nomes_e_caminhos():
    assert arquivo.nome_seguro('Conta: "A/B" <x>?.') == "Conta- -A-B- -x--"
    rel = arquivo.caminho_relativo("Família", "Santander", "Ordem", date(2026, 9, 1), date(2026, 9, 30), ".XLS")
    assert rel.as_posix() == "Família/Santander/Ordem/2026/2026-09-01_a_2026-09-30_Ordem.xls"
    assert arquivo.caminho_relativo("F", "B", "C", None, None, ".exe").suffix == ""


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Santander ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1",
          iban="PT50 0018 0000 1234 5678 9012 3")
    _post(c, "/contas/nova?entidade=1", nome="Outra Santander", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    return c


def test_importacao_fica_arquivada(admin, app, tmp_path):
    conteudo = xls_santander(SANT)
    _, r = _importar(admin, "descarga (7).xls", conteudo)
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        imp = db.query(Importacao).one()
        assert imp.arquivo == CAMINHO
    assert (tmp_path / "arquivo" / CAMINHO).read_bytes() == conteudo
    pag = admin.get("/importar").text
    assert f'href="/importar/{imp.id}/ficheiro"' in pag and "2026-10-10_a_2026-10-13_Santander ordem.xls" in pag
    d = admin.get(f"/importar/{imp.id}/ficheiro")
    assert d.status_code == 200 and d.content == conteudo
    # outro ficheiro diferente com o mesmo período não apaga o primeiro
    _importar(admin, "outro.xls", xls_santander(SANT[:3] + [("10-10-2026", "Vencimento", 1500.0, 1029.20)] + []), conta_id="1")
    assert (tmp_path / "arquivo" / CAMINHO).read_bytes() == conteudo


def test_pasta_entrada(admin, app, tmp_path):
    entrada = tmp_path / "arquivo" / "_Entrada"
    entrada.mkdir(parents=True, exist_ok=True)
    (entrada / "descarga (9).xls").write_bytes(xls_santander(SANT))
    (entrada / "notas.txt").write_text("isto não é um extrato")
    (entrada / "imagem.png").write_bytes(b"x")                      # extensão ignorada
    pag = admin.get("/importar").text
    assert "descarga (9).xls" in pag and "Santander · 4 mov." in pag and "imagem.png" not in pag
    assert "formato não reconhecido" in pag
    bloco = re.search(r'value="descarga \(9\)\.xls">.*?</form>', pag, re.S).group(0)
    assert bloco.count(" selected>") == 1                           # há sempre uma conta Santander sugerida

    f = admin.get("/importar")
    previa = admin.post("/importar", data={"csrf": _csrf(f), "conta_id": "1", "entrada": "descarga (9).xls"})
    assert previa.status_code == 200 and "Importar 4 movimentos" in previa.text
    assert 'name="entrada" value="descarga (9).xls"' in previa.text
    sha = re.search(r'name="sha" value="([0-9a-f]+)"', previa.text).group(1)
    r = admin.post("/importar/confirmar", data={"csrf": _csrf(previa), "conta_id": "1", "sha": sha,
                                                "nome": "descarga (9).xls", "entrada": "descarga (9).xls"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert not (entrada / "descarga (9).xls").exists() and (tmp_path / "arquivo" / CAMINHO).exists()
    assert "retirado da pasta _Entrada" in admin.get("/contas/1/movimentos").text

    # sem «../» nem ficheiros fora da _Entrada
    f = admin.get("/importar")
    for nome in ("../gestor.db", "..\\gestor.db", "nao-existe.xls"):
        r = admin.post("/importar", data={"csrf": _csrf(f), "conta_id": "1", "entrada": nome})
        assert r.status_code == 400 and "já não está na pasta _Entrada" in r.text


def test_reimportar_da_entrada_sem_novos_arquiva(admin, tmp_path):
    _importar(admin, "a.xls", xls_santander(SANT), conta_id="1")
    entrada = tmp_path / "arquivo" / "_Entrada"
    (entrada / "repetido.xls").write_bytes(xls_santander(SANT))
    # duas contas Santander: sugere a que já recebeu extratos deste formato (não a 1.ª por ordem alfabética)
    pag = admin.get("/importar").text
    bloco = re.search(r'value="repetido\.xls">.*?</form>', pag, re.S).group(0)
    assert '<option value="1" selected>' in bloco
    f = admin.get("/importar")
    previa = admin.post("/importar", data={"csrf": _csrf(f), "conta_id": "1", "entrada": "repetido.xls"})
    assert "Nada de novo — arquivar o ficheiro" in previa.text


def test_arquivar_importacoes_antigas_e_estado(admin, app, tmp_path):
    _importar(admin, "a.xls", xls_santander(SANT))
    (tmp_path / "arquivo" / CAMINHO).unlink()
    with app.state.SessionLocal() as db:
        db.query(Importacao).update({"arquivo": None})
        db.commit()
    pag = admin.get("/admin/importadores").text
    assert "Arquivo de extratos" in pag and "1 importação(ões) anterior(es) ainda por arquivar" in pag
    assert "Dentro da pasta de dados da app" in pag and "✓ acessível" in pag
    r = _post(admin, "/admin/importadores", "/importar/arquivar-antigas")
    assert r.status_code == 303 and (tmp_path / "arquivo" / CAMINHO).exists()
    with app.state.SessionLocal() as db:
        assert db.query(Importacao).one().arquivo == CAMINHO


def test_pasta_partilhada_por_variavel(tmp_path, monkeypatch):
    partilha = tmp_path / "partilha"
    monkeypatch.setenv("GESTOR_ARQUIVO_DIR", str(partilha))
    app = create_app(tmp_path / "dados", backups_automaticos=False)
    assert app.state.defs.arquivo_partilhado and app.state.defs.arquivo_dir == partilha
    e = arquivo.estado(partilha, True)
    assert e.escrita and (partilha / "_Entrada").is_dir()
