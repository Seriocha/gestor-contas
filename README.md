# Gestor de Contas

Gestor financeiro doméstico, multiutilizador e self-hosted, para a NAS QNAP. Este projeto
importa movimentos de bancos portugueses e gere contas à ordem, cartões, investimentos e
Certificados de Aforro. O design completo e as fases estão em [DESIGN.md](DESIGN.md).

## Estado: v0.31.0

- Login com várias pessoas: passwords em argon2, bloqueio ao fim de 5 tentativas falhadas,
  proteção CSRF e invalidação das sessões quando a password muda.
- Entidades **Família** e **Mãe** separadas, com permissões por entidade (Ver / Editar / Administrar).
- Contas: criar, editar e arquivar. Tipos: à ordem, poupança, cartão de crédito (com a conta
  que o paga e o ciclo do extrato), investimento, Certificados de Aforro e Revolut. Cada conta
  pode ter titulares.
- Backup automático diário da base SQLite (ficam guardados 30 dias), backup manual e registo de auditoria.
- Migrações Alembic aplicadas automaticamente no arranque.
- Deploy: GitHub Actions → GHCR → Container Station (ver [qnap/README.md](qnap/README.md)).
- **Importação de extratos**: Santander (.xls de «Saldos e movimentos»), ActivoBank e Crédito Agrícola
  (.xlsx). Há pré-visualização antes de gravar e deteção de movimentos repetidos, por isso podes
  reimportar períodos sobrepostos. Os saldos do banco são verificados linha a linha. Na 1.ª
  importação o saldo inicial é calculado a partir do extrato. Cada importação pode ser anulada.
  Os ficheiros originais ficam guardados em `data/uploads/`.
- Lista de movimentos por conta, com pesquisa e filtro por datas. O saldo atual é o indicado pelo
  banco, com um alerta quando difere do saldo calculado.
- **Qualificação de movimentos** (v0.4.0): categorias em 2 níveis (receita/despesa, com cor),
  etiquetas, «transferência entre contas próprias» e «excluir dos relatórios». A categoria muda-se
  diretamente na linha; há edição em massa e uma página de detalhe com notas. Regras automáticas
  correm em cada importação. As condições são: descrição contém / começa por / expressão regular,
  sentido, conta e intervalo de montante. As ações são: categoria, etiqueta, transferência,
  excluir. Há teste antes de gravar e «criar regra a partir deste movimento». As qualificações
  feitas à mão nunca são reescritas pelas regras. Vêm cerca de 50 categorias e 23 regras
  iniciais para descrições de bancos portugueses.
- **Cartão de crédito** (v0.6.0): importação do extrato do cartão Santander («Consulta Movimentos
  Extracto», .xls). Compras ficam negativas e pagamentos/devoluções positivos; os movimentos ainda
  em curso ficam de fora até serem extratados. O pagamento do cartão é marcado automaticamente como
  transferência nos dois lados: o crédito «PAG…» no cartão e o débito do mesmo valor (±7 dias) na
  conta que o paga. Assim a despesa conta só uma vez (as compras). A página do cartão mostra a
  dívida, as compras desde o último fecho, o próximo fecho e a data de pagamento. Um extrato de
  cartão importado numa conta à ordem é recusado.
- **v0.31.0 — Subscrições e assinaturas.** Novo menu «Subscrições» com um catálogo de ~40 serviços
  comuns em Portugal (streaming, telecomunicações, energia, ginásio, seguros, Via Verde…). Cada subscrição
  guarda valor, periodicidade, dia de pagamento, conta, texto do extrato, data de início, fim da fidelização
  e data de fim. Os débitos são ligados automaticamente aos movimentos (±7 dias do dia de pagamento), com
  situação «em dia» / «aguarda extrato» / «em atraso» / «terminada», aviso quando o valor muda, falhas
  registadas, total pago, meses de permanência e dias de fidelização que faltam. Custo mensal e anual,
  próximos 30 dias e sugestões detetadas nos movimentos (catálogo ou débitos recorrentes), que se podem
  adicionar ou ignorar.
