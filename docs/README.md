# SuperLive Telegram

Bot privado para monitorar, gravar e enviar lives do SuperLive, StripChat, Kick e TikTok automaticamente.
Executa em Windows x64 ou em Docker com Linux; veja [migração e operação com Docker](DOCKER.md).

## Iniciar

```powershell
python main.py
```

O iniciador seleciona `.venv`, carrega credenciais de `.env` e opcoes de `settings.env`, e inicia a Bot API local, o polling, os monitoramentos
salvos e a fila de envios. Nao inicie o servidor ou outro bot separadamente. Ctrl+C fecha tudo em ordem.
A trava do projeto e o mutex por token impedem duas instancias locais. Um Conflict encerra a instancia
em conflito; nenhum token pode estar em uso por outro polling externo ao projeto.

## Estrutura

```text
main.py                   iniciador unico
src/                      codigo da aplicacao
.venv/                    Python e dependencias locais
.env                      somente credenciais
settings.env              configuracoes operacionais sem credenciais
requirements.txt          versoes Python verificadas
database/bot.sqlite3      usuarios, inscricoes, estados e fila
database/telegram-bot-api/ sessao persistente do servidor oficial
database/backups/         banco/configuracao anteriores a reorganizacao
cache/spool/              segmentos temporarios ativos ou em recuperacao
cache/tmp/                perfis temporarios do Chromium e demais temporarios
cache/telegram-bot-api/    arquivos auxiliares e temporarios da API
logs/bot.log              atividades e erros, com rotacao
logs/launcher.log         uma linha de inicializacao ou erro critico
logs/legacy/              logs de servicos anteriores preservados
tools/                    FFmpeg, FFprobe, Bot API, DLLs e Chromium
scripts/                  setup, testes e manutencao
tests/                    regressao automatizada
```

A antiga `.runtime` foi substituida por `cache` e `database`. Residuos cuja exclusao foi bloqueada
ficam em `cache/obsolete-*`; nao sao usados pelo sistema. Consulte [LIMPEZA.md](LIMPEZA.md).
O projeto é mantido em um repositório privado. Credenciais e dados de execução ficam fora do Git.

## Comandos do Telegram

- `/start`: ajuda e escolha de idioma (English / Português).
- `/add`: mostra os botões SuperLive, StripChat, Kick e TikTok; depois solicita o perfil e ativa o monitoramento continuo.
- `/monitor`: somente sua lista, status, progresso real e botoes.
- `/stats`: horas e GB gravados, videos recebidos, lives, fila e ranking dos seus perfis.
- `/stop`: solicita o streamer, envia a parte atual e continua gravando a mesma live.
- `/remove`: solicita o streamer, remove sua inscricao e cancela os envios ainda pendentes para voce.

Na primeira execucao do /start, a ajuda aparece em ingles com os botoes de escolha de idioma.
Escolher portugues reenvia a ajuda traduzida. O idioma fica salvo no SQLite e vale para comandos,
botoes, estados, erros, estatisticas e legendas dos videos, inclusive em entregas compartilhadas.
Use /start novamente para mudar de idioma. /estatisticas foi substituido por /stats.

No /add, selecione primeiro a plataforma e depois envie o nome de usuário ou link do perfil.
No SuperLive, perfis sem username também são aceitos. Exemplos equivalentes:

```text
/add https://superlivechat.tv/pt/profile/30314654
/add 30314654
/add id:30314654
/add shared:89633559
```

O número em `/profile/` é o `user_id` da conta. `shared:` consulta o outro ID, exibido
no SuperLive, e exige correspondência exata. No bot, números sem prefixo representam
perfis do SuperLive; URLs `/livestream/` são recusadas no cadastro de monitoramento.
O nome de exibição aparece no monitor, botões, histórico e legendas; os identificadores
continuam separados e fixos internamente. Adicionar a mesma conta por outra forma
reutiliza o cadastro, preservando pausa, /stop, destinatários e limite individual.
Os formatos de ID e o link também funcionam em /stop e /remove depois do cadastro.

O /monitor identifica a plataforma de cada perfil. Nomes iguais em sites diferentes são independentes.
No /stop e /remove, use a URL ou `superlive:username`, `stripchat:username`, `kick:username` ou `tiktok:username` se houver nomes iguais.
Os botões do painel já identificam o perfil correto. O limite de 3 perfis cadastrados por usuário soma as quatro plataformas, incluindo os pausados.

Envie somente o comando e responda com o streamer na mensagem seguinte. Se houver erro, corrija
o streamer enviando outra mensagem. Cada usuario tem sua propria conversa; /start ou /monitor
encerra a solicitacao pendente e outro comando troca a acao. O formato antigo com argumento
na mesma mensagem continua aceito. Solicitacoes pendentes ficam em memoria; depois de reiniciar,
envie o comando novamente. Inscricoes, pausas, gravacoes e envios continuam seguindo o banco.

