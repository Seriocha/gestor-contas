# Gestor de Contas — Documento de Design

> Estado: proposta v0.1 (2026-10-14). Nada implementado ainda.
> App própria para a NAS QNAP, segue o padrão de `C:\Projetos\QNAP-Container-Apps\SETUP.md`
> (GitHub privado → Actions → GHCR → Container Station).

---

## 1. Objetivo e âmbito

Gestor financeiro doméstico, self-hosted, para 2 utilizadores (eu + mulher), que:

1. Importa movimentos de bancos portugueses (ficheiros exportados do homebanking; open banking numa fase posterior).
2. Gere **contas à ordem/poupança**, **cartões de crédito**, **contas de investimento** e **Certificados de Aforro**.
3. Deteta e liga **transferências entre bancos** (incl. carregamentos Revolut para viagens), para não contarem como despesa/receita.
4. Calcula **ganhos/perdas** de investimentos a partir de *snapshots* periódicos da carteira (valor + composição).
5. Mantém a **conta da Mãe (Crédito Agrícola) como património separado**, fora dos totais da família.

Fora de âmbito (pelo menos na v1): pagamentos/ordens a partir da app, guardar credenciais de homebanking, exposição à Internet.

## 2. Instituições e contas previstas

| Instituição | Tipo de conta | Fonte de dados (v1) | Notas |
|---|---|---|---|
| Santander | Ordem, cartão de crédito | Exportação XLS/CSV do NetBanco | Extrato do cartão em PDF/XLS |
| ActivoBank | Ordem, cartão de crédito | Exportação XLS/CSV | |
| Banco BiG | Investimento (+ conta à ordem associada) | Exportação de movimentos + **posição da carteira** (XLS) | Snapshots periódicos |
| CTT / IGCP | Certificados de Aforro | Introdução manual / PDF de posição do AforroNet | Valor = unidades × valor unitário capitalizado |
| Revolut | Multi-moeda (viagens) | CSV de extrato (por moeda) | Carregamentos vindos das contas à ordem; câmbios |
| Crédito Agrícola | Ordem (**Mãe**) | Exportação CSV/XLS | Entidade "Mãe", isolada |

Os formatos exatos de cada exportação têm de ser validados com ficheiros reais (amostras anonimizadas em `tests/fixtures/`).

## 3. Arquitetura

```
 Browser (PC / telemóvel na LAN ou VPN)
        │  HTTPS (reverse proxy QTS ou Tailscale)
        ▼
 ┌──────────────────────── container gestor-contas ────────────────────────┐
 │ FastAPI  ─ rotas HTML (Jinja2 + HTMX + Chart.js, sem build de frontend)  │
 │          ─ API JSON /api/...                                             │
 │ Serviços: importação (parsers por banco) · dedup · categorização        │
 │           · matcher de transferências · cálculo de desempenho            │
 │ SQLAlchemy + Alembic  →  SQLite (WAL)                                    │
 └──────────────┬───────────────────────────────────────────────────────────┘
                │ volumes
   /data  → /share/Container/gestor-contas/data   (gestor.db, uploads/)
   /backup→ /share/Container/gestor-contas/backup (dump diário)
```

**Porquê esta stack**: é a mesma do MyGeneo (FastAPI), SQLite chega de sobra para 2 utilizadores e simplifica backups (1 ficheiro), e HTMX evita um build de frontend no Dockerfile. Postgres só se justificaria com muitos utilizadores concorrentes.

- Porta externa: **8200** (interna 8000) — registar na tabela §4 do SETUP.md.
- Imagem: `ghcr.io/seriocha/gestor-contas` (multi-arch, tags `vX.Y.Z`).
- Repo GitHub **privado**. ⚠️ Tornar o pacote GHCR público expõe só o código, nunca dados — mas, sendo uma app financeira, avaliar manter privado + `docker login` com PAT `read:packages` na NAS.

## 4. Multiutilizador e privacidade

Três conceitos separados:

