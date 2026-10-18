"""Património: casas e carros, atribuição das despesas a cada bem, avaliação (IA e à mão) e revisão periódica."""
import json
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import ia
from app import patrimonio as pt
from app.main import create_app
from app.models import AvaliacaoBem, Bem, Categoria, Movimento
from tests.test_app import PW, _csrf, _post
from tests.test_previsoes import _mov

HOJE = date(2026, 10, 17)


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


def _categorizar(app, desc, nome_cat):
    with app.state.SessionLocal() as db:
        cat = db.scalar(select(Categoria).where(Categoria.nome == nome_cat))
        for m in db.scalars(select(Movimento).where(Movimento.descricao_original == desc)):
            m.categoria_id = cat.id
        db.commit()


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    for d in ("2026-08-10", "2026-09-10"):
        _mov(c, 1, d, "60,00", "EDP COMERCIAL", sentido="saida")
        _mov(c, 1, d, "45,00", "CONDOMINIO PRAIA SOL", sentido="saida")
        _mov(c, 1, d, "70,00", "GALP ALVALADE", sentido="saida")
        _mov(c, 1, d, "40,00", "CARRIS NAVEGANTE", sentido="saida")
    _mov(c, 1, "2026-09-20", "500,00", "RENDA AIRBNB PRAIA", sentido="entrada")
    _categorizar(app, "EDP COMERCIAL", "Eletricidade")
    _categorizar(app, "CONDOMINIO PRAIA SOL", "Condomínio")
    _categorizar(app, "GALP ALVALADE", "Combustível")
    _categorizar(app, "CARRIS NAVEGANTE", "Transportes públicos")
    return c


def _criar(c, tipo="casa", **dados):
    base = {"casa": {"nome": "Casa de Lisboa", "uso": "principal", "tipologia": "T3", "area_m2": "110",
                     "localizacao": "Alvalade, Lisboa", "ano": "1975", "estado": "bom", "revisao_meses": "6",
                     "omissao": "1", "valor_aquisicao": "250.000,00", "data_aquisicao": "2015-03-01"},
            "carro": {"nome": "Golf", "uso": "principal", "marca": "Volkswagen", "modelo": "Golf", "ano": "2019",
                      "km": "85 000", "combustivel": "gasoleo", "revisao_meses": "12", "omissao": "1"}}[tipo]
    r = _post(c, f"/patrimonio/novo?entidade=1&tipo={tipo}", **{**base, **dados})
    assert r.status_code == 303, r.text
    return int(re.search(r"/patrimonio/(\d+)", r.headers["location"]).group(1))


def _bens(app):
    with app.state.SessionLocal() as db:
        return {m.descricao_original: (m.bem.nome if m.bem else None)
                for m in db.scalars(select(Movimento))}


def test_atribuir_despesas_principal_ferias_e_carro(admin, app):
    _criar(admin)
    _criar(admin, nome="Casa da praia", uso="ferias", tipologia="2", area_m2="70", localizacao="Armação de Pêra, Silves",
           omissao="", padroes="CONDOMINIO PRAIA\nAIRBNB", valor_aquisicao="", data_aquisicao="")
    _criar(admin, "carro")
    b = _bens(app)
    assert b["EDP COMERCIAL"] == "Casa de Lisboa"           # Casa › … sem padrão → por omissão
    assert b["CONDOMINIO PRAIA SOL"] == "Casa da praia"    # padrão ganha à omissão
    assert b["RENDA AIRBNB PRAIA"] == "Casa da praia"      # receitas também (padrão)
    assert b["GALP ALVALADE"] == "Golf"
    assert b["CARRIS NAVEGANTE"] is None                   # transportes públicos não são do carro
    with app.state.SessionLocal() as db:
        r = pt.resumo(db, 1, HOJE)
        assert r["casas_por_uso"] == [("Habitação principal", 12000, 1), ("Casa de férias", 9000 - 50000, 1)]
        assert r["valor"] == 25000000 and r["sem_bem"] == 0
    p = admin.get("/patrimonio?entidade=1").text
    assert "Casas: principal vs. férias" in p and "Casa da praia" in p and "por omissão" in p
    assert "Casas e carros" in admin.get("/?entidade=1").text
    lista = admin.get("/contas/1/movimentos").text
    assert "etiqueta-bem" in lista and 'value="bem:' in lista


