# Marca d'água

No `.env`, a partir da versão 2026.11.19.3:

```dotenv
WATERMARK_ENABLED=true
```

Use `false` para desativar a marca sem apagar ou alterar seu texto. Somente `true` e `false`
são aceitos. Reinicie o bot após alterar esse valor. O estado inicial foi mantido ativado.

O texto fica no `settings.env`:

```dotenv
WATERMARK_TEXT='teste'
```

O texto aparece no centro do vídeo, em branco semitransparente com contorno discreto,
somente enquanto o timestamp é menor que 30 segundos. Cada arquivo enviado tem seus
próprios primeiros 30 segundos, inclusive quando uma parte precisa ser subdividida para
caber no limite de 300 MB. Texto vazio desativa o recurso. Alterações exigem reiniciar o bot.

O texto é lido de um arquivo UTF-8 pelo FFmpeg, com expansão desativada; caracteres como
porcentagem, aspas e dois-pontos são tratados como texto. A configuração aceita uma linha
de até 200 caracteres. A fonte DejaVu Sans é uma dependência explícita da imagem Linux;
no Windows é usada Arial.

## Processamento e limites

- A captura continua recebendo a qualidade original. A marca é aplicada depois, na fila
  existente, uma codificação por vez, compartilhada entre os destinatários do mesmo arquivo.
- O codificador, o decodificador e o filtro são limitados a um thread cada. Uma nova tarefa
  com marca aguarda CPU abaixo de 45% da cota do contêiner, memória abaixo de 70%, ausência
  de bloqueio de recursos e conclusão de bootstraps de captura em andamento.
- Resolução e frames são preservados, incluindo timestamps de fontes com intervalos
  variáveis. O vídeo é reencodado em H.264 CRF 20; portanto, deixa de ser uma cópia bit a bit.
  Não há redução deliberada de FPS ou resolução. O áudio AAC compatível é copiado.
  O redimensionamento automático do FFmpeg fica desativado nessa etapa; uma configuração
  incompatível deve falhar com a origem preservada, em vez de reduzir o vídeo silenciosamente.
- Embora o texto apareça por apenas 30 segundos, **o arquivo inteiro é reencodado**. Misturar
  uma introdução reencodada com um restante copiado não foi adotado, para evitar mudanças
  de cabeçalhos e incompatibilidades de reprodução no mesmo MP4.
- Os sheets continuam sendo gerados a partir do vídeo sem marca: somente as nove imagens,
  sem texto. O bruto permanece preservado até a conclusão do fluxo de entrega.
- Se a aplicação da marca falhar, o arquivo não é encaminhado automaticamente sem a marca;
  a origem é preservada e há alerta operacional. Clipes menores que o mínimo de envio
  existente, de 10 segundos, não gastam CPU com a marca.
- O recurso vale para as novas partes processadas com essa configuração. Arquivos já
  preparados ou enviados não são regenerados.

## Teste de consumo

Amostra real de **45,026 segundos**, H.264 **720×1280**, taxa nominal 30 FPS, 1.294 frames e
áudio Opus. Todos os 1.294 frames foram preservados, com saída H.264/AAC decodificada sem
erros. A quantidade de frames da fonte reflete seus intervalos originais; não foi forçada
uma taxa constante para preencher lacunas.

Contêiner isolado, teto de 3 CPUs e 2 GiB, armazenamento temporário Linux. Foram medidos
processamento, preparação MP4 e sheet; captura, rede, upload e validação posterior ficam
fora do cronômetro. Os dois controles sem marca e as duas medições com um thread de
codificação usaram exatamente a mesma fonte e as mesmas dimensões.

| Medida | Sem marca, média de dois controles | Com marca, duas medições finais |
|---|---:|---:|
| Tempo total | 2,54 s | 21,98–24,58 s |
| CPU acumulada | 3,10 segundos de CPU | 26,88–29,83 segundos de CPU |
| CPU média durante o trabalho | 1,22 núcleo lógico | 1,21–1,22 núcleo lógico |
| Pico de memória do contêiner | 82–91 MiB | 151–155 MiB |
| Arquivo final | 10,01 MB | 12,86 MB |

Neste teste, o custo total de CPU aumentou aproximadamente **8,7–9,6 vezes**, e o arquivo
ficou cerca de 28% maior. A média de núcleos ocupados é parecida porque a configuração
final limita o paralelismo: o processador permanece ocupado por muito mais tempo.

Também foi testado o codificador com dois threads: 11,60 s de tempo total, 28,77 segundos
de CPU e média de 2,48 núcleos. A versão final usa um thread para limitar os picos, aceitando
mais tempo na fila. O pico de memória inclui os arquivos temporários em tmpfs, além dos
processos; não representa RAM adicional permanente por live monitorada.

Resultados e scripts: `cache/experiments/watermark/`, especialmente
`baseline-1-result.json`, `baseline-2-result.json`, `marked-1-result.json`,
`marked-serial-result.json`, `marked-final-result.json` e `benchmark.py`. O exemplo real processado está em
`marked-final-video.mp4` nessa pasta. O benchmark não envia mensagens nem altera a fila
de produção.

## Validação

Suíte Linux: **226 testes, sucesso, com 3 específicos de Windows ignorados**. Os testes
verificam centralização, presença antes dos 30 segundos, ausência a partir dos
30 segundos, áudio, dimensões, preservação de frames, timestamps variáveis, subdivisão,
texto literal, configuração desativada, espera por recursos, bruto preservado e sheet sem
marca. O log está em `cache/watermark-linux-full.log`; o exemplo sintético de 35 segundos
está em `cache/experiments/watermark/preview.mp4`.

A versão foi aplicada no Docker e confirmada como saudável. `WATERMARK_TEXT=teste`,
limite de 300 MB e fonte instalada foram conferidos dentro do contêiner. As 12 inscrições
existentes antes dessa atualização foram preservadas, incluindo pausas, paradas e gerações.
O monitor ativo retomou pelo viewer leve, e o primeiro backup automático da versão nova
foi verificado em `database/backups/production-20261119T231245082375Z.zip`.

Os resultados são de uma amostra e não comprovam capacidade para 100 gravações simultâneas
com marca. Sob carga contínua, a codificação pode acumular fila; o controle de recursos e
o buffer limitado continuam se aplicando. Aceleração por GPU não foi implementada.
