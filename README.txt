===================================================
BOT TRADING IQ OPTION 24x7 REESTRUTURADO
===================================================

Documentacao detalhada da arquitetura, fluxo atual e regras de negocio:
- DOCUMENTACAO_PROJETO.md

ESTRUTURA DE PASTAS:
- .env               <- Configuracao local (confirme se o ponto de entrada a utiliza)
- config.py          <- Leitor opcional de configuracoes via dotenv
- trading_worker.py  <- Dispatcher, analise, execucao e monitores de ordens
- main.py            <- Interface e ponto de entrada ativo
- requirements.txt   <- Dependências do projeto
- iqoptionapi/       <- Pasta com a API da IQ Option (coloque a sua pasta iqoptionapi aqui na raiz)

COMO CONFIGURAR:
1. Extraia o ZIP.
2. Copie a pasta 'iqoptionapi' para dentro da pasta raiz extraída (no mesmo nível do main.py).
3. Na primeira instalação pelo Git, copie config.example.py para config.py e
   .env.example para .env. No PowerShell, com o projeto como diretorio atual:
   Copy-Item config.example.py config.py
   Copy-Item .env.example .env
   Edite o .env com suas proprias credenciais. Ambos os arquivos locais sao
   ignorados pelo Git. Nao sobrescreva um config.py ou .env local ja existente.
   O padrao do modelo e IQ_ACCOUNT_TYPE=PRACTICE; o bot inicia automaticamente.
4. REAL exige IQ_ACCOUNT_TYPE=REAL e IQ_ENABLE_REAL_TRADING=YES explicitamente.
   A aplicação não alterna de conta durante uma sessão.
5. Instale as dependências:
   pip install -r requirements.txt
6. Execute o bot:
   python main.py
   No Windows, iniciar_deskubuddy.bat usa venv\Scripts\python.exe quando existir;
   caso contrario, usa o launcher py.exe. O bot inicia o motor automaticamente.

MAQUINA DO TEMPO AUTOMATICA:
- Ao iniciar o bot, cada par selecionado recebe uma simulacao dos ultimos tres dias.
  A coleta usa a conexao existente apenas para consultar candles; nao envia ordens.
  Uma thread separada atualiza os dados periodicamente, sem interromper os workers,
  e avalia um sinal por fechamento M5 para cada cenario: regra M5 e contingencia M15
  hipotetica. O replay nao acompanha cada analise por minuto do bot.
- A interface mostra brevemente ENTRADA SIMULADA e RESULTADO SIMULADO. Os eventos
  completos ficam em logs\deskubuddy_debug.log. Uma entrada e uma estimativa de
  abertura M1, nao e uma ordem enviada. A coleta sequencial e a disponibilidade dos
  dados podem atrasar os avisos; o atraso de registro aparece na mensagem.
- Em PRACTICE e REAL, um filtro adicional pode vetar um sinal, inclusive a contingencia,
  quando pelo menos 20 WIN/LOSS estimados no mesmo ativo, direcao, setup e cenario
  estiverem abaixo do ponto de equilibrio do payout atual. Empates e resultados
  indeterminados nao contam. A simulacao apenas veta: nao autoriza entradas.
  Se a amostra for insuficiente ou a simulacao indisponivel/desatualizada, o filtro
  nao e aplicado e a interface informa; isso nao dispensa as demais regras de risco.
- Os resultados sao mantidos em memoria durante a sessao e reconstruidos a partir
  dos candles ao reiniciar. O payout atual e hipotese, nao payout historico.
  Nao simula Ollama, fila, risco, preco efetivo, venda antecipada ou a frequencia
  real das entradas; nao e previsao de lucro. Dados ausentes podem impedir calculos.
  A consulta historica pode competir pelo envio de requisicoes com o motor.

MAQUINA DO TEMPO (SIMULACAO OFFLINE OPCIONAL):
- Com o bot fechado, colete candles e payout atual para um unico ativo do catalogo:
  py maquina_do_tempo.py coletar --ativo EURUSD-OP --dias 3 --saida historico.json
  Este comando conecta a corretora com as credenciais locais, mas nao envia ordens.
  Pode ser necessario usar o Python do venv: .\venv\Scripts\python.exe no lugar de py.
  Ativos OTC devem usar o nome exato, por exemplo EURUSD-OTC. Os dados dependem da
  disponibilidade da corretora; a API pode demorar ou ficar aguardando resposta.
- Execute depois, sem conexao e sem iniciar o bot:
  py maquina_do_tempo.py simular --dados historico.json --saida relatorio.json
  Os arquivos de saida nao sao sobrescritos. A simulacao avalia cada fechamento M5
  usando apenas candles ja fechados e compara a regra M5/contexto com uma hipotetica
  contingencia M15/contexto em cada avaliacao. Nao representa o ciclo real de startup.
- O payout do catalogo Binary no momento da coleta e hipotese aplicada ao passado,
  nao payout historico comprovado ou necessariamente igual ao payout Turbo. A entrada
  e a saida sao estimadas por candles M1; atrasos, recusas, venda antecipada, custos,
  Ollama, limites de risco e selecao de pares nao sao reproduzidos. Lacunas de dados
  podem impedir a avaliacao ou tornar o resultado INDETERMINADO. Nao use resultados
  simulados como autorizacao para REAL; valide em PRACTICE.
