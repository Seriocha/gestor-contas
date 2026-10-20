# Gestor de Contas

Gestor financeiro doméstico, multiutilizador e self-hosted, para a NAS QNAP. Este projeto
importa movimentos de bancos portugueses e gere contas à ordem, cartões, investimentos e
Certificados de Aforro. O design completo e as fases estão em [DESIGN.md](DESIGN.md).

## Estado: v0.61.0

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
- **v0.71.0 — Comparar fornecedores: ver e atribuir cada débito.** Cada contrato proposto tem «Ver os N débitos» (data,
  dia do mês, descrição, conta, valor) para confirmar antes de o ligar a uma casa. Qualquer débito do serviço — por
  atribuir, de uma proposta, de uma casa ou da lista de todos — pode ser escolhido e atribuído à mão a uma casa
  (Movimento.bem_id com bem_manual: ganha aos contratos e conta também nas despesas da casa no Património); «devolver aos
  contratos» tira a escolha à mão. A lista mostra a casa de cada débito e porquê («contrato» ou «à mão»).
- **v0.70.0 — Comparar fornecedores por casa.** Contratos por casa (tabela contrato_servico, migração 0038): fornecedor,
  referência (o n.º no fim do débito direto) e, quando o mesmo fornecedor cobra as duas casas com a mesma referência (a G9),
  o intervalo de dias do mês. A página propõe os contratos a partir dos débitos (por fornecedor e referência; com 2 débitos
  por mês, divididos pelo dia — o débito grande costuma ser a casa onde se vive) e basta escolher a casa de cada um. Depois,
  cada casa tem a sua análise (mudança de fornecedor com os 12 meses antes/depois, tabela por ano, grelha mês a mês) e os
  gráficos mostram as casas lado a lado: por ano, uma pilha por casa e fornecedor; por mês, uma linha por casa (meses sem
  extratos em branco).
- **v0.69.0 — Relatórios › Comparar fornecedores.** Um serviço da casa (por omissão a Eletricidade; qualquer categoria
  de despesa) ano a ano e por fornecedor (EDP, G9, Galp, Endesa, … reconhecidos na descrição): total, n.º de débitos,
  média por mês com dados, variação face ao ano anterior e gráfico empilhado. Deteta as mudanças de fornecedor (ex.:
  EDP → G9, 1.º débito do novo) e compara os 12 meses antes com os 12 meses depois (média por mês e diferença por ano).
  Grelha mês a mês (o mesmo mês entre anos tira o efeito da estação). Textos extra apanham um fornecedor novo ainda sem
  categoria; a página sugere-o (só do mesmo tipo de serviço) e ««G9» → Eletricidade» cria a regra e categoriza-os.
- **v0.68.0 — Viatura da empresa igual aos outros carros, com plafond de manutenção.** Um carro só conta como
  vendido/devolvido a partir da data indicada: a viatura da empresa com a data de devolução no futuro volta a ter o
  plano de manutenção (IA), o registo de km e as idas à oficina. Todos os carros têm os mesmos separadores
  (Manutenção, Abastecimentos, Via Verde, Despesas e receitas). Novo plafond de manutenção (valor em N anos a contar
  de uma data — por omissão 6 anos desde «Desde»; migração 0037): a secção Manutenção mostra o usado (idas à oficina
  com custo), o que resta, a barra do usado face ao prazo decorrido e a projeção ao ritmo atual.
- **v0.67.0 — As fotografias ajudam a detetar viagens.** Em Viagens › Detetar viagens, quem tem chave do Immich vê em
  cada período «📷 N fotos · Albufeira, Portugal …» (os lugares mais fotografados, carregados depois da página, 3
  períodos de cada vez) e a etiqueta «📷 confirmada pelas fotos» quando há fotos noutro país ou metade das fotos com
  cidade é fora de casa (cidades de casa em GESTOR_CIDADES_CASA; por omissão, Lisboa e arredores). O «+ Criar viagem»
  leva o destino e o país do lugar mais fotografado quando a IA não os deu, e a IA recebe os lugares das fotos (só
  «cidade, país» e quantas fotos — nunca nomes de ficheiros, datas ou coordenadas).
  Nova preferência «As chaves da API de cada pessoa são configuradas pelo administrador» (Administração › Preferências):
  ligada, o token do Paperless e a chave do Immich de cada utilizador configuram-se em Administração › Utilizadores ›
  (utilizador) › Ligações (guardar depois de testar, testar, remover — app/chaves_api.py) e o Perfil só mostra o estado;
  desligada (por omissão), cada pessoa guarda as suas no Perfil, como antes.
