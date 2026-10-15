"""v0.11: organização dos ativos, pesquisa por valor e ordenação, NIB, movimentos manuais."""
import re

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Movimento
from tests.test_app import PW, _csrf, _post
from tests.test_importacao import _importar, xls_santander

LINHAS = [
    ("13-10-2025", "Supermercado X", -40.05, 1000.00),
    ("12-10-2025", "Farmacia", -12.00, 1040.05),
    ("11-10-2025", "Restaurante", -40.05, 1052.05),
    ("10-10-2025", "Vencimento", 1100.0, 1092.10),
]


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Santander ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1",
          iban="pt50 0018 0003 1234 5678 9012 3")
    _post(c, "/contas/nova?entidade=1", nome="Depósito 12M", tipo="POUPANCA", moeda="EUR", instituicao_id="2",
          saldo_inicial="5.000,00", data_saldo_inicial="2025-10-01")
    _importar(c, "d.xls", xls_santander(LINHAS))
    return c


def _linhas(html):
    return re.findall(r'class="descricao">([^<]+)<', html)


def test_pesquisa_por_valor(admin):
    r = admin.get("/movimentos?entidade=1&q=40,05")
    assert sorted(_linhas(r.text)) == ["Restaurante", "Supermercado X"]       # entradas e saídas, sem sinal
    assert _linhas(admin.get("/movimentos?entidade=1&q=-40.05").text).__len__() == 2
    r = admin.get("/movimentos?entidade=1&vmin=20&vmax=50")
    assert sorted(_linhas(r.text)) == ["Restaurante", "Supermercado X"]
    assert _linhas(admin.get("/movimentos?entidade=1&q=farm").text) == ["Farmacia"]  # texto continua a funcionar


def test_ordenar_por_colunas(admin):
    # os dois −40,05 € primeiro; empate desfeito pela data (11-10 antes de 13-10)
    assert _linhas(admin.get("/movimentos?entidade=1&ordem=montante").text)[:2] == ["Restaurante", "Supermercado X"]
    assert _linhas(admin.get("/movimentos?entidade=1&ordem=-montante").text)[0] == "Vencimento"
    assert _linhas(admin.get("/movimentos?entidade=1&ordem=descricao").text) == \
        ["Farmacia", "Restaurante", "Supermercado X", "Vencimento"]
    assert _linhas(admin.get("/movimentos?entidade=1").text)[0] == "Supermercado X"  # por omissão: data desc
    r = admin.get("/contas/1/movimentos?ordem=categoria")
    assert r.status_code == 200 and 'aria-sort="ascending"' in r.text
    assert admin.get("/movimentos?entidade=1&ordem=inexistente").status_code == 200


def test_painel_agrupar_e_ordenar(admin):
    p = admin.get("/?entidade=1&agrupar=banco&ordenar=saldo").text
    ativos = p.split("<h2>Ativos</h2>")[1].split("<h2>Passivos</h2>")[0]
    assert ativos.index(">ActivoBank<") < ativos.index(">Santander<")  # grupos por banco
    p = admin.get("/?entidade=1").text  # a escolha fica memorizada
    assert 'value="banco" selected' in p and 'value="saldo" selected' in p
    p = admin.get("/?entidade=1&agrupar=nenhum&ordenar=nome").text
    assert '<tr class="grupo">' not in p.split("<h2>Ativos</h2>")[1].split("<h2>Passivos</h2>")[0]


def test_preferencia_mostrar_iban(admin):
    contas = admin.get("/contas").text
    assert "PT50 •••• 0123" in contas and "0018 0003" not in contas
    r = admin.get("/admin/utilizadores")
    admin.post("/admin/preferencias", data={"csrf": _csrf(r), "mostrar_iban_completo": "1"})
    assert "PT50 0018 0003 1234 5678 9012 3" in admin.get("/contas").text
    r = admin.get("/admin/utilizadores")
    assert 'name="mostrar_iban_completo" value="1" checked' in r.text
    admin.post("/admin/preferencias", data={"csrf": _csrf(r)})  # desmarcar
    assert "PT50 •••• 0123" in admin.get("/contas").text


