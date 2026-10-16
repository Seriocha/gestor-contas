"""Calendário do mês no topo dos filtros de movimentos: clicar num dia filtra esse dia."""
import re
from html import unescape

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.test_app import PW, _post
from tests.test_previsoes import _mov


@pytest.fixture
def admin(tmp_path):
    c = TestClient(create_app(tmp_path, backups_automaticos=False))
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _mov(c, 1, "2026-09-03", "10,00", "CAFE A", sentido="saida")
    _mov(c, 1, "2026-09-03", "25,00", "RECEBIDO B")
    _mov(c, 1, "2026-09-20", "7,00", "CAFE C", sentido="saida")
    _mov(c, 1, "2026-08-15", "50,00", "AGOSTO")
    return c


def _dias(html):
    return {int(n): (href, cls) for href, cls, n in re.findall(
        r'<a class="cal-dia([^"]*)" href="([^"]+)"[^>]*>\s*<span class="cal-num">(\d+)</span>', html)
        and [(unescape(h), c, n) for c, h, n in re.findall(
            r'<a class="cal-dia([^"]*)" href="([^"]+)"[^>]*>\s*<span class="cal-num">(\d+)</span>', html)]}


def test_calendario_do_mes_mais_recente(admin):
    p = admin.get("/movimentos?entidade=1").text
    assert "Set 2026" in p and "3 mov." in p and "+8,00 €" not in p
    dias = _dias(p)
    assert len(dias) == 30 and "com-mov" in dias[3][1] and "com-mov" in dias[20][1] and "com-mov" not in dias[4][1]
    assert "de=2026-09-03&ate=2026-09-03" in dias[3][0]
    # o dia 3 tem dois movimentos: saldo +15
    bloco = p.split('<span class="cal-num">3</span>')[1][:200]
    assert '<span class="cal-n">2</span>' in bloco and '<span class="cal-soma pos">15</span>' in bloco
    # 1 de setembro de 2026 é terça-feira: uma célula vazia antes
    grelha = p.split('class="cal-grelha"')[1]
    assert grelha.index('cal-vazio') < grelha.index('<span class="cal-num">1</span>')


def test_clicar_no_dia_filtra_e_volta_a_limpar(admin):
    p = admin.get("/movimentos?entidade=1&de=2026-09-03&ate=2026-09-03").text
    assert "CAFE A" in p and "RECEBIDO B" in p and "CAFE C" not in p
    dias = _dias(p)
    assert "escolhido" in dias[3][1] and "&de=" not in dias[3][0] and "mes=2026-09" in dias[3][0]
    assert "dia 03/09/2026" in p


def test_navegar_meses_e_mes_inteiro_com_outros_filtros(admin):
    p = admin.get("/movimentos?entidade=1&mes=2026-08&q=AGOSTO").text
    assert "Ago 2026" in p and "1 mov." in p
    dias = _dias(p)
    assert "q=AGOSTO" in dias[15][0] and "com-mov" in dias[15][1]
    assert "mes=2026-07" in unescape(p.split('aria-label="Mês anterior"')[0].rsplit('href="', 1)[1])
    inteiro = admin.get("/movimentos?entidade=1&de=2026-09-01&ate=2026-09-30").text
    assert "CAFE C" in inteiro and ">limpar<" in inteiro
    conta = admin.get("/contas/1/movimentos?mes=2026-09").text
    assert "Set 2026" in conta and 'href="?mes=2026-09&amp;de=' not in conta and "de=2026-09-20" in conta