- **v0.30.0 — Revolut: extrato completo em Excel e PDF.** O Excel da Revolut (CSV numa só coluna,
  texto mal codificado) passa a ler-se, com cada produto na sua subconta: Atual (em cada moeda),
  Conta reforçada, cofres («Poupanças») e Investimentos. A conta Revolut em euros recebe também as
  outras moedas convertidas pelos câmbios do próprio extrato (a descrição guarda o valor original),
  uma conta poupança com «cofre»/«poupanças» no nome recebe os cofres e uma conta de Investimento
  recebe as ações como carteira (sem as compras/vendas; dividendos, impostos e custódia como
  ganho/perda). Também se importa o «Extrato personalizado» consolidado em PDF da Revolut. Cada conta e moeda é uma subconta: a conta Revolut em euros
  recebe a conta EUR e as outras moedas (USD, CHF, JPY, KRW, AED…) convertidas a euros pelo valor do
  próprio extrato, com o valor original na descrição; uma conta numa moeda estrangeira recebe só essa
  moeda; uma conta poupança recebe o cofre com o mesmo nome ou os juros da Conta reforçada (o PDF não
  traz as entradas/saídas dela). Câmbios, carregamentos e envios para poupança/investimento são
  transferências; comissões e impostos são informativos (o saldo já os inclui). Não duplica o que
  já veio pelo CSV: mesmo saldo (e valor, ou a linha «Comissão Revolut» separada) até 4 dias de
  diferença. Nova dependência: pdfplumber.
- **v0.29.1 — novo formato do cartão de crédito Santander:** o importador do cartão passa a ler também
  a exportação «Consulta Movimentos Cartão Crédito» (Data Operação, Descrição, Montante (EUR) com sinal,
  histórico completo, n.º do cartão no cabeçalho). Carregamentos do Revolut pagos com o cartão ficam
  como transferência. Nos cartões, um movimento com a mesma data e valor já na conta conta como
  repetido, para os dois formatos (Extracto e Consulta) não duplicarem a mesma compra.
- **v0.29.0 — dividir a conta:** ao ligar 2 movimentos selecionados (uma saída e uma entrada), a opção
  «÷ Dividir a conta» põe a entrada (a parte que outros pagaram, ex.: MB WAY de um amigo) na categoria
  da saída — ou «Restaurantes e cafés» se a saída ainda não tiver — para abater à despesa: nos
  relatórios conta só a tua parte. O painel mostra a saída, a entrada e a tua parte antes de ligar.
- **v0.28.2 — movimentos já etiquetados entram na viagem e no modelo:** todos os movimentos com a
  etiqueta da viagem (mesmo marcados à mão em Movimentos, ou antes de a viagem existir) fazem parte
  dela; «↻ Recalcular», guardar o modelo e criar/editar a viagem ensinam-nos ao modelo — cada
  movimento uma só vez por modelo (migração 0015) — e retirar um movimento da viagem desfaz essa
  aprendizagem. Ao criar uma viagem pode escolher-se uma etiqueta já existente (ou é reutilizada a
  «Viagem: <nome>» livre); apagar a viagem só apaga etiquetas criadas por ela.
- **v0.28.1 — gestão dos modelos de viagem:** em Viagens, cada modelo mostra quantas viagens o usam e
  pode ser renomeado (sem nomes repetidos) e apagado — só quando nenhuma viagem o usa. Ao guardar um
  modelo, as sugestões das viagens que o usam são recalculadas e listadas («Viagens que usam este
  modelo»). Na ficha da viagem, «↻ Recalcular» troca o modelo e refaz as sugestões. O modelo base só é
  recriado se não existir nenhum modelo.
- **v0.28.0 — viagens:** novo menu **Viagens**. Cada viagem regista-se com nome, destino, país, datas,
  pessoas, motivo, orçamento, moeda local e notas, e marca os seus movimentos com uma etiqueta própria
  («Viagem: …»). A ficha da viagem sugere os movimentos que podem ser gastos dela — reservas antes da
  partida (AIRBNB, RYANAIR, HOTEL…), gastos durante as datas nas contas de viagem (Revolut, cartão) —
  com pontos e razões; associa-se ou rejeita-se em lote. Mostra o gasto total, por dia, por pessoa,
  antes da partida, por categoria e por conta, face ao orçamento. Os **modelos de viagem** guardam os
  critérios (dias antes/depois, tipos de conta e contas, textos a incluir/excluir, categorias típicas,
  valor mínimo), copiam-se e aprendem: cada confirmação/rejeição conta para o padrão da descrição e
  pesa nas viagens seguintes (migração 0014).