- **Utilizador** — quem faz login (eu, mulher).
- **Entidade (património)** — conjunto de contas que se somam entre si: `Família` e `Mãe`. Os dashboards são sempre por entidade; a Mãe nunca entra nos totais da Família.
- **Titular** — a quem pertence a conta dentro da entidade (meu, dela, conjunta) → filtros "as minhas / as dela / todas".

Permissões por entidade: `admin` (gere contas, regras, utilizadores), `editar` (importa, categoriza), `ver`. Ex.: eu = admin em Família e Mãe; mulher = editar em Família, sem acesso a Mãe (a decidir).

Autenticação: login próprio (password com argon2, sessão em cookie `HttpOnly`/`SameSite=Strict`), bloqueio após tentativas falhadas. Sem acesso pela Internet — só LAN/VPN.

## 5. Modelo de dados

Montantes guardados em **cêntimos (inteiro)** + código de moeda, nunca float.

```
utilizador(id, nome, email, password_hash, ativo)
entidade(id, nome)                                   -- Família, Mãe
permissao(utilizador_id, entidade_id, papel)         -- admin|editar|ver

instituicao(id, nome, bic, parser)                   -- santander, activobank, big, igcp, revolut, ca
conta(id, entidade_id, instituicao_id, nome, tipo, moeda, iban_mascarado,
      conta_pagamento_id NULL,                       -- cartão → conta à ordem que o paga
      dia_fecho NULL, dia_pagamento NULL,            -- cartões
      saldo_inicial, data_saldo_inicial, ativa)
      -- tipo: ORDEM | POUPANCA | CARTAO_CREDITO | INVESTIMENTO | AFORRO | CARTEIRA_DIGITAL
conta_titular(conta_id, utilizador_id)

importacao(id, conta_id, utilizador_id, ficheiro, sha256, parser, criado_em,
           n_lidos, n_novos, n_duplicados, estado)
movimento(id, conta_id, importacao_id,
          data_mov, data_valor, descricao_original, descricao_norm,
          montante, moeda, montante_eur, taxa_cambio NULL,
          saldo_apos NULL, categoria_id NULL, transferencia_id NULL,
          viagem_id NULL, extrato_cartao_id NULL, notas,
          hash_dedup UNIQUE)

categoria(id, nome, pai_id, natureza)                -- RECEITA | DESPESA | NEUTRA
regra_categoria(id, entidade_id, padrao_regex, conta_id NULL,
                montante_min NULL, montante_max NULL, categoria_id, prioridade)

transferencia(id, tipo, mov_saida_id, mov_entrada_id NULL, estado, criado_por)
      -- tipo: INTERNA | PAGAMENTO_CARTAO | CARREGAMENTO_REVOLUT | CAMBIO | INVESTIMENTO
      -- estado: SUGERIDA | CONFIRMADA | REJEITADA

extrato_cartao(id, conta_id, periodo_ini, periodo_fim, data_limite,
               total, minimo, valor_pago, estado)     -- ABERTO | FECHADO | PAGO | EM_ATRASO
viagem(id, entidade_id, nome, data_ini, data_fim, orcamento_eur)

ativo(id, isin, ticker, nome, classe, moeda)
      -- classe: ACAO | ETF | FUNDO | OBRIGACAO | PPR | CERT_AFORRO | LIQUIDEZ
snapshot_carteira(id, conta_id, data, valor_total, liquidez, origem, importacao_id NULL)
posicao(snapshot_id, ativo_id, quantidade, preco, valor, custo_total NULL)
fluxo_investimento(id, conta_id, data, tipo, montante, movimento_id NULL, ativo_id NULL)
      -- tipo: ENTRADA | SAIDA | DIVIDENDO | JURO | COMISSAO | IMPOSTO
```

## 6. Importação

Fluxo: **upload → deteção do formato → pré-visualização → confirmar → pós-processamento**.

