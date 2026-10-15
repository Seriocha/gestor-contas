"""Retificação do saldo (Aforro/Investimento) e ordenação da lista de contas."""
import re

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Conta, Movimento
from app.servicos import saldo_conta
from tests.test_app import PW, _csrf, _post


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Certificados", tipo="AFORRO", moeda="EUR", instituicao_id="4")
    _post(c, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1",
          saldo_inicial="500,00")
    _manual(c, 1, "2026-01-10", "100,00", "Subscrição série F")
    return c


def _manual(c, conta, data, valor, desc, sentido="entrada"):
    f = c.get(f"/contas/{conta}/movimentos/novo")
    r = c.post(f"/contas/{conta}/movimentos/novo", data={"csrf": _csrf(f), "sentido": sentido, "data": data,
                                                          "valor": valor, "descricao": desc}, follow_redirects=False)
    assert r.status_code == 303
    return int(re.search(r"/movimentos/(\d+)", r.headers["location"]).group(1))


def _retificar(c, conta, data, saldo):
    p = c.get(f"/contas/{conta}/movimentos")
    return c.post(f"/contas/{conta}/retificar", data={"csrf": _csrf(p), "data": data, "saldo": saldo,
                                                       "nota": "valor do AforroNet"}, follow_redirects=True)


def _estado(app, conta_id=1):
    with app.state.SessionLocal() as db:
        c = db.get(Conta, conta_id)
        acertos = [m.montante for m in db.query(Movimento).filter(Movimento.conta_id == conta_id,
                                                                     Movimento.hash_dedup.like("retif:%"))]
        return saldo_conta(db, c).atual, acertos


def test_retificar_avisa_e_encolhe_com_movimentos(admin, app):
    r = _retificar(admin, 1, "2026-03-31", "150,00")
    assert "Diferença por explicar com movimentos: 50,00 €" in r.text
    assert _estado(app) == (15000, [5000])                       # saldo = valor real; acerto de 50
    assert "Saldo retificado." in r.text and "50,00 € por explicar" in r.text
    assert "⚠ saldo retificado em 31-03-2026" in admin.get("/contas").text
    assert 'class="aviso-conta"' in admin.get("/?entidade=1").text
    # o acerto fica fora dos relatórios
    rel = admin.get("/relatorios?periodo=personalizado&de=2026-01-01&ate=2026-03-31").text
    assert "Retificação" not in rel.split("Despesas por categoria")[1]
    # inserir depois os movimentos em falta: a diferença encolhe sozinha
    _manual(admin, 1, "2026-02-15", "30,00", "Reforço")
    assert _estado(app) == (15000, [2000])
    juros = _manual(admin, 1, "2026-03-20", "20,00", "Juros capitalizados")
    assert _estado(app) == (15000, [])                           # explicado: acerto desaparece
    assert "saldo retificado em" not in admin.get("/contas").text
    assert "explicado" in admin.get("/contas/1/movimentos").text
    # apagar um movimento volta a abrir a diferença
    p = admin.get(f"/movimentos/{juros}")
    admin.post(f"/movimentos/{juros}/apagar", data={"csrf": _csrf(p)})
    assert _estado(app) == (15000, [2000])


def test_movimentos_depois_da_data_nao_mexem_na_retificacao(admin, app):
    _retificar(admin, 1, "2026-03-31", "150,00")
    _manual(admin, 1, "2026-05-01", "10,00", "Juros de maio")
    assert _estado(app) == (16000, [5000])  # 150 real + 10 depois da data


def test_apagar_retificacao_e_saldo_inicial(admin, app):
    _retificar(admin, 1, "2026-03-31", "150,00")
    p = admin.get("/contas/1/movimentos")
    rid = re.search(r"/contas/1/retificacoes/(\d+)/apagar", p.text).group(1)
    admin.post(f"/contas/1/retificacoes/{rid}/apagar", data={"csrf": _csrf(p)})
    assert _estado(app) == (10000, [])


def test_so_aforro_e_investimento(admin):
    p = admin.get("/contas/2/movimentos")
    assert "Retificar saldo" not in p.text
    r = admin.post("/contas/2/retificar", data={"csrf": _csrf(p), "data": "2026-03-31", "saldo": "1,00"})
    assert r.status_code == 400


def test_ordenar_lista_de_contas(admin):
    def nomes(html):
        return re.findall(r'<strong><a href="/contas/\d+/movimentos">([^<]+)</a>', html)
    assert nomes(admin.get("/contas?ordem=-saldo").text) == ["Ordem", "Certificados"]   # 500 > 100
    assert nomes(admin.get("/contas?ordem=nome").text) == ["Certificados", "Ordem"]
    assert nomes(admin.get("/contas").text) == ["Certificados", "Ordem"]                 # escolha memorizada
    assert 'aria-sort="ascending"' in admin.get("/contas").text
