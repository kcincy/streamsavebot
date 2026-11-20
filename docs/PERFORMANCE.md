# Medições de captura — 19/11/2026

Máquina: Ryzen 5 4600G, 6 núcleos físicos/12 lógicos, 16 GB de RAM.
Docker Desktop Linux/WSL 2 disponibiliza aproximadamente 7,65 GiB para a VM.
CPU abaixo é percentual de **um núcleo lógico**: 100% significa um núcleo ocupado,
não o computador inteiro. Memória é a contabilização do cgroup, incluindo cache de arquivos.

## SuperLive

| Teste real | CPU média | Memória média | Vídeo |
| --- | ---: | ---: | --- |
| kaaah, player anterior, 20 s | 62,69% | 870,6 MiB | H.264 720×1280 + Opus |
| kaaah, viewer mínimo, 60 s, três partes | 26,53% | 526,3 MiB | H.264 720×1280 + Opus |

Redução observada de aproximadamente 58% na CPU e 40% na memória. São amostras em
momentos diferentes da mesma live, não uma garantia fixa para todos os perfis.
O baseline anterior de heleena chegou a 87,69% de um núcleo. Heleena e sassyadri
ficaram offline durante parte dos ensaios, então kaaah foi usada na comparação.
As três voltaram a gravar juntas no bot após a implantação. Kaaah encerrou e sassyadri
mudou para privada: suas partes foram entregues separadamente enquanto heleena continuou.

O viewer mínimo remove a interface e os players de recomendações. Ele usa o SDK Web
oficial Agora RTC 4.24.10, sem alterar o bundle, e mantém a sessão legítima do site:
consulta da live, entrada, confirmação periódica, renovação de token e saída.
Solicita somente o publisher principal do canal confirmado, em HIGH, com fallback
para vídeo inferior desativado. Conta, live, canal e identidade do viewer são conferidos.
Uma mudança encerra a parte atual antes da reconexão.

O MediaRecorder continua fazendo o mux e a sincronização. Os três arquivos foram
decodificados por inteiro depois da preparação para envio; mantiveram 720×1280 e
o hash dos dados de imagem H.264 foi idêntico antes/depois. Apenas o áudio Opus foi
convertido para AAC na preparação do MP4. A sincronização perceptual de lábios não
foi medida; os finais de áudio e vídeo ficaram a menos de 60 ms nos três arquivos.
Como no motor anterior, reiniciar o MediaRecorder pode esperar o próximo keyframe:
nos cortes curtos de teste, o primeiro vídeo começou entre 0,5 e 1,8 s após o áudio.

Após o primeiro handshake, a verificação do perfil reutiliza a requisição HTTP normal
em vez de abrir outra página. Em três consultas reais: 0,20–0,28 s e zero páginas abertas;
o primeiro handshake levou 6,21 s. Tokens e cabeçalhos ficam somente na memória.
Depois da primeira consulta à sala, estados privados também são conferidos por HTTP,
sem recarregar a interface repetidamente. A identidade e o estado atual são revalidados
antes de cada nova captura; erros de rede retomam o handshake normal.

Também foram testados:

- SDK nativo Agora Python 2.4.10: recebeu alguns frames originais, mas o servidor
  encerrou a conexão com erro 123. Não foi ativado nem foi tentado contornar a rejeição.
- Cópia de frames codificados com RTCRtpScriptTransform: uma sessão de 120 s ficou
  estável a 23,08% de um núcleo, mas o mux experimental não comprovou sincronização
  por RTCP. Não foi ativado; a diferença para o MediaRecorder não justificou esse risco.
- HTTP/HLS/FLV, Streamlink e extratores genéricos: Streamlink 8.6.3 retornou
  NoPluginError e yt-dlp 2026.10.26 retornou Unsupported URL para o perfil de heleena.
  A sessão pública inspecionada
  ofereceu WebRTC/Agora, sem URL HLS/FLV/DASH utilizável. Não há captura HTTP equivalente
  validada para o SuperLive neste projeto.

## Cem capturas controladas

Teste isolado, com limite de 4 CPUs e 4 GiB. Usou 100 motores reais de captura e remux
Streamlink/FFmpeg, SQLite, cotas, spool, preparação de MP4 e geração de folhas de contato.
A descoberta de perfis foi substituída por um arquivo local entregue em tempo real;
o Telegram foi um destino simulado. Nenhuma mensagem desses 100 testes foi enviada.

| Medição | Resultado |
| --- | ---: |
| Fontes simultaneamente gravando durante toda a amostra | 100 |
| Amostra estável | 20,37 s |
| Fonte por captura | H.264 720×1280, AAC, aproximadamente 1,83 Mbps |
| CPU agregada das 100 capturas | 44,58% de um núcleo |
| Memória média / pico | 2.082,5 / 2.458,4 MiB |
| Partes processadas e entregas simuladas confirmadas | 100 / 100 |
| Folhas de contato concluídas | 100 |
| Fila após finalizar todas as fontes juntas | drenada em 175 s |

O teste prova que o caminho de cópia consegue manter 100 workers e concluir os arquivos
nesse cenário. Não inclui o custo de 100 conexões aos sites, desafios de acesso, TLS,
variações de bitrate, falhas externas, 100 sessões Agora, ou upload real ao Telegram.

O processamento permanece limitado a uma parte por vez, preservando a ordem dos envios
e limitando picos de CPU. Fontes H.264/HEVC compatíveis são copiadas, sem reduzir resolução.

