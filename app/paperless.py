"""Ligação ao Paperless-ngx da NAS (documentos: faturas, recibos, garantias, contratos).

O Paperless guarda os documentos e faz o OCR; o Gestor só os procura (pela API REST) e guarda a ligação entre um
documento e um movimento ou um item do recheio. Configuração no ficheiro paperless.env da NAS:
  GESTOR_PAPERLESS_URL    endereço visto pelo container do Gestor (ex.: http://paperless:8000, na rede «nas-apps»)
  GESTOR_PAPERLESS_TOKEN  token da API (Paperless › O meu perfil › Token da API)
  GESTOR_PAPERLESS_LINK   endereço para o browser abrir os documentos (ex.: http://192.168.1.232:8300)
Sem URL e token, as opções do Paperless não aparecem.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta


class ErroPaperless(RuntimeError):
    pass


def url_api() -> str:
    return (os.environ.get("GESTOR_PAPERLESS_URL") or "").rstrip("/")


def url_browser() -> str:
    return (os.environ.get("GESTOR_PAPERLESS_LINK") or url_api()).rstrip("/")


def disponivel() -> bool:
    return bool(url_api() and os.environ.get("GESTOR_PAPERLESS_TOKEN"))


def _pedido(caminho: str, params: dict | None = None, binario: bool = False):
    if not disponivel():
        raise ErroPaperless("O Paperless não está configurado (paperless.env na NAS).")
    url = f"{url_api()}{caminho}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    pedido = urllib.request.Request(url, headers={"Authorization": f"Token {os.environ.get('GESTOR_PAPERLESS_TOKEN', '')}",
                                                  "Accept": "application/json; version=5"})
    try:
        with urllib.request.urlopen(pedido, timeout=20) as r:
            dados = r.read()
    except urllib.error.HTTPError as e:
        raise ErroPaperless(f"O Paperless respondeu {e.code}." + (" Confirma o token." if e.code in (401, 403) else "")) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ErroPaperless(f"Sem ligação ao Paperless ({e}).") from e
    return dados if binario else json.loads(dados.decode())


@dataclass
class Documento:
    id: int
    titulo: str
    data: date | None
    correspondente: str | None
    tipo: str | None
    ficheiro: str | None

    @property
    def link(self) -> str:
        return f"{url_browser()}/documents/{self.id}/details"


def _data(txt) -> date | None:
    try:
        return date.fromisoformat(str(txt)[:10])
    except (TypeError, ValueError):
        return None


def _nomes(caminho: str) -> dict[int, str]:
    """id → nome (correspondentes, tipos de documento)."""
    try:
        r = _pedido(caminho, {"page_size": 1000})
    except ErroPaperless:
        return {}
    return {x["id"]: x.get("name") for x in r.get("results", [])}


def procurar(texto: str = "", de: date | None = None, ate: date | None = None, limite: int = 12) -> list[Documento]:
    """Pesquisa no texto (OCR), título e correspondente; opcionalmente só entre duas datas (data do documento)."""
    params = {"query": texto.strip() or None, "page_size": limite, "ordering": "-created", "truncate_content": "true",
              "created__date__gt": (de - timedelta(days=1)).isoformat() if de else None,
              "created__date__lt": (ate + timedelta(days=1)).isoformat() if ate else None}
    r = _pedido("/api/documents/", params)
    correspondentes = _nomes("/api/correspondents/")
    tipos = _nomes("/api/document_types/")
    return [Documento(d["id"], d.get("title") or f"Documento {d['id']}", _data(d.get("created")),
                      correspondentes.get(d.get("correspondent")), tipos.get(d.get("document_type")),
                      d.get("original_file_name")) for d in r.get("results", [])]


def documento(pid: int) -> Documento:
    d = _pedido(f"/api/documents/{int(pid)}/")
    return Documento(d["id"], d.get("title") or f"Documento {d['id']}", _data(d.get("created")),
                     _nomes("/api/correspondents/").get(d.get("correspondent")),
                     _nomes("/api/document_types/").get(d.get("document_type")), d.get("original_file_name"))


def miniatura(pid: int) -> bytes:
    return _pedido(f"/api/documents/{int(pid)}/thumb/", binario=True)


def valor_txt(cent: int) -> str:
    """Como o valor aparece numa fatura portuguesa: 1234,56 → «1234,56» (a pesquisa também apanha «1.234,56»)."""
    return f"{abs(cent) / 100:.2f}".replace(".", ",")


def sugestoes(m, limite: int = 8) -> list[tuple[Documento, str]]:
    """Documentos que podem ser deste movimento: o valor no texto e/ou o comerciante, com data de 30 dias antes a
    10 depois do movimento. [(documento, porquê)], primeiro os que têm o valor."""
    from .sugestoes import padrao_de
    de, ate = m.data_mov - timedelta(days=30), m.data_mov + timedelta(days=10)
    # o comerciante pela 1.ª palavra (a localidade do extrato, «WORTEN AMADORA», raramente está na fatura)
    palavras = [p for p in (padrao_de(m.descricao_norm or m.descricao_original or "") or "").split() if len(p) >= 3]
    comerciante = palavras[0].title() if palavras else ""
    out, vistos = [], set()
    for texto, porque in ((f'"{valor_txt(m.montante)}"', f"valor {valor_txt(m.montante)} €"), (comerciante, "comerciante")):
        if not texto.strip('"').strip():
            continue
        for d in procurar(texto, de, ate, limite):
            if d.id not in vistos:
                vistos.add(d.id)
                out.append((d, porque))
    return out[:limite]