- **v0.27.0 — calendário nos movimentos:** no topo dos filtros (lista geral e de cada conta), o
  calendário do mês mostra em cada dia o número de movimentos e o saldo do dia, com os outros
  filtros aplicados; clicar num dia filtra esse dia (clicar outra vez limpa), as setas mudam de
  mês e «mês inteiro» filtra o mês.
- **v0.26.0 — contas de fundos contabilizadas como carteira:** um extrato importado para uma conta de
  Investimento (ex.: CSV da conta de liquidez da carteira BiG «FA Agressiva») já não é tratado como
  conta à ordem: as subscrições e resgates de fundos são trocas dentro da carteira e não se importam
  (a pré-visualização mostra quantos e os totais); o saldo do ficheiro (só a liquidez) é ignorado;
  as entradas vindas da conta de liquidez são aplicações (transferências) e juros, rendimentos e
  impostos ficam em «Rendimentos de investimento». O valor real da carteira entra por «Retificar
  saldo». Novo **Limpar a conta** (fundo da página da conta, admin, escrever o nome da conta;
  faz backup antes) para apagar movimentos, importações e retificações e reimportar.
  Quando não se sabe o capital investido antes do extrato, a 1.ª retificação pode ser marcada como
  **valor de partida** (migração 0013): a diferença conta como capital, não como ganho, não fica «por
  explicar», e o ganho/perda de mercado passa a medir-se a partir dessa data.
- **v0.25.0 — movimentos pareáveis:** novo ecrã **Movimentos › Pareáveis ↔** com as sugestões de
  ligação: débito numa conta e crédito noutra da mesma entidade, mesmo valor, no mesmo dia ou até
  1/3/5/7/10 dias, ainda sem par (filtros por conta e valor mínimo; respeita o início da análise).
  Cada sugestão mostra a confiança (transferência já marcada, pista na descrição, mesmo dia; desce
  se um lado já tem categoria) e se é ambígua. «Ligar» (como transferência, numa categoria comum
  ou excluídos) e «Ignorar» por linha ou em lote; as ignoradas ficam guardadas (migração 0012).
  A ligação automática ao importar e as ligações existentes não mudam.
- **v0.24.0 — movimentos visíveis só dentro da cobertura dos dados:** com início da análise definido
  (Contas › Cobertura dos dados), as listas de movimentos (geral e de cada conta) mostram só os
  movimentos desde essa data, com um aviso no ecrã a dizer quantos ficaram ocultos e as ligações
  «Mostrar também» (aparecem esbatidos) e «alterar a data». As ações em massa sobre todo o filtro,
  a contagem «sem categoria» do painel e o ecrã «Por classificar» seguem o mesmo corte; os saldos
  continuam a contar tudo.
- **v0.23.0 — ligar 2 movimentos selecionados:** em Movimentos, com exatamente uma entrada e uma
  saída selecionadas, a barra de ações mostra «Ligar estes 2 como»: transferência entre contas
  minhas, uma categoria para os dois (ex.: compra e reembolso, que se anulam nessa categoria) ou
  excluir ambos dos relatórios. Ficam ligados (cor e letra comuns) e qualificados à mão.
- **v0.22.0 — classificar por descrição, com sugestões por IA:** em **Categorias e regras › Por
  classificar**, os movimentos sem categoria aparecem agrupados pelo texto comum da descrição (sem
  números nem prefixos do banco), maiores primeiro, com a categoria sugerida pelo histórico. Um clique
  classifica todos os movimentos com esse texto e, opcionalmente, cria a regra para os próximos.
  «Pedir sugestões à IA» pergunta à Claude API (chave `ANTHROPIC_API_KEY`, na NAS em `ia.env`) que
  comerciante/serviço está por trás de cada padrão — só vai o texto do padrão e se é entrada ou saída,
  nunca valores, datas, contas nem transferências/MB WAY; as respostas ficam em cache (migração 0011).
- **v0.21.2 — datas em dd/mm/aaaa:** todas as datas mostradas (tabelas, períodos, mensagens,
  gráfico do património, histórico, administração) passam a dd/mm/aaaa. A leitura continua a
  aceitar aaaa-mm-dd, dd-mm-aaaa e dd/mm/aaaa.