1. **Parsers por banco** (`app/parsers/<banco>.py`), cada um com `detetar(ficheiro) -> bool` (assinatura do cabeçalho) e `ler(ficheiro) -> list[MovimentoBruto]`. Lidam com as particularidades PT: `dd-mm-aaaa`, decimal `1.234,56`, débito/crédito em colunas separadas, linhas de cabeçalho/rodapé, encoding Windows-1252.
2. **Parser genérico CSV/XLSX** com mapeamento de colunas guardado por conta (para bancos ainda sem parser dedicado).
3. **Deduplicação**: `hash_dedup = sha256(conta, data_mov, montante, descricao_norm, n.º de ocorrência no dia)`. Reimportar o mesmo período é seguro. O `sha256` do ficheiro inteiro avisa se já foi importado.
4. **Reconciliação de saldo**: quando o extrato traz saldo, comparar o saldo calculado com o do banco e assinalar diferenças.
5. **Pós-processamento**: categorização por regras → matcher de transferências → associação a extrato de cartão / viagem.

**Normalização de descrições** (exemplos típicos PT): `COMPRA 1234 CONTINENTE LISBOA` → `CONTINENTE`; prefixos `TRF`, `MB WAY`, `DD` (débito direto), `LEV ATM`, `PAG SERV` tratados por regras base.

**Open banking (fase 4)**: via um agregador PSD2 (ex.: Enable Banking, que tem plano gratuito para uso pessoal; o GoCardless Bank Account Data deixou de aceitar novos registos em 2025). Consentimento renovável a cada 90–180 dias. A cobertura de BiG, Crédito Agrícola e IGCP tem de ser confirmada — por isso a importação por ficheiro é a base e o open banking é só mais uma "fonte".

## 7. Transferências entre bancos e Revolut

**Matcher automático** — para cada movimento de saída sem par, procurar entrada noutra conta da mesma entidade com:
- mesmo montante absoluto (ou montante convertido ± tolerância, se moedas diferentes),
- data dentro de ±3 dias úteis,
- pistas na descrição (IBAN parcial, "TRF", "REVOLUT", nome do titular).

Resultado = `transferencia` em estado `SUGERIDA`; o utilizador confirma num ecrã de "por reconciliar". Transferências confirmadas têm natureza **NEUTRA** (não contam em receitas/despesas). Se só um lado existir (conta não gerida), passa a despesa/receita normal.

**Casos Revolut**:
- *Carregamento*: saída na conta à ordem (Santander/ActivoBank) ↔ "Top-up" na Revolut EUR → `CARREGAMENTO_REVOLUT`.
- *Câmbio*: EUR → USD dentro da Revolut = dois movimentos ligados como `CAMBIO`; guarda-se a taxa.
- *Despesas em viagem*: movimentos na moeda local com `montante_eur` calculado pela taxa do câmbio associado (ou BCE do dia, se não houver). Atribuição automática à **viagem** cujo intervalo de datas contém o movimento.
- *Sobras*: devolução Revolut → conta à ordem = transferência normal.

Relatório de viagem: total gasto em EUR, por categoria e por dia, vs. orçamento; custo cambial (comissões + spread).

## 8. Cartões de crédito

- Conta do tipo `CARTAO_CREDITO` com `dia_fecho`, `dia_pagamento` e `conta_pagamento_id`.
- Compras do cartão são as despesas reais (categorizadas). O débito mensal na conta à ordem é ligado como `PAGAMENTO_CARTAO` (neutro) → evita contar a despesa duas vezes.
- `extrato_cartao` por ciclo: total, mínimo, data limite, valor pago → estado (pago / parcial / em atraso). Alerta no dashboard para pagamentos a vencer.
- Saldo devedor atual = compras desde o último fecho + extrato em aberto.

## 9. Investimentos

**Snapshots** (BiG por import da posição; outros manualmente): data, valor total, liquidez e posições (ativo, quantidade, preço, valor, custo se disponível).

**Fluxos externos**: entradas/saídas de dinheiro da conta de investimento (normalmente ligadas por transferência a uma conta à ordem) + dividendos, juros, comissões, impostos.