def test_um_so_por_omissao_e_lote(admin, app):
    a = _criar(admin)
    b = _criar(admin, nome="Casa 2", tipologia="1", localizacao="Porto")     # também marcada por omissão
    with app.state.SessionLocal() as db:
        assert not db.get(Bem, a).omissao and db.get(Bem, b).omissao
        mid = db.scalar(select(Movimento.id).where(Movimento.descricao_original == "CARRIS NAVEGANTE"))
    f = admin.get("/contas/1/movimentos")
    r = admin.post("/movimentos-lote", data={"csrf": _csrf(f), "acao": f"bem:{a}", "ids": [str(mid)],
                                            "voltar": "/contas/1/movimentos"}, follow_redirects=False)
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        assert db.get(Movimento, mid).bem_id == a
    _post(admin, f"/movimentos/{mid}", f"/patrimonio/movimento/{mid}", bem_id="")
    with app.state.SessionLocal() as db:
        assert db.get(Movimento, mid).bem_id is None
    # apagar o bem deixa os movimentos sem bem
    _post(admin, f"/patrimonio/{b}/editar", f"/patrimonio/{b}/apagar")
    with app.state.SessionLocal() as db:
        assert db.get(Bem, b) is None
        assert not db.scalar(select(Movimento.id).where(Movimento.bem_id == b))


def test_validacao(admin):
    r = _post(admin, "/patrimonio/novo?entidade=1&tipo=casa", nome="", tipologia="TX", ano="3000", padroes="(")
    assert r.status_code == 400
    assert "Dá um nome" in r.text and "Tipologia" in r.text and "Ano inválido" in r.text


def test_avaliar_ia_historico_e_revisao(admin, app, monkeypatch):
    bid = _criar(admin)
    enviados = []

    def falso(sistema, mensagem, ferramentas=None, modelo_ia=None):
        enviados.append((mensagem, ferramentas))
        if ferramentas:   # conta sem pesquisa na web: a 1.ª tentativa falha, repete sem ferramenta
            raise ia.ErroIA("A API respondeu 400: web_search is not enabled")
        return "Aqui está:\n" + json.dumps({"valor": 412000, "minimo": 380000, "maximo": 440000, "preco_m2": 3745,
                                             "justificacao": "Preço mediano em Alvalade.", "confianca": 0.6,
                                             "referencias": ["https://www.idealista.pt/x", "nao-url"]})
    monkeypatch.setattr(ia, "disponivel", lambda: True)
    monkeypatch.setattr(ia, "_chamar", falso)
    with app.state.SessionLocal() as db:
        b = db.get(Bem, bid)
        assert pt.revisao_pendente(b, HOJE)          # sem avaliações → pendente
        a = pt.avaliar_ia(db, b, HOJE)
        db.commit()
        assert (a.valor, a.minimo, a.maximo, a.fonte) == (41200000, 38000000, 44000000, "ia")
        assert a.referencias == "https://www.idealista.pt/x" and "3745 €/m²" in a.justificacao
        assert pt.valor_atual(b) == 41200000 and pt.proxima_revisao(b) == date(2027, 4, 17)
        assert not pt.revisao_pendente(b, HOJE)
        assert pt.rever_pendentes(db, HOJE) == 0
        assert pt.rever_pendentes(db, date(2027, 4, 17)) == 1      # passados 6 meses
        assert len(b.avaliacoes) == 2
    msg = enviados[0][0]
    assert "T3" in msg and "Alvalade, Lisboa" in msg and "110 m²" in msg and "1975" in msg
    assert "Casa de Lisboa" not in msg and "250" not in msg     # nem o nome nem o preço de compra
    assert "412000 €" in enviados[-1][0]                         # 2.ª avaliação recebe o histórico
    p = admin.get(f"/patrimonio/{bid}").text
    assert "Histórico do valor" in p and 'data-grafico="bem"' in p and "idealista" in p


