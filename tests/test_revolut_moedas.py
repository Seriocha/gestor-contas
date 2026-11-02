"""Revolut: cada operação só na conta da sua moeda, os totais em euros e a limpeza dos repetidos já gravados."""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import relatorios as rel
from app import revolut_moedas
from app.main import create_app
from app.models import Categoria, Conta, Etiqueta, Movimento
from tests.test_app import PW, _csrf, _post


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Revolut", tipo="CARTEIRA_DIGITAL", moeda="EUR", instituicao_id="5")   # 1
    _post(c, "/contas/nova?entidade=1", nome="Revolut CHF", tipo="CARTEIRA_DIGITAL", moeda="CHF", instituicao_id="5")  # 2
    return c


def _mov(db, conta, desc, montante, saldo=None, valor_eur=None, moeda="EUR", dia=3, **extra):
    m = Movimento(conta_id=conta, data_mov=date(2026, 4, dia), descricao_original=desc, descricao_norm=desc.upper(),
                  montante=montante, moeda=moeda, saldo_apos=saldo, valor_eur=valor_eur,
                  hash_dedup=f"{conta}-{desc}-{montante}", **extra)
    db.add(m)
    db.flush()
    return m


def test_totais_em_euros(app, admin):
    with app.state.SessionLocal() as db:
        _mov(db, 2, "Coop", -1200, valor_eur=-1303, moeda="CHF")
        _mov(db, 2, "Sem taxa", -5000, moeda="CHF")               # sem valor em euros: não conta como 50 €
        _mov(db, 1, "Lidl", -1000)
        db.commit()
        movs = rel.movimentos_reportaveis(db, 1, date(2026, 1, 1), date(2026, 12, 31))
        assert rel.resumir(movs).despesas == 2303


def test_patrimonio_a_taxa_do_ultimo_movimento(app, admin):
    with app.state.SessionLocal() as db:
        _mov(db, 2, "Conversão", 20000, saldo=20000, valor_eur=21000, moeda="CHF", transferencia=True)
        db.commit()
        p = rel.patrimonio(db, 1, [date(2026, 12, 31)])
        assert dict((c.nome, s) for c, s in p["contas"])["Revolut CHF"] == 21000
        assert p["serie"][-1]["ativos"] == 21000


def test_limpar_repetidos(app, admin):
    with app.state.SessionLocal() as db:
        cat = db.scalar(select(Categoria))
        etq = Etiqueta(nome="suica")
        db.add(etq)
        nativo = _mov(db, 2, "Coop", -1200, moeda="CHF")
        copia = _mov(db, 1, "Coop (-12 CHF)", -1303, categoria_id=cat.id, qualificado_por="manual", notas="almoço")
        copia.etiquetas.append(etq)
        _mov(db, 2, "Kiosk", -1350, moeda="CHF", dia=4)
        _mov(db, 1, "Kiosk (-13,5 CHF)", -1465, dia=4)
        _mov(db, 1, "Dubai Mall (-100 AED)", -2500)               # sem conta em AED: fica
        _mov(db, 1, "Sem par (-7 CHF)", -760)                     # sem movimento igual: fica
        _mov(db, 1, "Lidl (Sintra) 12", -1000, saldo=5000)        # com saldo: é da conta em euros
        db.commit()
        nativo_id, cat_id = nativo.id, cat.id
    r = admin.get("/admin/importadores")
    assert "compras repetidas" in r.text and "Corrigir os 2 repetidos" in r.text and "1 em AED" in r.text
    r = admin.post("/admin/importadores/revolut-repetidos", data={"csrf": _csrf(r)}, follow_redirects=True)
    assert "2 movimento(s) repetido(s) retirado(s)" in r.text
    with app.state.SessionLocal() as db:
        n = db.get(Movimento, nativo_id)
        assert (n.valor_eur, n.categoria_id, n.qualificado_por, n.notas) == (-1303, cat_id, "manual", "almoço")
        assert [e.nome for e in n.etiquetas] == ["suica"]
        restam = sorted(db.scalars(select(Movimento.descricao_original).join(Conta).where(Conta.moeda == "EUR")))
        assert restam == ["Dubai Mall (-100 AED)", "Lidl (Sintra) 12", "Sem par (-7 CHF)"]
        assert revolut_moedas.previa(db).pares == []
        assert rel.resumir(rel.movimentos_reportaveis(db, 1, date(2026, 1, 1), date(2026, 12, 31))).despesas == \
            1303 + 1465 + 2500 + 760 + 1000                       # cada compra só uma vez


def test_saldo_colado_a_moeda():
    from tests.test_revolut_pdf import _cabecalho, _linha, _texto
    from app.parsers.revolut_pdf import interpretar
    pagina = (_texto(144, 40, "Contas-correntes Extratos de operações") + _texto(185, 40, "Conta Pessoal (CHF)") + _cabecalho(208)
              + _linha(233, data="3 de abr. de 2026", descricao="Coop", categoria="Comerciante", montante="-12,00 CHF217,44")
              + _linha(243, montante="-13,03€") + _linha(255, saldo="CHF") + _linha(265, saldo="236,01€"))
    m = interpretar([pagina]).subcontas[("Atual", "CHF")][0]
    assert (m.montante, m.saldo, m.valor_eur) == (-1200, 21744, -1303)


def test_reimportar_completa_a_linha_gravada_sem_saldo(app, admin):
    from app import servicos
    from app.parsers import Extrato, MovimentoBruto
    linha = MovimentoBruto(date(2026, 4, 3), None, "Coop", -1200, 21744, valor_eur=-1303)
    with app.state.SessionLocal() as db:
        conta = db.get(Conta, 2)
        m = _mov(db, 2, "Coop", -1200, moeda="CHF")
        m.descricao_norm = servicos.normalizar_descricao("Coop")
        db.commit()
        ext = Extrato(parser="revolut_pdf", banco="Revolut (PDF)", movimentos=[linha], moeda="CHF")
        ext.dedup_dias = 4
        linhas = servicos.preparar(db, conta, ext)
        assert [l.duplicado for l in linhas] == [True]            # não entra outra vez
        servicos.preencher_valor_eur(db, conta, linhas)
        db.commit()
        db.refresh(m)
        assert (m.saldo_apos, m.valor_eur) == (21744, -1303)