- **v0.66.0 — Viagens com menu próprio e fotografias do Immich.** «Viagens» passa a ser um grupo do menu (ao nível de
  Dinheiro e Dados), com «Viagens» e «Detetar viagens». Ligação ao Immich da NAS, como a do Paperless: `immich.env`
  (GESTOR_IMMICH_URL/LINK, opcional GESTOR_IMMICH_TOKEN partilhado para administradores) e uma chave da API por pessoa
  em Perfil › Immich (cifrada; migração 0036). A ficha da viagem ganha «📷 Fotografias»: as fotos e vídeos tirados nas
  datas da viagem (pela hora local da foto), os lugares mais fotografados (EXIF) com «usar como destino», miniaturas
  servidas pelo Gestor e «Criar o álbum no Immich» (ou juntar ao álbum as fotos que faltam). Nada é copiado.
- **v0.65.0 — Viagens › Detetar viagens.** A app lê os gastos por ordem cronológica e propõe períodos em que a família
  esteve fora de casa: compras presenciais no estrangeiro ou noutra moeda, câmbios, seguro de viagem diário, códigos
  postais fora das zonas habituais (calculadas pelo histórico), hotéis/transportes e comerciantes pouco habituais —
  as compras online, débitos fixos e levantamentos em euros não contam. Cada período mostra a confiança, moedas, zonas,
  comerciantes, os movimentos e porquê, e as reservas pagas antes da partida. «+ Criar viagem» abre o formulário já
  preenchido; «Não é viagem» descarta-o (tabela viagem_descartada, migração 0035); os já registados ficam escondidos.
  Opcional, com a Claude API: «Analisar com IA» diz se é viagem, o destino, o país e um nome — só recebe as descrições
  dos comerciantes por dia relativo (sem valores, datas ou contas); a resposta fica em cache.
- **v0.64.0 — Relatórios: vários anos e filtro por categoria.** Novo período «Vários anos»: escolhem-se os anos
  (seguidos ou não, ex.: 2017–2019 e 2021; por omissão os 3 mais recentes) e os totais, as categorias e as tendências
  contam só esses anos. A secção «Comparação por ano» põe os anos lado a lado — receitas, despesas, poupança, taxa de
  poupança e cada categoria, com a média por ano e a variação face ao ano anterior (quando os dois estão escolhidos).
  O filtro de etiqueta deu lugar ao de categoria (a categoria de cima inclui as subcategorias); o CSV segue os filtros.
- **v0.63.0 — Contas › Meses importados.** Para cada conta, uma grelha com os anos em linhas e os meses em colunas:
  verde = mês com extrato (n.º de movimentos; um extrato sem movimentos também cobre o mês), vermelho = em falta,
  tracejado = mês atual ainda sem extrato; do 1.º mês com dados até hoje. Resumo por conta (meses importados, em falta e
  quais — «jan–dez 2020»). Filtro por tipo de conta (contas à ordem primeiro, por omissão) e por conta; cada mês abre
  os movimentos desse mês. Coluna do ano fixa ao deslizar no telemóvel.
- **v0.62.1 — Conta certa para os extratos em PDF do Santander.** O extrato consolidado só é proposto para contas à
  ordem; sem conta detetada, a pré-visualização abre numa conta do mesmo banco (não na 1.ª da lista); a conta sem IBAN
  aprende-o do extrato ao validar, e os extratos seguintes já a encontram sozinhos. Extratos vindos do Paperless dizem
  isso na pré-visualização (não «passa para importados/»).