- **v0.21.1 — regresso à v0.20.0:** a v0.21.0 (arquivo organizado dos extratos e pasta partilhada)
  foi retirada; a app volta a funcionar como na v0.20.0. Fica só a migração 0010 (coluna
  `importacao.arquivo`, sem uso), para que uma base que já a tenha aplicado continue a arrancar.
- **v0.20.0 — cobertura dos dados e início da análise:** em **Contas › Cobertura dos dados**,
  um cronograma mostra de quando a quando há movimentos em cada conta. A app sugere o início da
  análise: o 1.º dia do mês seguinte ao primeiro movimento da conta do dia a dia que começa mais
  tarde (poupança, investimento, aforro e empréstimo não contam). Definida a data (por entidade,
  migração 0009), os movimentos anteriores ficam na base e nos saldos mas deixam de contar nos
  relatórios, fluxo de caixa, tendências, gráfico do património e «O que mudou».
- **v0.19.0 — tarefas de importação, mês nos relatórios, grupos de contas:** novo ecrã
  **Importar › Tarefas de importação do mês**: por banco, ligação ao serviço online, onde exportar
  cada extrato (da ficha do importador) e se cada conta já foi atualizada no ciclo; contas sem
  importador (investimento, aforro, Caixa Break) com a indicação de atualização manual. O lembrete
  («Tarefas do mês» no menu e aviso no painel) só aparece entre os dias configurados em
  Administração › Preferências (por omissão 25 a 5). Relatórios: com o período «Mês» escolhe-se
  qual mês. Painel e Contas: o título de cada grupo é maior e as contas ficam indentadas; Contas
  passa a agrupar por tipo, banco ou sem grupos.
- **v0.18.1 — receitas previstas nos relatórios:** no mês de referência, os Relatórios somam
  também as receitas previstas por receber (linhas «… (previsto)» em Receitas por categoria e
  nos KPIs), respeitando o filtro de conta. A escolha «ver só o real / somar as previstas» é a
  mesma do painel.
- **v0.18.0 — importação do cartão refeição Santander:** NetBanco › Cartões › Consultar
  Movimentos de Cartão › Exportar (.xls: Data Operação, Data Valor, Descrição, Montante; sem
  saldo, montantes em texto com ponto decimal). Importa-se numa conta do tipo «Cartão refeição»;
  o carregamento «SUB REFEICAO» fica com a categoria «Subsídio de alimentação» (regra nova,
  também acrescentada às bases existentes). O n.º do cartão (últimos 4) aparece na pré-visualização.
- **v0.17.0 — receitas previstas e cartões refeição:** em **Receitas previstas** (menu Configuração)
  define-se cada entrada mensal: conta, dia (ex.: ordenado Santander a 23, ActivoBank a 25),
  valor fixo ou média dos últimos 3 recebidos, e como reconhecer o crédito real (texto da
  descrição, categoria, ou valor ±15%). No painel, o fluxo de caixa de «Este mês» soma as que
  ainda não entraram como «(previsto)» no sankey e nos KPIs, com a lista recebida/prevista/em
  atraso e o atalho «ver só o real». Novo tipo de conta **Cartão refeição** (com retificação de
  saldo), instituição Caixa Geral de Depósitos e categoria «Subsídio de alimentação». Não há API
  pública para os cartões refeição Santander nem Caixa Break: o PSD2 exclui estes cartões de rede
  limitada, por isso os movimentos entram à mão ou por retificação do saldo.
- **v0.16.0 — desempenho dos investimentos:** nas contas de Investimento e de Certificados de
  Aforro, cada movimento é interpretado como **aplicação** ou **resgate** (transferências, mexem
  no capital investido) ou como **ganho** ou **perda** (todos os outros, incluindo o acerto de uma
  retificação). O formulário «+ Movimento» destas contas pede a natureza. Os ganhos e perdas
  ficam na categoria «Rendimentos de investimento» (receita); uma perda reduz a receita e não
  conta como despesa. A página da conta tem o cartão **Desempenho**: valor atual, capital
  investido, ganho/perda acumulado e rentabilidade, ganho/perda do ano, gráfico do valor face ao
  capital e tabela mensal.
