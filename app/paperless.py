"""Ligação ao Paperless-ngx da NAS (documentos: faturas, recibos, garantias, contratos).

O Paperless guarda os documentos e faz o OCR; o Gestor só os procura (pela API REST) e guarda a ligação entre um
documento e um movimento ou um item do recheio. Configuração no ficheiro paperless.env da NAS:
  GESTOR_PAPERLESS_URL    endereço visto pelo container do Gestor (ex.: http://paperless:8000, na rede «nas-apps»)
  GESTOR_PAPERLESS_LINK   endereço para o browser abrir os documentos (ex.: http://192.168.1.232:8300)
  GESTOR_PAPERLESS_TOKEN  (opcional) token partilhado, usado só por administradores sem token pessoal

Multiutilizador: cada utilizador guarda o SEU token da API em Perfil (cifrado na base de dados com a chave secreta da
app). Os pedidos ao Paperless vão com o token de quem está a usar a página — é o Paperless (dono, grupos, permissões)
que decide o que cada um vê.
"""
import base64
import hashlib
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


def configurado() -> bool:
    """O Gestor sabe onde está o Paperless (falta, por utilizador, o token)."""
    return bool(url_api())


# ------------------------------------------------------------------ token pessoal (cifrado)
def _fernet(segredo: str):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"gestor-paperless|" + segredo.encode()).digest()))


def cifrar(token: str, segredo: str) -> str:
    return _fernet(segredo).encrypt(token.strip().encode()).decode()


def decifrar(cifrado: str | None, segredo: str) -> str | None:
    if not cifrado:
        return None
    try:
        return _fernet(segredo).decrypt(cifrado.encode()).decode()
    except Exception:        # chave secreta mudou: o token tem de ser guardado outra vez
        return None


def token_de(u, segredo: str) -> str | None:
    """O token com que este utilizador fala com o Paperless: o pessoal; senão, para administradores, o partilhado."""
    if not configurado() or u is None:
        return None
    pessoal = decifrar(getattr(u, "paperless_token", None), segredo)
    if pessoal:
        return pessoal
    partilhado = (os.environ.get("GESTOR_PAPERLESS_TOKEN") or "").strip()
    return partilhado if partilhado and u.is_admin else None


def _pedido(caminho: str, params: dict | None = None, binario: bool = False, token: str | None = None):
    if not configurado():
        raise ErroPaperless("O Paperless não está configurado (paperless.env na NAS).")
    if not token:
        raise ErroPaperless("Guarda o teu token do Paperless em Perfil.")
    url = f"{url_api()}{caminho}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    # Sem fixar a versão da API («application/json; version=N» dá 406 quando o Paperless já não a suporta)
    pedido = urllib.request.Request(url, headers={"Authorization": f"Token {token.strip()}",
                                                  "Accept": "*/*" if binario else "application/json"})
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


def _nomes(caminho: str, token: str) -> dict[int, str]:
    """id → nome (correspondentes, tipos de documento)."""
    try:
        r = _pedido(caminho, {"page_size": 1000}, token=token)
    except ErroPaperless:
        return {}
    return {x["id"]: x.get("name") for x in r.get("results", [])}


def procurar(texto: str = "", de: date | None = None, ate: date | None = None, limite: int = 12, *,
             token: str | None) -> list[Documento]:
    """Pesquisa no texto (OCR), título e correspondente; opcionalmente só entre duas datas (data do documento)."""
    params = {"query": texto.strip() or None, "page_size": limite, "ordering": "-created", "truncate_content": "true",
              "created__date__gt": (de - timedelta(days=1)).isoformat() if de else None,
              "created__date__lt": (ate + timedelta(days=1)).isoformat() if ate else None}
    r = _pedido("/api/documents/", params, token=token)
    correspondentes = _nomes("/api/correspondents/", token)
    tipos = _nomes("/api/document_types/", token)
    return [Documento(d["id"], d.get("title") or f"Documento {d['id']}", _data(d.get("created")),
                      correspondentes.get(d.get("correspondent")), tipos.get(d.get("document_type")),
                      d.get("original_file_name")) for d in r.get("results", [])]


def documento(pid: int, *, token: str | None) -> Documento:
    d = _pedido(f"/api/documents/{int(pid)}/", token=token)
    return Documento(d["id"], d.get("title") or f"Documento {d['id']}", _data(d.get("created")),
                     _nomes("/api/correspondents/", token).get(d.get("correspondent")),
                     _nomes("/api/document_types/", token).get(d.get("document_type")), d.get("original_file_name"))


def miniatura(pid: int, *, token: str | None) -> bytes:
    return _pedido(f"/api/documents/{int(pid)}/thumb/", binario=True, token=token)


def testar(token: str) -> str:
    """Confirma o token: devolve a mensagem para o utilizador (ErroPaperless se falhar)."""
    r = _pedido("/api/documents/", {"page_size": 1}, token=token)
    n = r.get("count", 0)
    return f"Ligação ao Paperless confirmada: vês {n} documento{'s' if n != 1 else ''}."


def valor_txt(cent: int) -> str:
    """Como o valor aparece numa fatura portuguesa: 1234,56 → «1234,56» (a pesquisa também apanha «1.234,56»)."""
    return f"{abs(cent) / 100:.2f}".replace(".", ",")


def sugestoes(m, limite: int = 8, *, token: str | None) -> list[tuple[Documento, str]]:
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
        for d in procurar(texto, de, ate, limite, token=token):
            if d.id not in vistos:
                vistos.add(d.id)
                out.append((d, porque))
    return out[:limite]


# ------------------------------------------------------------------ extratos bancários guardados no Paperless
def _id_por_nome(caminho: str, nome: str, token: str) -> int | None:
    r = _pedido(caminho, {"name__iexact": nome.strip(), "page_size": 5}, token=token)
    return next((x["id"] for x in r.get("results", [])), None)


def extratos(nome: str, *, token: str | None, limite: int = 2000) -> list[Documento]:
    """Documentos com a etiqueta OU o tipo de documento «nome» (ex.: «Extrato bancário»), mais antigos primeiro."""
    filtros = []
    if (t := _id_por_nome("/api/tags/", nome, token)) is not None:
        filtros.append({"tags__id__all": t})
    if (d := _id_por_nome("/api/document_types/", nome, token)) is not None:
        filtros.append({"document_type__id": d})
    out, vistos = [], set()
    for f in filtros:
        pagina = 1
        while len(out) < limite:
            r = _pedido("/api/documents/", {**f, "page": pagina, "page_size": 100, "ordering": "created",
                                             "truncate_content": "true"}, token=token)
            for x in r.get("results", []):
                if x["id"] not in vistos:
                    vistos.add(x["id"])
                    out.append(Documento(x["id"], x.get("title") or f"Documento {x['id']}", _data(x.get("created")),
                                         None, None, x.get("original_file_name")))
            if not r.get("next"):
                break
            pagina += 1
    return sorted(out, key=lambda d: (d.data or date.min, d.id))


def descarregar(pid: int, *, token: str | None) -> bytes:
    """O ficheiro original (não a versão arquivada/OCR que o Paperless gera)."""
    return _pedido(f"/api/documents/{int(pid)}/download/", {"original": "true"}, binario=True, token=token)


def por_checksum(md5: str, *, token: str | None) -> int | None:
    """O documento cujo original tem este MD5 (o Paperless guarda o MD5 de cada original)."""
    r = _pedido("/api/documents/", {"checksum__iexact": md5, "page_size": 1}, token=token)
    return next((x["id"] for x in r.get("results", [])), None)