- **v0.62.0 — Histórico do Santander em PDF e extrato original de cada movimento.** Novo importador do «Extrato
  Consolidado» mensal do Santander (`EXTCON….pdf`): lê a tabela da conta à ordem do texto do PDF (sem OCR; poupanças,
  depósitos e empréstimo ficam de fora), confere saldo inicial + movimentos = saldo final e reconhece pelo valor e saldo
  o que já entrou pelo .xls (descrições diferentes). Entra pela pasta partilhada (`Santander/`) ou pelo Paperless:
  Importar › Por validar › «Ler do Paperless» lê os documentos com a etiqueta ou o tipo «Extrato bancário» (com
  `PAPERLESS_CONSUMER_SUBDIRS_AS_TAGS`, basta pô-los em `consume/Extrato bancário/`); com `GESTOR_PAPERLESS_TOKEN`, é
  lido sozinho com o intervalo da pasta. «Validar todos com conta» valida de uma vez, do mais antigo para o mais
  recente, os que não têm avisos. Cada importação guarda onde está o original (`importacao.paperless_id` / `origem`,
  migração 0034; o Paperless é encontrado também pelo MD5 do ficheiro, e «Procurar as outras no Paperless» liga as
  importações antigas): no detalhe do movimento, «Importado de» e «Também no extrato» abrem o documento no Paperless
  ou a cópia guardada (`/importar/{id}/original`).
- **v0.61.0 — Paperless multiutilizador: um token por pessoa.** Cada utilizador guarda o seu token da API do Paperless
  em Perfil › Paperless (testado antes de guardar, cifrado na base de dados com a chave secreta da app; «Testar
  ligação» e «Remover»). Procurar, ligar e as miniaturas usam o token de quem está a usar — o Paperless (dono, grupos,
  permissões) decide o que cada um vê; um documento ligado sem acesso aparece com 🔒. O token partilhado do
  `paperless.env` fica opcional e só serve administradores sem token pessoal.
- **v0.60.1 — Paperless: pedido à API sem versão fixa.** O Gestor pedia a versão 5 da API e o Paperless-ngx atual
  respondia 406; passa a pedir a versão por omissão (e as miniaturas como imagem).
- **v0.60.0 — Documentos do Paperless-ngx.** Instalação do Paperless-ngx na NAS ao lado do Gestor (`qnap/paperless/`:
  porta 8300, OCR em português, Redis + SQLite, rede Docker `nas-apps`) e cópia dos documentos que já existem numa pasta
  partilhada (as subpastas viram etiquetas; os originais ficam intactos). Com `paperless.env` (URL e token da API), o
  detalhe do movimento e a ficha de cada item do recheio ganham «📎 Documentos»: «Procurar no Paperless» sugere os
  documentos com o valor do movimento ou o comerciante, de 30 dias antes a 10 depois (ou pela loja/marca e data da
  compra, no recheio), e pesquisa por texto; «Ligar» guarda a ligação (com miniatura e ligação para abrir no Paperless).
  O documento aparece também no mapa de ligações do movimento.
- **v0.59.0 — Otimizar regras.** Em Categorias e regras › «Otimizar regras» (ou «🔍 Otimizar regras» na lista), as regras
  ativas são simuladas sobre os movimentos (por prioridade, a 1.ª que corresponde ganha) e aparecem: **agregáveis**
  (mesmas condições e mesmo resultado, padrões diferentes → «Agregar em 1 regra» com «A|B|C», só proposto quando a
  simulação mostra que nenhum movimento muda), **redundantes** (duplicadas e regras que nunca ganham porque outra anterior
  apanha sempre os mesmos movimentos — marcadas para apagar quando fazem o mesmo, assinaladas como conflito quando fazem
  outra coisa) e **sem uso** (não correspondem a nenhum movimento). Nada muda sem confirmar.
- **v0.58.1 — Detalhe do movimento: anterior / seguinte.** Setas ← e → (e as teclas ← →) passam ao movimento anterior
  ou seguinte da lista de onde se veio, com os mesmos filtros (texto, datas, valores, tipo, categoria, etiqueta, bem,
  cartão, conta) e a mesma ordenação; mostra «N de total».
