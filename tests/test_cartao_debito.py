"""Cartões de débito: o ficheiro de cada cartão identifica que cartão (pessoa) fez cada compra da conta à ordem.
Ficheiros fictícios gerados aqui (nunca extratos reais no git)."""
import re
from datetime import date

import pytest
import xlwt
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import cartoes
from app.main import create_app
from app.models import CartaoDebito, Movimento, MovimentoCartao
from app.parsers import ler_extrato
from tests.test_app import PW, _csrf, _post
from tests.test_importacao import xls_santander


def xls_debito(final, linhas) -> bytes:
    """linhas: (data 'dd-mm-aaaa', montante, descritivo), mais recente primeiro (como o NetBanco exporta)."""
    import io
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Movimentos")
    for i, t in enumerate(["Domingo, 18 de Outubro de 2026", "TITULAR FICTICIO", "", "Consulta  Movimentos Cartões Débito",
                           f"Nº Cartão XXXXXXXXXXXX{final}", f"Nome Favorito: XXXXXXXXXXXX{final}", "", "Listagem Movimentos"]):
        ws.write(i, 0, t)
    for j, t in enumerate(["Data Operação", "Montante", "Descritivo"]):
        ws.write(8, j, t)
    for i, (d, v, desc) in enumerate(linhas, start=9):
        for j, x in enumerate([d, str(v), desc]):
            ws.write(i, j, x)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


CONTA = [  # extrato da conta (mais recente primeiro): as compras dos dois cartões, sem dizer de que cartão
    ("12-10-2026", "Royal Versailles", -1.7, 1000.00),
    ("12-10-2026", "Royal Versailles", -1.7, 1001.70),
    ("10-10-2026", "Mercadona Lisboa", -45.3, 1003.40),
    ("08-10-2026", "Farmacia Central", -12.0, 1048.70),
    ("05-10-2026", "Vencimento", 1500.0, 1060.70),
]
SERGIO = [("13-10-2026", -9.9, "COMPRA TPA*1264 PADARIA (ainda por lançar)"),
          ("11-10-2026", -1.7, "COMPRA TPA*1264 ROYAL VERSAILLES NTRO TO"),
          ("09-10-2026", -45.3, "COMPRA TPA*1264 MERCADONA LISBOA")]
ANA = [("11-10-2026", -1.7, "COMPRA TPA*5555 ROYAL VERSAILLES NTRO TO"),
       ("07-10-2026", -12.0, "COMPRA TPA*5555 FARMACIA CENTRAL")]


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Santander ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _post(c, "/contas/nova?entidade=1", nome="Cartão crédito", tipo="CARTAO_CREDITO", moeda="EUR", instituicao_id="1",
          conta_pagamento_id="1", dia_fecho="5", dia_pagamento="20")
    return c


def _carregar(c, nome, conteudo, conta_id="1"):
    r = c.get("/importar")
    return c.post("/importar", data={"csrf": _csrf(r), "conta_id": conta_id}, files={"ficheiro": (nome, conteudo)})


def _confirmar_cartao(c, previa, nome, conta_id="1"):
    sha = re.search(r'name="sha" value="([0-9a-f]+)"', previa.text).group(1)
    return c.post("/importar/cartao/confirmar", data={"csrf": _csrf(previa), "conta_id": conta_id, "sha": sha, "nome": nome})


def _importar_conta(c):
    previa = _carregar(c, "conta.xls", xls_santander(CONTA))
    sha = re.search(r'name="sha" value="([0-9a-f]+)"', previa.text).group(1)
    c.post("/importar/confirmar", data={"csrf": _csrf(previa), "conta_id": "1", "sha": sha, "nome": "conta.xls"})


def test_parser():
    e = ler_extrato("descarga.xls", xls_debito("1264", SERGIO))
    assert (e.parser, e.debito_cartao, e.conta_ref, len(e.movimentos)) == ("santander_debito", True, "1264", 3)
    assert e.movimentos[0].data_mov == date(2026, 10, 9) and e.movimentos[0].montante == -4530   # cronológico