**Cálculos** entre dois snapshots *t0* e *t1*:
- Ganho absoluto: `G = V1 − V0 − (entradas − saídas)`
- Rentabilidade no período: Modified Dietz (pondera os fluxos pelo tempo).
- Rentabilidade anualizada desde o início: XIRR (TIR com datas).
- Por ativo (quando há custo): mais/menos-valias latentes = `valor − custo_total`; composição por classe de ativo e por moeda.

**Certificados de Aforro**: cada subscrição é um ativo `CERT_AFORRO` (série, data, unidades). V1: valor atualizado manualmente a partir do AforroNet. Fase posterior: calcular o valor pelas taxas publicadas pelo IGCP.

## 10. Ecrãs

1. **Dashboard** (por entidade): património líquido e evolução mensal, saldos por conta, receitas vs despesas do mês, cartões a pagar, transferências por reconciliar.
2. **Contas → Movimentos**: tabela filtrável (conta, titular, categoria, datas, texto), edição de categoria em linha, dividir movimento.
3. **Importar**: upload, pré-visualização com novos/duplicados, histórico de importações (com anular importação).
4. **Reconciliar**: transferências sugeridas, confirmar/rejeitar.
5. **Cartões**: ciclos, extratos, próximo pagamento.
6. **Investimentos**: evolução do valor vs capital investido, ganhos por período, composição.
7. **Viagens**: lista e relatório por viagem.
8. **Relatórios**: despesas por categoria/mês, comparação anual, exportação CSV/XLSX.
9. **Configuração**: categorias, regras, contas, utilizadores e permissões.

## 11. Segurança e backups

- Sem porta aberta para a Internet; acesso remoto só por VPN (QVPN/Tailscale).
- HTTPS pelo reverse proxy do QTS (ou certificado do Tailscale).
- Nunca guardar credenciais de homebanking. Tokens de open banking (fase 4) cifrados com chave em variável de ambiente.
- Backup diário dentro do container (`sqlite3 .backup` → `/backup`, retenção 30 dias) + HBS3 da pasta para outro destino.
- Registo de auditoria: quem importou/alterou o quê.

## 12. Estrutura do repositório

```
gestor-contas/
├── app/
│   ├── main.py            # FastAPI, rotas
│   ├── models.py          # SQLAlchemy
│   ├── auth.py
│   ├── parsers/           # santander.py, activobank.py, big.py, revolut.py, ca.py, generico.py
│   ├── services/          # importacao.py, dedup.py, categorias.py, transferencias.py, desempenho.py
│   ├── templates/  static/
├── migrations/            # Alembic
├── tests/fixtures/        # extratos reais ANONIMIZADOS
├── Dockerfile  .dockerignore  release.sh
├── .github/workflows/docker-publish.yml
└── qnap/docker-compose.yml  qnap/update.sh  qnap/README.md
```

## 13. Fases

| Fase | Conteúdo | Resultado |
|---|---|---|
| **1 — MVP** | Esqueleto + deploy NAS, login multiutilizador, entidades/contas, import Santander + ActivoBank + Revolut, dedup, categorias e regras, dashboard simples | Já substitui folhas de cálculo para as contas à ordem |
| **2** | Matcher de transferências, cartões de crédito, viagens/Revolut multi-moeda, Crédito Agrícola (Mãe) | Despesas reais sem duplicações |
| **3** | Investimentos: snapshots BiG, fluxos, Dietz/XIRR, Certificados de Aforro | Ganhos/perdas por período |
| **4** | Open banking (PSD2), parsing de PDFs, cálculo automático CA, preços de mercado | Menos trabalho manual |

## 14. Decisões

Tomadas (2026-10-14, propostas por omissão):
1. A mulher **não** vê a entidade Mãe — novos utilizadores: *Editar* na Família, sem acesso à Mãe.
2. Contas conjuntas: suportadas via vários titulares por conta.
3. Moeda base: EUR.
4. Pacote GHCR público (só código), repo GitHub privado — padrão das outras apps da NAS.

Em aberto:
5. Histórico: desde quando importar? (define o saldo inicial de cada conta)
6. Preciso de **ficheiros de exportação reais** de cada banco (podem ter os valores alterados) para escrever os parsers.