- **v0.58.0 — Análise do movimento.** No detalhe de um movimento, «🔎 Análise do movimento»: «Analisar com IA» pergunta à
  Claude API (com pesquisa na web, quando a conta o permite) quem é o comerciante, a atividade, o local e o site, com uma
  explicação, as fontes e a categoria sugerida — «Aplicar a este movimento» ou criar uma regra. Só se envia a descrição, o
  valor, a data e o tipo de conta; a resposta fica guardada por comerciante (a mesma cache das sugestões da
  classificação) e aparece como mais uma ligação no mapa. «Pesquisar na web» abre uma pesquisa pelo comerciante, sem IA.
- **v0.58.0 — Menus reorganizados.** O menu lateral passa a ter cinco grupos — **Visão geral** (Início, Relatórios),
  **Dinheiro** (Movimentos, Contas, Subscrições, Previsões), **Património** (Património, Viagens), **Dados** (Importar) e
  **Configuração** (Categorias e regras, Administração) — e cada secção tem separadores debaixo do título: Início
  (Resumo · Contas e fluxo, o antigo Painel), Movimentos (Lista · Pareáveis), Contas (Saldos · Cartões de débito ·
  Empréstimos), Património (Bens · Recheio · Abastecimentos · Via Verde), Importar (Carregar ficheiro · Por validar ·
  Tarefas do mês · Histórico) e Administração (Utilizadores · Entidades · Preferências · Cobertura · Backups e atividade ·
  Consolidação · Importadores · Fuelio). «Importar» mostra um contador do que espera validação (extratos da pasta da
  NAS e abastecimentos do telemóvel), também na barra do telemóvel. As categorias de recheio passam para Património ›
  Recheio. Os endereços antigos (`/admin/tarefas`, `/admin/recheio`) encaminham para o sítio novo.
- **v0.57.0 — Cartões de débito: gastos por cartão/pessoa.** O ficheiro de movimentos de cada cartão de débito
  (Santander: «Consulta Movimentos Cartões Débito», .xls) importa-se como os outros — à mão ou pela pasta partilhada —
  mas não cria movimentos: as compras já estão no extrato da conta à ordem, onde não se sabe de que cartão são. Cada
  compra liga-se ao débito igual na conta (mesmo valor, lançado até 5 dias depois; entre iguais, o mais próximo e de
  descrição mais parecida). As que ainda não têm débito ligam-se sozinhas quando chegar o extrato da conta. Em
  «💳 Cartões de débito» (na conta) dá-se o nome de quem usa cada cartão e vêem-se os gastos por cartão e por mês; na
  lista de movimentos cada compra mostra «💳 nome» e pode filtrar-se por cartão.
  **Análise do movimento pela IA:** no detalhe do movimento (clique no valor, ou «🔎 Detalhe e análise» na edição),
  «Analisar com IA» mostra os passos enquanto a IA identifica o comerciante pesquisando na web (nome, atividade,
  localização, site, fontes), explica o movimento e sugere a categoria, que se aplica com um clique. Só é enviado a
  descrição, o valor, a data e o tipo de conta; a análise fica guardada e vale para os movimentos do mesmo comerciante.
- **v0.56.0 — Colunas por grupo no Início.** No modo «Personalizar», cada grupo do «Detalhe dos ativos» (ativos
  bancários, investimentos, património imóvel, carros, passivos) tem «Auto · 1 · 2 · 3 · 4»: quantos cartões ficam lado
  a lado. Com 1 coluna os cartões ficam em lista (ícone, nome e valor numa linha). Guardado por utilizador com o resto
  da disposição; «Repor original» volta ao automático.
- **v0.55.1 — Pasta de extratos: pastas de sistema ignoradas.** As pastas do QTS dentro da partilha (`@Recycle`,
  `@Recently-Snapshot`, `@eaDir`, `#recycle`, `.streams`…) deixam de ser tratadas como bancos (os ficheiros apagados
  na reciclagem não são propostos). Novo `qnap/criar_pastas_extratos.sh`: cria as subpastas dos bancos na partilha.
