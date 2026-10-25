"""Modelo de dados — Fase 1 (utilizadores, entidades, permissões, contas).

Montantes em cêntimos (inteiro), nunca float. Ver DESIGN.md §5 para o
modelo completo; as tabelas das fases seguintes entram por migração Alembic.
"""
from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String, Table, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

PAPEIS = {"ver": 1, "editar": 2, "admin": 3}
PAPEIS_ROTULO = {"ver": "Ver", "editar": "Editar", "admin": "Administrar"}

TIPOS_CONTA = {
    "ORDEM": "Conta à ordem",
    "POUPANCA": "Poupança / depósito a prazo",
    "CARTAO_CREDITO": "Cartão de crédito",
    "INVESTIMENTO": "Investimento",
    "AFORRO": "Certificados de Aforro",
    "PENSOES": "Fundo de pensões / PPR",
    "CARTEIRA_DIGITAL": "Carteira digital (Revolut)",
    "CARTAO_REFEICAO": "Cartão refeição",
    "EMPRESTIMO": "Empréstimo (habitação)",
}
# Contas cujo saldo é uma dívida (entram como passivo no património).
TIPOS_PASSIVO = {"CARTAO_CREDITO", "EMPRESTIMO"}


class Utilizador(Base):
    __tablename__ = "utilizador"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(100))
    login: Mapped[str] = mapped_column(String(50), unique=True)
    email: Mapped[str | None] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    # Incrementar invalida todas as sessões abertas (ex.: troca de password).
    versao_sessao: Mapped[int] = mapped_column(Integer, default=1)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())
    ultimo_login: Mapped[datetime | None] = mapped_column(DateTime)
    # Token pessoal da API do Paperless-ngx, cifrado com a chave secreta da app (ver paperless.py).
    paperless_token: Mapped[str | None] = mapped_column(Text)
    # Chave pessoal da API do Immich (fotografias), cifrada da mesma forma (ver immich.py).
    immich_token: Mapped[str | None] = mapped_column(Text)

    permissoes: Mapped[list["Permissao"]] = relationship(
        back_populates="utilizador", cascade="all, delete-orphan"
    )


TIPOS_ENTIDADE = {"pessoal": "Pessoal", "heranca": "Herança"}
# Módulos que cada entidade usa (Administração › Entidades): os ecrãs só mostram o que a entidade tem.
MODULOS = {"imoveis": "Casas e propriedades", "viaturas": "Viaturas (carros, Via Verde, abastecimentos)",
           "subscricoes": "Subscrições e assinaturas", "viagens": "Viagens",
           "saude": "Saúde (perfis e dados do Google Health)"}


def fracao(pct: float | None) -> str:
    """33.333 → '1/3'; 16.667 → '1/6'; 25 → '1/4'; senão '12,5%'."""
    if pct is None:
        return ""
    for d in (2, 3, 4, 5, 6, 8, 9, 10, 12):
        n = round(pct * d / 100)
        if 0 < n < d and abs(n / d * 100 - pct) < 0.01:
            return f"{n}/{d}"
    return ("%g%%" % round(pct, 4)).replace(".", ",")


class Entidade(Base):
    """Património cujas contas se somam entre si (Família, Mãe, uma herança indivisa…)."""
    __tablename__ = "entidade"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(100), unique=True)
    descricao: Mapped[str | None] = mapped_column(Text)
    ordem: Mapped[int] = mapped_column(Integer, default=0)
    # Início da análise: movimentos anteriores ficam fora dos relatórios, do fluxo de caixa e dos
    # gráficos (enquanto não houver extratos antigos de todas as contas). Os saldos não mudam.
    data_inicio: Mapped[date | None] = mapped_column(Date)
    # Pessoal, ou herança (indivisa) de que se detém uma quota (%): mostra-se a parte de cada valor.
    tipo: Mapped[str] = mapped_column(String(10), default="pessoal", server_default="pessoal")
    quota: Mapped[float | None] = mapped_column(Float)
    # 2.º cenário da partilha (ex.: hoje 1/6 de cada filho e 4/6 da mãe; por morte da mãe, 1/2 de cada filho).
    quota_futura: Mapped[float | None] = mapped_column(Float)
    quota_futura_nota: Mapped[str | None] = mapped_column(String(100))
    modulos: Mapped[str] = mapped_column(String(120), default="imoveis,viaturas,subscricoes,viagens,saude",
                                         server_default="imoveis,viaturas,subscricoes,viagens")

    contas: Mapped[list["Conta"]] = relationship(back_populates="entidade")

    @property
    def heranca(self) -> bool:
        return self.tipo == "heranca"

    @property
    def tipo_rotulo(self) -> str:
        return TIPOS_ENTIDADE.get(self.tipo, self.tipo)

    def parte(self, valor: int | None) -> int | None:
        """A parte de um valor que cabe ao utilizador (toda, numa entidade pessoal)."""
        if valor is None:
            return None
        return round(valor * self.quota / 100) if self.heranca and self.quota is not None else valor

    def parte_futura(self, valor: int | None) -> int | None:
        """A parte no 2.º cenário da partilha (None se não houver)."""
        if valor is None or not self.heranca or self.quota_futura is None:
            return None
        return round(valor * self.quota_futura / 100)

    @property
    def quota_txt(self) -> str:
        return fracao(self.quota)

    @property
    def quota_futura_txt(self) -> str:
        return fracao(self.quota_futura)

    def tem(self, modulo: str) -> bool:
        return modulo in (self.modulos or "").split(",")


class Permissao(Base):
    __tablename__ = "permissao"

    utilizador_id: Mapped[int] = mapped_column(
        ForeignKey("utilizador.id", ondelete="CASCADE"), primary_key=True
    )
    entidade_id: Mapped[int] = mapped_column(
        ForeignKey("entidade.id", ondelete="CASCADE"), primary_key=True
    )
    papel: Mapped[str] = mapped_column(String(10))  # ver | editar | admin

    utilizador: Mapped[Utilizador] = relationship(back_populates="permissoes")
    entidade: Mapped[Entidade] = relationship()


class Instituicao(Base):
    __tablename__ = "instituicao"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(100), unique=True)
    bic: Mapped[str | None] = mapped_column(String(11))
    parser: Mapped[str | None] = mapped_column(String(30))  # chave do parser de importação
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)


conta_titular = Table(
    "conta_titular",
    Base.metadata,
    Column("conta_id", ForeignKey("conta.id", ondelete="CASCADE"), primary_key=True),
    Column("utilizador_id", ForeignKey("utilizador.id", ondelete="CASCADE"), primary_key=True),
)


