# SuperLive, StripChat, Kick e TikTok

O bot usa a mesma fila SQLite, orçamento de temporários, limite configurável de partes
(300 MB nesta instalação, teto de 500 MB na API local), envio por file_id,
logs e regras individuais nas quatro plataformas. /add apresenta a seleção de plataforma; /monitor
mostra o site de cada perfil. Pausar, Retomar, /stop e /remove continuam individuais.

As chaves antigas permanecem intactas e significam SuperLive. Novos perfis StripChat usam a chave
interna `stripchat:username`, impedindo colisões nos usuários, monitores, identidades, segmentos e
destinatários. Nenhum cadastro antigo é convertido, removido ou reativado durante a atualização.
A tabela adicional platform_sessions preserva a identificação da sessão do StripChat entre reinícios.
Quando o site informa outra streamName, ou o monitor observa offline e depois online, uma nova sessão
é criada. Uma passagem offline que ocorra inteiramente com o bot desligado pode não ser observada se
o site reutilizar a mesma streamName; o estado de /stop é preservado para evitar retomar uma live já parada.

O código de identidade do perfil, ordenação fMP4 e seleção de qualidade foi aproveitado do
stripchat_recorder.py fornecido. O arquivo original permanece como referência e não é usado pelo bot.
O adaptador ativo está em src/stripchat_recorder.py. Não inicia CLI, listagem de modelos ou comandos
de login. A captura HTTP atende somente salas públicas; não depende de uma sessão de navegador.

No StripChat, a captura usa HLS direto, com os hosts anunciados no HTML do próprio perfil ou na API
/api/front/v3/config/static. Se essa configuração estiver indisponível, tenta os CDNs doppiocdn
usados pelo StreaMonitor. Master e mídia podem estar em hosts edge-hls e media-hls diferentes.
Um cliente HTTP persistente baixa um segmento completo por vez, sem abrir Chromium ou reproduzir
vídeo. São aceitos MPEG-TS e fMP4 sem criptografia de mídia. As URLs ofuscadas MOUFLON v1/v2 são
resolvidas antes dos downloads. O pedido usa playlistType=standard e preferredVideoCodec=h264
para obter segmentos completos e evitar conversão de HEVC quando o servidor disponibiliza H.264.
O início acompanha os dois últimos segmentos completos; depois não repete sequências já recebidas.
Saltos de sequência do CDN são registrados como lacunas e não encerram uma live ainda pública.
O arquivo continua recebendo os segmentos disponíveis do mesmo perfil; partes removidas pelo CDN
não são recriadas. Mudança de init/configuração ainda fecha uma parte antes da próxima configuração.
Playlists finitas (preview/VOD), com áudio separado, byte ranges ou somente partes LL-HLS são recusadas.
URLs fora dos CDNs permitidos, de outro perfil, de anúncios /cpa/ ou placeholders media.mp4 são recusadas,
assim como redirecionamentos e respostas acima de 8 MiB (1 MiB para playlists).
Falhas preservam a parte capturada e repetem a tentativa por HTTP, sem abrir Chrome. Os logs distinguem
`captura HTTP direta` de `captura HTTP indisponível` e preservam o motivo, inclusive chave ausente.
O HLS pode variar por região/perfil; é necessário conferir uma live real para confirmar continuidade
e qualidade, mesmo quando a playlist é aceita. O remux reutiliza os codecs;
mudanças de init/configuração fecham uma parte antes de aplicar a configuração seguinte.
O gravador aceita somente status `public` com transmissão ativa e identidade confirmada. Offline,
salas privadas/grupo, bloqueios geográficos e respostas sem identidade ficam em monitoramento,
sem captura. O status é consultado novamente por HTTP durante a gravação (a cada três segundos),
porque o JSON inicial da página pode continuar público mesmo depois de o player trocar para preview.
A detecção é periódica, depende da resposta atual do site e não é instantânea.

Um init criptografado encerra a tentativa. Mudanças para um estado não público interrompem a captura
e descartam os dados ainda não confirmados. Uma troca real do ID da conta continua bloqueada;
indisponibilidade temporária ou resposta de outro perfil não encerra permanentemente o monitor.