- **v0.55.0 — Extratos numa pasta partilhada da NAS, lidos automaticamente.** Uma subpasta por banco
  (`/share/Extratos/Santander/`, `ActivoBank/`…), montada no container em `/importar` (`GESTOR_IMPORT_DIR`; o
  `qnap/update.sh` monta-a se existir). A app lê a pasta de hora a hora (15 min a 1 dia, em Importar › Por validar,
  com «Ler a pasta agora») e cada ficheiro novo (pelo conteúdo) fica **por validar** — nada entra nos movimentos sozinho.
  A conta é detetada pelo banco (formato e nome da subpasta), tipo e moeda, n.º de conta no ficheiro e, havendo várias,
  pelos movimentos que já existem. «Ver e validar» mostra a pré-visualização (a conta pode ser mudada); validado, é
  importado e o ficheiro passa para `<banco>/importados/AAAA-MM-DD_<ficheiro>`. Rejeitado, fica na pasta e não volta a
  ser proposto. Os por validar aparecem na agenda do Início.
- **v0.54.0 — Manutenção mais arrumada; editar idas à oficina e associar o movimento.** No separador Manutenção do carro,
  as operações do plano ficam num bloco fechado (com o número em atraso / em breve à vista) e o registo só abre com
  «+ Nova ida à oficina». Ao registar — ou depois, em «Editar» na lista das idas à oficina — pesquisa-se o débito do
  extrato por texto ou valor (sem pesquisa, os débitos perto da data, os deste carro primeiro, sem combustível) e
  associa-se; o custo é preenchido pelo valor. Editar os km substitui a leitura de km dessa ida.
  Casas e propriedades: «Despesas e receitas» passa a ser só um resumo gráfico (como nos carros) — total e média por mês,
  recebido e saldo, gráfico por mês empilhado pelas maiores categorias, barras por categoria — com a ligação para os
  movimentos do bem em vez das tabelas.
- **v0.53.3 — Idas à oficina reconhecidas no plano.** Uma operação escrita em «Outro» conta no item do plano
  correspondente (sem acentos nem maiúsculas; «Inspeção» ou «IPO» ↔ «Inspeção periódica obrigatória»; uma contida na outra).
  A mensagem diz que itens do plano foram atualizados e quais ficaram só nas idas à oficina; os erros (ex.: km
  incoerentes) avisam no topo da página em vez de passarem despercebidos.
- **v0.53.2 — Adicionar ao recheio a partir do movimento.** Na edição de um débito, «📦 Adicionar ao recheio» (escolhendo
  a casa) cria a ficha do item com a data e o valor da compra, mesmo que a categoria não seja de recheio ou o valor esteja
  abaixo do mínimo, e abre-a para completar. Se o movimento já tem item, mostra a ligação para a ficha.
- **v0.53.1 — «Refrescar movimentos» na edição da subscrição.** Mostra, sem guardar, que débitos contam com os dados do
  formulário e os débitos parecidos (mesma palavra no texto) que ficam de fora, com todos os motivos: noutra conta, antes do
  início, depois do fim, antes do início da análise, texto do extrato que não bate, ou associado a outra subscrição.
  Sugere um texto mais largo (ex.: «VODAFONE» em vez de «VODAFONE PORTUGA») e avisa quando há débitos noutras contas;
  os débitos assinalados ficam associados à subscrição ao guardar.
- **v0.53.0 — Detalhe do movimento com mapa de ligações.** Um clique no valor de um movimento (lista de movimentos,
  ficha do bem, subscrição, viagem, regra, variações) abre o detalhe só de leitura: o movimento ao centro e, à volta, ligadas
  por linhas, todas as ligações — conta, entidade, categoria (e como foi escolhida), regra, o outro lado da transferência,
  bem, subscrição (associada à mão ou pelo texto do extrato), viagem, etiquetas, passagens Via Verde, abastecimento, ida à
  oficina, item do recheio e a origem (importação ou à mão). Cada cartão abre a respetiva ficha; «Editar» vai para a edição.
  No telemóvel o mapa fica empilhado.
- **v0.52.1 — Agenda por entidade.** A agenda do Início mostra só os itens da entidade escolhida (a herança deixa de
  aparecer na Família e vice-versa).