class Conta(Base):
    __tablename__ = "conta"
    __table_args__ = (UniqueConstraint("entidade_id", "nome"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entidade_id: Mapped[int] = mapped_column(ForeignKey("entidade.id"))
    instituicao_id: Mapped[int] = mapped_column(ForeignKey("instituicao.id"))
    nome: Mapped[str] = mapped_column(String(100))
    tipo: Mapped[str] = mapped_column(String(20))
    moeda: Mapped[str] = mapped_column(String(3), default="EUR")
    iban: Mapped[str | None] = mapped_column(String(40))  # como foi escrito; a apresentação depende das preferências
    # Cartões de crédito: conta à ordem que paga o cartão + ciclo do extrato.
    conta_pagamento_id: Mapped[int | None] = mapped_column(ForeignKey("conta.id"))
    dia_fecho: Mapped[int | None] = mapped_column(Integer)
    dia_pagamento: Mapped[int | None] = mapped_column(Integer)
    saldo_inicial: Mapped[int] = mapped_column(BigInteger, default=0)  # cêntimos
    data_saldo_inicial: Mapped[date | None] = mapped_column(Date)
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)
    notas: Mapped[str | None] = mapped_column(Text)
    # Investimento / pensões: início do contrato e liquidez (saldo contabilístico fora dos fundos).
    data_contrato: Mapped[date | None] = mapped_column(Date)
    liquidez: Mapped[int | None] = mapped_column(BigInteger)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    entidade: Mapped[Entidade] = relationship(back_populates="contas")
    posicoes: Mapped[list["Posicao"]] = relationship(cascade="all, delete-orphan", order_by="Posicao.nome")
    instituicao: Mapped[Instituicao] = relationship()
    conta_pagamento: Mapped["Conta | None"] = relationship(remote_side="Conta.id")
    titulares: Mapped[list[Utilizador]] = relationship(secondary=conta_titular)
    emprestimo: Mapped["Emprestimo | None"] = relationship(back_populates="conta", uselist=False,
                                                           cascade="all, delete-orphan")

    @property
    def tipo_rotulo(self) -> str:
        return TIPOS_CONTA.get(self.tipo, self.tipo)


class Posicao(Base):
    """Fundo (ou título) numa conta de investimento / pensões: unidades de participação × cotação = valor;
    valias = valor − valor de aquisição."""
    __tablename__ = "posicao"

    id: Mapped[int] = mapped_column(primary_key=True)
    conta_id: Mapped[int] = mapped_column(ForeignKey("conta.id", ondelete="CASCADE"), index=True)
    nome: Mapped[str] = mapped_column(String(120))
    gestora: Mapped[str | None] = mapped_column(String(80))
    categoria: Mapped[str | None] = mapped_column(String(60))          # Imobiliário, Ações, Obrigações, PPR…
    isin: Mapped[str | None] = mapped_column(String(12))
    risco: Mapped[int | None] = mapped_column(Integer)                 # 1 a 7 (indicador sintético de risco)
    unidades: Mapped[float] = mapped_column(Float, default=0)
    cotacao: Mapped[float | None] = mapped_column(Float)              # € por unidade
    data_cotacao: Mapped[date | None] = mapped_column(Date)
    valor_aquisicao: Mapped[int | None] = mapped_column(BigInteger)   # cêntimos

    @property
    def valor(self) -> int:
        return round(self.unidades * (self.cotacao or 0) * 100)

    @property
    def valias(self) -> int | None:
        return self.valor - self.valor_aquisicao if self.valor_aquisicao else None

    @property
    def valias_pct(self) -> float | None:
        return self.valias / self.valor_aquisicao * 100 if self.valor_aquisicao else None


TIPOS_TAXA = {"variavel": "Variável", "fixa": "Fixa", "mista": "Mista"}


class Emprestimo(Base):
    """Dados de um empréstimo (1:1 com uma Conta do tipo EMPRESTIMO).

    O saldo da conta é o capital em dívida (negativo). A conta que paga a
    prestação é Conta.conta_pagamento_id e o dia da prestação Conta.dia_pagamento.
    Taxas em centésimas de milésima de ponto percentual (1,54895% → 154895)."""
    __tablename__ = "emprestimo"

    conta_id: Mapped[int] = mapped_column(ForeignKey("conta.id", ondelete="CASCADE"), primary_key=True)
    montante_contratado: Mapped[int | None] = mapped_column(BigInteger)
    data_contrato: Mapped[date | None] = mapped_column(Date)
    prazo_meses: Mapped[int] = mapped_column(Integer)          # n.º total de prestações do contrato
    # N.º da última prestação paga à data do capital indicado (quando não se importa o extrato do empréstimo).
    ultima_prestacao_n: Mapped[int | None] = mapped_column(Integer)
    tipo_taxa: Mapped[str] = mapped_column(String(10), default="variavel")
    indexante: Mapped[str | None] = mapped_column(String(30))  # ex.: Euribor 12M
    spread_e5: Mapped[int | None] = mapped_column(Integer)
    taxa_e5: Mapped[int] = mapped_column(Integer)              # TAN em vigor
    padrao_prestacao: Mapped[str | None] = mapped_column(String(200))
    encargos: Mapped[int] = mapped_column(BigInteger, default=0)  # cêntimos por prestação (comissões, selo)

    conta: Mapped["Conta"] = relationship(back_populates="emprestimo")


class Importacao(Base):
    __tablename__ = "importacao"

    id: Mapped[int] = mapped_column(primary_key=True)
    conta_id: Mapped[int] = mapped_column(ForeignKey("conta.id", ondelete="CASCADE"))
    utilizador_id: Mapped[int | None] = mapped_column(ForeignKey("utilizador.id", ondelete="SET NULL"))
    ficheiro_nome: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    parser: Mapped[str] = mapped_column(String(30))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())
    data_ini: Mapped[date | None] = mapped_column(Date)
    data_fim: Mapped[date | None] = mapped_column(Date)
    n_lidos: Mapped[int] = mapped_column(Integer, default=0)
    n_novos: Mapped[int] = mapped_column(Integer, default=0)
    n_duplicados: Mapped[int] = mapped_column(Integer, default=0)
    estado: Mapped[str] = mapped_column(String(12), default="confirmada")  # confirmada | anulada
    # Onde está o extrato original, além da cópia em data/uploads/<sha256>: o documento no Paperless e/ou o
    # ficheiro na pasta partilhada da NAS (relativo a ela, depois de passar para importados/).
    paperless_id: Mapped[int | None] = mapped_column(Integer, index=True)
    origem: Mapped[str | None] = mapped_column(String(500))

    conta: Mapped[Conta] = relationship()
    utilizador: Mapped[Utilizador | None] = relationship()