- **v0.15.0 — retificação de saldo e ordenação de contas:** nas contas de Certificados de Aforro
  e de Investimento, «Retificar saldo» acerta a conta ao valor real numa data. A diferença vira
  um acerto fora dos relatórios. O acerto é recalculado sempre que os movimentos da conta mudam,
  por isso os movimentos em falta podem ser inseridos depois e a diferença encolhe até
  desaparecer. Enquanto houver diferença por explicar, a conta mostra um aviso na sua página, na
  lista de contas e no painel. A lista de contas ordena-se clicando no título das colunas (Conta,
  Tipo, Instituição, Saldo), e a escolha fica memorizada.
- **v0.14.0 — pares visíveis:** na lista de movimentos, os dois lados de uma ligação têm a mesma
  cor (barra lateral) e a mesma letra (etiqueta «↔ A»). Passar o rato sobre a etiqueta realça os
  dois; clicar salta para o outro lado, ou abre-o se estiver noutra conta (a etiqueta indica qual).
  Corrigido um corte à direita dos cartões de movimentos no telemóvel.
- **v0.13.0 — Banco BiG e Importadores:** importação do CSV de movimentos da conta à ordem do
  BiG. A descrição duplicada fica só com a versão completa. «EUR» é o valor com sinal e
  «Contabilistico*» o saldo. As aplicações em fundos e o «Levantamento SuperConta» contam como
  transferência. *Administração › Importadores* mostra uma tabela com os atributos de cada
  importador: extrato, conta de destino, formato, onde exportar, colunas, ordem, saldo,
  particularidades e utilização. A tabela é gerada a partir da ficha (`FICHA`) de cada parser.
- **v0.12.0 — «O que mudou»:** clicar (ou tocar) num ponto do gráfico do património, no Painel
  ou nos Relatórios, mostra o que mudou desde o ponto anterior: a variação, as receitas, as
  despesas e as transferências internas; a variação por conta (saldo inicial e final, quanto os
  movimentos explicam e a parte **não explicada**, que é sinal de um extrato em falta); e os
  movimentos do período, do maior para o menor, com ligação para a lista completa. Os scripts
  passaram a levar a versão no endereço, para o browser não usar versões antigas em cache.
- **v0.11.0:** os ativos e passivos do painel podem ser agrupados por tipo, por banco ou sem
  grupos, e ordenados por nome, saldo ou banco; a escolha fica memorizada. Nos movimentos há
  pesquisa por valor (escrever «40,05» encontra entradas e saídas desse montante), intervalo de
  valor e ordenação clicando no título de cada coluna. Administração › Preferências tem a opção
  «Mostrar NIB/IBAN completo»; o NIB passa a ser guardado tal como é escrito. Movimentos manuais:
  «+ Movimento» em qualquer conta, a pensar nos depósitos e fundos que não se importam. Podem ser
  editados e apagados, e mostram os candidatos de ligação (valor simétrico noutra conta, até 10
  dias de diferença).