- **v0.52.0 — Início personalizável na própria página.** O botão «✎ Personalizar» põe a página em modo de edição:
  cada bloco (património líquido, composição, detalhe dos ativos, evolução, agenda, património por entidade) pode ser
  arrastado pelo ⠿ (ou movido com ↑ ↓ no telemóvel), ter a largura ¼, ⅓, ½, ⅔ ou inteira, e ser ocultado/mostrado.
  «Guardar» grava a disposição só para o utilizador que a fez; «Repor original» volta à de omissão. No telemóvel os blocos
  ocupam sempre a largura toda.
- **v0.51.0 — Registo de abastecimentos no telemóvel.** A página sem login foi refeita para o telefone: letras e campos
  grandes, carros em botões, km com a última leitura (aviso se for menor), €/l calculado à medida, posto com sugestões e
  botão «Registar» sempre visível. Aparecem os carros da empresa com data de devolução futura; o ecrã de validação mostra
  que carros estão na página, os que não estão (e porquê) e as matrículas da Via Verde sem carro registado. Com a chave da
  Claude API, «📷 Fotografar o talão» (e, opcional, o conta-quilómetros) lê a fotografia no servidor e preenche o
  formulário — confirma-se antes de registar; nada é gravado sem essa confirmação.
- **v0.50.0 — Movimento associado a uma subscrição.** Na ficha de um débito escolhe-se a subscrição (ou cria-se uma já
  preenchida a partir dele, ficando associado). O débito associado conta nos pagamentos dessa subscrição mesmo que o texto não
  bata com o padrão, deixa de contar noutra e de ser sugerido; se a subscrição não tinha texto do extrato, fica com o deste
  movimento, para os débitos seguintes serem reconhecidos sozinhos.
- **v0.49.0 — Recheio das casas (seguro e garantias).** Em Administração › Recheio escolhem-se as categorias que são recheio
  (a marca vale para as subcategorias), a garantia por omissão (36 meses, a garantia legal) e o valor mínimo da compra. Cada
  compra classificada nessas categorias — à mão, por regra, ao importar ou na consolidação — passa a item do recheio da casa
  (a do movimento ou a «por omissão»), com data, valor e loja do movimento. Ficha do item: tipo, marca, modelo, n.º de série,
  características, garantia (fim calculado), contactos do fornecedor (ligar / email com o assunto preenchido / site), estado e
  pedidos de garantia (aberto, em reparação, resolvido, recusado). Na casa: valor do recheio vs. capital seguro do recheio (aviso
  de subseguro), garantias a terminar e pedidos abertos; na agenda do Início, fim das garantias e pedidos em curso.
- **v0.48.0 — A minha parte, condicional e em blocos.** Preferência em Administração «Mostrar a minha parte nas heranças»
  (ligada/desligada). As partes aparecem em blocos distintos (a tua parte · 1/6; por morte da mãe · 1/2) e as variações do
  cabeçalho do Início em blocos simples (este mês; desde …). No Início da entidade pessoal (Família) aparece a tua parte em
  cada herança — só o 2.º cenário (1/2) — e o total «Família + herança».
- **v0.47.0 — Início mais gráfico.** Cabeçalho com o património líquido em destaque, variação mensal e mini-gráfico;
  composição dos ativos em quatro grupos (ativos bancários, investimentos, património imóvel, carros) com percentagens;
  detalhe dos ativos em cartões com ícone (contas à ordem, poupança, carteira digital, cada fundo, cada casa ou propriedade,
  cada carro) e cartões dos passivos (crédito à habitação, cartões). O Início passa a estar na barra de baixo do telemóvel.
- **v0.46.0 — Página Início.** Nova página de entrada (depois do login e no logótipo): património líquido, ativos (contas +
  casas, propriedades e carros, pelo valor da última avaliação em cada data) e passivos, com evolução em 12 meses (ou desde
  o início da análise), onde está o património (gráfico) e o total por entidade com a parte de cada herança. Agenda dos
  próximos 60 dias, atrasados primeiro: débitos das subscrições e fim das fidelizações, receitas previstas, pagamento dos
  cartões, prestações dos empréstimos, manutenção e km dos carros, revisão do valor dos bens, tiragem da cortiça,
  importação dos extratos do mês e abastecimentos por validar.
