"""Enviar recibos de vencimento (PDF com password) para o Paperless, a partir da app.

Os recibos vêm protegidos por password e, assim, o Paperless não lhes consegue ler o texto (nem o fluxo «Entrada
Vencimento» os reconhece, nem os valores se extraem). Aqui, para cada ficheiro (vários de uma vez):
  1. tira-se a password EM MEMÓRIA (pypdf) — tenta-se a password dada e, se houver, a 2.ª (recibos antigos);
  2. confirma-se que o PDF tem texto (senão é uma imagem e precisa de OCR, que o Paperless faz, mas avisa-se);
  3. envia-se ao Paperless com o token do utilizador escolhido (que fica dono do documento).
Nada é gravado em disco no Gestor e a password não é guardada nem registada (a auditoria só tem quantos ficheiros).
"""
import io
from dataclasses import dataclass

from . import paperless

MAX_FICHEIROS = 24
TEXTO_MINIMO = 80          # caracteres na 1.ª página para considerar que o PDF tem texto


@dataclass
class Resultado:
    nome: str
    protegido: bool = False
    paginas: int = 0
    com_texto: bool = False
    estado: str = "por enviar"     # enviado | ja_existe | erro | ignorado (ensaio)
    detalhe: str = ""
    tarefa: str | None = None


def desproteger(conteudo: bytes, senhas: list[str]) -> tuple[bytes, bool, int, bool]:
    """(PDF sem password, estava protegido, n.º de páginas, tem texto). ValueError se nenhuma password serve."""
    from pypdf import PdfReader, PdfWriter
    try:
        r = PdfReader(io.BytesIO(conteudo))
    except Exception as e:
        raise ValueError(f"não é um PDF válido ({e.__class__.__name__})") from e
    protegido = r.is_encrypted
    if protegido:
        if not any(s and r.decrypt(s) for s in senhas):
            raise ValueError("password errada" if any(senhas) else "o PDF tem password e não foi indicada")
        w = PdfWriter(clone_from=r)
        buf = io.BytesIO()
        w.write(buf)
        conteudo = buf.getvalue()
        r = PdfReader(io.BytesIO(conteudo))
    try:
        texto = (r.pages[0].extract_text() or "") if r.pages else ""
    except Exception:
        texto = ""
    return conteudo, protegido, len(r.pages), len(texto.strip()) >= TEXTO_MINIMO


def processar(ficheiros: list[tuple[str, bytes]], senhas: list[str], token: str | None, enviar: bool = True) -> list[Resultado]:
    out = []
    for nome, conteudo in ficheiros[:MAX_FICHEIROS]:
        res = Resultado(nome)
        out.append(res)
        if not nome.lower().endswith(".pdf") and conteudo[:5] != b"%PDF-":
            res.estado, res.detalhe = "erro", "não é um PDF"
            continue
        try:
            limpo, res.protegido, res.paginas, res.com_texto = desproteger(conteudo, senhas)
        except ValueError as e:
            res.estado, res.detalhe = "erro", str(e)
            continue
        if not res.com_texto:
            res.detalhe = "sem texto (imagem): o Paperless faz OCR, mas confirma os valores"
        if not enviar:
            res.estado = "ignorado"
            continue
        try:
            res.tarefa = paperless.enviar_documento(nome, limpo, token=token)
            res.estado = "enviado"
        except paperless.ErroPaperless as e:
            res.estado, res.detalhe = "erro", str(e)
    return out