O protocolo foi conferido em [StreaMonitor](https://github.com/lossless1024/StreaMonitor/blob/master/streamonitor/sites/stripchat.py)
e no seu [downloader HLS](https://github.com/lossless1024/StreaMonitor/blob/master/streamonitor/downloaders/hls.py).
O arquivo src/stripchat_mouflon_keys.json é uma cópia dos dados públicos de
[kesamom/stripchat_mouflon](https://github.com/kesamom/stripchat_mouflon), blob
9cb373d9d83c704c53c34f8229ab49f3f0623a3c, sob MIT (licença em stripchat-mouflon-LICENSE.txt).
Ele é carregado uma vez por processo. Se o site trocar as chaves, a captura para com motivo explícito;
é necessário atualizar o JSON e reiniciar. Não são baixados nem executados scripts remotos.

Os arquivos de saída são MP4 com H.264 ou HEVC e AAC, preparados para streaming. A fila verifica
os codecs reais com FFprobe: copia os codecs compatíveis, convertendo somente o necessário.
VP8/VP9 e outros formatos incompatíveis são convertidos com FFmpeg em dois threads, preservando
o bruto até confirmar o envio. A política é a mesma para as duas plataformas. Arquivos MKV que já
foram entregues não são alterados; novos envios usam MP4, respeitando PART_MAX_MB.

# Relatório de leitura com Playwright

O relatório erro_leitura_segmento_playwright.md foi analisado integralmente. A espera por identidade e
por um lock antes de iniciar response.body() aumentava a exposição à corrida com navegações.
Esse fator foi confirmado no código original; a ocorrência específica relatada não foi reproduzida.

A nova fila inicia a leitura de cada resposta imediatamente. As leituras são concorrentes, mas a aplicação
ao escritor respeita a ordem de chegada e a identidade confirmada. Leituras de rede não seguram o lock
do escritor. Há até 12 respostas pendentes, corpo de até 8 MiB por segmento aceito e buffer de reordenação
de 16 MiB. Uma fila cheia encerra a tentativa de captura, preserva o prefixo e permite reconectar ao alvo.
Corpos acima do limite são rejeitados; esses limites não são uma cota para toda a memória do Chromium.

Quando o corpo não está disponível, uma recuperação explícita usa a mesma URL, Range e cookies do
contexto, sem redirecionamentos. Só é tentada na mesma época de navegação e identidade. Se o recurso
sumiu ou a identidade mudou, não há como garantir reconstrução: o incidente é registrado e a sessão
é finalizada para não misturar fluxos. Navegações automáticas para outro perfil são bloqueadas.
Os logs registram hash do segmento, época, tempo desde a navegação e motivo local, omitindo URLs assinadas.

O SuperLive captura pelo MediaRecorder, sem usar Response.body() para mídia. A leitura do perfil
passou a usar route.fetch()/APIResponse, consumindo o JSON antes de liberar a resposta. Isso evita
depender de um recurso da página já invalidado por navegação. O player também bloqueia navegações
para outro alvo antes de fechar a parte atual. Em ambos os adaptadores, buffers/tarefas e a última
parte são finalizados antes do navegador; falhas deixam as fontes recuperáveis no spool.

Os testes cobrem respostas lentas e fora de ordem, identidade atrasada, navegação durante leitura,
recuperação e liberação de respostas, limites de buffer, init diferente, ordenação LL-HLS, botões,
homônimos, inscrições/stop independentes e restauração. Chromium e FFmpeg reais são exercitados com
um site local, tanto em fMP4 quanto no fallback MediaRecorder. Esses testes não comprovam continuidade
integral de uma transmissão real nem acesso a lives privadas, geobloqueadas ou protegidas.

Referências primárias: [corrida de body e navegação](https://github.com/microsoft/playwright/issues/41512),
[Response](https://playwright.dev/python/docs/api/class-response),
[APIRequestContext e cookies compartilhados](https://playwright.dev/python/docs/api/class-apirequestcontext).

## Kick e TikTok por Streamlink

Os perfis usam chaves `kick:username` e `tiktok:username`, com ID imutavel da conta
e ID da transmissao confirmados antes de gravar. A API de livestream da Kick fornece
o titulo e o fluxo HLS; se necessario, a API do canal confirma seu ID. O TikTok usa
a API consultada pelo plugin oficial e aceita somente a qualidade `origin`. A Kick
seleciona `source` ou a melhor qualidade disponivel, sem forcar uma resolucao menor.

Streamlink entrega os bytes ao FFmpeg; `-c copy` produz partes MP4 em keyframes, sem
reduzir a resolucao, sem renderizar o player e sem codificar o video durante a captura.
O AAC em ADTS do HLS recebe apenas a conversao de cabecalho necessaria ao MP4.
As partes passam pela mesma fila, legendas por idioma e contact sheets 3x3 em 16:9.

Na Kick, um HTTP 403 pode exigir uma breve sessao Chromium para cookies de acesso.
O contexto e fechado apos obter os cookies. A gravacao continua via Streamlink;
automacao de login, resolucao de CAPTCHA e acesso a salas privadas nao estao incluidos.
Se o site bloquear a consulta, o monitor tenta novamente; se a identidade nao puder
ser confirmada ou nao houver a qualidade de origem, nenhum video menor e gravado.

Validacao: plugins oficiais com HTTP controlado, identidade trocada, origem ausente,
HLS real em 1920x1080, FLV real em 720x1280, cortes e preservacao de audio. Uma live
publica da Kick foi capturada no Docker em 1920x1080 com H.264/AAC. A live publica `psyloma` no TikTok tambem foi capturada no Docker: HEVC/AAC na
resolucao original de 720x1280, 24,1 segundos, processamento para Telegram e contact
sheet gerado com sucesso.

Fontes: [plugin Kick 8.6.3](https://github.com/streamlink/streamlink/blob/8.6.3/src/streamlink/plugins/kick.py)
e [plugin TikTok 8.6.3](https://github.com/streamlink/streamlink/blob/8.6.3/src/streamlink/plugins/tiktok.py).

### Compatibilidade HEVC no Docker

Algumas origens do TikTok usam FLV com o codec ID legado 12 (HEVC). O FFmpeg 6.1
do Ubuntu 24.04 nao identifica esse video e falha com `dimensions not set`. A imagem
agora usa FFmpeg/FFprobe da serie 9.0, fixados no Dockerfile com checksum SHA-256.
A captura copia o HEVC de origem; o HEVC compativel e mantido no MP4 com a tag `hvc1`, sem recodificacao. A resolucao
e os pacotes de video originais sao preservados. Os estados distinguem consulta
de perfil, conexao ao fluxo e espera de dados, e os logs mostram o motivo seguro da
retentativa. Uma API inacessivel nao e classificada como perfil offline.

### Fila, partes e /stop

O contador de tempo do /monitor corresponde a parte atual e reinicia a cada corte.
Partes brutas ou em processamento tambem aparecem como pendentes no painel e em /stats.

Videos HEVC compativeis sao remuxados para MP4 com `hvc1`, sem conversao para H.264.
A conversao integral de HEVC congestionava a fila unica e ocupava os slots de captura;
com a copia de pacotes, o TikTok nao segura a Kick durante essa conversao. A API do
Telegram reconhece h265: https://core.telegram.org/constructor/documentAttributeVideo.
O suporte de reproducao depende do cliente e dos codecs do aparelho.

Ao perder todos os destinatarios elegiveis, inclusive por /stop, a espera por um slot
do buffer e cancelada sem reservar outra parte. Pedidos de corte antigos sao limpos
ao encerrar/iniciar uma parte, evitando um corte imediato depois de Retomar.

O buffer continua limitado: uma rede/upload mais lento que a soma das lives pode
voltar a preencher a fila e causar lacunas. Segmentos que nao foram recebidos durante
um bloqueio nao podem ser reconstruidos a partir dos arquivos salvos.