No monitor, use Pausar/Retomar e Remover, lado a lado na mesma linha, e Atualizar status.
Para receber a parte atual sem interromper a gravação, use `/stop`. Remover tem a mesma função de `/remove`.
O nome SuperLive inclui o ID entre parênteses, por exemplo `Nome 💖 (30314654) · SuperLive`;
use `/remove 30314654` para remover sem precisar copiar fontes estilizadas ou emojis.
Usuários comuns veem `Slot vazio` em cada uma das três vagas ainda não ocupadas. Pausar preserva o cadastro e
as partes ja finalizadas. Retomar permite voltar a capturar a mesma live depois de uma pausa.
O `/stop` mantém a inscrição ativa e a captura continua na parte seguinte, sem esperar outra transmissão.

A consulta offline e periodica (15 segundos), sem notificacoes. A identidade da conta permanece
fixada no banco: recomendacoes e trocas para outra conta sao bloqueadas.

Uma captura atende todos os inscritos no streamer. O corte solicitado por um /stop fecha uma parte
para todos, e todos continuam no mesmo player. A primeira entrega faz o upload; as seguintes
usam file_id. Remover uma inscricao nao encerra a captura se houver outros interessados.
Um envio ja aceito pelo Telegram nao pode ser recolhido.

Não há cota semanal de gravação nem comando `/limits`. O limite de cadastro continua em
três perfis por usuário comum, incluindo pausados. `SUPERUSER_ID` é isento do limite de
perfis; os controles técnicos de CPU, memória, fila e armazenamento valem para todos.
O histórico antigo de uso é preservado no banco, mas não interrompe nenhuma gravação.
Entrar ou retomar uma captura compartilhada solicita um corte: o novo inscrito recebe a
próxima parte, evitando acesso a conteúdo anterior à sua inscrição. O painel de estatísticas
só pode ser atualizado pelo próprio usuário, em conversa privada.

O painel /monitor mostra um resumo dos seus streamers ao vivo/offline, monitoramentos ativados e
gravacoes ativas. Cada streamer tem um bloco com seu estado e os botoes de controle. Durante a
captura, mostra MB no formato do idioma escolhido e tempo do segmento em MM:SS ou HH:MM:SS, mais o horario
da ultima atualizacao do painel. Esses dois contadores zeram quando uma parte e fechada e outra comeca, inclusive
apos `/stop`. Sao dados reais, atualizados aproximadamente a cada segundo.
Sem o primeiro bloco, o painel mostra "Aguardando dados da captura", sem um cronometro ficticio.
Consultas ao gravador no navegador possuem prazo de 5 segundos por operacao. Se o player ou o
MediaRecorder nao responder, o prefixo em disco e preservado para processamento e a sessao e fechada.
O contador para antes de aguardar a finalizacao; uma nova captura inicia seu proprio segmento.
Use o comando ou o botao Atualizar para consultar; nao ha mensagens de progresso automaticas.
Um perfil pausado sem monitor compartilhado aparece como desconhecido. Contadores de captura
reiniciam com o processo; inscricoes, pausas, stops por live e fila permanecem no SQLite.

## Configuracao

Edite `settings.env` na raiz para mudar as opcoes operacionais e reinicie com `python main.py`.
O `.env` guarda somente TELEGRAM_BOT_TOKEN, TELEGRAM_API_ID e TELEGRAM_API_HASH.
Variaveis definidas no processo tem prioridade sobre os arquivos.

| Variavel | Uso / padrao |
|---|---|
| TELEGRAM_BOT_TOKEN | Token do bot; obrigatorio |
| TELEGRAM_API_MODE | local (padrao) ou public |
| TELEGRAM_API_URL | http://127.0.0.1:8081 |
| TELEGRAM_API_ID / TELEGRAM_API_HASH | Credenciais do servidor oficial |
| SUPERLIVE_STATE | Sessao existente do site, opcional; caminho relativo a raiz ou absoluto |
| STRIPCHAT_STATE | Sessao existente do StripChat, opcional e independente da sessao SuperLive |
| PART_MAX_MB | Teto por parte em MB decimais: 32–500 na API local, 32–45 na pública; settings.env usa 300 |
| SPOOL_MAX_MB | Teto de temporários em MiB; padrão sem configuração 4096, settings.env usa 16384; exige quatro partes com margem |
| TEMP_RETENTION_SECONDS | 3600; prazo para alertar sobre recuperação atrasada; não apaga pendentes |
| MAX_ACTIVE_CAPTURES | 100; teto de capturas, sujeito aos recursos disponíveis e ao buffer |
| WATERMARK_TEXT | Texto central nos primeiros 30 segundos de cada arquivo; vazio desativa; esta instalação usa `teste` |
| WATERMARK_ENABLED | No `.env`: `true` ativa a marca; `false` desativa mantendo o texto configurado; padrão `true` |
| MAX_MONITORS_PER_USER | 3 perfis cadastrados por usuario (maximo 3); pausados tambem contam |
| SUPERUSER_ID | ID numérico do Telegram isento do limite de perfis; 0 desativa a isenção |
| LOW_RESOURCE_MODE | 1 (padrao): Chromium compartilhado, contextos isolados e viewer mínimo SuperLive em qualidade original; 0: player anterior |