- **v0.45.0 — Abastecimentos corrigíveis; Fuelio nas tarefas.** Os «últimos 12 meses» dos bens (gráficos, custos, resumo)
  começam no início da análise da entidade quando este é mais recente. Na tabela de abastecimentos, a diferença para o
  débito no banco tem coluna própria e cada abastecimento pode ser corrigido (data, hora, km, litros, valor, posto, cheio —
  a leitura de km acompanha), usar o valor do banco num clique, ou apagado. A importação do histórico do Fuelio passa para
  Administração › Fuelio (escolher o carro e o ficheiro).
- **v0.44.0 — Ficha do carro em separadores.** Manutenção, Abastecimentos, Via Verde e Despesas e receitas passam a
  separadores (o endereço, ex.: #km, abre o certo). As despesas do carro mostram-se condensadas — total, gráfico por mês
  empilhado pelas maiores categorias e barras por categoria — com link para os movimentos do carro (novo filtro por bem
  na lista de movimentos, que vale também para o calendário e as ações em massa). O histórico do valor abre e fecha
  (fechado por omissão nos carros).
- **v0.43.0 — Entidades, contas por entidade e cobertura em todo o lado.** Atividade recente filtrável (último dia,
  semana, mês). Na edição da entidade: partilha da herança em dois cenários (hoje 1/6; por morte da mãe 1/2 — frações ou %),
  com a tua parte nos dois ao lado de cada valor; e os módulos que a entidade tem (casas e propriedades, viaturas,
  subscrições, viagens) — o que não tiver deixa de aparecer. As Contas passam a um separador por entidade, como o Património.
  O início da análise (cobertura dos dados) aplica-se também a subscrições, viagens, Via Verde, abastecimentos e consumo,
  oficina e consolidação. Os campos de data mostram e aceitam sempre dd/mm/aaaa (qualquer browser), com botão de calendário.
- **v0.42.0 — Fundos de investimento e de pensões.** Novo tipo de conta «Fundo de pensões / PPR» (como o investimento:
  saldo retificado, aplicações, desempenho), com n.º de contrato e data de início. Nas contas de investimento e pensões,
  posições por fundo — nome, gestora, categoria, ISIN, risco 1–7, unidades de participação, cotação e data, valor de
  aquisição — com valor, valias (€ e %) e atualização rápida da cotação; liquidez (saldo contabilístico). Cada mudança acerta
  o saldo da conta para fundos + liquidez e, sem histórico de aplicações, o capital de partida é o valor de aquisição +
  liquidez (o ganho mostrado são as valias). N.ºs de conta/contrato curtos deixam de ser partidos em grupos de 4.
- **v0.41.0 — Regras ligadas a um bem.** No formulário das regras, a ação «Ligar a casa / propriedade / carro» (bens
  agrupados por entidade). A regra fica condicionada ao universo do bem — só corresponde a movimentos das contas da entidade
  do bem (também no «Testar») — e o bem da regra ganha à atribuição «por omissão» e aos textos do bem.
- **v0.40.0 — Construções das propriedades rústicas.** Na ficha da propriedade, um quadro de construções (designação,
  tipo, artigo matricial, área de terreno e área bruta em m², estado), agrupado pelo artigo: construções no mesmo artigo
  partilham a área de terreno, que conta uma vez no «Total de construção». As construções entram na avaliação por IA.
- **v0.39.0 — Registo de abastecimentos sem login.** Uma página simples para o telemóvel (/abastecer/<código>) regista
  abastecimentos (carro, data, hora, km, litros, valor, depósito cheio, posto) sem entrar na app — acessível só na rede/VPN e
  com o código secreto do link, gerado/mudado/desligado por um administrador em Património › Abastecimentos. Os registos
  ficam por validar: quem tem acesso à entidade confirma ou corrige (aviso se os km recuam) e valida — passam a
  abastecimentos como os do Fuelio (km, ligação ao débito no banco, consumo) — ou rejeita.
- **v0.38.0 — Heranças e propriedades rústicas.** As entidades editam-se em Administração e passam a ser «pessoal» ou
  «herança» com a percentagem detida: o painel e o património mostram a tua parte ao lado dos valores da herança.
  Novo tipo de bem «Propriedade rústica»: área (ha), artigo matricial, localização, ocupação do solo (montado de sobro e de
  azinho, olival, vinha, pinhal, pastagem, regadio…), água, acesso, construções e cortiça (anos de tiragem, sobreiros,
  produção em arrobas; próxima tiragem pelo ciclo de 9 anos). A avaliação por IA usa estes atributos (preço por hectare,
  peso da cortiça e dos anos até à próxima tiragem); as receitas (ex.: venda da cortiça) atribuem-se pelos textos do extrato.
- **v0.37.1** — o formulário de importação do Fuelio aparece sempre a quem pode editar (também em carros vendidos/devolvidos, com aviso).
- **v0.37.0 — Fuelio.** Na ficha do carro importa-se o ficheiro de sincronização do Fuelio (vehicle-N-sync.csv ou
  o ZIP). Cada abastecimento dá uma leitura de km (média km/mês e previsão da manutenção) e liga-se ao débito do banco
  (mesmo valor ou até 0,20 € de diferença, de 1 dia antes a 5 dias depois, primeiro os que parecem combustível): o débito
  passa a Combustível e fica nesse carro, antes do carro «por omissão». Consumo (l/100 km) e custo por km entre depósitos
  cheios. A matrícula do ficheiro tem de ser a do carro (ou passa a sê-lo). Não se guardam VIN, coordenadas, fotos nem viagens.
- **v0.36.0 — Consolidação.** Administração › Consolidação mostra o que está por ligar em cada entidade
  (movimentos sem categoria, pagamentos de cartão, registos Via Verde sem débito, matrículas sem carro) e volta a correr,
  depois de um backup, as regras, os pagamentos de cartão, as transferências, a atribuição às casas e carros, a ligação
  Via Verde e os saldos retificados — para os dados registados depois de cada importação passarem a contar.
- **v0.35.0 — Via Verde.** Em Património › Via Verde carregam-se os extratos mensais (CSV, ou os ZIP tal como
  vêm, vários de uma vez). Cada portagem, parque ou estacionamento fica guardado (sem nome, NIF nem morada) e o grupo
  com a mesma data de pagamento (ou o mesmo dia, sem data) liga-se ao débito «Via Verde» do banco do mesmo valor
  (até 7 dias depois). Com a matrícula de cada carro, os débitos repartem-se pelos carros nos custos de cada um,
  mesmo quando um débito junta os dois; a ficha do carro e a página do débito mostram o detalhe.
- **v0.34.0 — Manutenção dos carros.** Registo periódico dos km (com lembrete quando a última leitura tem mais
  de um mês) e média de km/mês. A IA (Claude API, com pesquisa web quando disponível) dá o plano de manutenção do
  fabricante para a marca, modelo, ano, combustível e versão — operações com intervalo em km e/ou meses e custo
  estimado. Com as idas à oficina registadas (data, km, operações, custo, movimento do extrato) a app calcula o próximo
  marco de cada operação, estima quando lá chegas pelos km/dia e avisa quando é preciso ir à oficina.
- **v0.33.0 — Viatura da empresa.** Um carro pode ter o uso «Viatura da empresa»: não conta no património
  (sem valor, dívida nem avaliação) e serve só para lhe alocar o combustível — pelos textos do extrato (ex.: cartão
  de frota) e, sendo «por omissão», todo o Transportes › Combustível do período em que a usas, antes do carro próprio.
- **v0.32.0 — Património: casas e carros.** Novo menu «Património». Cada casa guarda uso (habitação principal,
  férias, arrendada), tipologia, área, localização (freguesia e concelho), ano de construção, estado, compra, venda e
  o empréstimo associado (dívida e capital próprio); cada carro guarda marca, modelo, ano, km e combustível.
  Os movimentos ficam ligados a um bem: pelos textos do extrato de cada um e, para o bem «por omissão», as
  despesas de Casa/Transportes que sobrarem; também à mão (página do movimento, ações em massa). Comparação de
  custos casa principal vs. férias. Valor com histórico de avaliações: à mão ou por IA (Claude API, com pesquisa
  web quando disponível; envia só os atributos, nunca nomes, contas nem o preço de compra), com revisão periódica
  automática (diária, configurável em Administração). O painel mostra o valor das casas e carros.
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