- **Novo visual e telemóvel** (v0.10.0), inspirado no [votoavoto.pt](https://votoavoto.pt):
  tipografia Bricolage Grotesque e Geist (incluídas na app, sem CDN), fundo cinza-claro,
  cartões brancos de cantos largos e amarelo #FFD23F nas ações, com modo escuro. No computador
  há uma barra lateral e o conteúdo ocupa a largura disponível (até 1600px). No telemóvel
  (referência: Galaxy S24, 360×780) há barra de topo, menu deslizante e abas em baixo; os
  movimentos aparecem como cartões, os filtros são recolhíveis e as ações em massa só aparecem
  quando são precisas. Nenhuma página excede a largura do ecrã. Com o manifesto e os ícones, a
  app pode ser instalada no ecrã principal do telemóvel. Para abrir como aplicação, sem a barra
  do browser, o acesso tem de ser por HTTPS.
- **Revolut** (v0.9.0): extrato CSV, em português ou inglês. Cada subconta (produto e moeda) é
  importada na conta certa: «Atual» na conta Revolut, «Depósito»/Conta reforçada numa conta
  poupança Revolut. As comissões passam a movimentos à parte, em «Comissões bancárias», e os
  movimentos revertidos ficam de fora. Carregamentos, câmbios, transferências entre subcontas e
  transferências para investimento contam como transferência. **Ligação automática de
  transferências entre contas:** valor simétrico, até 5 dias de diferença, e o outro lado com
  «REVOLUT» na descrição ou já marcado como transferência. A leitura de CSV passou a aceitar aspas
  e vírgulas dentro das descrições.
- **Empréstimo à habitação** (v0.8.0): novo tipo de conta, que conta como passivo. Guarda a TAN
  (5 casas decimais), o tipo de taxa, o indexante, o spread, o total de prestações e a conta de
  débito. O extrato do empréstimo Santander («Consulta Movimentos Empréstimo», .xls) importa cada
  prestação com os valores exatos do banco: a prestação é transferência e fica ligada ao débito
  «Cred. Habitacao…» na conta à ordem; os juros são despesa («Casa › Juros do empréstimo»); o saldo
  é o saldo em dívida do banco. Se o débito chegar antes do extrato, a prestação é estimada
  (capital × TAN × dias/360) e depois substituída pelos valores reais. A página do empréstimo mostra
  o capital em dívida, a próxima prestação, o fim previsto, os juros e o capital do ano, as
  prestações registadas e um quadro previsional de 12 meses, com «Atualizar taxa» e «Acertar capital».
- **Pagamentos de cartão ligados** (v0.7.0): os dois lados de cada pagamento ficam ligados.
  A página de detalhe tem uma secção «Ligado a», com link para o outro lado e o botão
  «Desligar». O relatório *Relatórios › Pagamentos de cartão* mostra os pares encontrados e os
  pagamentos sem par, com os débitos candidatos (mesmo valor, ±30 dias) e um botão «Ligar».
  O botão «Procurar pares agora» liga também os pagamentos marcados pela v0.6.0.
- **Repor qualificações** (v0.5.0): os filtros «Qualificados à mão» e «Qualificados por regra»
  e a indicação «à mão»/«auto» em cada linha. As ações «Repor e reaplicar as regras» e «Repor e
  deixar sem categoria» desfazem categoria, transferência e excluído, mantendo etiquetas e notas.
  Todas as ações em massa podem aplicar-se aos movimentos selecionados ou a todos os do filtro
  (todas as páginas), com confirmação. Acrescentar ou retirar etiquetas já não marca o movimento
  como «à mão».
- **Painel** (inspirado no [Sure](https://sure.am)): património líquido com variação a 30 dias e
  evolução em 12 meses, ativos/passivos por tipo de conta, fluxo de caixa (sankey receitas →
  despesas/poupança) e despesas por categoria.
- **Relatórios**: mês, trimestre, 6 e 12 meses, ano até hoje ou período personalizado, com
  filtros por conta e etiqueta. Inclui comparação com o período anterior, taxa de poupança,
  receitas e despesas por categoria (com subcategorias), tendência mensal, estatísticas por
  categoria (média, mediana, mínimo e máximo) e exportação CSV (categorias × meses).

Próximo passo: orçamentos por categoria, deteção automática de transferências entre contas e
Revolut (precisa de um CSV de exemplo).
Para juntar um banco novo: criar `app/parsers/<banco>.py` com `Parser` (chave, banco, `FICHA`, `detetar`, `ler`) e acrescentá-lo a `todos()` em [app/parsers/__init__.py](app/parsers/__init__.py).

## Desenvolvimento local

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m uvicorn --factory app.main:create_app --port 8200 --reload
```

Os dados locais ficam em `./data/` (ignorado pelo git). Para guardá-los noutra pasta, define `GESTOR_DATA_DIR`.

Para gerar uma migração depois de alterar `app/models.py`:
```bash
python -m alembic revision --autogenerate -m "descricao"
```

## Variáveis de ambiente

| Variável | Omissão | |
|---|---|---|
| `GESTOR_DATA_DIR` | `./data` (container: `/data`) | `gestor.db` + `secret.key` |
| `GESTOR_BACKUP_DIR` | `$GESTOR_DATA_DIR/backup` (container: `/backup`) | |
| `GESTOR_BACKUP_INTERVALO_HORAS` | `24` | `0` desativa os backups automáticos |
| `GESTOR_BACKUP_RETENCAO_DIAS` | `30` | |
| `GESTOR_COOKIE_SECURE` | `0` | `1` atrás de HTTPS |
| `GESTOR_SECRET_KEY` | gerada em `secret.key` | |