def test_movimento_manual_criar_ligar_editar_apagar(admin, app):
    form = admin.get("/contas/2/movimentos/novo")
    assert form.status_code == 200 and "Novo movimento" in form.text
    r = admin.post("/contas/2/movimentos/novo", data={
        "csrf": _csrf(form), "sentido": "entrada", "data": "2025-10-12", "valor": "12,00",
        "descricao": "Reforço do depósito", "transferencia": "1"}, follow_redirects=True)
    assert "Movimento criado" in r.text
    # o débito «Farmacia» de −12,00 em 12-10 aparece como candidato a outro lado
    assert "Possíveis outros lados" in r.text and "Farmacia" in r.text
    with app.state.SessionLocal() as db:
        manual = db.query(Movimento).filter_by(descricao_original="Reforço do depósito").one()
        farm = db.query(Movimento).filter_by(descricao_original="Farmacia").one()
        mid, fid = manual.id, farm.id
        assert manual.montante == 1200 and manual.transferencia and manual.hash_dedup.startswith("manual:")
    admin.post(f"/movimentos/{mid}/ligar", data={"csrf": _csrf(r), "par_id": str(fid)})
    with app.state.SessionLocal() as db:
        assert db.get(Movimento, mid).par_id == fid and db.get(Movimento, fid).transferencia
    # saldo da conta de depósito = soma dos movimentos manuais
    assert "5.012,00 €" in admin.get("/contas").text
    # editar o valor
    det = admin.get(f"/movimentos/{mid}")
    assert "Movimento inserido à mão" in det.text and "Apagar movimento" in det.text
    admin.post(f"/movimentos/{mid}", data={"csrf": _csrf(det), "data": "2025-10-12", "sentido": "entrada", "valor": "15,00",
                                           "descricao": "Reforço do depósito", "transferencia": "1"})
    with app.state.SessionLocal() as db:
        assert db.get(Movimento, mid).montante == 1500
    # apagar: o outro lado fica sem par
    admin.post(f"/movimentos/{mid}/apagar", data={"csrf": _csrf(det)})
    with app.state.SessionLocal() as db:
        assert db.get(Movimento, mid) is None and db.get(Movimento, fid).par_id is None


def test_importados_nao_se_apagam(admin, app):
    with app.state.SessionLocal() as db:
        mid = db.query(Movimento).filter_by(descricao_original="Farmacia").one().id
    det = admin.get(f"/movimentos/{mid}")
    assert "Apagar movimento" not in det.text
    assert admin.post(f"/movimentos/{mid}/apagar", data={"csrf": _csrf(det)}).status_code == 400


def test_movimento_manual_validacao(admin):
    form = admin.get("/contas/2/movimentos/novo")
    r = admin.post("/contas/2/movimentos/novo", data={"csrf": _csrf(form), "sentido": "saida", "data": "", "valor": "0",
                                                       "descricao": ""})
    assert r.status_code == 400 and "Indica a data" in r.text and "Indica a descrição" in r.text


def test_variacao_do_patrimonio_explicada(admin, app):
    from datetime import date
    from app import relatorios as rel
    with app.state.SessionLocal() as db:
        v = rel.variacao_patrimonio(db, 1, date(2025, 10, 1), date(2025, 10, 31))
    santander = next(c for c in v.contas if c.conta.nome == "Santander ordem")
    # 1092,10 → 1000,00: variação −92,10 = soma dos 3 movimentos de outubro (o vencimento foi em 10-10)
    assert (santander.inicio, santander.fim) == (-790, 100000)
    assert santander.variacao == santander.movimentos and santander.nao_explicado == 0
    assert v.resumo.receitas == 110000 and v.resumo.despesas == 9210
    assert [m.descricao_original for m in v.movimentos][:1] == ["Vencimento"]  # maiores primeiro


def test_variacao_nao_explicada_quando_falta_extrato(admin, app):
    # setembro sem extrato: o saldo salta de 1092,10 (antes) para 5000 (novo extrato em novembro)
    _importar(admin, "nov.xls", xls_santander([("05-11-2025", "Bonus", 100.0, 5000.0)]))
    from datetime import date
    from app import relatorios as rel
    with app.state.SessionLocal() as db:
        v = rel.variacao_patrimonio(db, 1, date(2025, 10, 14), date(2025, 11, 30))
    s = next(c for c in v.contas if c.conta.nome == "Santander ordem")
    assert s.variacao == 400000 and s.movimentos == 10000 and s.nao_explicado == 390000


def test_rota_variacao_e_dica_no_painel(admin):
    r = admin.get("/painel/variacao?entidade=1&de=2025-10-01&ate=2025-10-31")
    assert r.status_code == 200 and "O que mudou" in r.text and "Santander ordem" in r.text and "Vencimento" in r.text
    assert "ordem=-montante" in r.text  # ligação «Ver todos em Movimentos»
    assert admin.get("/painel/variacao?entidade=1&de=2025-10-31&ate=2025-10-01").status_code == 400
    p = admin.get("/").text
    assert 'id="detalhe-variacao"' in p and '"datas":' in p and "Clica (ou toca) num ponto" in p
