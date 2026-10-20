"""Chaves da API do Paperless e do Immich de cada utilizador: guardar (depois de testar), testar e remover.

Quem as gere depende da preferência «tokens_pelo_admin»: cada pessoa no seu Perfil, ou o administrador na ficha de
cada utilizador (Administração › Utilizadores). As chaves ficam cifradas com a chave secreta da app.
"""
from dataclasses import dataclass


@dataclass
class Servico:
    chave: str          # paperless | immich
    nome: str           # «Paperless»
    campo: str          # atributo do Utilizador
    palavra: str        # «token» | «chave»

    @property
    def modulo(self):
        from . import immich, paperless
        return paperless if self.chave == "paperless" else immich

    @property
    def erro(self):
        m = self.modulo
        return m.ErroPaperless if self.chave == "paperless" else m.ErroImmich


SERVICOS = {"paperless": Servico("paperless", "Paperless", "paperless_token", "token"),
            "immich": Servico("immich", "Immich", "immich_token", "chave")}


def pelo_admin(prefs: dict) -> bool:
    return prefs.get("tokens_pelo_admin") == "1"


def estado(u, segredo: str) -> dict:
    """{servico: {"cfg": configurado na NAS, "guardada": tem chave pessoal}} para os ecrãs."""
    return {k: {"cfg": s.modulo.configurado(), "guardada": bool(s.modulo.decifrar(getattr(u, s.campo, None), segredo)),
                "nome": s.nome, "palavra": s.palavra, "link": s.modulo.url_browser()} for k, s in SERVICOS.items()}


def gerir(alvo, servico: str, acao: str, valor: str, segredo: str) -> tuple[str, str]:
    """Aplica a ação à chave do utilizador `alvo`. Devolve (mensagem, tipo do aviso). Não faz commit."""
    s = SERVICOS[servico]
    m, artigo = s.modulo, ("O" if s.palavra == "token" else "A")
    if acao == "remover":
        setattr(alvo, s.campo, None)
        return f"{artigo} {s.palavra} do {s.nome} de {alvo.nome} foi removid{artigo.lower()}.", "ok"
    if acao == "testar":
        atual = m.decifrar(getattr(alvo, s.campo, None), segredo)
        if not atual:
            return f"{alvo.nome} ainda não tem {s.palavra} do {s.nome}.", "erro"
        try:
            return m.testar(atual), "ok"
        except s.erro as e:
            return f"Não foi possível: {e}", "erro"
    valor = (valor or "").strip()
    if not valor:
        return f"Cola {artigo.lower()} {s.palavra} da API do {s.nome}.", "erro"
    try:
        msg = m.testar(valor)
    except s.erro as e:
        if "401" in str(e) or "403" in str(e):
            return f"{s.palavra.capitalize()} recusad{artigo.lower()} pelo {s.nome} ({e}). Não foi guardad{artigo.lower()}.", "erro"
        msg = f"Guardad{artigo.lower()}, mas não foi possível confirmar agora: {e}"
    setattr(alvo, s.campo, m.cifrar(valor, segredo))
    return f"{s.palavra.capitalize()} do {s.nome} de {alvo.nome} guardad{artigo.lower()}. {msg}", "ok"
