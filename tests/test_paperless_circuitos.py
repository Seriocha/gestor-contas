"""Administração › Paperless: circuitos de documentos criados pela app (em vez dos scripts PowerShell). O Paperless é
simulado em memória: pré-visualizar não cria nada, aplicar cria só o que falta (idempotente), o fluxo de entrada aponta
para os elementos criados e fica com o dono do token, e a palavra-passe da conta de email não fica no Gestor."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import paperless
from app.main import create_app
from app.models import Auditoria
from tests.test_app import PW, _csrf, _post


@pytest.fixture
def falso(monkeypatch):
    monkeypatch.setenv("GESTOR_PAPERLESS_URL", "http://paperless:8000")
    monkeypatch.setenv("GESTOR_PAPERLESS_TOKEN", "token-admin")
    estado = {"dados": {}, "seq": 100, "enviados": [], "rejeitar_campos_no_fluxo": False}

    def pedido(caminho, params=None, binario=False, token=None):
        if caminho == "/api/ui_settings/":
            return {"user": {"id": 3, "username": "sergio"}}
        ep = caminho.strip("/").split("/")[1]
        itens = estado["dados"].get(ep, [])
        nome = (params or {}).get("name__iexact")
        if ep in ("workflows", "mail_accounts", "mail_rules"):
            nome = None                                  # como o Paperless real: estes ignoram o filtro por nome
        res = [x for x in itens if nome is None or x["name"].lower() == nome.lower()]
        return {"count": len(res), "results": res}

    def enviar(metodo, caminho, corpo, *, token):
        ep = caminho.strip("/").split("/")[1]
        if ep == "workflows" and estado["rejeitar_campos_no_fluxo"] and "assign_custom_fields" in corpo["actions"][0]:
            raise paperless.ErroPaperless("O Paperless respondeu 400.")
        estado["seq"] += 1
        novo = {"id": estado["seq"], **corpo}
        estado["dados"].setdefault(ep, []).append(novo)
        estado["enviados"].append((ep, corpo))
        return novo
    monkeypatch.setattr(paperless, "_pedido", pedido)
    monkeypatch.setattr(paperless, "enviar", enviar)
    return estado


@pytest.fixture
def admin(tmp_path, falso):
    app = create_app(tmp_path, backups_automaticos=False)
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    c.app_ = app
    return c


def _correr(c, modelo, acao, **params):
    t = c.get(f"/admin/paperless?modelo={modelo}")
    return c.post("/admin/paperless", data={"csrf": _csrf(t), "modelo": modelo, "acao": acao, "utilizador": "1", **params})


def test_recibo_ver_aplicar_e_repetir(admin, falso):
    assert "Circuitos de documentos no Paperless" in admin.get("/admin/paperless").text
    r = _correr(admin, "recibo_vencimento", "ver", empregador="Banco Santander Totta")
    assert "Pré-visualização" in r.text and "10 elementos a criar. Nada foi alterado." in r.text and not falso["enviados"]
    r = _correr(admin, "recibo_vencimento", "aplicar", empregador="Banco Santander Totta")
    assert "Resultado" in r.text and "dono: sergio" in r.text
    criados = [(ep, c["name"]) for ep, c in falso["enviados"]]
    assert ("custom_fields", "Líquido") in criados and ("storage_paths", "Pessoal - Vencimentos") in criados
    fluxo = next(c for ep, c in falso["enviados"] if ep == "workflows")
    acao = fluxo["actions"][0]
    ids = {(ep, c["name"]): x["id"] for ep in falso["dados"] for x in falso["dados"][ep] for c in [x]}
    assert acao["assign_owner"] == 3 and acao["assign_document_type"] == ids[("document_types", "Recibo de vencimento")]
    assert acao["assign_correspondent"] == ids[("correspondents", "Banco Santander Totta")]
    assert len(acao["assign_custom_fields"]) == 5 and fluxo["triggers"][0]["match"] == "abonos descontos acumulados"
    assert fluxo["triggers"][0]["matching_algorithm"] == 2
    # repetir não cria nada
    n = len(falso["enviados"])
    r = _correr(admin, "recibo_vencimento", "aplicar", empregador="Banco Santander Totta")
    assert len(falso["enviados"]) == n and "já existe" in r.text
    # outra pessoa: nomes e caminho próprios, reaproveita os campos
    _correr(admin, "recibo_vencimento", "aplicar", empregador="Outra Empresa SA", sufixo="Ana", filtro="abonos descontos acumulados 123456789")
    novos = [(ep, c["name"]) for ep, c in falso["enviados"][n:]]
    assert ("workflows", "Entrada Vencimento - Ana") in novos and ("storage_paths", "Pessoal - Vencimentos - Ana") in novos
    assert not any(ep == "custom_fields" for ep, _ in novos)
    caminho = next(c for ep, c in falso["enviados"][n:] if ep == "storage_paths")["path"]
    assert caminho == "Pessoal/Vencimentos/Ana/{{ created_year }}/{{ title }}"
    with admin.app_.state.SessionLocal() as db:
        assert db.scalar(select(Auditoria).where(Auditoria.acao == "paperless_circuito")) is not None


def test_fatura_regra_de_email_precisa_da_conta(admin, falso):
    r = _correr(admin, "fatura_fornecedor", "aplicar", nome="Vodafone", debito_direto="sim", remetente="vodafone.pt", conta="Gmail Lar")
    assert "em falta" in r.text and "Gmail Lar" in r.text
    assert ("tags", "Débito direto") in [(ep, c["name"]) for ep, c in falso["enviados"]]
    assert not any(ep == "mail_rules" for ep, _ in falso["enviados"])
    # criar a conta (palavra-passe só no momento) e voltar a aplicar: só falta a regra
    _correr(admin, "conta_email", "aplicar", nome="Gmail Lar", utilizador="eu@gmail.com", senha="abcd efgh")
    conta = next(c for ep, c in falso["enviados"] if ep == "mail_accounts")
    assert conta["password"] == "abcd efgh" and conta["imap_server"] == "imap.gmail.com"
    with admin.app_.state.SessionLocal() as db:
        assert all("abcd" not in (a.detalhe or "") for a in db.scalars(select(Auditoria)))
    n = len(falso["enviados"])
    _correr(admin, "fatura_fornecedor", "aplicar", nome="Vodafone", debito_direto="sim", remetente="vodafone.pt", conta="Gmail Lar")
    assert [ep for ep, _ in falso["enviados"][n:]] == ["mail_rules"]
    regra = falso["enviados"][-1][1]
    assert regra["filter_from"] == "vodafone.pt"
    assert regra["account"] == falso["dados"]["mail_accounts"][0]["id"] and "_conta" not in regra


def test_fluxo_sem_campos_quando_o_paperless_os_recusa(admin, falso):
    falso["rejeitar_campos_no_fluxo"] = True
    r = _correr(admin, "fatura_fornecedor", "aplicar", nome="EDP")
    fluxo = next(c for ep, c in falso["enviados"] if ep == "workflows")
    assert "assign_custom_fields" not in fluxo["actions"][0] and "sem campos personalizados" in r.text


def test_validacao_e_sem_token(admin, falso, monkeypatch):
    r = _correr(admin, "fatura_fornecedor", "ver", nome="")
    assert r.status_code == 400 and "Indica o nome do fornecedor." in r.text
    monkeypatch.delenv("GESTOR_PAPERLESS_TOKEN")
    r = _correr(admin, "recibo_vencimento", "ver", empregador="X")
    assert "não tem token do Paperless" in r.text and not falso["enviados"]


def test_fluxo_de_outro_circuito_nao_conta_como_existente(admin, falso):
    """Regressão: o Paperless devolve todos os fluxos (ignora name__iexact); «Entrada Lar» não é «Entrada Vencimento»."""
    falso["dados"]["workflows"] = [{"id": 6, "name": "Entrada Lar"}]
    r = _correr(admin, "recibo_vencimento", "ver", empregador="Banco Santander Totta")
    assert "10 elementos a criar" in r.text
    _correr(admin, "recibo_vencimento", "aplicar", empregador="Banco Santander Totta")
    assert [c["name"] for ep, c in falso["enviados"] if ep == "workflows"] == ["Entrada Vencimento"]