O limite e individual e validado ao cadastrar um perfil novo no /add, inclusive quando a captura ja e
compartilhada com outra pessoa. /stop e Pausar continuam ocupando vaga. Somente Remover libera a vaga.
Retomar um perfil existente nao exige uma nova vaga. Cadastros anteriores acima de 3 sao preservados,
mas bloqueiam novos cadastros ate que o usuario remova perfis suficientes. Adicionar novamente uma inscricao existente
nao cria outra nem reativa uma pausa. O antigo MAX_MONITORS global deixou de ser utilizado.
Uma captura compartilhada continua atendendo todos os inscritos; usuarios diferentes podem
monitorar ate tres perfis cada. Mais perfis distintos ativos aumentam o uso total de CPU.

O teto da API local é **500.000.000 bytes por segmento**. `PART_MAX_MB` permite partes
menores sem alterar qualidade; esta instalação usa 300 MB e margem para o último bloco RTC.
A live não tem duração máxima nem limite semanal individual.
As configurações antigas SEGMENT_SECONDS e RECORDINGS_DIR não são usadas.
No modo público, configure PART_MAX_MB em até 45 MB.
As capturas têm um executor separado do processamento, com até 128 workers.
Veja [as medições e os limites para cem fontes](PERFORMANCE.md).

## Estatisticas e recursos

O StripChat captura HLS por HTTP persistente, sem player ou navegador, copiando segmentos completos
MPEG-TS/fMP4 e resolvendo URLs MOUFLON v1/v2 com as chaves públicas incluídas no projeto. A configuração
é consultada pela API quando não está no HTML. São solicitados segmentos standard e vídeo H.264.
Falhas preservam o prefixo já capturado e geram nova tentativa HTTP, sem fallback para Chrome.
Os logs mostram `captura HTTP direta` ou `captura HTTP indisponível` com o motivo. Uma mudança nas
chaves do site pode exigir atualizar src/stripchat_mouflon_keys.json e reiniciar. As regras de acesso
público, identidade, /stop e fila são mantidas. Veja PLATAFORMAS.md.

Kick e TikTok usam Streamlink 8.6.3 para descobrir o fluxo e FFmpeg com `-c copy` para gravar
fragmentos MP4 completos, sem recodificar nem reduzir a resolucao. Na Kick e escolhida a maior
qualidade disponivel; no TikTok somente `origin` (H.264 ou H.265), sem fallback para HD/SD/auto.
Se a origem nao estiver disponivel, o monitor aguarda em vez de gravar uma versao menor.
A Kick pode exigir uma abertura breve do Chromium para obter cookies do desafio de acesso; o
player nao e usado na gravacao. Bloqueios, autenticacao ou mudancas de API podem impedir a captura.
As quatro plataformas usam as mesmas regras de identidade, limite de perfis, fila e contact sheets.

Uma lacuna de sequência no HLS é registrada e a captura do mesmo perfil continua no arquivo atual;
não é tratada como encerramento da live. Segmentos já removidos pelo CDN não podem ser recuperados
por esse caminho e podem deixar lacunas no vídeo. Encerramento, limite de tamanho e /stop continuam
finalizando partes por perfil. O fim de uma live não corta nem publica a captura de outro perfil.

O /stats usa apenas os metadados SQLite do proprio usuario, inclusive depois da remocao de
um perfil ou da limpeza dos videos temporarios. Mostra tempo e volume de gravacoes finalizadas,
tempo/GB/videos com envio confirmado, lives e perfis distintos, maior video recebido, primeira e
ultima gravacao, fila, falhas, cadastros ativos/pausados e os tres perfis mais gravados por tempo.
O segmento em andamento entra nos totais depois do processamento. Historico antigo sem duracao
de FFprobe usa o tempo de captura disponivel e e identificado como estimado. GB usa 1.000.000.000 bytes.
Uma captura compartilhada e contada uma vez em cada historico individual, sem somar destinatarios
de outras contas. Gravacoes inteiramente dispensadas por terem menos de 10 segundos nao entram no historico.

