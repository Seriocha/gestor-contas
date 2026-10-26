"""Desempenho da app e saúde da base de dados (Administração › Desempenho).

- **Tempos das páginas**: um middleware mede cada pedido (tempo e n.º de queries) e junta por rota — n.º de pedidos,
  média, p95, máximo. Fica só em memória (recomeça quando a app reinicia) e não guarda nada dos pedidos além da rota.
- **Queries lentas**: as que passam de LIMITE_SQL_MS, agrupadas pelo texto (sem os valores, que vão em parâmetros).
- **Diagnóstico da base**: tamanho, WAL, páginas livres (fragmentação), estatísticas do planeador, índices em falta
  nas chaves estrangeiras das tabelas grandes e índices redundantes (prefixo de outro) — com sugestões e as ações
  seguras que se podem correr daqui (ANALYZE/optimize, checkpoint do WAL, criar os índices em falta, VACUUM com
  backup antes).
"""
import contextvars
import re
import sqlite3
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import event

LIMITE_SQL_MS = 200          # queries mais lentas do que isto ficam registadas
AMOSTRAS = 200               # últimos tempos guardados por rota (para o p95)
LINHAS_TABELA_GRANDE = 1000  # chaves estrangeiras sem índice só importam em tabelas com movimento
FRAGMENTACAO_VACUUM = 0.20   # páginas livres acima disto (e > 2 MB) → sugerir VACUUM
WAL_GRANDE = 64 * 1024 ** 2

_queries_pedido: contextvars.ContextVar = contextvars.ContextVar("queries_pedido", default=None)


@dataclass
class Rota:
    caminho: str
    n: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    queries: int = 0
    erros: int = 0
    tempos: deque = field(default_factory=lambda: deque(maxlen=AMOSTRAS))

    @property
    def media_ms(self) -> float:
        return self.total_ms / self.n if self.n else 0.0

    @property
    def p95_ms(self) -> float:
        t = sorted(self.tempos)
        return t[min(len(t) - 1, int(len(t) * 0.95))] if t else 0.0

    @property
    def media_queries(self) -> float:
        return self.queries / self.n if self.n else 0.0


@dataclass
class QueryLenta:
    sql: str
    n: int = 0
    max_ms: float = 0.0
    total_ms: float = 0.0
    rota: str = ""