def test_cartoes_ligam_e_gastos_por_pessoa(admin, app):
    _importar_conta(admin)
    n_movs = lambda: len(admin.app.state.SessionLocal().scalars(select(Movimento)).all())
    antes = n_movs()
    # ficheiro do cartão carregado numa conta errada (cartão de crédito): passa para a conta à ordem
    p = _carregar(admin, "sergio.xls", xls_debito("1264", SERGIO), conta_id="2")
    assert "Cartão de débito …1264" in p.text and "escolhida «Santander ordem»" in p.text
    assert "<strong>3</strong>" in p.text and "1 sem débito na conta" in p.text
    r = _confirmar_cartao(admin, p, "sergio.xls")
    assert "3 compras novas, 2 ligadas ao débito" in r.text and r.url.path == "/contas/1/cartoes"
    assert n_movs() == antes                                                    # não cria movimentos
    _confirmar_cartao(admin, _carregar(admin, "ana.xls", xls_debito("5555", ANA)), "ana.xls")
    with app.state.SessionLocal() as db:
        sergio, ana = (db.scalar(select(CartaoDebito).where(CartaoDebito.final == f)) for f in ("1264", "5555"))
        ligados = {mc.cartao_id: [] for mc in db.scalars(select(MovimentoCartao))}
        for mc in db.scalars(select(MovimentoCartao).where(MovimentoCartao.movimento_id.is_not(None))):
            ligados[mc.cartao_id].append(db.get(Movimento, mc.movimento_id).descricao_original)
        assert sorted(ligados[sergio.id]) == ["Mercadona Lisboa", "Royal Versailles"]
        assert sorted(ligados[ana.id]) == ["Farmacia Central", "Royal Versailles"]       # os dois cafés iguais: um para cada
        sid, aid = sergio.id, ana.id
    # dar nome a cada cartão
    pg = admin.get("/contas/1/cartoes")
    admin.post("/contas/1/cartoes", data={"csrf": _csrf(pg), f"nome_{sid}": "Sérgio", f"nome_{aid}": "Ana", f"cor_{aid}": "#e87ba4"})
    pg = admin.get("/contas/1/cartoes").text
    assert "Sérgio" in pg and "Ana" in pg and "47,00 €" in pg and "13,70 €" in pg     # 1,70+45,30 · 1,70+12,00
    assert "Compras lidas ainda sem débito na conta: Sérgio 1" in pg
    lista = admin.get("/contas/1/movimentos").text
    assert "💳 Sérgio" in lista and "💳 Ana" in lista and "Cartões de débito" in lista
    so_ana = admin.get(f"/movimentos?entidade=1&cartao={aid}").text
    assert "Só as compras do cartão" in so_ana and "Farmacia Central" in so_ana and "Mercadona Lisboa" not in so_ana
    assert f'name="f_cartao" value="{aid}"' in so_ana
    # voltar a carregar o mesmo ficheiro não duplica
    r = _confirmar_cartao(admin, _carregar(admin, "sergio.xls", xls_debito("1264", SERGIO)), "sergio.xls")
    assert "0 compras novas" in r.text


def test_cartao_antes_do_extrato_da_conta(admin, app):
    """O ficheiro do cartão pode chegar antes: as compras ligam-se quando o extrato da conta for importado."""
    _confirmar_cartao(admin, _carregar(admin, "ana.xls", xls_debito("5555", ANA)), "ana.xls")
    with app.state.SessionLocal() as db:
        assert db.scalar(select(MovimentoCartao).where(MovimentoCartao.movimento_id.is_not(None))) is None
    _importar_conta(admin)
    with app.state.SessionLocal() as db:
        assert len(db.scalars(select(MovimentoCartao).where(MovimentoCartao.movimento_id.is_not(None))).all()) == 2
        assert cartoes.detetar_conta(db, ler_extrato("x.xls", xls_debito("5555", ANA)))[1].startswith("cartão …5555 já registado")


def test_cartao_pela_pasta_partilhada(tmp_path, monkeypatch):
    from app.models import ImportacaoPendente
    raiz = tmp_path / "extratos"
    (raiz / "Santander").mkdir(parents=True)
    monkeypatch.setenv("GESTOR_IMPORT_DIR", str(raiz))
    app = create_app(tmp_path / "dados", backups_automaticos=False)
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Santander ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _importar_conta(c)
    (raiz / "Santander" / "cartao.xls").write_bytes(xls_debito("1264", SERGIO))
    c.post("/admin/pasta/ler", data={"csrf": _csrf(c.get("/admin/tarefas"))})
    with app.state.SessionLocal() as db:
        p = db.scalar(select(ImportacaoPendente))
        assert (p.conta_id, p.n_novos, p.estado) == (1, 3, "pendente")
        pid = p.id
    v = c.get(f"/admin/pasta/{pid}")
    assert "Validar e ligar 2 compras" in v.text
    sha = re.search(r'name="sha" value="([0-9a-f]+)"', v.text).group(1)
    r = c.post("/importar/cartao/confirmar", follow_redirects=False,
               data={"csrf": _csrf(v), "conta_id": "1", "sha": sha, "nome": "cartao.xls", "pendente": str(pid)})
    assert r.headers["location"] == "/admin/tarefas#pasta"
    assert (raiz / "Santander" / "importados" / f"{date.today().isoformat()}_cartao.xls").exists()
