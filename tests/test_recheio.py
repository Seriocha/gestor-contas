"""Recheio das casas: categorias de recheio, criação automática dos itens, ficha, garantia e pedidos de garantia."""
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import inicio
from app.main import create_app
from app.models import Categoria, Entidade, ItemRecheio, Movimento
from tests.test_app import PW, _csrf, _post
from tests.test_previsoes import _mov

HOJE = date.today()


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    r = _post(c, "/patrimonio/novo?entidade=1&tipo=casa", nome="Casa", uso="principal", omissao="1", revisao_meses="0")
    assert r.status_code == 303
    return c


def _cat(app, nome):
    with app.state.SessionLocal() as db:
        return db.scalar(select(Categoria.id).where(Categoria.nome == nome))


def test_categorias_e_itens_automaticos(admin, app):
    compras = _cat(app, "Compras")
    tecnologia = _cat(app, "Tecnologia")
    _mov(admin, 1, (HOJE - timedelta(days=100)).isoformat(), "649,00", "WORTEN AMADORA", sentido="saida")
    with app.state.SessionLocal() as db:
        m = db.scalar(select(Movimento))
        m.categoria_id = tecnologia
        db.commit()
    # marcar a categoria-mãe «Compras» vale para as filhas e cria logo o item da compra já classificada
    assert admin.get("/admin/recheio", follow_redirects=False).headers["location"] == "/patrimonio/recheio"
    f = admin.get("/patrimonio/recheio?entidade=1")
    assert "Que categorias são recheio" in f.text and 'action="/admin/recheio"' in f.text
    admin.post("/admin/recheio", data={"csrf": _csrf(f), "categorias": [str(compras)], "garantia": "36"})
    with app.state.SessionLocal() as db:
        it = db.scalar(select(ItemRecheio))
        assert (it.valor, it.data_compra, it.tipo, it.garantia_meses) == (64900, HOJE - timedelta(days=100), "informatica", 36)
        assert it.bem.nome == "Casa" and it.loja == "Worten Amadora" and it.nome == "Compra · Worten Amadora"
        assert it.estado_garantia(HOJE) == "em garantia"
    # nova compra classificada à mão numa categoria de recheio → item novo
    _mov(admin, 1, HOJE.isoformat(), "399,00", "IKEA LOURES", sentido="saida")
    with app.state.SessionLocal() as db:
        mid = db.scalar(select(Movimento.id).where(Movimento.descricao_original == "IKEA LOURES"))
    v = admin.get("/contas/1/movimentos")
    r = admin.post("/movimentos-lote", data={"csrf": _csrf(v), "acao": f"categoria:{_cat(app, 'Casa e decoração')}",
                                             "ids": [str(mid)], "voltar": "/contas/1/movimentos"}, follow_redirects=False)
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        assert db.query(ItemRecheio).count() == 2
        assert db.scalar(select(ItemRecheio).where(ItemRecheio.movimento_id == mid)).tipo == "decoracao"
    ficha = admin.get("/patrimonio/1").text
    assert "Recheio" in ficha and "1.048,00 €" in ficha and "Compra · Worten Amadora" in ficha
    # abaixo do valor mínimo (50 €) não entra
    _mov(admin, 1, HOJE.isoformat(), "4,53", "LEROY MERLIN", sentido="saida")
    with app.state.SessionLocal() as db:
        from app.qualificacao import qualificar_manual
        m = db.scalar(select(Movimento).where(Movimento.descricao_original == "LEROY MERLIN"))
        qualificar_manual(db, m, categoria_id=_cat(app, "Casa e decoração"))
        db.commit()
        assert db.query(ItemRecheio).count() == 2


