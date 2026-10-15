"""Modelo de dados — Fase 1 (utilizadores, entidades, permissões, contas).

Montantes em cêntimos (inteiro), nunca float. Ver DESIGN.md §5 para o
modelo completo; as tabelas das fases seguintes entram por migração Alembic.
"""
from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Boolean, Column, Date, DateTime, ForeignKey, Integer, String, Table, Text,
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
    "CARTEIRA_DIGITAL": "Carteira digital (Revolut)",
}


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

    permissoes: Mapped[list["Permissao"]] = relationship(
        back_populates="utilizador", cascade="all, delete-orphan"
    )


class Entidade(Base):
    """Património cujas contas se somam entre si (Família, Mãe)."""
    __tablename__ = "entidade"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(100), unique=True)
    descricao: Mapped[str | None] = mapped_column(Text)
    ordem: Mapped[int] = mapped_column(Integer, default=0)

    contas: Mapped[list["Conta"]] = relationship(back_populates="entidade")


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
    iban_mascarado: Mapped[str | None] = mapped_column(String(40))
    # Cartões de crédito: conta à ordem que paga o cartão + ciclo do extrato.
    conta_pagamento_id: Mapped[int | None] = mapped_column(ForeignKey("conta.id"))
    dia_fecho: Mapped[int | None] = mapped_column(Integer)
    dia_pagamento: Mapped[int | None] = mapped_column(Integer)
    saldo_inicial: Mapped[int] = mapped_column(BigInteger, default=0)  # cêntimos
    data_saldo_inicial: Mapped[date | None] = mapped_column(Date)
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)
    notas: Mapped[str | None] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    entidade: Mapped[Entidade] = relationship(back_populates="contas")
    instituicao: Mapped[Instituicao] = relationship()
    conta_pagamento: Mapped["Conta | None"] = relationship(remote_side="Conta.id")
    titulares: Mapped[list[Utilizador]] = relationship(secondary=conta_titular)

    @property
    def tipo_rotulo(self) -> str:
        return TIPOS_CONTA.get(self.tipo, self.tipo)


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

    conta: Mapped[Conta] = relationship()
    utilizador: Mapped[Utilizador | None] = relationship()


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

    conta: Mapped[Conta] = relationship()
    par: Mapped["Movimento | None"] = relationship(remote_side="Movimento.id", foreign_keys=[par_id],
                                                   post_update=True)
    categoria: Mapped["Categoria | None"] = relationship()
    etiquetas: Mapped[list["Etiqueta"]] = relationship(secondary="movimento_etiqueta")


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
    marcar_transferencia: Mapped[bool] = mapped_column(Boolean, default=False)
    excluir: Mapped[bool] = mapped_column(Boolean, default=False)
    prioridade: Mapped[int] = mapped_column(Integer, default=100)
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    conta: Mapped[Conta | None] = relationship()
    categoria: Mapped[Categoria | None] = relationship()
    etiqueta: Mapped[Etiqueta | None] = relationship()


class Auditoria(Base):
    __tablename__ = "auditoria"

    id: Mapped[int] = mapped_column(primary_key=True)
    quando: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())
    utilizador_id: Mapped[int | None] = mapped_column(ForeignKey("utilizador.id", ondelete="SET NULL"))
    acao: Mapped[str] = mapped_column(String(50))
    detalhe: Mapped[str | None] = mapped_column(Text)