- Apos retomada manual da pausa, a contingencia de primeira ordem pode ser analisada
  no estado de observacao; permanece sujeita ao contexto, janela e autorizacao de risco.
  Essa contingencia nao aplica retracao M5 nem Ollama e pode operar tambem em REAL.
  Ela nao e liberada em observacao decorrente de cooldown ou reconciliacao.

DECISAO E APRENDIZADO:
- Ao iniciar, cada ativo continua sendo avaliado pelo analyzer atual; candidatos e dados
  técnicos usados na análise são salvos em deskubuddy.db.
- Com menos de 5 resultados decisivos comparáveis (mesmo ativo, direção e setup), a
  decisão segue o analyzer para permitir coleta inicial de dados.
- A partir de 5 resultados, o sistema exige expectativa histórica positiva para o payout
  atual e solicita ao Ollama uma decisão estruturada OPERAR/PULAR. Se o Ollama falhar ou
  responder fora do formato esperado, o sinal é pulado.
- Sinais pulados são avaliados em modo virtual comparando a abertura do candle M1 de entrada
  com o fechamento do candle M1 anterior à expiração Binary. Isso é uma estimativa, não
  replica o preço de execução da corretora; resultados virtuais são marcados como
  PAPER_WIN/PAPER_LOSS/PAPER_DRAW/PAPER_INDETERMINADO e separados dos reais. Resultados
  virtuais não autorizam operações REAL.
- O Ollama não exibe pareceres na interface; sua resposta é usada apenas pelo filtro de
  decisão. Confira os logs e o banco para auditar decisões e resultados.

SELECAO DINAMICA DE PARES:
- A cada hora, o sistema consulta o catálogo Binary e sorteia sem reposição até dez pares
  elegíveis da categoria permitida para o horário. Cada par selecionado possui um worker.
  As ordens são acompanhadas como Turbo ou Binary
  conforme a classificação da corretora.
- Entre 05:00 e 16:59 UTC em dias uteis, seleciona somente pares regulares; das 17:00 as
  04:59 UTC e durante fins de semana, seleciona somente pares OTC. As sessoes cambiais sao
  registradas, mas nao alteram a amostragem aleatoria; nao ha fallback entre categorias.
- O universo da selecao vem do catalogo Binary aberto da corretora. Ativos fechados, sem
  payout valido, duplicados ou ausentes no mapa local da API nao entram na selecao. Se nao
  houver pares validos, o sistema tenta novamente em intervalos de ate 15 segundos.
- A lista e atualizada sem interromper workers ainda elegiveis; workers de pares removidos
  sao encerrados cooperativamente e novos pares iniciam sem reiniciar os demais. Falhas ou
  respostas invalidas do catalogo preservam os workers atuais e sao tentadas novamente em
  intervalos de ate 15 segundos. Monitores de ordens abertas nao sao interrompidos.
- Cada ordem aceita recebe um monitor proprio, identificado pelo ID, que
  acompanha o P/L por venda via inscricao de atualizacoes frequentes, renovada durante a
  operacao. O alvo atualmente configurado e 60% do valor investido; ele so e avaliado com
  uma nova atualizacao e apos tres segundos da abertura, para evitar cotacao antiga e
  cancelamento de venda prematura.
  Ao atingir o alvo, solicita uma venda antecipada; se a corretora nao confirmar em ate
  cinco segundos, registra o erro e continua monitorando ate o fechamento.
- Martingale/recuperacao automatica esta desativado. Um LOSS e registrado uma unica vez;
  nao ha ordem recursiva nem reutilizacao do sinal que originou a perda. A retomada depende
  de um sinal novo aprovado pelos filtros apos o cooldown.

FILTRO DE CONTEXTO E PROTECAO DE RISCO:
- Antes de enviar uma ordem, o bot exige contexto local favoravel: eficiência direcional
  mínima de 0,20 nos últimos oito candles M15 e volatilidade relativa máxima de 2,50x
  quando disponível. Contexto insuficiente ou inválido bloqueia a entrada. Este filtro
  usa apenas candles e métricas técnicas; não consulta notícias nem prevê eventos externos.
- A banca inicial da conta selecionada é lida da corretora. A stake parte de um valor fixo
  baseado na banca inicial e limitado por ENTRY_AMOUNT; ela não aumenta quando o saldo cresce.
- STOP_WIN é uma meta monetária absoluta de lucro da sessão. Ao atingi-la, novas entradas
  são bloqueadas. O high-water mark é persistido separadamente e continua sendo a referência
  do stop móvel de 10%.
- Dois WINs reais consecutivos ativam proteção global e cooldown configurável. Durante a
  proteção, os workers continuam analisando e registrando resultados virtuais. A retomada
  exige um sinal novo, gerado após o cooldown e aprovado pelos filtros; a stake é a base.
- O P/L e o estado diário de risco, incluindo meta, high-water mark, cooldowns e resultados
  já processados, são persistidos por data e tipo de conta no SQLite. DRAW não conta como
  WIN. Resultado REAL INDETERMINADO bloqueia entradas até reconciliação explícita.
- O stop móvel é baseado em P/L realizado. Ordens já abertas quando o limite é atingido
  não são canceladas e podem liquidar depois.
- O executor legado em bot_core.py não envia ordens; use o dispatcher de trading com a
  autorização centralizada e monitoramento.