def test_avaliacao_manual_e_sem_chave(admin, app, monkeypatch):
    bid = _criar(admin, "carro")
    monkeypatch.setattr(ia, "disponivel", lambda: False)
    r = _post(admin, f"/patrimonio/{bid}", f"/patrimonio/{bid}/avaliar")
    assert r.status_code == 303
    assert "não está configurada" in admin.get(f"/patrimonio/{bid}").text
    _post(admin, f"/patrimonio/{bid}", f"/patrimonio/{bid}/avaliacao", data="2026-10-01", valor="18.500,00",
          nota="proposta do stand")
    with app.state.SessionLocal() as db:
        b = db.get(Bem, bid)
        assert [(a.valor, a.fonte) for a in b.avaliacoes] == [(1850000, "manual")]
        aid = b.avaliacoes[0].id
        assert pt.descricao_para_ia(b).startswith("Automóvel: Volkswagen Golf") and "85000" in pt.descricao_para_ia(b)
    _post(admin, f"/patrimonio/{bid}", f"/patrimonio/{bid}/avaliacoes/{aid}/apagar")
    with app.state.SessionLocal() as db:
        assert db.get(AvaliacaoBem, aid) is None


def test_viatura_da_empresa_so_combustivel(admin, app):
    _mov(admin, 1, "2026-09-12", "300,00", "OFICINA BOSCH", sentido="saida")
    _categorizar(app, "OFICINA BOSCH", "Manutenção automóvel")
    _mov(admin, 1, "2026-09-15", "55,00", "PRIO CARTAO FROTA", sentido="saida")   # sem categoria
    proprio = _criar(admin, "carro")
    emp = _criar(admin, "carro", nome="Carro da empresa", uso="empresa", marca="", modelo="", ano="", km="",
                 valor_aquisicao="20.000,00", padroes="CARTAO FROTA", data_aquisicao="2026-09-01")
    with app.state.SessionLocal() as db:
        b, p = db.get(Bem, emp), db.get(Bem, proprio)
        assert b.empresa and b.omissao and p.omissao          # grupos diferentes: ambos por omissão
        assert b.valor_aquisicao is None and b.revisao_meses == 0
        assert pt.valor_atual(b) is None and pt.proxima_revisao(b) is None
    m = _bens(app)
    assert m["PRIO CARTAO FROTA"] == "Carro da empresa"     # padrão, ainda sem categoria
    assert m["OFICINA BOSCH"] == "Golf"                     # manutenção não vai para a viatura da empresa
    with app.state.SessionLocal() as db:
        setembro = db.scalar(select(Movimento).where(Movimento.descricao_original == "GALP ALVALADE",
                                                     Movimento.data_mov == date(2026, 9, 10)))
        assert setembro.bem.nome == "Carro da empresa"      # combustível: a viatura da empresa tem prioridade
        agosto = db.scalar(select(Movimento).where(Movimento.descricao_original == "GALP ALVALADE",
                                                   Movimento.data_mov == date(2026, 8, 10)))
        assert agosto.bem.nome == "Golf"                    # antes de ter a viatura da empresa
        r = pt.resumo(db, 1, HOJE)
        assert r["valor"] == 0 and pt.valor_total(db, 1) == (0, 1)
        with pytest.raises(ia.ErroIA):
            pt.avaliar_ia(db, db.get(Bem, emp), HOJE)
    p = admin.get(f"/patrimonio/{emp}").text
    assert "Combustível em 12 meses" in p and "Histórico do valor" not in p
    assert "+ Viatura da empresa" in admin.get("/patrimonio?entidade=1").text