class CartaoDebito(Base):
    """Cartão de débito de uma conta à ordem (pelos 4 últimos dígitos), com o nome de quem o usa."""
    __tablename__ = "cartao_debito"
    __table_args__ = (UniqueConstraint("conta_id", "final"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conta_id: Mapped[int] = mapped_column(ForeignKey("conta.id", ondelete="CASCADE"), index=True)
    final: Mapped[str] = mapped_column(String(4))
    nome: Mapped[str] = mapped_column(String(60))
    cor: Mapped[str | None] = mapped_column(String(7))

    conta: Mapped["Conta"] = relationship()


class MovimentoCartao(Base):
    """Compra lida do ficheiro de um cartão de débito. Não é um movimento: liga-se ao débito igual no extrato da
    conta (movimento_id) para se saber que cartão o fez."""
    __tablename__ = "movimento_cartao"
    __table_args__ = (UniqueConstraint("cartao_id", "hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    cartao_id: Mapped[int] = mapped_column(ForeignKey("cartao_debito.id", ondelete="CASCADE"), index=True)
    data: Mapped[date] = mapped_column(Date)
    montante: Mapped[int] = mapped_column(BigInteger)
    descricao: Mapped[str] = mapped_column(String(200))
    hash: Mapped[str] = mapped_column(String(64))
    movimento_id: Mapped[int | None] = mapped_column(ForeignKey("movimento.id", ondelete="SET NULL"), index=True)

    cartao: Mapped[CartaoDebito] = relationship()


class DocumentoLigado(Base):
    """Documento do Paperless-ngx (fatura, recibo, garantia…) ligado a um movimento ou a um item do recheio.
    Guarda-se o id no Paperless e uma cópia do título/correspondente/data para mostrar sem perguntar ao Paperless."""
    __tablename__ = "documento_ligado"

    id: Mapped[int] = mapped_column(primary_key=True)
    paperless_id: Mapped[int] = mapped_column(Integer, index=True)
    titulo: Mapped[str] = mapped_column(String(200))
    correspondente: Mapped[str | None] = mapped_column(String(120))
    data: Mapped[date | None] = mapped_column(Date)
    movimento_id: Mapped[int | None] = mapped_column(ForeignKey("movimento.id", ondelete="CASCADE"), index=True)
    item_recheio_id: Mapped[int | None] = mapped_column(ForeignKey("item_recheio.id", ondelete="CASCADE"), index=True)
    utilizador_id: Mapped[int | None] = mapped_column(ForeignKey("utilizador.id", ondelete="SET NULL"))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())


class ImportacaoPendente(Base):
    """Extrato encontrado na pasta partilhada da NAS (lida periodicamente): fica à espera de validação pelo
    administrador (Importar › Por validar). Validado, é importado como os outros e o ficheiro passa para importados/."""
    __tablename__ = "importacao_pendente"

    id: Mapped[int] = mapped_column(primary_key=True)
    pasta: Mapped[str] = mapped_column(String(120))                    # subpasta (banco)
    ficheiro: Mapped[str] = mapped_column(String(255))
    caminho: Mapped[str] = mapped_column(String(500))                  # relativo à pasta partilhada
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    parser: Mapped[str | None] = mapped_column(String(30))
    banco: Mapped[str | None] = mapped_column(String(60))
    conta_id: Mapped[int | None] = mapped_column(ForeignKey("conta.id", ondelete="SET NULL"))
    deteccao: Mapped[str | None] = mapped_column(String(200))         # como a conta foi escolhida (ou porque não)
    n_lidos: Mapped[int] = mapped_column(Integer, default=0)
    n_novos: Mapped[int | None] = mapped_column(Integer)
    data_ini: Mapped[date | None] = mapped_column(Date)
    data_fim: Mapped[date | None] = mapped_column(Date)
    erro: Mapped[str | None] = mapped_column(String(500))
    estado: Mapped[str] = mapped_column(String(12), default="pendente")   # pendente | validada | rejeitada | erro
    nota: Mapped[str | None] = mapped_column(String(300))
    detetado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())
    tratado_em: Mapped[datetime | None] = mapped_column(DateTime)
    tratado_por: Mapped[int | None] = mapped_column(ForeignKey("utilizador.id", ondelete="SET NULL"))
    importacao_id: Mapped[int | None] = mapped_column(ForeignKey("importacao.id", ondelete="SET NULL"))
    # Extrato vindo do Paperless (pasta = «Paperless», sem ficheiro na pasta partilhada)
    paperless_id: Mapped[int | None] = mapped_column(Integer, index=True)

    conta: Mapped["Conta | None"] = relationship()


class Movimento(Base):
    __tablename__ = "movimento"
    __table_args__ = (UniqueConstraint("conta_id", "hash_dedup"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conta_id: Mapped[int] = mapped_column(ForeignKey("conta.id", ondelete="CASCADE"), index=True)
    importacao_id: Mapped[int | None] = mapped_column(ForeignKey("importacao.id", ondelete="CASCADE"))
    data_mov: Mapped[date] = mapped_column(Date, index=True)
    data_valor: Mapped[date | None] = mapped_column(Date)
    # Posição cronológica dentro do mesmo dia (o extrato traz a ordem real).
    ordem: Mapped[int] = mapped_column(Integer, default=0)
    descricao_original: Mapped[str] = mapped_column(String(500))
    descricao_norm: Mapped[str] = mapped_column(String(500))
    montante: Mapped[int] = mapped_column(BigInteger)  # cêntimos, negativo = saída
    moeda: Mapped[str] = mapped_column(String(3), default="EUR")
    saldo_apos: Mapped[int | None] = mapped_column(BigInteger)  # saldo dado pelo banco
    notas: Mapped[str | None] = mapped_column(Text)
    hash_dedup: Mapped[str] = mapped_column(String(64))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    # Qualificação (relatórios). Transferências entre contas próprias e
    # movimentos excluídos não contam como receita nem despesa.
    categoria_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id", ondelete="SET NULL"), index=True)
    transferencia: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    excluido: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 'manual' protege a qualificação de ser reescrita pelas regras.
    qualificado_por: Mapped[str | None] = mapped_column(String(10))  # manual | regra
    regra_id: Mapped[int | None] = mapped_column(ForeignKey("regra.id", ondelete="SET NULL"))
    # O outro lado de uma transferência (ex.: pagamento do cartão ↔ débito na conta pagadora).
    par_id: Mapped[int | None] = mapped_column(ForeignKey("movimento.id", ondelete="SET NULL"))
    # Bem (casa, carro) a que a despesa/receita diz respeito: separa casa principal, de férias, cada carro…
    bem_id: Mapped[int | None] = mapped_column(ForeignKey("bem.id", ondelete="SET NULL"), index=True)
    bem_manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")  # escolhido à mão: a atribuição automática não mexe
    # Débito de uma subscrição, associado à mão (conta nela mesmo que o texto não bata com o padrão).
    subscricao_id: Mapped[int | None] = mapped_column(ForeignKey("subscricao.id", ondelete="SET NULL"), index=True)

    conta: Mapped[Conta] = relationship()
    par: Mapped["Movimento | None"] = relationship(remote_side="Movimento.id", foreign_keys=[par_id],
                                                   post_update=True)
    categoria: Mapped["Categoria | None"] = relationship()
    etiquetas: Mapped[list["Etiqueta"]] = relationship(secondary="movimento_etiqueta")
    bem: Mapped["Bem | None"] = relationship(lazy="selectin")


NATUREZAS = {"DESPESA": "Despesa", "RECEITA": "Receita"}

# Paleta categórica validada (8 cores, ordem fixa) — ver static/graficos.js.
CORES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]


class Categoria(Base):
    __tablename__ = "categoria"
    __table_args__ = (UniqueConstraint("pai_id", "nome"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(80))
    pai_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id", ondelete="CASCADE"))
    natureza: Mapped[str] = mapped_column(String(10))  # DESPESA | RECEITA
    cor: Mapped[str | None] = mapped_column(String(7))
    ordem: Mapped[int] = mapped_column(Integer, default=0)
    # Compras nesta categoria (e nas filhas) são recheio da casa: entram no inventário para o seguro e as garantias.
    recheio: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    pai: Mapped["Categoria | None"] = relationship(remote_side="Categoria.id", back_populates="filhas")
    filhas: Mapped[list["Categoria"]] = relationship(back_populates="pai", order_by="Categoria.nome")

    @property
    def nome_completo(self) -> str:
        return f"{self.pai.nome} › {self.nome}" if self.pai else self.nome

    @property
    def raiz(self) -> "Categoria":
        return self.pai or self

    @property
    def cor_efetiva(self) -> str:
        return self.cor or (self.pai.cor if self.pai else None) or "#8a8a85"


class Etiqueta(Base):
    __tablename__ = "etiqueta"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(50), unique=True)


movimento_etiqueta = Table(
    "movimento_etiqueta",
    Base.metadata,
    Column("movimento_id", ForeignKey("movimento.id", ondelete="CASCADE"), primary_key=True),
    Column("etiqueta_id", ForeignKey("etiqueta.id", ondelete="CASCADE"), primary_key=True),
)

MODOS_REGRA = {"contem": "contém", "comeca": "começa por", "regex": "expressão regular"}
SENTIDOS = {"": "entradas e saídas", "SAIDA": "só saídas", "ENTRADA": "só entradas"}


class Regra(Base):
    """Se a descrição corresponder ao padrão (e às condições opcionais),
    aplica as ações. Avaliadas por prioridade crescente; a 1.ª que
    corresponder ganha."""
    __tablename__ = "regra"

    id: Mapped[int] = mapped_column(primary_key=True)
    padrao: Mapped[str] = mapped_column(String(200))
    modo: Mapped[str] = mapped_column(String(10), default="contem")
    conta_id: Mapped[int | None] = mapped_column(ForeignKey("conta.id", ondelete="CASCADE"))
    sentido: Mapped[str | None] = mapped_column(String(7))  # ENTRADA | SAIDA | None
    montante_min: Mapped[int | None] = mapped_column(BigInteger)  # cêntimos, valor absoluto
    montante_max: Mapped[int | None] = mapped_column(BigInteger)
    categoria_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id", ondelete="CASCADE"))
    etiqueta_id: Mapped[int | None] = mapped_column(ForeignKey("etiqueta.id", ondelete="SET NULL"))
    # Liga o movimento a um bem (casa, propriedade, carro). A regra passa a valer só nas contas da entidade do bem.
    bem_id: Mapped[int | None] = mapped_column(ForeignKey("bem.id", ondelete="SET NULL"))
    marcar_transferencia: Mapped[bool] = mapped_column(Boolean, default=False)
    excluir: Mapped[bool] = mapped_column(Boolean, default=False)
    prioridade: Mapped[int] = mapped_column(Integer, default=100)
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    conta: Mapped[Conta | None] = relationship()
    categoria: Mapped[Categoria | None] = relationship()
    etiqueta: Mapped[Etiqueta | None] = relationship()
    bem: Mapped["Bem | None"] = relationship()


# Contas cujo saldo se pode retificar à data (valor real da carteira / dos certificados).
TIPOS_RETIFICAVEIS = {"AFORRO", "INVESTIMENTO", "PENSOES", "CARTAO_REFEICAO"}


class RetificacaoSaldo(Base):
    """Saldo real de uma conta numa data. A diferença para o saldo calculado pelos
    movimentos vira um movimento de acerto (fora dos relatórios), recalculado
    sempre que os movimentos da conta mudam — os movimentos em falta podem ser
    inseridos depois e o acerto encolhe até zero."""
    __tablename__ = "retificacao_saldo"

    id: Mapped[int] = mapped_column(primary_key=True)
    conta_id: Mapped[int] = mapped_column(ForeignKey("conta.id", ondelete="CASCADE"), index=True)
    data: Mapped[date] = mapped_column(Date)
    saldo: Mapped[int] = mapped_column(BigInteger)       # cêntimos
    # Valor de partida: sem histórico do capital investido, a diferença conta como capital (não como ganho).
    partida: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    nota: Mapped[str | None] = mapped_column(Text)
    criado_por: Mapped[int | None] = mapped_column(ForeignKey("utilizador.id", ondelete="SET NULL"))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    conta: Mapped["Conta"] = relationship()


class ReceitaPrevista(Base):
    """Entrada recorrente esperada todos os meses numa conta (ordenado, carregamento
    do cartão refeição). Enquanto o crédito real desse mês não for importado,
    o fluxo de caixa do mês mostra-a como «prevista»."""
    __tablename__ = "receita_prevista"

    id: Mapped[int] = mapped_column(primary_key=True)
    conta_id: Mapped[int] = mapped_column(ForeignKey("conta.id", ondelete="CASCADE"), index=True)
    descricao: Mapped[str] = mapped_column(String(100))
    dia: Mapped[int] = mapped_column(Integer)                      # dia do mês em que costuma entrar
    valor: Mapped[int | None] = mapped_column(BigInteger)          # cêntimos; None = média dos últimos recebidos
    categoria_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id", ondelete="SET NULL"))
    padrao: Mapped[str | None] = mapped_column(String(200))        # texto do crédito real (ex.: VENCIMENTO)
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    conta: Mapped["Conta"] = relationship()
    categoria: Mapped["Categoria | None"] = relationship()


MOTIVOS_VIAGEM = {"lazer": "Lazer / férias", "trabalho": "Trabalho", "familia": "Família", "outro": "Outro"}


class ModeloViagem(Base):
    """Critérios para encontrar os gastos de uma viagem, reutilizáveis e afinados com o uso."""
    __tablename__ = "modelo_viagem"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(80), unique=True)
    dias_antes: Mapped[int] = mapped_column(Integer, default=90)     # reservas (voos, alojamento) antes da partida
    dias_depois: Mapped[int] = mapped_column(Integer, default=3)
    tipos_conta: Mapped[str] = mapped_column(String(200), default="")  # CSV de TIPOS_CONTA usados em viagem
    contas: Mapped[str] = mapped_column(String(200), default="")       # CSV de ids de contas (além dos tipos)
    incluir: Mapped[str] = mapped_column(Text, default="")             # um padrão por linha (texto da descrição)
    excluir: Mapped[str] = mapped_column(Text, default="")
    categorias: Mapped[str] = mapped_column(String(300), default="")   # CSV de ids de categorias típicas
    valor_min: Mapped[int | None] = mapped_column(BigInteger)          # cêntimos (valor absoluto)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    padroes: Mapped[list["ModeloPadrao"]] = relationship(back_populates="modelo", cascade="all, delete-orphan",
                                                         order_by="ModeloPadrao.padrao")


class ModeloPadrao(Base):
    """O que se aprendeu: quantas vezes um padrão de descrição foi confirmado / rejeitado como gasto de viagem."""
    __tablename__ = "modelo_padrao"
    __table_args__ = (UniqueConstraint("modelo_id", "padrao"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    modelo_id: Mapped[int] = mapped_column(ForeignKey("modelo_viagem.id", ondelete="CASCADE"), index=True)
    padrao: Mapped[str] = mapped_column(String(200))
    positivos: Mapped[int] = mapped_column(Integer, default=0)
    negativos: Mapped[int] = mapped_column(Integer, default=0)

    modelo: Mapped[ModeloViagem] = relationship(back_populates="padroes")


class Viagem(Base):
    __tablename__ = "viagem"

    id: Mapped[int] = mapped_column(primary_key=True)
    entidade_id: Mapped[int] = mapped_column(ForeignKey("entidade.id", ondelete="CASCADE"), index=True)
    nome: Mapped[str] = mapped_column(String(100))
    destino: Mapped[str | None] = mapped_column(String(120))
    pais: Mapped[str | None] = mapped_column(String(80))
    data_ini: Mapped[date] = mapped_column(Date)
    data_fim: Mapped[date] = mapped_column(Date)
    pessoas: Mapped[int] = mapped_column(Integer, default=1)
    motivo: Mapped[str] = mapped_column(String(20), default="lazer")
    orcamento: Mapped[int | None] = mapped_column(BigInteger)          # cêntimos
    moeda: Mapped[str | None] = mapped_column(String(3))               # moeda local (informativa)
    notas: Mapped[str | None] = mapped_column(Text)
    modelo_id: Mapped[int | None] = mapped_column(ForeignKey("modelo_viagem.id", ondelete="SET NULL"))
    etiqueta_id: Mapped[int | None] = mapped_column(ForeignKey("etiqueta.id", ondelete="SET NULL"))
    immich_album_id: Mapped[str | None] = mapped_column(String(64))    # álbum da viagem no Immich
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    entidade: Mapped["Entidade"] = relationship()
    modelo: Mapped[ModeloViagem | None] = relationship()
    etiqueta: Mapped["Etiqueta | None"] = relationship()

    @property
    def dias(self) -> int:
        return (self.data_fim - self.data_ini).days + 1


class ViagemRejeitado(Base):
    """Movimento sugerido que o utilizador disse não ser desta viagem (não volta a ser sugerido)."""
    __tablename__ = "viagem_rejeitado"
    __table_args__ = (UniqueConstraint("viagem_id", "movimento_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    viagem_id: Mapped[int] = mapped_column(ForeignKey("viagem.id", ondelete="CASCADE"), index=True)
    movimento_id: Mapped[int] = mapped_column(ForeignKey("movimento.id", ondelete="CASCADE"))


class ContadorEnergia(Base):
    """Contador de eletricidade (CPE) de uma casa, com as leituras importadas do Excel do Balcão Digital da E-Redes."""
    __tablename__ = "contador_energia"
    __table_args__ = (UniqueConstraint("entidade_id", "cpe"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entidade_id: Mapped[int] = mapped_column(ForeignKey("entidade.id", ondelete="CASCADE"), index=True)
    cpe: Mapped[str] = mapped_column(String(30))
    bem_id: Mapped[int | None] = mapped_column(ForeignKey("bem.id", ondelete="SET NULL"))
    morada: Mapped[str | None] = mapped_column(String(200))
    numero: Mapped[str | None] = mapped_column(String(30))             # n.º do contador

    bem: Mapped["Bem | None"] = relationship()
    leituras: Mapped[list["LeituraEnergia"]] = relationship(back_populates="contador", cascade="all, delete-orphan",
                                                              order_by="LeituraEnergia.data")


class LeituraEnergia(Base):
    """Leitura acumulada do contador num dia, por período horário (JSON {«Vazio»: 9713, «Ponta»: 1222, …} em kWh)."""
    __tablename__ = "leitura_energia"
    __table_args__ = (UniqueConstraint("contador_id", "data"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    contador_id: Mapped[int] = mapped_column(ForeignKey("contador_energia.id", ondelete="CASCADE"), index=True)
    data: Mapped[date] = mapped_column(Date)
    valores: Mapped[str] = mapped_column(Text)
    tipo: Mapped[str | None] = mapped_column(String(20))               # Real | Estimada …

    contador: Mapped[ContadorEnergia] = relationship(back_populates="leituras")


class ContratoServico(Base):
    """Um contrato de um serviço da casa (luz, gás, água…) e a casa a que pertence, para a comparação de fornecedores:
    os débitos com o texto (ex.: «EDP»), a referência (o n.º no fim da descrição) e, quando o mesmo fornecedor cobra
    duas casas com a mesma referência, o intervalo de dias do mês em que cada uma é cobrada."""
    __tablename__ = "contrato_servico"

    id: Mapped[int] = mapped_column(primary_key=True)
    entidade_id: Mapped[int] = mapped_column(ForeignKey("entidade.id", ondelete="CASCADE"), index=True)
    bem_id: Mapped[int] = mapped_column(ForeignKey("bem.id", ondelete="CASCADE"), index=True)
    texto: Mapped[str] = mapped_column(String(60))
    referencia: Mapped[str | None] = mapped_column(String(30))
    dia_de: Mapped[int | None] = mapped_column(Integer)
    dia_ate: Mapped[int | None] = mapped_column(Integer)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    bem: Mapped["Bem"] = relationship()


class ViagemDescartada(Base):
    """Período detetado (Viagens › Detetar) que o utilizador disse não ser uma viagem: não volta a ser proposto."""
    __tablename__ = "viagem_descartada"

    id: Mapped[int] = mapped_column(primary_key=True)
    entidade_id: Mapped[int] = mapped_column(ForeignKey("entidade.id", ondelete="CASCADE"), index=True)
    data_ini: Mapped[date] = mapped_column(Date)
    data_fim: Mapped[date] = mapped_column(Date)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())


class ViagemConfirmado(Base):
    """Movimento de uma viagem que já foi ensinado (como confirmado) a um modelo — para não contar duas vezes."""
    __tablename__ = "viagem_confirmado"
    __table_args__ = (UniqueConstraint("viagem_id", "movimento_id", "modelo_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    viagem_id: Mapped[int] = mapped_column(ForeignKey("viagem.id", ondelete="CASCADE"), index=True)
    movimento_id: Mapped[int] = mapped_column(ForeignKey("movimento.id", ondelete="CASCADE"))
    modelo_id: Mapped[int] = mapped_column(ForeignKey("modelo_viagem.id", ondelete="CASCADE"))


PERIODICIDADES = {"mensal": ("Mensal", 1), "bimestral": ("Bimestral", 2), "trimestral": ("Trimestral", 3),
                  "semestral": ("Semestral", 6), "anual": ("Anual", 12)}


class Subscricao(Base):
    """Subscrição / assinatura / contrato com pagamento periódico (streaming, telemóvel, ginásio, seguro…)."""
    __tablename__ = "subscricao"

    id: Mapped[int] = mapped_column(primary_key=True)
    entidade_id: Mapped[int] = mapped_column(ForeignKey("entidade.id", ondelete="CASCADE"), index=True)
    nome: Mapped[str] = mapped_column(String(100))
    servico: Mapped[str | None] = mapped_column(String(40))          # chave do catálogo (netflix, meo, …)
    categoria_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id", ondelete="SET NULL"))
    conta_id: Mapped[int | None] = mapped_column(ForeignKey("conta.id", ondelete="SET NULL"))   # onde é debitada
    valor: Mapped[int] = mapped_column(BigInteger)                      # cêntimos, por pagamento
    periodicidade: Mapped[str] = mapped_column(String(12), default="mensal")
    dia_pagamento: Mapped[int | None] = mapped_column(Integer)          # dia do mês em que costuma ser debitada
    data_inicio: Mapped[date] = mapped_column(Date)
    fim_fidelizacao: Mapped[date | None] = mapped_column(Date)          # até quando há período de fidelização
    data_fim: Mapped[date | None] = mapped_column(Date)                 # cancelada / terminada
    padrao: Mapped[str | None] = mapped_column(String(200))             # texto do débito no extrato
    notas: Mapped[str | None] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    conta: Mapped["Conta | None"] = relationship()
    categoria: Mapped["Categoria | None"] = relationship()

    @property
    def meses_periodo(self) -> int:
        return PERIODICIDADES.get(self.periodicidade, ("", 1))[1]

    @property
    def custo_mensal(self) -> float:
        return self.valor / self.meses_periodo

    def ativa_em(self, d: date) -> bool:
        return self.data_inicio <= d and (self.data_fim is None or self.data_fim >= d)


class SubscricaoIgnorada(Base):
    """Padrão recorrente que o utilizador disse não ser uma subscrição (não volta a ser sugerido)."""
    __tablename__ = "subscricao_ignorada"
    __table_args__ = (UniqueConstraint("entidade_id", "padrao"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entidade_id: Mapped[int] = mapped_column(ForeignKey("entidade.id", ondelete="CASCADE"))
    padrao: Mapped[str] = mapped_column(String(200))


TIPOS_BEM = {"casa": "Casa", "carro": "Carro", "rustico": "Propriedade rústica"}
USOS_BEM = {
    "casa": {"principal": "Habitação principal", "ferias": "Casa de férias", "arrendada": "Arrendada", "outra": "Outra"},
    "rustico": {"exploracao": "Exploração agrícola / florestal", "lazer": "Lazer / casa de campo",
                "arrendada": "Arrendada", "sem_uso": "Sem exploração"},
    "carro": {"principal": "Carro principal", "secundario": "Segundo carro", "empresa": "Viatura da empresa",
              "outro": "Outro"},
}
ESTADOS_BEM = {"novo": "Novo / como novo", "bom": "Bom estado", "usado": "Precisa de obras / desgaste",
               "recuperar": "Para recuperar"}
# Propriedades rústicas: o que influencia o valor (ocupação do solo, água, acesso, cortiça).
OCUPACOES = {"montado_sobro": "Montado de sobro", "montado_azinho": "Montado de azinho", "olival": "Olival",
             "vinha": "Vinha", "amendoal": "Amendoal", "pomar": "Pomar", "pinhal": "Pinhal", "eucaliptal": "Eucaliptal",
             "pastagem": "Pastagem", "sequeiro": "Cultura arvense de sequeiro", "regadio": "Regadio",
             "horta": "Horta", "mato": "Mato / incultos"}
AGUAS = {"furo": "Furo", "poco": "Poço", "charca": "Charca / barragem", "ribeira": "Ribeira / linha de água",
         "rede": "Rede pública", "regadio_publico": "Perímetro de rega"}
ACESSOS = {"alcatrao": "Estrada alcatroada", "terra_bom": "Caminho de terra em bom estado", "terra_mau": "Caminho de terra difícil"}
TIPOS_CONSTRUCAO = {"habitacao": "Habitação", "armazem": "Armazém / apoio agrícola", "anexo": "Anexo / garagem",
                    "ruina": "Ruína", "outra": "Outra"}
CICLO_CORTICA = 9          # anos entre tiragens (mínimo legal em Portugal)
COMBUSTIVEIS = {"gasolina": "Gasolina", "gasoleo": "Gasóleo", "hibrido": "Híbrido", "hibrido_plugin": "Híbrido plug-in",
                "eletrico": "Elétrico", "gpl": "GPL"}


class Bem(Base):
    """Património fora das contas: casas e carros, com atributos para avaliação e as despesas associadas."""
    __tablename__ = "bem"

    id: Mapped[int] = mapped_column(primary_key=True)
    entidade_id: Mapped[int] = mapped_column(ForeignKey("entidade.id", ondelete="CASCADE"), index=True)
    tipo: Mapped[str] = mapped_column(String(10))                      # casa | carro
    nome: Mapped[str] = mapped_column(String(100))
    uso: Mapped[str] = mapped_column(String(20), default="principal")  # ver USOS_BEM
    # Casa: assoalhadas (T2 = 2), área útil, localização (freguesia, concelho), ano de construção.
    tipologia: Mapped[int | None] = mapped_column(Integer)
    area_m2: Mapped[int | None] = mapped_column(Integer)
    localizacao: Mapped[str | None] = mapped_column(String(200))
    ano: Mapped[int | None] = mapped_column(Integer)                   # construção (casa) / matrícula (carro)
    estado: Mapped[str | None] = mapped_column(String(12))
    extras: Mapped[str | None] = mapped_column(String(200))           # garagem, piscina… / versão do carro
    # Carro
    marca: Mapped[str | None] = mapped_column(String(60))
    modelo: Mapped[str | None] = mapped_column(String(60))
    km: Mapped[int | None] = mapped_column(Integer)
    combustivel: Mapped[str | None] = mapped_column(String(16))
    matricula: Mapped[str | None] = mapped_column(String(12))        # liga as portagens/parques da Via Verde ao carro
    # Propriedade rústica
    area_ha: Mapped[float | None] = mapped_column(Float)
    artigo: Mapped[str | None] = mapped_column(String(60))           # artigo matricial / descrição predial
    ocupacao: Mapped[str | None] = mapped_column(String(300))        # chaves de OCUPACOES separadas por vírgulas
    agua: Mapped[str | None] = mapped_column(String(120))            # chaves de AGUAS
    acesso: Mapped[str | None] = mapped_column(String(12))
    tem_cortica: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    anos_tiragem: Mapped[str | None] = mapped_column(String(120))    # ex.: "2008, 2017, 2026"
    sobreiros: Mapped[int | None] = mapped_column(Integer)           # n.º aproximado de sobreiros em produção
    producao_arrobas: Mapped[int | None] = mapped_column(Integer)    # produção da última tiragem (@ = 15 kg)
    # Aquisição, dívida e venda
    data_aquisicao: Mapped[date | None] = mapped_column(Date)
    valor_aquisicao: Mapped[int | None] = mapped_column(BigInteger)   # cêntimos
    emprestimo_conta_id: Mapped[int | None] = mapped_column(ForeignKey("conta.id", ondelete="SET NULL"))
    data_venda: Mapped[date | None] = mapped_column(Date)
    # Plafond de manutenção (viatura da empresa): valor a gastar em N anos a contar de uma data (cêntimos).
    plafond_manutencao: Mapped[int | None] = mapped_column(BigInteger)
    plafond_anos: Mapped[int | None] = mapped_column(Integer)
    plafond_inicio: Mapped[date | None] = mapped_column(Date)
    valor_venda: Mapped[int | None] = mapped_column(BigInteger)
    # Revisão periódica do valor (meses entre avaliações; 0 = só à mão).
    revisao_meses: Mapped[int] = mapped_column(Integer, default=6, server_default="6")
    # Atribuição das despesas: textos do extrato (um por linha) e «por omissão» (fica com as despesas
    # da categoria Casa / Transportes que nenhum padrão atribuiu).
    padroes: Mapped[str | None] = mapped_column(Text)
    omissao: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    notas: Mapped[str | None] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    # Carros: plano de manutenção (gerado pela IA a partir de marca, modelo, ano e combustível).
    capital_recheio: Mapped[int | None] = mapped_column(BigInteger)   # capital seguro do recheio (casas), cêntimos
    plano_notas: Mapped[str | None] = mapped_column(Text)
    plano_em: Mapped[date | None] = mapped_column(Date)
    plano_modelo: Mapped[str | None] = mapped_column(String(60))

    emprestimo_conta: Mapped["Conta | None"] = relationship()
    avaliacoes: Mapped[list["AvaliacaoBem"]] = relationship(back_populates="bem", cascade="all, delete-orphan",
                                                            order_by="AvaliacaoBem.data")
    construcoes: Mapped[list["Construcao"]] = relationship(cascade="all, delete-orphan",
                                                          order_by="[Construcao.ordem, Construcao.id]")
    registos_km: Mapped[list["RegistoKm"]] = relationship(cascade="all, delete-orphan",
                                                         order_by="[RegistoKm.data, RegistoKm.km]")
    plano: Mapped[list["ItemPlano"]] = relationship(cascade="all, delete-orphan", order_by="ItemPlano.ordem")
    manutencoes: Mapped[list["Manutencao"]] = relationship(cascade="all, delete-orphan",
                                                          order_by="[Manutencao.data, Manutencao.km]")

    @property
    def tipo_rotulo(self) -> str:
        return TIPOS_BEM.get(self.tipo, self.tipo)

    @property
    def uso_rotulo(self) -> str:
        return USOS_BEM.get(self.tipo, {}).get(self.uso, self.uso)

    @property
    def lista_padroes(self) -> list[str]:
        return [p.strip().upper() for p in (self.padroes or "").replace("|", "\n").splitlines() if p.strip()]

    @property
    def vendido(self) -> bool:
        """Já vendido (ou, na viatura da empresa, devolvido): só a partir da data. Uma data futura (o fim previsto
        do contrato da viatura da empresa) não o tira de uso — continua com manutenção, km e plano."""
        return self.data_venda is not None and self.data_venda <= date.today()

    @property
    def lista_ocupacao(self) -> list[str]:
        return [x for x in (self.ocupacao or "").split(",") if x in OCUPACOES]

    @property
    def lista_agua(self) -> list[str]:
        return [x for x in (self.agua or "").split(",") if x in AGUAS]

    @property
    def tiragens(self) -> list[int]:
        """Anos de tiragem da cortiça, por ordem."""
        anos = set()
        for x in (self.anos_tiragem or "").replace(";", ",").replace(" ", ",").split(","):
            if x.strip().isdigit() and 1900 < int(x) < 2200:
                anos.add(int(x))
        return sorted(anos)

    @property
    def proxima_tiragem(self) -> int | None:
        t = self.tiragens
        return t[-1] + CICLO_CORTICA if self.tem_cortica and t else None

    @property
    def empresa(self) -> bool:
        """Viatura da empresa: não é património (sem valor, dívida nem avaliação) — serve só para
        lhe alocar o combustível."""
        return self.tipo == "carro" and self.uso == "empresa"


def norm_matricula(m: str | None) -> str:
    return "".join(ch for ch in (m or "").upper() if ch.isalnum())


class ViaVerde(Base):
    """Linha do extrato Via Verde (portagem, parque, estacionamento…), ligada ao débito no banco que a pagou.
    Não se guardam nome, NIF nem morada do extrato."""
    __tablename__ = "via_verde"
    __table_args__ = (UniqueConstraint("entidade_id", "hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entidade_id: Mapped[int] = mapped_column(ForeignKey("entidade.id", ondelete="CASCADE"), index=True)
    contrato: Mapped[str | None] = mapped_column(String(20))
    matricula: Mapped[str] = mapped_column(String(12))               # normalizada (AC71ZE)
    data: Mapped[date] = mapped_column(Date, index=True)               # data de saída (ou da transação)
    hora: Mapped[str | None] = mapped_column(String(5))
    entrada: Mapped[str | None] = mapped_column(String(80))
    saida: Mapped[str | None] = mapped_column(String(80))
    valor: Mapped[int] = mapped_column(BigInteger)                    # cêntimos, positivo
    operador: Mapped[str | None] = mapped_column(String(80))
    servico: Mapped[str | None] = mapped_column(String(60))           # Portagens | Parques de estacionamento | …
    documento: Mapped[str | None] = mapped_column(String(40))
    data_pagamento: Mapped[date | None] = mapped_column(Date)
    hash: Mapped[str] = mapped_column(String(40))
    movimento_id: Mapped[int | None] = mapped_column(ForeignKey("movimento.id", ondelete="SET NULL"), index=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    movimento: Mapped["Movimento | None"] = relationship()

    @property
    def matricula_fmt(self) -> str:
        m = self.matricula
        return f"{m[:2]}-{m[2:4]}-{m[4:]}" if len(m) == 6 else m


class Abastecimento(Base):
    """Abastecimento registado no Fuelio (importado do CSV de sincronização), ligado ao débito no banco."""
    __tablename__ = "abastecimento"
    __table_args__ = (UniqueConstraint("bem_id", "guid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bem_id: Mapped[int] = mapped_column(ForeignKey("bem.id", ondelete="CASCADE"), index=True)
    data: Mapped[date] = mapped_column(Date, index=True)
    hora: Mapped[str | None] = mapped_column(String(5))
    km: Mapped[int | None] = mapped_column(Integer)
    litros: Mapped[float] = mapped_column(Float)
    cheio: Mapped[bool] = mapped_column(Boolean, default=True)
    valor: Mapped[int | None] = mapped_column(BigInteger)             # cêntimos, total pago
    preco_litro: Mapped[float | None] = mapped_column(Float)
    posto: Mapped[str | None] = mapped_column(String(120))
    notas: Mapped[str | None] = mapped_column(String(300))
    guid: Mapped[str] = mapped_column(String(64))
    movimento_id: Mapped[int | None] = mapped_column(ForeignKey("movimento.id", ondelete="SET NULL"), index=True)

    movimento: Mapped["Movimento | None"] = relationship()


class Construcao(Base):
    """Construção numa propriedade rústica (casa, armazém…): designação, artigo matricial, área de terreno e
    área bruta. Construções no mesmo artigo partilham a área de terreno (conta uma vez no total)."""
    __tablename__ = "construcao"

    id: Mapped[int] = mapped_column(primary_key=True)
    bem_id: Mapped[int] = mapped_column(ForeignKey("bem.id", ondelete="CASCADE"), index=True)
    designacao: Mapped[str] = mapped_column(String(100))
    tipo: Mapped[str | None] = mapped_column(String(12))
    artigo: Mapped[str | None] = mapped_column(String(60))
    area_terreno: Mapped[float | None] = mapped_column(Float)          # m² (do artigo)
    area_bruta: Mapped[float | None] = mapped_column(Float)            # m²
    estado: Mapped[str | None] = mapped_column(String(12))
    ordem: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def tipo_rotulo(self) -> str:
        return TIPOS_CONSTRUCAO.get(self.tipo or "", "")


TIPOS_RECHEIO = {"eletrodomestico": "Eletrodoméstico", "eletronica": "Eletrónica / imagem e som",
                 "informatica": "Informática / telemóvel", "mobiliario": "Mobiliário", "decoracao": "Decoração / têxteis",
                 "ferramenta": "Ferramentas / jardim", "outro": "Outro"}
ESTADOS_RECHEIO = {"ativo": "Em uso", "avariado": "Avariado", "abatido": "Abatido / vendido", "ignorado": "Não é recheio"}
ESTADOS_PEDIDO = {"aberto": "Aberto", "reparacao": "Em reparação", "resolvido": "Resolvido", "recusado": "Recusado"}
GARANTIA_LEGAL_MESES = 36       # Portugal, bens novos comprados desde 2022


class ItemRecheio(Base):
    """Bem do recheio de uma casa (eletrodoméstico, televisão, móvel…), criado a partir da compra (movimento) numa
    categoria marcada como recheio. Ficha para o seguro e para a garantia."""
    __tablename__ = "item_recheio"

    id: Mapped[int] = mapped_column(primary_key=True)
    entidade_id: Mapped[int] = mapped_column(ForeignKey("entidade.id", ondelete="CASCADE"), index=True)
    bem_id: Mapped[int | None] = mapped_column(ForeignKey("bem.id", ondelete="SET NULL"), index=True)   # a casa
    movimento_id: Mapped[int | None] = mapped_column(ForeignKey("movimento.id", ondelete="SET NULL"), index=True)
    nome: Mapped[str] = mapped_column(String(120))
    tipo: Mapped[str | None] = mapped_column(String(16))
    marca: Mapped[str | None] = mapped_column(String(60))
    modelo: Mapped[str | None] = mapped_column(String(80))
    n_serie: Mapped[str | None] = mapped_column(String(60))
    atributos: Mapped[str | None] = mapped_column(Text)               # classe energética, capacidade, dimensões…
    data_compra: Mapped[date | None] = mapped_column(Date)
    valor: Mapped[int | None] = mapped_column(BigInteger)             # cêntimos
    garantia_meses: Mapped[int] = mapped_column(Integer, default=GARANTIA_LEGAL_MESES, server_default="36")
    loja: Mapped[str | None] = mapped_column(String(120))
    contacto_telefone: Mapped[str | None] = mapped_column(String(40))
    contacto_email: Mapped[str | None] = mapped_column(String(120))
    contacto_site: Mapped[str | None] = mapped_column(String(200))
    estado: Mapped[str] = mapped_column(String(10), default="ativo", server_default="ativo")
    notas: Mapped[str | None] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    bem: Mapped["Bem | None"] = relationship()
    movimento: Mapped["Movimento | None"] = relationship()
    pedidos: Mapped[list["PedidoGarantia"]] = relationship(cascade="all, delete-orphan",
                                                           order_by="PedidoGarantia.data.desc()")

    @property
    def fim_garantia(self) -> date | None:
        if not self.data_compra or not self.garantia_meses:
            return None
        import calendar
        m = self.data_compra.month - 1 + self.garantia_meses
        a, m = self.data_compra.year + m // 12, m % 12 + 1
        return date(a, m, min(self.data_compra.day, calendar.monthrange(a, m)[1]))

    def estado_garantia(self, hoje: date) -> str:
        """em garantia | termina em breve (< 90 dias) | expirada | sem data"""
        f = self.fim_garantia
        if not f:
            return "sem data"
        if f < hoje:
            return "expirada"
        return "termina em breve" if (f - hoje).days <= 90 else "em garantia"

    @property
    def pedido_aberto(self) -> "PedidoGarantia | None":
        return next((p for p in self.pedidos if p.estado in ("aberto", "reparacao")), None)


class PedidoGarantia(Base):
    """Pedido de garantia / assistência de um item do recheio."""
    __tablename__ = "pedido_garantia"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item_recheio.id", ondelete="CASCADE"), index=True)
    data: Mapped[date] = mapped_column(Date)
    problema: Mapped[str] = mapped_column(Text)
    referencia: Mapped[str | None] = mapped_column(String(60))        # n.º do processo na loja / marca
    estado: Mapped[str] = mapped_column(String(10), default="aberto")
    resolucao: Mapped[str | None] = mapped_column(Text)
    data_fecho: Mapped[date | None] = mapped_column(Date)


class AbastecimentoPendente(Base):
    """Abastecimento registado na página sem login (/abastecer/…): fica à espera de validação por quem tem
    acesso à entidade do carro; só depois entra nos abastecimentos, nos km e na ligação ao banco."""
    __tablename__ = "abastecimento_pendente"

    id: Mapped[int] = mapped_column(primary_key=True)
    bem_id: Mapped[int] = mapped_column(ForeignKey("bem.id", ondelete="CASCADE"), index=True)
    data: Mapped[date] = mapped_column(Date)
    hora: Mapped[str | None] = mapped_column(String(5))
    km: Mapped[int | None] = mapped_column(Integer)
    litros: Mapped[float] = mapped_column(Float)
    cheio: Mapped[bool] = mapped_column(Boolean, default=True)
    valor: Mapped[int | None] = mapped_column(BigInteger)             # cêntimos
    posto: Mapped[str | None] = mapped_column(String(120))
    notas: Mapped[str | None] = mapped_column(String(300))
    quem: Mapped[str | None] = mapped_column(String(60))              # nome escrito por quem registou (opcional)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    bem: Mapped["Bem"] = relationship()


class RegistoKm(Base):
    """Leitura do conta-quilómetros de um carro numa data (registo periódico ou ida à oficina)."""
    __tablename__ = "registo_km"

    id: Mapped[int] = mapped_column(primary_key=True)
    bem_id: Mapped[int] = mapped_column(ForeignKey("bem.id", ondelete="CASCADE"), index=True)
    data: Mapped[date] = mapped_column(Date)
    km: Mapped[int] = mapped_column(Integer)
    origem: Mapped[str] = mapped_column(String(10), default="manual")   # manual | oficina | ficha
    nota: Mapped[str | None] = mapped_column(String(200))


class ItemPlano(Base):
    """Uma operação do plano de manutenção do carro (ex.: óleo e filtro a cada 15 000 km / 12 meses)."""
    __tablename__ = "item_plano"

    id: Mapped[int] = mapped_column(primary_key=True)
    bem_id: Mapped[int] = mapped_column(ForeignKey("bem.id", ondelete="CASCADE"), index=True)
    item: Mapped[str] = mapped_column(String(120))
    intervalo_km: Mapped[int | None] = mapped_column(Integer)
    intervalo_meses: Mapped[int | None] = mapped_column(Integer)
    custo_min: Mapped[int | None] = mapped_column(BigInteger)          # cêntimos, estimativa
    custo_max: Mapped[int | None] = mapped_column(BigInteger)
    notas: Mapped[str | None] = mapped_column(String(300))
    ordem: Mapped[int] = mapped_column(Integer, default=0)


class Manutencao(Base):
    """Ida à oficina / intervenção feita: data, km, operações do plano cobertas e custo."""
    __tablename__ = "manutencao"

    id: Mapped[int] = mapped_column(primary_key=True)
    bem_id: Mapped[int] = mapped_column(ForeignKey("bem.id", ondelete="CASCADE"), index=True)
    data: Mapped[date] = mapped_column(Date)
    km: Mapped[int | None] = mapped_column(Integer)
    itens: Mapped[str | None] = mapped_column(Text)                     # nomes dos itens do plano, um por linha
    custo: Mapped[int | None] = mapped_column(BigInteger)
    movimento_id: Mapped[int | None] = mapped_column(ForeignKey("movimento.id", ondelete="SET NULL"))
    nota: Mapped[str | None] = mapped_column(String(300))

    movimento: Mapped["Movimento | None"] = relationship()

    @property
    def lista_itens(self) -> list[str]:
        return [i for i in (self.itens or "").splitlines() if i.strip()]


class AvaliacaoBem(Base):
    """Valor estimado de um bem numa data (IA ou indicado à mão) — o histórico das revisões."""
    __tablename__ = "avaliacao_bem"

    id: Mapped[int] = mapped_column(primary_key=True)
    bem_id: Mapped[int] = mapped_column(ForeignKey("bem.id", ondelete="CASCADE"), index=True)
    data: Mapped[date] = mapped_column(Date)
    valor: Mapped[int] = mapped_column(BigInteger)                    # cêntimos
    minimo: Mapped[int | None] = mapped_column(BigInteger)
    maximo: Mapped[int | None] = mapped_column(BigInteger)
    fonte: Mapped[str] = mapped_column(String(10), default="manual")  # ia | manual
    justificacao: Mapped[str | None] = mapped_column(Text)
    referencias: Mapped[str | None] = mapped_column(Text)             # URLs consultadas pela IA (uma por linha)
    confianca: Mapped[float | None] = mapped_column(Float)
    modelo: Mapped[str | None] = mapped_column(String(60))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    bem: Mapped[Bem] = relationship(back_populates="avaliacoes")


class ParIgnorado(Base):
    """Sugestão de ligação (débito ↔ crédito) que o utilizador disse não ser o mesmo dinheiro."""
    __tablename__ = "par_ignorado"
    __table_args__ = (UniqueConstraint("debito_id", "credito_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    debito_id: Mapped[int] = mapped_column(ForeignKey("movimento.id", ondelete="CASCADE"))
    credito_id: Mapped[int] = mapped_column(ForeignKey("movimento.id", ondelete="CASCADE"))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())


class SugestaoIA(Base):
    """Resposta da IA para um padrão de descrição (cache: cada padrão só é perguntado uma vez)."""
    __tablename__ = "sugestao_ia"

    id: Mapped[int] = mapped_column(primary_key=True)
    chave: Mapped[str] = mapped_column(String(200), unique=True)   # padrão + sentido, ex.: "ROYAL VERSAILLES|SAIDA"
    comerciante: Mapped[str | None] = mapped_column(String(120))
    atividade: Mapped[str | None] = mapped_column(String(200))
    categoria_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id", ondelete="SET NULL"))
    confianca: Mapped[float | None] = mapped_column(Float)
    modelo: Mapped[str | None] = mapped_column(String(60))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())
    # Análise de um movimento (ecrã do movimento): localização, site, explicação e fontes, em JSON; web = com pesquisa
    detalhe: Mapped[str | None] = mapped_column(Text)
    web: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    categoria: Mapped["Categoria | None"] = relationship()

    @property
    def info(self) -> dict:
        import json
        try:
            return json.loads(self.detalhe) if self.detalhe else {}
        except ValueError:
            return {}


class Definicao(Base):
    """Preferências da app (chave → valor em texto), editáveis em Administração."""
    __tablename__ = "definicao"

    chave: Mapped[str] = mapped_column(String(50), primary_key=True)
    valor: Mapped[str] = mapped_column(String(500))


class Auditoria(Base):
    __tablename__ = "auditoria"

    id: Mapped[int] = mapped_column(primary_key=True)
    quando: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())
    utilizador_id: Mapped[int | None] = mapped_column(ForeignKey("utilizador.id", ondelete="SET NULL"))
    acao: Mapped[str] = mapped_column(String(50))
    detalhe: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------- saúde (Google Health / Takeout)
class PerfilSaude(Base):
    """Perfil de saúde de uma pessoa da entidade (ex.: Família › Sérgio). Vê-o quem tem acesso à entidade; os dados
    vêm da exportação do Google Health (Takeout). Ligar a um utilizador é opcional (pessoas sem conta na app)."""
    __tablename__ = "perfil_saude"
    __table_args__ = (UniqueConstraint("entidade_id", "nome"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entidade_id: Mapped[int] = mapped_column(ForeignKey("entidade.id", ondelete="CASCADE"), index=True)
    nome: Mapped[str] = mapped_column(String(80))
    utilizador_id: Mapped[int | None] = mapped_column(ForeignKey("utilizador.id", ondelete="SET NULL"))
    altura_cm: Mapped[float | None] = mapped_column(Float)
    importado_em: Mapped[datetime | None] = mapped_column(DateTime)
    importado_ate: Mapped[date | None] = mapped_column(Date)      # último dia com dados na última importação


class MedidaSaude(Base):
    """Um valor diário de um perfil: passos, peso, sono, frequência cardíaca em repouso… (tipos em app/saude.py).
    Reimportar substitui o valor do dia."""
    __tablename__ = "medida_saude"
    __table_args__ = (UniqueConstraint("perfil_id", "data", "tipo"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    perfil_id: Mapped[int] = mapped_column(ForeignKey("perfil_saude.id", ondelete="CASCADE"), index=True)
    data: Mapped[date] = mapped_column(Date, index=True)
    tipo: Mapped[str] = mapped_column(String(30))
    valor: Mapped[float] = mapped_column(Float)
    fonte: Mapped[str | None] = mapped_column(String(60))


class TreinoSaude(Base):
    """Treino/exercício registado (Google Health › UserExercises)."""
    __tablename__ = "treino_saude"
    __table_args__ = (UniqueConstraint("perfil_id", "externo_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    perfil_id: Mapped[int] = mapped_column(ForeignKey("perfil_saude.id", ondelete="CASCADE"), index=True)
    externo_id: Mapped[str] = mapped_column(String(40))
    inicio: Mapped[datetime] = mapped_column(DateTime)            # hora local
    duracao_min: Mapped[float | None] = mapped_column(Float)
    atividade: Mapped[str] = mapped_column(String(60))
    calorias: Mapped[float | None] = mapped_column(Float)
    passos: Mapped[int | None] = mapped_column(Integer)
    distancia_km: Mapped[float | None] = mapped_column(Float)
    fc_media: Mapped[float | None] = mapped_column(Float)
    fc_maxima: Mapped[float | None] = mapped_column(Float)
    manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class DispositivoTV(Base):
    """Televisão emparelhada (vista /tv, só de consulta). Vê as entidades escolhidas ao aprovar, com as permissões do
    utilizador que a aprovou; deixa de funcionar se o utilizador mudar a password (versao_sessao) ou perder o acesso.
    Guarda-se só o hash do token do cookie."""
    __tablename__ = "dispositivo_tv"

    id: Mapped[int] = mapped_column(primary_key=True)
    utilizador_id: Mapped[int] = mapped_column(ForeignKey("utilizador.id", ondelete="CASCADE"), index=True)
    nome: Mapped[str] = mapped_column(String(60))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    entidades: Mapped[str] = mapped_column(String(200), default="")         # ids separados por vírgulas
    versao_sessao: Mapped[int] = mapped_column(Integer)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    ultimo_acesso: Mapped[datetime | None] = mapped_column(DateTime)

    utilizador: Mapped[Utilizador] = relationship()

    @property
    def ids_entidades(self) -> list[int]:
        return [int(x) for x in self.entidades.split(",") if x.strip().isdigit()]


class RegraDocumento(Base):
    """Regra do motor de classificação dos documentos do Paperless (app/motor_docs.py). Condição sobre o texto (OCR) e,
    opcionalmente, sobre o tipo/correspondente atual; ações: correspondente, tipo, etiquetas e atributos a extrair para
    campos personalizados. Cada gravação sobe a versão e guarda uma cópia em RegraDocumentoVersao.
    Uma regra sem extração pode ser publicada no Paperless como fluxo nativo (paperless_fluxo_id)."""
    __tablename__ = "regra_documento"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(120), unique=True)
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)
    ordem: Mapped[int] = mapped_column(Integer, default=100)
    algoritmo: Mapped[str] = mapped_column(String(10), default="todas")      # qualquer | todas | exato | regex
    texto: Mapped[str] = mapped_column(String(500), default="")
    so_tipo: Mapped[str | None] = mapped_column(String(128))                # só documentos deste tipo (nome)
    so_sem_correspondente: Mapped[bool] = mapped_column(Boolean, default=False)
    correspondente: Mapped[str | None] = mapped_column(String(128))
    tipo: Mapped[str | None] = mapped_column(String(128))
    etiquetas: Mapped[str] = mapped_column(String(500), default="")        # nomes separados por vírgulas
    extrair: Mapped[str] = mapped_column(String(200), default="")          # chaves de motor_docs.ATRIBUTOS
    confianca: Mapped[str] = mapped_column(String(10), default="alta")      # alta = aplica sozinha | media = revisão
    nota: Mapped[str | None] = mapped_column(Text)
    origem: Mapped[str] = mapped_column(String(10), default="manual")       # manual | ia | sugestao
    versao: Mapped[int] = mapped_column(Integer, default=1)
    paperless_fluxo_id: Mapped[int | None] = mapped_column(Integer)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    alterado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    @property
    def lista_etiquetas(self) -> list[str]:
        return [x.strip() for x in (self.etiquetas or "").split(",") if x.strip()]

    @property
    def lista_extrair(self) -> list[str]:
        return [x.strip() for x in (self.extrair or "").split(",") if x.strip()]


class RegraDocumentoVersao(Base):
    """Histórico das regras: cópia (JSON) de cada versão, quem a gravou e porquê."""
    __tablename__ = "regra_documento_versao"
    __table_args__ = (UniqueConstraint("regra_id", "versao"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    regra_id: Mapped[int] = mapped_column(ForeignKey("regra_documento.id", ondelete="CASCADE"), index=True)
    versao: Mapped[int] = mapped_column(Integer)
    dados: Mapped[str] = mapped_column(Text)
    utilizador_id: Mapped[int | None] = mapped_column(ForeignKey("utilizador.id", ondelete="SET NULL"))
    nota: Mapped[str | None] = mapped_column(String(200))
    em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ClassificacaoDocumento(Base):
    """Último resultado do motor para um documento do Paperless: fila de revisão e base da auditoria de regras.
    estado: aplicado | revisao | sem_regra | ignorado | erro."""
    __tablename__ = "classificacao_documento"

    id: Mapped[int] = mapped_column(primary_key=True)
    paperless_id: Mapped[int] = mapped_column(Integer, unique=True)
    titulo: Mapped[str] = mapped_column(String(200), default="")
    adicionado: Mapped[date | None] = mapped_column(Date)
    estado: Mapped[str] = mapped_column(String(12), index=True)
    origem: Mapped[str | None] = mapped_column(String(10))                  # regra | ia
    regra_id: Mapped[int | None] = mapped_column(ForeignKey("regra_documento.id", ondelete="SET NULL"), index=True)
    versao_regra: Mapped[int | None] = mapped_column(Integer)
    confianca: Mapped[str | None] = mapped_column(String(10))
    proposta: Mapped[str | None] = mapped_column(Text)                      # JSON: alterações propostas e atributos
    motivo: Mapped[str | None] = mapped_column(Text)
    movimento_id: Mapped[int | None] = mapped_column(ForeignKey("movimento.id", ondelete="SET NULL"))
    processado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AlteracaoDocumento(Base):
    """Registo de auditoria: cada campo alterado num documento do Paperless pelo motor (antes → depois, regra e versão)."""
    __tablename__ = "alteracao_documento"

    id: Mapped[int] = mapped_column(primary_key=True)
    paperless_id: Mapped[int] = mapped_column(Integer, index=True)
    lote: Mapped[str] = mapped_column(String(40), index=True)
    campo: Mapped[str] = mapped_column(String(60))
    antes: Mapped[str | None] = mapped_column(Text)
    depois: Mapped[str | None] = mapped_column(Text)
    origem: Mapped[str] = mapped_column(String(10))                          # regra | ia | manual
    regra_id: Mapped[int | None] = mapped_column(ForeignKey("regra_documento.id", ondelete="SET NULL"))
    versao_regra: Mapped[int | None] = mapped_column(Integer)
    utilizador_id: Mapped[int | None] = mapped_column(ForeignKey("utilizador.id", ondelete="SET NULL"))
    em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class DispositivoSaude(Base):
    """Telemóvel emparelhado que envia os dados do Health Connect (app Android «Gestor Saúde») para um perfil de saúde.
    Só pode enviar dados para esse perfil (/api/saude/enviar); guarda-se o hash do token. Deixa de funcionar se quem o
    aprovou mudar a password (versao_sessao) ou perder o «editar» na entidade do perfil."""
    __tablename__ = "dispositivo_saude"

    id: Mapped[int] = mapped_column(primary_key=True)
    perfil_id: Mapped[int] = mapped_column(ForeignKey("perfil_saude.id", ondelete="CASCADE"), index=True)
    utilizador_id: Mapped[int] = mapped_column(ForeignKey("utilizador.id", ondelete="CASCADE"))
    nome: Mapped[str] = mapped_column(String(60))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    versao_sessao: Mapped[int] = mapped_column(Integer)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    ultimo_envio: Mapped[datetime | None] = mapped_column(DateTime)
    ultimo_resumo: Mapped[str | None] = mapped_column(String(200))

    perfil: Mapped[PerfilSaude] = relationship()
    utilizador: Mapped[Utilizador] = relationship()
