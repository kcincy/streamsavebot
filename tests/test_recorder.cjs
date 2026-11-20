// Teste local do JS injetado: scripts/test.ps1 usa o Node incluido no Playwright.
// Usa o pacote Playwright ja instalado; BROWSER_EXECUTABLE permite escolher o binario.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require(path.join(__dirname, '..', '.venv', 'Lib', 'site-packages', 'playwright', 'driver', 'package'));

const source = fs.readFileSync(path.join(__dirname, '..', 'src', 'recorder.py'), 'utf8');
const init = source.match(/INIT_JS = r"""([\s\S]*?)"""/)[1];

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'slrec-'));
  const browser = await chromium.launch({
    channel: 'chromium',
    headless: true, executablePath: process.env.BROWSER_EXECUTABLE || undefined,
    args: ['--autoplay-policy=no-user-gesture-required', '--mute-audio', '--enable-gpu', '--allow-loopback-in-peer-connection',
      '--disable-features=WebRtcHideLocalIpsWithMdns'],
  });
  try {
    for (const [codec, bitrate] of [['video/H264', 0], ['video/VP8', 0], ['video/H264', 4000000]]) {
      const page = await browser.newPage();
      await page.addInitScript(init);
      await page.goto('about:blank');
      // Um video de recomendacao carregado antes da confirmacao deve ser ignorado.
      await page.evaluate(() => {
        const canvas = document.createElement('canvas');
        canvas.width = 960; canvas.height = 720;
        canvas.getContext('2d').fillRect(0, 0, 960, 720);
        const video = document.createElement('video');
        video.srcObject = canvas.captureStream(30);
        document.body.append(video);
      });
      assert.equal((await page.evaluate(() => window.__slRec.start({}))).ok, false);
      assert.equal(await page.evaluate(() => window.__slRec.info().hasVideo), false);
      await page.evaluate(() => window.__slRec.arm({livestreamId:'123456', channelId:'target'}));
      await page.evaluate(async ({codec, bitrate}) => {
        const canvas = document.createElement('canvas');
        canvas.width = 320; canvas.height = 240;
        const ctx = canvas.getContext('2d');
        let frame = 0;
        setInterval(() => {
          ctx.fillStyle = `hsl(${frame++ % 360}, 80%, 50%)`;
          ctx.fillRect(0, 0, 320, 240);
        }, 33);
        const ac = new AudioContext();
        await ac.resume();
        const osc = ac.createOscillator();
        const dest = ac.createMediaStreamDestination();
        osc.connect(dest); osc.start();
        const stream = canvas.captureStream(30);
        stream.addTrack(dest.stream.getAudioTracks()[0]);
        const sender = new RTCPeerConnection({iceServers: []});
        const receiver = new RTCPeerConnection({iceServers: []});
        window.peers = [sender, receiver];
        sender.onicecandidate = e => { if (e.candidate) receiver.addIceCandidate(e.candidate); };
        receiver.onicecandidate = e => { if (e.candidate) sender.addIceCandidate(e.candidate); };
        receiver.ontrack = e => {
          if (e.track.kind === 'video') {
            const video = document.createElement('video');
            video.dataset.pipChannel = 'target';
            video.srcObject = new MediaStream([e.track]);
            video.muted = true; video.play();
            const root = document.createElement('div');
            root.id = 'teleport-video-123456';
            root.append(video); document.body.append(root);
          } else {
            ac.createMediaStreamSource(new MediaStream([e.track])).connect(ac.destination);
            const audio = document.createElement('audio');
            audio.srcObject = new MediaStream([e.track]);
            audio.play(); document.body.append(audio);
          }
        };
        for (const track of stream.getTracks()) sender.addTrack(track, stream);
        const transceiver = sender.getTransceivers().find(t => t.sender.track.kind === 'video');
        transceiver.setCodecPreferences(RTCRtpSender.getCapabilities('video').codecs.filter(
          c => c.mimeType.toLowerCase() === codec.toLowerCase()));
        await sender.setLocalDescription(await sender.createOffer());
        await receiver.setRemoteDescription(sender.localDescription);
        await receiver.setLocalDescription(await receiver.createAnswer());
        await sender.setRemoteDescription(receiver.localDescription);
        window.bitrate = bitrate;
        window.fixture = {canvas, sender, receiver};
      }, {codec, bitrate});
      await page.waitForFunction(() => {
        const info = window.__slRec.info();
        return info.hasVideo && info.hasAudio;
      });
      await page.evaluate(() => {
        const canvas = document.createElement('canvas');
        canvas.width = 960; canvas.height = 720;
        canvas.getContext('2d').fillRect(0, 0, 960, 720);
        const video = document.createElement('video');
        video.dataset.pipChannel = 'recommendation'; video.srcObject = canvas.captureStream(30);
        video.muted = true; video.play(); document.body.append(video);
        const ac = new AudioContext(); const dest = ac.createMediaStreamDestination();
        const osc = ac.createOscillator(); osc.frequency.value = 880;
        osc.connect(dest); osc.start();
        ac.createMediaStreamSource(dest.stream).connect(ac.destination);
      });
      const selected = await page.evaluate(() => window.__slRec.info());
      assert.equal(selected.width, 320, 'selecionou a recomendacao maior');
      assert.equal(selected.audioTrackId, await page.evaluate(() =>
        window.fixture.receiver.getReceivers().find(r => r.track.kind === 'audio').track.id));
      await page.evaluate(() => {
        document.querySelector('#teleport-video-123456 video').dataset.pipChannel = 'recycled';
        window.__slRec.arm({livestreamId:'123456', channelId:'recycled'});
      });
      assert.equal(await page.evaluate(() => window.__slRec.info().hasVideo), false,
        'elemento reciclado nao pode atribuir o RTC antigo a outro canal');
      await page.evaluate(() => {
        document.querySelector('#teleport-video-123456 video').dataset.pipChannel = 'target';
        window.__slRec.arm({livestreamId:'123456', channelId:'target'});
      });
      const started = await page.evaluate(() => window.__slRec.start({
        videoBitrate: window.bitrate,
        mimeTypes: ['video/webm;codecs=h264,opus', 'video/webm;codecs=vp8,opus'],
      }));
      assert.equal(started.ok, true, JSON.stringify(started));
      assert.equal(await page.evaluate(() => window.__slRec.quiet), true);
      assert.equal(await page.evaluate(() => document.querySelector('#teleport-video-123456 video').paused), true);
      await page.waitForFunction(() => window.__slRec.info().bytes > 0, null, {timeout: 10000, polling: 100});
      const progress = await page.evaluate(() => window.__slRec.videoStats());
      assert.ok(progress.frames > 0, JSON.stringify(progress));
      const originalTrack = await page.evaluate(() => window.__slRec.info().videoTrackId);
      await page.evaluate(() => {
        const target = document.querySelector('#teleport-video-123456 video');
        // Simula o processamento do site sem trocar a sala nem o RTC de origem.
        const decoder = document.createElement('video');
        decoder.srcObject = target.srcObject; decoder.muted = true;
        decoder.play(); document.body.append(decoder);
        const canvas = document.createElement('canvas');
        canvas.width = 160; canvas.height = 120;
        canvas.getContext('2d').fillRect(0, 0, 160, 120);
        target.srcObject = canvas.captureStream(30); target.play();
      });
      assert.equal(await page.evaluate(() => window.__slRec.info().videoTrackId), originalTrack);
      assert.equal(await page.evaluate(() => window.__slRec.info().identityValid), true);
      await page.waitForTimeout(3500);
      if (codec === 'video/H264' && bitrate === 0) {
        await page.evaluate(() => {
          window.fixture.canvas.width = 640; window.fixture.canvas.height = 360;
        });
        await page.waitForFunction(async () => {
          const video = await window.__slRec.videoStats();
          return video.width === 640 && video.height === 360;
        }, null, {polling: 100});
        await page.waitForTimeout(2000);
      }
      if (bitrate > 0) {
        // Outro video vivo nao pode esconder o encerramento do track gravado.
        await page.evaluate(() => {
          const canvas = document.createElement('canvas');
          canvas.width = 640; canvas.height = 480;
          canvas.getContext('2d').fillRect(0, 0, 640, 480);
          const video = document.createElement('video');
          video.srcObject = canvas.captureStream(30);
          video.muted = true; video.play(); document.body.append(video);
          window.__slRec.video.stop();
        });
        await page.waitForFunction(() => window.__slRec.info().state === 'stopped', null, {polling: 100});
        assert.equal(await page.evaluate(() => window.__slRec.info().hasVideo), false);
      } else if (codec === 'video/VP8') {
        await page.evaluate(() => {
          document.querySelector('#teleport-video-123456 video').dataset.pipChannel = 'other-room';
        });
        await page.waitForFunction(() => window.__slRec.info().state === 'stopped', null, {polling: 100});
        assert.match(await page.evaluate(() => window.__slRec.info().reason), /identidade/);
      } else {
        assert.ok((await page.evaluate(() => window.__slRec.videoStats())).frames > progress.frames);
        await page.evaluate(() => window.__slRec.stop());
      }
      // Aguarda tambem o ultimo dataavailable, mesmo se o timer ja pediu a parada.
      await page.evaluate(() => window.__slRec.stop());
      const info = await page.evaluate(() => window.__slRec.info());
      const blob = await page.evaluate(() => window.__slRec.drain());
      assert.equal(info.state, 'stopped');
      assert.ok(info.bytes > 2048, JSON.stringify(info));
      assert.equal(await page.evaluate(() => window.__slRec.drain()), '');
      const file = path.join(tmp, `clip-${codec.split('/')[1]}-${bitrate}.mkv`);
      fs.writeFileSync(file, Buffer.from(blob, 'base64'));
      const remuxed = file.replace('.mkv', '-remuxed.mkv');
      execFileSync('ffmpeg', ['-v', 'error', '-y', '-i', file, '-c', 'copy', remuxed]);
      const probe = JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-show_streams',
        '-show_format', '-of', 'json', remuxed], {encoding: 'utf8'}));
      assert.equal(probe.streams.find(s => s.codec_type === 'video').codec_name,
        codec === 'video/H264' ? 'h264' : 'vp8');
      assert.equal(probe.streams.find(s => s.codec_type === 'audio').codec_name, 'opus');
      assert.ok(Number(probe.format.duration) >= 2.5, probe.format.duration);
      assert.match(info.mimeType, codec === 'video/H264' ? /avc|h264/i : /vp8/i);
      if (codec === 'video/H264' && bitrate === 0) {
        const normalized = file.replace('.mkv', '-normalized.mkv');
        execFileSync('ffmpeg', ['-v', 'error', '-y', '-i', file, '-vf',
          'scale=320:240:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=320:240:(ow-iw)/2:(oh-ih)/2,setsar=1',
          '-c:v', 'libx264', '-preset', 'veryfast', '-fps_mode', 'vfr', '-c:a', 'aac', normalized]);
        const frames = JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-select_streams', 'v:0',
          '-show_entries', 'frame=width,height,best_effort_timestamp_time', '-of', 'json', normalized],
          {encoding: 'utf8'})).frames;
        assert.ok(frames.length > 150, 'video perdido apos mudar a resolucao');
        assert.ok(Number(frames.at(-1).best_effort_timestamp_time) > 5.3);
        assert.ok(frames.every(f => f.width === 320 && f.height === 240));
        execFileSync('ffmpeg', ['-v', 'error', '-xerror', '-i', normalized, '-f', 'null', '-']);
        console.log('ok mudanca de resolucao durante o mesmo MediaRecorder');
      }
      console.log(`ok ${codec} bitrate=${bitrate}: ${info.mimeType}, ${info.bytes}B`);
      await page.close();
    }
  } finally {
    await browser.close();
    fs.rmSync(tmp, {recursive: true, force: true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