def test_ficha_garantia_pedido_e_seguro(admin, app):
    _mov(admin, 1, (HOJE - timedelta(days=1060)).isoformat(), "500,00", "FNAC", sentido="saida")
    with app.state.SessionLocal() as db:
        c = db.scalar(select(Categoria).where(Categoria.nome == "Tecnologia"))
        c.recheio = True
        db.commit()
        m = db.scalar(select(Movimento))
        from app.qualificacao import qualificar_manual
        qualificar_manual(db, m, categoria_id=c.id)
        db.commit()
        iid = db.scalar(select(ItemRecheio.id))
    item = admin.get(f"/patrimonio/recheio/{iid}")
    assert "termina em breve" in item.text                                   # 36 meses quase a acabar
    admin.post(f"/patrimonio/recheio/{iid}", data={"csrf": _csrf(item), "nome": "Portátil", "tipo": "informatica",
               "marca": "Lenovo", "modelo": "T14", "n_serie": "PF12345", "data_compra": (HOJE - timedelta(days=1060)).isoformat(),
               "valor": "500,00", "garantia_meses": "36", "loja": "Fnac", "contacto_email": "apoio@fnac.pt",
               "estado": "ativo", "bem_id": "1"})
    admin.post(f"/patrimonio/recheio/{iid}/pedidos", data={"csrf": _csrf(item), "problema": "Ecrã com riscas", "referencia": "R-1"})
    with app.state.SessionLocal() as db:
        it = db.get(ItemRecheio, iid)
        assert it.marca == "Lenovo" and it.estado == "avariado" and it.pedido_aberto.referencia == "R-1"
        pid = it.pedidos[0].id
        proximos, _ = inicio.agenda(db, [db.get(Entidade, 1)], app.state.prefs, HOJE)
        titulos = [i.titulo for i in proximos]
        assert "Pedido de garantia · Portátil" in titulos and "Fim da garantia · Portátil" in titulos
    admin.post(f"/patrimonio/recheio/{iid}/pedidos", data={"csrf": _csrf(item), "id": str(pid), "estado": "resolvido",
               "resolucao": "Ecrã trocado"})
    with app.state.SessionLocal() as db:
        it = db.get(ItemRecheio, iid)
        assert it.estado == "ativo" and it.pedidos[0].data_fecho == HOJE and it.pedido_aberto is None
    # capital seguro abaixo do valor do recheio → subseguro
    f = admin.get("/patrimonio/1")
    admin.post("/patrimonio/1/capital-recheio", data={"csrf": _csrf(f), "capital": "300,00"})
    assert "subseguro" in admin.get("/patrimonio/1").text
    # «não é recheio»: sai do inventário e não volta a ser criado
    admin.post(f"/patrimonio/recheio/{iid}/apagar", data={"csrf": _csrf(f)})
    with app.state.SessionLocal() as db:
        assert db.get(ItemRecheio, iid).estado == "ignorado"
        from app import recheio
        assert recheio.preencher(db, 1) == 0


def test_adicionar_ao_recheio_a_partir_do_movimento(admin, app):
    """Na edição de um movimento: «Adicionar ao recheio» mesmo sem categoria de recheio e abaixo do valor mínimo."""
    d = (date.today() - timedelta(days=5)).isoformat()
    _mov(admin, 1, d, "29,90", "LOJA ABC CANDEEIRO", sentido="saida")
    _mov(admin, 1, d, "100,00", "REEMBOLSO")
    with app.state.SessionLocal() as db:
        compra, credito = db.scalars(select(Movimento).order_by(Movimento.id)).all()
        ids = compra.id, credito.id
    p = admin.get(f"/movimentos/{ids[0]}").text
    assert "Adicionar ao recheio" in p and '<option value="1" selected>Casa</option>' in p
    assert "Adicionar ao recheio" not in admin.get(f"/movimentos/{ids[1]}").text       # créditos não
    r = admin.post(f"/movimentos/{ids[0]}/recheio", data={"csrf": _csrf(admin.get(f"/movimentos/{ids[0]}")), "bem_id": "1"},
                   follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/patrimonio/recheio/")
    with app.state.SessionLocal() as db:
        item = db.scalars(select(ItemRecheio)).one()
        assert (item.movimento_id, item.bem_id, item.valor, item.data_compra.isoformat()) == (ids[0], 1, 2990, d)
    p = admin.get(f"/movimentos/{ids[0]}").text
    assert "Adicionar ao recheio" not in p and f'href="/patrimonio/recheio/{item.id}"' in p
    # segunda vez: não duplica, abre a ficha existente
    r = admin.post(f"/movimentos/{ids[0]}/recheio", data={"csrf": _csrf(admin.get(f"/movimentos/{ids[0]}"))},
                   follow_redirects=False)
    assert r.headers["location"] == f"/patrimonio/recheio/{item.id}"
    r = admin.post(f"/movimentos/{ids[1]}/recheio", data={"csrf": _csrf(admin.get(f"/movimentos/{ids[1]}"))},
                   follow_redirects=False)
    assert r.headers["location"] == f"/movimentos/{ids[1]}"
    with app.state.SessionLocal() as db:
        assert db.scalars(select(ItemRecheio)).all().__len__() == 1