PLANO = {"itens": [
    {"item": "Óleo e filtro de óleo", "intervalo_km": 15000, "intervalo_meses": 12, "custo_min": 120, "custo_max": 180},
    {"item": "Correia de distribuição", "intervalo_km": 120000, "intervalo_meses": 60, "custo_min": 450, "custo_max": 700},
    {"item": "Inspeção periódica", "intervalo_km": None, "intervalo_meses": 24},
    {"item": "Sem intervalo", "intervalo_km": None, "intervalo_meses": None}],
    "notas": "Motor 1.6 TDI com correia."}


def test_km_plano_e_oficina(admin, app, monkeypatch):
    from app import manutencao as mt
    from app.models import Manutencao
    bid = _criar(admin, "carro", data_aquisicao="2019-03-01", km="80 000")
    enviados = []

    def falso(sistema, mensagem, ferramentas=None, modelo_ia=None):
        enviados.append(mensagem)
        return json.dumps(PLANO)
    monkeypatch.setattr(ia, "disponivel", lambda: True)
    monkeypatch.setattr(ia, "_chamar", falso)
    with app.state.SessionLocal() as db:
        b = db.get(Bem, bid)
        assert [r.km for r in b.registos_km] == [80000]          # os km da ficha entram como leitura
        b.registos_km[0].data = date(2026, 6, 1)
        mt.registar_km(db, b, date(2026, 8, 30), 83000)         # 3000 km em 90 dias
        with pytest.raises(ValueError):
            mt.registar_km(db, b, date(2026, 9, 1), 82000)      # não anda para trás
        assert round(mt.km_por_dia(b), 2) == 33.33
        assert mt.km_estimados(b, HOJE) == 83000 + round(3000 / 90 * 48)
        assert mt.gerar_plano(db, b, HOJE) == 3                 # «Sem intervalo» é ignorado
        db.commit()
        assert "Volkswagen Golf" in enviados[0] and "Ano de fabrico: 2019" in enviados[0] and "Golf\n" not in enviados[0][:5]
        e = {x.item.item: x for x in mt.estado_itens(b, HOJE)}
        oleo = e["Óleo e filtro de óleo"]                     # nunca registado: desde 0 km / 03-2019
        assert oleo.sem_registo and oleo.estado == "em atraso" and oleo.prox_km == 15000
        assert e["Correia de distribuição"].estado == "em atraso"   # 60 meses desde 2019
        assert mt.resumo(b, HOJE).precisa_oficina and mt.lembrar_km(b, HOJE)     # última leitura há 48 dias
    # ida à oficina: óleo feito em setembro aos 83 500 km
    _post(admin, f"/patrimonio/{bid}", f"/patrimonio/{bid}/oficina", data="2026-09-20", km="83 500",
          itens=["Óleo e filtro de óleo", "Inválido"], outro="Pastilhas", custo="150,00")
    with app.state.SessionLocal() as db:
        b = db.get(Bem, bid)
        m = db.scalar(select(Manutencao))
        assert m.lista_itens == ["Óleo e filtro de óleo", "Pastilhas"] and m.custo == 15000 and b.km == 83500
        oleo = {x.item.item: x for x in mt.estado_itens(b, HOJE)}["Óleo e filtro de óleo"]
        assert (oleo.estado, oleo.prox_km, oleo.prox_data) == ("ok", 98500, date(2027, 9, 20))
        # atualizar o plano mantém os itens (pelo nome) e o histórico continua a contar
        ids = {it.item: it.id for it in b.plano}
        mt.gerar_plano(db, b, HOJE)
        db.commit()
        assert {it.item: it.id for it in b.plano} == ids
    p = admin.get(f"/patrimonio/{bid}").text
    assert "Manutenção e quilómetros" in p and "precisa já" in p and "Correia de distribuição" in p and "Pastilhas" in p
    lista = admin.get("/patrimonio?entidade=1").text
    assert "Precisa de ir à oficina" in lista and "oficina: 2 em atraso" in lista


