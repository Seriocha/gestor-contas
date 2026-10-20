"""Importar › Recibos › Valores: lê o texto dos recibos de vencimento no Paperless, tira Período, Bruto, Descontos, Cartão
refeição e Líquido, valida as contas, grava os campos personalizados (sem perder os que lá estavam) ou marca «Valor por
verificar», e liga cada recibo ao crédito do vencimento. Textos sintéticos com a estrutura dos recibos reais (sem dados
pessoais); o Paperless é simulado em memória."""
import hashlib
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import paperless, recibos
from app.main import create_app
from app.models import Auditoria, DocumentoLigado, Movimento
from tests.test_app import PW, _csrf, _post

CABECALHO = """AVISO DE CRÉDITO / DÉBITO
Empresa Exemplo, S.A.
Rua do Exemplo, 1 1000-000 Lisboa
Nome Colaborador Exemplo Número 000000
Categoria Técnico Nível 09
2.490,40€ Rendimento Líquido Colaborador
"""

# 2025-12 (formato de 2025: mês por extenso)
DEZ_2025 = CABECALHO + """DEZEMBRO DE 2025 Encargos
Abonos
VENCIMENTO BASE 202512 3.500,00
SUBSIDIO DE ALMOCO 202512 22,00 10,23 225,06
ISENCAO HORARIO TRABALHO 202512 278,95
CARTAO REFEICAO - VALOR A TRANSFERIR 202512 -200,03
Descontos
SEGURANCA SOCIAL 202512 11,00 440,44
I.R.S. 202512 1.073,17
Acumulados
CDEF 30.571,80 458,62 Abonos 3.803,98
I.R.S. 12.000,00 1.073,17 Descontos 1.513,61
S.A.M.S. 100,00 10,00 Líquido 2.290,37
"""

# 2024-05 (formato «Aviso de Crédito» de 2024: sem o mês por extenso → AAAAMM mais frequente)
MAI_2024 = CABECALHO.replace(" / DÉBITO", "") + """Período de processamento
Abonos
VENCIMENTO BASE 202405 3.500,00
SUBSIDIO DE ALMOCO 202405 22,00 10,51 231,32
ISENCAO HORARIO TRABALHO 202405 178,95
CARTAO REFEICAO - VALOR A TRANSFERIR 202405 -220,21
Descontos
SEGURANCA SOCIAL 202405 11,00 430,00
I.R.S. 202405 1.063,69
Acumulados
CDEF 15.000,00 300,00 Abonos 3.690,06
I.R.S. 5.000,00 1.063,69 Descontos 1.493,69
S.A.M.S. 50,00 5,00 Líquido 2.196,37
"""

# janeiro: acerto negativo das férias (não é o cartão) e sem linha do cartão
JAN_2025 = CABECALHO + """JANEIRO DE 2025 Encargos
Abonos
VENCIMENTO BASE 202501 3.500,00
SUBSIDIO DE ALMOCO FERIAS 202412 -240,00
ISENCAO HORARIO TRABALHO 202501 278,95
Descontos
SEGURANCA SOCIAL 202501 11,00 389,28
I.R.S. 202501 610,72
Acumulados
CDEF 3.538,95 389,28 Abonos 3.538,95
I.R.S. 3.538,95 610,72 Descontos 1.000,00
S.A.M.S. 10,00 1,00 Líquido 2.538,95
"""

ERRADO = DEZ_2025.replace("Líquido 2.290,37", "Líquido 2.390,37")


def test_casos_da_especificacao():
    r = recibos.extrair(DEZ_2025)
    assert r.valido, r.erros
    assert (r.periodo, r.bruto, r.descontos, r.cartao, r.liquido, r.abonos) == ("2025-12", 400401, 151361, 20003, 229037, 380398)
    r = recibos.extrair(MAI_2024)
    assert r.valido, r.erros
    assert (r.periodo, r.bruto, r.descontos, r.cartao, r.liquido, r.abonos) == ("2024-05", 391027, 149369, 22021, 219637, 369006)


def test_negativos_que_nao_sao_o_cartao_e_sem_cartao():
    r = recibos.extrair(JAN_2025)
    assert r.valido, r.erros
    assert (r.periodo, r.bruto, r.cartao, r.liquido) == ("2025-01", 353895, 0, 253895) and r.sem_cartao


def test_contas_que_nao_batem_e_texto_vazio():
    r = recibos.extrair(ERRADO)
    assert not r.valido and "Bruto − Descontos − Cartão ≠ Líquido" in r.erros
    assert not recibos.extrair("").valido and "texto curto" in recibos.extrair("").erros[0]


# ------------------------------------------------------------------ Paperless simulado
CAMPOS = [{"id": 2, "name": "Mes e Ano do Extracto"}, {"id": 3, "name": "Período"}, {"id": 6, "name": "Bruto"},
          {"id": 7, "name": "Descontos"}, {"id": 8, "name": "Líquido"}, {"id": 9, "name": "Cartão refeição"}]