class Metricas:
    """Tempos por rota e queries lentas, desde que a app arrancou (ou desde «Recomeçar»)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.rotas: dict[str, Rota] = {}
        self.lentas: dict[str, QueryLenta] = {}
        self.desde = datetime.now()

    def registar(self, caminho: str, ms: float, queries: int, erro: bool) -> None:
        with self._lock:
            r = self.rotas.get(caminho) or self.rotas.setdefault(caminho, Rota(caminho))
            r.n += 1
            r.total_ms += ms
            r.max_ms = max(r.max_ms, ms)
            r.queries += queries
            r.erros += int(erro)
            r.tempos.append(ms)

    def query_lenta(self, sql: str, ms: float, rota: str) -> None:
        chave = re.sub(r"\s+", " ", sql).strip()[:600]
        with self._lock:
            q = self.lentas.get(chave) or self.lentas.setdefault(chave, QueryLenta(chave))
            q.n += 1
            q.total_ms += ms
            q.max_ms = max(q.max_ms, ms)
            q.rota = rota or q.rota
            if len(self.lentas) > 100:          # fica com as 50 piores
                for k, _ in sorted(self.lentas.items(), key=lambda kv: kv[1].max_ms)[:50]:
                    self.lentas.pop(k, None)

    def recomecar(self) -> None:
        with self._lock:
            self.rotas.clear()
            self.lentas.clear()
            self.desde = datetime.now()

    def resumo(self) -> dict:
        with self._lock:
            rotas = sorted(self.rotas.values(), key=lambda r: -r.total_ms)
            todos = [t for r in rotas for t in r.tempos]
            n = sum(r.n for r in rotas)
            return {
                "rotas": rotas, "lentas": sorted(self.lentas.values(), key=lambda q: -q.max_ms),
                "n": n, "desde": self.desde,
                "media_ms": sum(r.total_ms for r in rotas) / n if n else 0.0,
                "p95_ms": statistics.quantiles(todos, n=20)[-1] if len(todos) >= 20 else (max(todos) if todos else 0.0),
                "lentas_1s": sum(1 for t in todos if t >= 1000),
                "pior": max(rotas, key=lambda r: r.p95_ms) if rotas else None,
            }


def instalar(app, engine) -> Metricas:
    """Liga a medição: middleware HTTP (tempo e queries por rota) e eventos do motor (queries lentas)."""
    m = Metricas()
    app.state.metricas = m

    @event.listens_for(engine, "before_cursor_execute")
    def _antes(conn, cursor, statement, parameters, context, executemany):
        conn.info.setdefault("t_query", []).append(time.perf_counter())
        contador = _queries_pedido.get()
        if contador is not None:
            contador[0] += 1

    @event.listens_for(engine, "after_cursor_execute")
    def _depois(conn, cursor, statement, parameters, context, executemany):
        pilha = conn.info.get("t_query")
        if not pilha:
            return
        ms = (time.perf_counter() - pilha.pop()) * 1000
        if ms >= LIMITE_SQL_MS:
            contador = _queries_pedido.get()
            m.query_lenta(statement, ms, contador[1] if contador else "(tarefa de fundo)")

    @app.middleware("http")
    async def _medir(request, call_next):
        if request.url.path.startswith(("/static", "/healthz")):
            return await call_next(request)
        contador = [0, request.url.path]
        token = _queries_pedido.set(contador)
        t = time.perf_counter()
        erro = True
        try:
            resposta = await call_next(request)
            erro = resposta.status_code >= 500
            return resposta
        finally:
            ms = (time.perf_counter() - t) * 1000
            rota = request.scope.get("route")
            caminho = f"{request.method} {getattr(rota, 'path', None) or '(sem rota)'}"
            contador[1] = caminho
            m.registar(caminho, ms, contador[0], erro)
            _queries_pedido.reset(token)

    return m


# ---------------------------------------------------------------- diagnóstico da base de dados

@dataclass
class Sugestao:
    nivel: str           # erro | alerta | ok
    titulo: str
    detalhe: str
    acao: str | None = None      # ação que se pode correr daqui (ver executar())
    rotulo: str | None = None


@dataclass
class Diagnostico:
    caminho: Path
    bytes_db: int = 0
    bytes_wal: int = 0
    pagina: int = 0
    paginas: int = 0
    livres: int = 0
    versao_sqlite: str = ""
    journal: str = ""
    estatisticas: bool = False
    tabelas: list = field(default_factory=list)        # [(nome, linhas, bytes|None, n_indices)]
    em_falta: list = field(default_factory=list)       # [(tabela, coluna, tabela_ref, linhas)]
    redundantes: list = field(default_factory=list)    # [(tabela, índice, coberto_por)]
    sugestoes: list = field(default_factory=list)

    @property
    def fragmentacao(self) -> float:
        return self.livres / self.paginas if self.paginas else 0.0

    @property
    def bytes_livres(self) -> int:
        return self.livres * self.pagina


def _indices(con: sqlite3.Connection, tabela: str) -> list[tuple[str, list[str], bool]]:
    """[(nome, colunas, único)] dos índices da tabela (inclui os automáticos das UNIQUE/PK)."""
    out = []
    for _, nome, unico, origem, *_ in con.execute(f'PRAGMA index_list("{tabela}")'):
        colunas = [r[2] for r in con.execute(f'PRAGMA index_info("{nome}")')]
        out.append((nome, colunas, bool(unico)))
    return out


def diagnosticar(db_path: Path) -> Diagnostico:
    """Lê o estado da base numa ligação só de leitura (não bloqueia a app)."""
    d = Diagnostico(Path(db_path))
    d.bytes_db = d.caminho.stat().st_size if d.caminho.exists() else 0
    wal = d.caminho.with_name(d.caminho.name + "-wal")
    d.bytes_wal = wal.stat().st_size if wal.exists() else 0
    con = sqlite3.connect(f"file:{d.caminho.as_posix()}?mode=ro", uri=True)
    try:
        d.pagina = con.execute("PRAGMA page_size").fetchone()[0]
        d.paginas = con.execute("PRAGMA page_count").fetchone()[0]
        d.livres = con.execute("PRAGMA freelist_count").fetchone()[0]
        d.versao_sqlite = con.execute("SELECT sqlite_version()").fetchone()[0]
        d.journal = con.execute("PRAGMA journal_mode").fetchone()[0]
        d.estatisticas = bool(con.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1'").fetchone()) and bool(
            con.execute("SELECT 1 FROM sqlite_stat1 LIMIT 1").fetchone())
        tamanhos = {}
        try:     # dbstat só existe se o SQLite foi compilado com ele
            tamanhos = dict(con.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name").fetchall())
        except sqlite3.DatabaseError:
            pass
        nomes = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "AND name != 'alembic_version' ORDER BY name")]
        for t in nomes:
            linhas = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            idx = _indices(con, t)
            bytes_t = None
            if tamanhos:
                bytes_t = tamanhos.get(t, 0) + sum(tamanhos.get(n, 0) for n, _, _ in idx)
            d.tabelas.append((t, linhas, bytes_t, len(idx)))
            primeiras = {cols[0] for _, cols, _ in idx if cols}
            if linhas >= LINHAS_TABELA_GRANDE:
                for fk in con.execute(f'PRAGMA foreign_key_list("{t}")'):
                    if fk[3] not in primeiras:
                        d.em_falta.append((t, fk[3], fk[2], linhas))
            # redundante: um índice não único cujas colunas são o início de outro índice da mesma tabela
            for nome, cols, unico in idx:
                if unico or nome.startswith("sqlite_autoindex"):
                    continue
                maior = next((n2 for n2, c2, _ in idx if n2 != nome and len(c2) > len(cols) and c2[:len(cols)] == cols),
                             None)
                if maior:
                    d.redundantes.append((t, nome, maior))
    finally:
        con.close()
    d.tabelas.sort(key=lambda x: -x[1])
    d.sugestoes = _sugestoes(d)
    return d


def _mb(b: int) -> str:
    return f"{b / 1024 ** 2:.1f} MB".replace(".", ",")


def _sugestoes(d: Diagnostico) -> list[Sugestao]:
    s = []
    if d.em_falta:
        lista = ", ".join(f"{t}.{c}" for t, c, _, _ in d.em_falta)
        s.append(Sugestao("erro", f"{len(d.em_falta)} chave(s) estrangeira(s) sem índice em tabelas grandes",
                          f"{lista}. Sem índice, apagar uma linha referenciada obriga o SQLite a ler a tabela inteira "
                          "(ex.: anular uma importação) e as junções ficam lentas.", "indices", "Criar os índices"))
    if not d.estatisticas:
        s.append(Sugestao("alerta", "Sem estatísticas para o planeador de queries",
                          "Sem ANALYZE o SQLite escolhe os índices às cegas. Corre-se em segundos.", "analyze",
                          "Atualizar estatísticas"))
    if d.fragmentacao >= FRAGMENTACAO_VACUUM and d.bytes_livres >= 2 * 1024 ** 2:
        s.append(Sugestao("alerta", f"{d.fragmentacao:.0%} da base são páginas livres ({_mb(d.bytes_livres)})",
                          "Espaço de dados apagados que o ficheiro não devolve. O VACUUM reescreve a base compacta "
                          "(faz-se um backup antes; a app fica bloqueada alguns segundos).", "vacuum", "Compactar (VACUUM)"))
    if d.bytes_wal >= WAL_GRANDE:
        s.append(Sugestao("alerta", f"Ficheiro WAL grande ({_mb(d.bytes_wal)})",
                          "O WAL guarda as escritas até serem passadas para a base. Um checkpoint esvazia-o.",
                          "checkpoint", "Fazer checkpoint"))
    if d.journal.lower() != "wal":
        s.append(Sugestao("alerta", f"Modo de journal «{d.journal}»",
                          "A app usa WAL (leituras não bloqueiam escritas). Reiniciar a app volta a ligá-lo."))
    if d.redundantes:
        lista = ", ".join(f"{nome} (coberto por {maior})" for _, nome, maior in d.redundantes)
        s.append(Sugestao("ok", f"{len(d.redundantes)} índice(s) redundante(s)",
                          f"{lista}. As colunas de cada um são o início de outro índice da mesma tabela, que já serve "
                          "as mesmas pesquisas. Ocupam algum espaço e atrasam as escritas um pouco — sem urgência; "
                          "saem numa próxima versão da app."))
    if not s or all(x.nivel == "ok" for x in s):
        s.insert(0, Sugestao("ok", "A base de dados está em boa forma",
                             "Índices nas chaves estrangeiras das tabelas grandes, estatísticas presentes e pouca "
                             "fragmentação. «Otimizar» (PRAGMA optimize) pode correr-se de vez em quando.",
                             "analyze", "Otimizar agora"))
    return s


def executar(acao: str, db_path: Path, engine) -> str:
    """Corre uma ação de manutenção. Devolve a mensagem para o utilizador."""
    t = time.perf_counter()
    if acao == "analyze":
        with engine.begin() as c:
            c.exec_driver_sql("ANALYZE")
            c.exec_driver_sql("PRAGMA optimize")
        msg = "Estatísticas atualizadas (ANALYZE + PRAGMA optimize)"
    elif acao == "checkpoint":
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            r = c.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        msg = f"Checkpoint do WAL feito ({r[2] if r else 0} páginas passadas para a base)"
    elif acao == "indices":
        d = diagnosticar(db_path)
        with engine.begin() as c:
            for tabela, coluna, _, _ in d.em_falta:
                c.exec_driver_sql(f'CREATE INDEX IF NOT EXISTS "ix_{tabela}_{coluna}" ON "{tabela}" ("{coluna}")')
            c.exec_driver_sql("ANALYZE")
        msg = f"{len(d.em_falta)} índice(s) criado(s) e estatísticas atualizadas"
    elif acao == "vacuum":
        antes = Path(db_path).stat().st_size
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:   # VACUUM fora de transação
            c.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            c.exec_driver_sql("VACUUM")
        depois = Path(db_path).stat().st_size
        msg = f"Base compactada: {_mb(antes)} → {_mb(depois)}"
    else:
        raise ValueError(f"Ação desconhecida: {acao}")
    segundos = f"{time.perf_counter() - t:.1f}".replace(".", ",")
    return f"{msg} em {segundos} s."