def test_lembrar_km_e_sem_plano(admin, app, monkeypatch):
    from app import manutencao as mt
    bid = _criar(admin, "carro", marca="", modelo="")
    monkeypatch.setattr(ia, "disponivel", lambda: True)
    _post(admin, f"/patrimonio/{bid}", f"/patrimonio/{bid}/plano")
    assert "indica: marca, modelo" in admin.get(f"/patrimonio/{bid}").text
    with app.state.SessionLocal() as db:
        b = db.get(Bem, bid)
        b.registos_km[0].data = date(2026, 8, 1)
        db.commit()
        assert mt.lembrar_km(b, HOJE)
    r = _post(admin, f"/patrimonio/{bid}", f"/patrimonio/{bid}/km", data="2026-10-10", km="86000")
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        b = db.get(Bem, bid)
        assert b.km == 86000 and not mt.lembrar_km(b, HOJE)


def test_movimentos_filtrados_por_bem(admin, app):
    golf = _criar(admin, "carro")
    t = admin.get(f"/movimentos?entidade=1&bem={golf}").text
    assert "Só os movimentos de" in t and "GALP ALVALADE" in t and "EDP COMERCIAL" not in t
    assert 'name="f_bem" value="%d"' % golf in t


def test_periodo_dos_bens_respeita_a_cobertura(admin, app):
    from app.models import Entidade
    golf = _criar(admin, "carro")
    with app.state.SessionLocal() as db:
        db.get(Entidade, 1).data_inicio = date(2026, 8, 1)
        db.commit()
        assert pt.inicio_periodo(db, 1, HOJE) == date(2026, 8, 1)
        c = pt.custos(db, db.get(Bem, golf), pt.inicio_periodo(db, 1, HOJE), HOJE)
        assert c.meses[0] == date(2026, 8, 1) and len(c.meses) == 3          # ago, set, out — sem meses a zero antes
    ficha = admin.get(f"/patrimonio/{golf}").text
    assert "desde 01/08/2026" in ficha


def test_inspecao_escrita_em_outro_conta_no_plano(admin, app, monkeypatch):
    """«Inspeção» escrita em «Outro» atualiza o item «Inspeção periódica» do plano; erros de km avisam e não gravam."""
    from app import manutencao as mt
    from app.models import Manutencao
    bid = _criar(admin, "carro", data_aquisicao="2019-03-01", km="80 000")
    monkeypatch.setattr(ia, "disponivel", lambda: True)
    monkeypatch.setattr(ia, "_chamar", lambda *a, **k: json.dumps(PLANO))
    with app.state.SessionLocal() as db:
        b = db.get(Bem, bid)
        mt.gerar_plano(db, b, HOJE)
        db.commit()
    dia = (HOJE - timedelta(days=3)).isoformat()
    r = _post(admin, f"/patrimonio/{bid}", f"/patrimonio/{bid}/oficina", data=dia, km="90 000", outro="Inspeção")
    assert r.headers["location"] == f"/patrimonio/{bid}"                       # sem âncora: o aviso fica à vista
    assert "Ida à oficina não registada" in admin.get(f"/patrimonio/{bid}").text
    with app.state.SessionLocal() as db:
        assert db.scalar(select(Manutencao)) is None
    _post(admin, f"/patrimonio/{bid}", f"/patrimonio/{bid}/oficina", data=dia, km="", outro="Inspeção", custo="35,00")
    p = admin.get(f"/patrimonio/{bid}").text
    assert "Plano atualizado: Inspeção periódica" in p
    with app.state.SessionLocal() as db:
        b = db.get(Bem, bid)
        insp = {x.item.item: x for x in mt.estado_itens(b, HOJE)}["Inspeção periódica"]
        assert insp.ultima_data == HOJE - timedelta(days=3) and insp.estado == "ok"
    assert mt.corresponde("IPO", "Inspeção periódica") and not mt.corresponde("Filtro de ar", "Filtro de óleo")