## Gargalos corrigidos e configuração

### Reteste do fallback e SDK Java

O bloqueio de joins de recomendações usava uma Promise que nunca resolvia. Isso podia
prender a fila do SDK antes de a conexão principal ser preparada. A inicialização agora
conclui esses joins simulados sem conectar canais adicionais e seleciona as credenciais
somente pelo canal previamente confirmado. Entradas e consultas de outras salas são bloqueadas.
Salas públicas com convidados usam o mesmo publisher principal; os convidados não são
inscritos ou misturados ao vídeo. Falhas registram a etapa controlada, sem tokens no log.

Quando o viewer mínimo estiver indisponível e o canal do SDK for reconhecido, o fallback
também impede conexões com outros canais. Quando não houver correlação segura, a captura
original é preservada e o log informa esse limite; não se bloqueia a transmissão por adivinhação.
Em amostras separadas de 30 s da live `patrizinha`:

| Caminho | Conexões RTC | CPU de um núcleo | Vídeo final |
| --- | ---: | ---: | --- |
| Fallback anterior, sem isolamento | 3 | 87,72% | 720×1280, áudio, decodificação integral válida |
| Fallback limitado ao canal confirmado | 1 | 54,05% | 720×1280, áudio, decodificação integral válida |
| Viewer mínimo corrigido | 1 | 32,67% | 720×1280, áudio, decodificação integral válida |

O fallback consumiu aproximadamente 38% menos CPU nessas amostras. Isso não representa
uma redução garantida no computador inteiro ou em todas as lives; a fonte muda com o tempo.
O teste do viewer mínimo ocorreu enquanto outra suíte de testes estava executando.

O SDK oficial Agora Recording Java 4.4.151.1 foi baixado da Agora e executado em uma imagem
Docker experimental separada. Serviço, gravação encoded-only e inscrições HIGH inicializaram
com retorno 0. O cliente conectou, identificou o publisher e fez o `/enter` normal (HTTP 200),
mas recebeu `ERR_CLIENT_IS_BANNED_BY_SERVER` em 1,176 s e fechou em 1,188 s. Não houve
conexão estável nem gravação utilizável. A sessão desse cliente nativo foi rejeitada;
isso não significa que o bot ou a conta inteira foram banidos.

O SDK Java não foi integrado à produção nem incluído nas dependências do bot. Nenhuma
tentativa foi feita de falsificar o tipo do cliente ou contornar a rejeição. O projeto
principal permanece Python. Não há medição válida de CPU de gravação nativa nesse teste.
Os relatórios estão em `cache/experiments/superlive-native-java/`: `java-result.json`,
`baseline-result.json`, `fallback-result.json` e `minimal-result.json`.

### Ajustes comuns

- Capturas longas tinham o mesmo executor das tarefas de processamento. O limite padrão
  do Python deixava monitores na fila e podia impedir que processamento/uploads usassem
  threads. Agora as capturas têm um executor separado, limitado a 128 workers.
- A checagem de um fragmento recalculava a cota de todos os monitores. Agora contabiliza
  apenas o perfil envolvido; o checkpoint periódico continua cobrindo todos. Com 100 perfis
  e 30 checagens, 9.000 SELECTs e 3.000 COMMITs viraram 90 SELECTs e 30 COMMITs.
  No ensaio SQLite, o tempo caiu de 5,56 s para 0,16 s. Um checkpoint de 100 perfis usa
  uma transação única, mantendo a divisão correta na virada da semana.
- O spool antigo comportava só cinco partes. Partes de 128 MB e teto de 16 GiB permitem
  122 reservas compartilhadas entre capturas e fila. As margens de processamento/corte/cache
  continuam reservadas. Os arquivos não mudam de resolução ou qualidade por esse ajuste.
- Compose: até 6 CPUs, 6 GiB e 4.096 processos/threads. Os limites não reservam recursos
  antecipadamente, nem transformam a máquina em um servidor capaz de qualquer mistura de fontes.

## Limites para uso real

**Não foi comprovado suporte a 100 lives reais simultâneas, especialmente 100 SuperLive.**
Extrapolar a amostra de 26,53% para 100 SuperLive exigiria cerca de 26,5 núcleos lógicos
ocupados, acima desta máquina. Essa projeção não é um benchmark de escala: compartilhar
o Chromium pode alterar os custos, e eles precisam ser medidos com a mistura real.

A rede precisa sustentar entrada e saída. Por exemplo, 100 fontes de 4 Mbps representam
aproximadamente 400 Mbps recebidos e outros 400 Mbps enviados, além do overhead. Uploads
reais anteriores de aproximadamente 499 MB levaram 45–46 s, cerca de 87–89 Mbps por envio;
isso não mede o máximo do link. Se a saída for menor que a entrada, a fila cresce até
esgotar o spool e a proteção de espaço pausa capturas, podendo causar lacunas.

Os 122 slots não significam 122 gravações mais uma fila ilimitada: todas compartilham o
mesmo orçamento. A validação de longa duração deve acompanhar CPU, memória, crescimento
da fila, disco e a taxa real de envio, sem diminuir resolução para fazer a conta fechar.

Os relatórios brutos e os scripts usados estão em `cache/experiments/superlive-scale/`;
`load100-result.json`, `production-viewer-kaaah/baseline-result.json` e
`production-viewer-kaaah/verification.json` registram esses resultados.