Com LOW_RESOURCE_MODE=1, um Chromium atende varios monitores em contextos separados: cada um
mantem cookies, paginas, identidade e gravacao proprios. Fechar um contexto nao encerra os outros.
A conexao de controle usa somente 127.0.0.1; nao exponha essa porta. Um crash do navegador pode
afetar varias capturas; os prefixos em disco sao preservados e o motor tenta reconectar e recriar
o navegador. O processo principal continua sendo o unico proprietario do Chromium compartilhado.

Na captura direta de um track RTC confirmado, a previa visual, animacoes e callbacks visuais
deixam de consumir recursos; o track, audio, resolucao e bitrate de origem continuam no gravador.
Capturas de canvas ou fallback sem track RTC identificado preservam a reproducao necessaria.
Imagens e fontes sao bloqueadas; scripts, API, documentos e midia continuam permitidos, inclusive
no StripChat. A GPU nao e desativada globalmente, para evitar transferir o custo de decodificacao
para a CPU. LOW_RESOURCE_MODE=0 permite voltar ao comportamento anterior. Picos ainda dependem
do codec, resolucao, numero de perfis distintos e necessidade de conversao pelo FFmpeg.

## Midia e recuperacao

As legendas identificam o @username como texto literal (sem mencao do Telegram), plataforma,
ID publico do perfil quando confirmado, inicio da gravacao e duracao. Nao exibem numero de parte.

Somente videos com **pelo menos 10 segundos reais** sao enviados, nas quatro plataformas, inclusive
apos /stop e ao recuperar a fila. Partes menores sao dispensadas e seus temporarios sao limpos
quando todos os destinatarios forem avaliados. O tempo capturado permanece no histórico.
A verificacao usa a duracao precisa do arquivo, antes do arredondamento exigido pelo Telegram.
Uma falha ao obter a duracao preserva o arquivo para recuperacao em vez de descarta-lo como curto.

O MediaRecorder continua capturando a proxima parte enquanto o FFmpeg processa a anterior.
Os envios usam MP4 com H.264 ou HEVC e AAC. H.264/HEVC compatível é copiado sem recodificação;
Opus é convertido para AAC e VP8/VP9/outros vídeos incompatíveis são convertidos para H.264,
com dois threads. Essa conversão necessária aumenta o custo de processamento. MKV/WebM ficam apenas como temporários.
Mudancas de resolucao e cortes que precisem de novos keyframes podem exigir codificacao com dois
threads. O escritor por pipe limita cada arquivo. Nenhum arquivo acima de 500 MB e enviado.

MP4 completos recebem um remux adicional com `-c copy -movflags +faststart`, sem perda de qualidade.
O resultado so e usado se preservar todos os pacotes de audio/video e respeitar o tamanho. O indice
fica no inicio e duracao/largura/altura sao informadas explicitamente ao Telegram. Se essa preparacao
nao for valida, a origem e preservada. A melhoria trata metadados e compatibilidade, sem afirmar que
o video que demorou a carregar estava corrompido.

O arquivo de origem permanece no spool ate confirmacao do envio ou conclusao do prazo de recuperacao.
Falhas de processamento provocam uma tentativa de remux recuperavel. Falhas de rede ambiguas ficam
sem confirmacao e preservam os arquivos ate o prazo, evitando reenviar mensagens possivelmente ja
recebidas. A Bot API nao oferece chave de idempotencia. Conexoes comprovadamente nao iniciadas e
rate limits possuem retentativas. O primeiro upload tem timeout de ate 1800 segundos.

Temporarios enviados sao limpos automaticamente. Partes sem identidade recuperavel sao preservadas
para avaliacao, nunca enviadas para usuarios escolhidos por suposicao. Uma fila cheia pode pausar a
captura e causar lacunas. Nao e possivel garantir captura continua durante uma falha ilimitada sem
armazenamento ilimitado. O spool nao e um arquivo permanente de gravacoes.

No encerramento, o programa aguarda a ultima parte, FFmpeg e upload em andamento. Pendencias
recuperaveis permanecem para a proxima inicializacao. Com o programa desligado nao existe limpeza
por expiração; arquivos pendentes são preservados, e as verificações são retomadas ao iniciar. Mover o projeto exige copiar tambem
banco, sessao e temporarios pendentes, com o processo encerrado.

## Testes e manutencao

```powershell
.\scripts\test.ps1
```

Incluem Chromium/FFmpeg reais com site local, comandos e botoes, stops individuais, retorno em outra
live, isolamento, persistencia, migracao da fila, limite de bytes, recuperacao, faststart, hash dos
pacotes e Ctrl+C. Respostas Telegram sao controladas nos testes; o estado de producao e conferido
separadamente nos logs e no banco. A reproducao no aplicativo Telegram precisa de validacao no cliente.

[Instalacao e portabilidade](AMBIENTE_LOCAL.md) | [Limpeza e limitacoes de execucao](LIMPEZA.md)

[Integração StripChat e correção de leitura de segmentos](PLATAFORMAS.md)