def test_oficina_editar_e_pesquisar_movimento(admin, app):
    """Plano fechado e registo só com «+ Nova»; pesquisa de débitos (texto, valor, perto da data); editar e associar."""
    from app.models import Manutencao, Movimento
    bid = _criar(admin, "carro", km="80 000")
    _mov(admin, 1, (HOJE - timedelta(days=10)).isoformat(), "35,00", "CENTRO INSPECOES AUTO", sentido="saida")
    p = admin.get(f"/patrimonio/{bid}").text
    assert '<details class="separado nova-oficina" id="oficina-nova">' in p and "+ Nova ida à oficina" in p
    assert "pesquisa_mov.js" in p
    dia = (HOJE - timedelta(days=10)).isoformat()
    _post(admin, f"/patrimonio/{bid}", f"/patrimonio/{bid}/oficina", data=dia, outro="Inspeção")
    with app.state.SessionLocal() as db:
        m = db.scalar(select(Manutencao))
        insp = db.scalar(select(Movimento).where(Movimento.descricao_original == "CENTRO INSPECOES AUTO"))
        mid, insp_id = m.id, insp.id
        assert m.movimento_id is None
    assert f'/patrimonio/{bid}/oficina/{mid}/editar">associar movimento' in admin.get(f"/patrimonio/{bid}").text
    url = f"/patrimonio/{bid}/oficina/movimentos"
    assert [r["id"] for r in admin.get(url, params={"q": "inspec"}).json()] == [insp_id]
    por_valor = admin.get(url, params={"q": "35,00"}).json()
    assert [r["id"] for r in por_valor] == [insp_id] and por_valor[0]["custo"] == "35,00"
    perto = admin.get(url, params={"data": dia}).json()
    assert insp_id in [r["id"] for r in perto] and "GALP ALVALADE" not in [r["descricao"] for r in perto]   # combustível não
    f = admin.get(f"/patrimonio/{bid}/oficina/{mid}/editar")
    assert f.status_code == 200 and 'value="Inspeção"' in f.text
    r = admin.post(f"/patrimonio/{bid}/oficina/{mid}/editar", follow_redirects=False,
                   data={"csrf": _csrf(f), "data": dia, "km": "79 500", "outro": "Inspeção, Escovas", "movimento_id": str(insp_id)})
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        m = db.get(Manutencao, mid)
        assert (m.movimento_id, m.custo, m.km, m.lista_itens) == (insp_id, 3500, 79500, ["Inspeção", "Escovas"])
        assert db.get(Movimento, insp_id).bem_id == bid
        assert any(x.km == 79500 and x.origem == "oficina" for x in db.get(Bem, bid).registos_km)
    assert admin.get(url, params={"q": "inspec"}).json()[0]["ligado"]
    assert not admin.get(url, params={"q": "inspec", "manutencao": str(mid)}).json()[0]["ligado"]
    # mudar os km da ida à oficina substitui a leitura antiga
    f = admin.get(f"/patrimonio/{bid}/oficina/{mid}/editar")
    admin.post(f"/patrimonio/{bid}/oficina/{mid}/editar",
               data={"csrf": _csrf(f), "data": dia, "km": "79 800", "outro": "Inspeção", "movimento_id": str(insp_id)})
    with app.state.SessionLocal() as db:
        kms = [x.km for x in db.get(Bem, bid).registos_km if x.origem == "oficina"]
        assert kms == [79800]
    assert admin.get(f"/patrimonio/{bid}/oficina/999/editar").status_code == 404


def test_casa_despesas_so_em_grafico(admin, app):
    """Casa: despesas e receitas condensadas (KPI, gráfico por mês, barras por categoria) e ligação ao detalhe."""
    _criar(admin)
    praia = _criar(admin, nome="Casa da praia", uso="ferias", omissao="", padroes="CONDOMINIO PRAIA\nAIRBNB",
                   valor_aquisicao="", data_aquisicao="")
    p = admin.get(f"/patrimonio/{praia}").text
    assert 'data-grafico="despesas_bem"' in p and 'class="barras-categoria"' in p
    assert "Ver os movimentos desta casa" in p and f"bem={praia}" in p
    assert "<h3>Por mês</h3>" not in p and "<h3>Movimentos</h3>" not in p and "CONDOMINIO PRAIA SOL</a>" not in p
    assert "Recebido" in p and "500,00 €" in p