@pytest.fixture
def falso(monkeypatch):
    monkeypatch.setenv("GESTOR_PAPERLESS_URL", "http://paperless:8000")
    monkeypatch.setenv("GESTOR_PAPERLESS_TOKEN", "token-admin")
    docs = {
        235: {"id": 235, "title": "Recibo 2025-12", "content": DEZ_2025, "tags": [1], "custom_fields": [{"field": 2, "value": "x"}]},
        240: {"id": 240, "title": "Recibo 2024-05", "content": MAI_2024, "tags": [1], "custom_fields": []},
        250: {"id": 250, "title": "Recibo errado", "content": ERRADO, "tags": [1], "custom_fields": []},
    }
    estado = {"docs": docs, "tags": [{"id": 1, "name": "Vencimento"}], "patches": [], "posts": []}

    def pedido(caminho, params=None, binario=False, token=None):
        if caminho == "/api/document_types/":
            return {"results": [{"id": 4, "name": "Recibo de vencimento"}]}
        if caminho == "/api/custom_fields/":
            return {"results": CAMPOS}
        if caminho == "/api/tags/":
            return {"results": estado["tags"]}
        if caminho == "/api/documents/":
            assert params["document_type__id"] == 4
            return {"results": [dict(d) for d in docs.values()], "next": None}
        raise AssertionError(caminho)

    def enviar(metodo, caminho, corpo, *, token):
        if caminho == "/api/tags/":
            novo = {"id": 99, **corpo}
            estado["tags"].append(novo)
            estado["posts"].append(corpo)
            return novo
        did = int(caminho.strip("/").split("/")[-1])
        assert metodo == "PATCH"
        estado["patches"].append((did, corpo))
        docs[did] = {**docs[did], **corpo}
        return docs[did]
    monkeypatch.setattr(paperless, "_pedido", pedido)
    monkeypatch.setattr(paperless, "enviar", enviar)
    return estado


def _credito(db, d, valor):
    h = hashlib.sha256(f"{d}{valor}".encode()).hexdigest()
    db.add(Movimento(conta_id=1, data_mov=d, descricao_original="TRF VENCIMENTO", descricao_norm="TRF VENCIMENTO",
                     montante=valor, moeda="EUR", hash_dedup=h))


@pytest.fixture
def admin(tmp_path, falso):
    app = create_app(tmp_path, backups_automaticos=False)
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    with app.state.SessionLocal() as db:
        _credito(db, date(2025, 12, 22), 229037)                                  # o vencimento de dezembro
        _credito(db, date(2024, 5, 24), 219637)                                   # maio…
        _credito(db, date(2024, 6, 3), 219637)                                    # …e outro igual na janela → ambíguo
        _credito(db, date(2025, 10, 22), 229037)                                  # fora da janela
        db.commit()
    c.app_ = app
    return c


def _correr(c, acao, **params):
    t = c.get("/importar/recibos")
    return c.post("/importar/recibos/valores", data={"csrf": _csrf(t), "acao": acao, "utilizador": "1", **params})


def test_ensaio_nao_altera_nada(admin, falso):
    r = _correr(admin, "ensaio")
    assert r.status_code == 200 and "nada foi alterado" in r.text and "2.290,37" in r.text
    assert not falso["patches"] and not falso["posts"]
    with admin.app_.state.SessionLocal() as db:
        assert db.scalar(select(DocumentoLigado)) is None


def test_gravar_preserva_campos_marca_e_liga(admin, falso):
    r = _correr(admin, "gravar")
    assert r.status_code == 200
    docs = falso["docs"]
    # 235: campos novos + o que já lá estava (Mes e Ano do Extracto)
    campos = {c["field"]: c["value"] for c in docs[235]["custom_fields"]}
    assert campos == {2: "x", 3: "2025-12", 6: "EUR4004.01", 7: "EUR1513.61", 8: "EUR2290.37", 9: "EUR200.03"}
    assert {c["field"]: c["value"] for c in docs[240]["custom_fields"]}[8] == "EUR2196.37"
    # 250: contas erradas → sem valores e com «Valor por verificar» (criada)
    assert docs[250]["custom_fields"] == [] and 99 in docs[250]["tags"] and falso["posts"][0]["name"] == "Valor por verificar"
    with admin.app_.state.SessionLocal() as db:
        lig = db.scalars(select(DocumentoLigado)).all()
        assert [(x.paperless_id, db.get(Movimento, x.movimento_id).data_mov) for x in lig] == [(235, date(2025, 12, 22))]
        assert db.scalar(select(Auditoria).where(Auditoria.acao == "recibos_valores")) is not None
    assert "ambíguo" in r.text and "valor por verificar" in r.text


def test_segunda_corrida_nao_muda_nada(admin, falso):
    _correr(admin, "gravar")
    n = len(falso["patches"])
    with admin.app_.state.SessionLocal() as db:
        ligados = len(db.scalars(select(DocumentoLigado)).all())
    r = _correr(admin, "gravar")
    assert len(falso["patches"]) == n and "já tinha valores" in r.text
    with admin.app_.state.SessionLocal() as db:
        assert len(db.scalars(select(DocumentoLigado)).all()) == ligados


def test_corrigido_e_forcado_tira_a_etiqueta(admin, falso):
    _correr(admin, "gravar")
    falso["docs"][250]["content"] = DEZ_2025.replace("DEZEMBRO DE 2025", "NOVEMBRO DE 2025").replace("202512", "202511")
    _correr(admin, "gravar", forcar="sim")
    d = falso["docs"][250]
    assert 99 not in d["tags"] and {c["field"]: c["value"] for c in d["custom_fields"]}[3] == "2025-11"
    assert {c["field"] for c in falso["docs"][235]["custom_fields"]} == {2, 3, 6, 7, 8, 9}      # forçado: sem duplicar


def test_sem_token(admin, falso, monkeypatch):
    monkeypatch.delenv("GESTOR_PAPERLESS_TOKEN")
    r = _correr(admin, "gravar")
    assert r.status_code == 400 and "não tem token do Paperless" in r.text and not falso["patches"]
