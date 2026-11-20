"""Captura HTTP sem navegador: seleção, limites, privacidade e recuperação do prefixo."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlsplit,parse_qs
from unittest.mock import AsyncMock,patch

import httpx

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from recorder import Live
from stripchat_capture import DirectUnavailable,hls_playlist,parse_model_info
from stripchat_recorder import DirectSession,StripchatRecorder


ROOT='https://edge-hls.example.org/hls/111/'
INIT=b'\x00\x00\x00\x0cftypavc1'
MEDIA=b'\x00\x00\x00\x0cmoofdata'


def profile():
    return {'username':'alvo','id':111,'stream_name':'111','recordable':True,'status':'public',
            'live':True,'hls_urls':[ROOT+'master/111_auto.m3u8']}


def playlist(start=1,count=2,discontinuity=0,init='init.mp4'):
    return ('#EXTM3U\n#EXT-X-TARGETDURATION:1\n'
        f'#EXT-X-MEDIA-SEQUENCE:{start}\n#EXT-X-DISCONTINUITY-SEQUENCE:{discontinuity}\n'
        +(f'#EXT-X-MAP:URI="{init}"\n' if init else '')
        +''.join(f'#EXTINF:1,\nx_{n}_source_111.mp4\n' for n in range(start,start+count)))


class PlaylistTests(unittest.TestCase):
    def test_descobre_hosts_do_proprio_site_e_seleciona_qualidade_original(self):
        html=json.dumps({'viewCamBase':{'model':dict(id=111,username='alvo',status='public',isLive=True,topic='Minha live')},
            'configV3':{'static':{'features':{'hlsFallback':{'fallbackDomains':[
                'example.org','example.org','evil.test/path','user@evil.test']}}}}})
        self.assertEqual(parse_model_info(html,'alvo')['hls_urls'],[ROOT+'master/111_auto.m3u8'])
        self.assertEqual(parse_model_info(html,'alvo')['title'],'Minha live')
        master='#EXTM3U\n#EXT-X-MOUFLON-ADVERT:metadata\n#EXT-X-STREAM-INF:BANDWIDTH=1\nlow.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=9\nhigh.m3u8\n'
        variant=hls_playlist(master,ROOT+'master.m3u8')[0]
        self.assertEqual(urlsplit(variant).path,'/hls/111/high.m3u8')
        self.assertEqual(parse_qs(urlsplit(variant).query)['preferredVideoCodec'],['h264'])

    def test_mouflon_v2_seleciona_chave_e_restaura_url_entre_cdns(self):
        master=('#EXTM3U\n#EXT-X-MOUFLON:PSCH:v2:unknown\n#EXT-X-MOUFLON:PSCH:v2:OLzu7QlySkG2fVRn\n'
                '#EXT-X-STREAM-INF:BANDWIDTH=9\nhttps://media-hls.example.org/b-hls-24/111/111.m3u8?playlistType=lowLatency\n')
        variant=hls_playlist(master,ROOT+'master/111_auto.m3u8')[0]
        query=parse_qs(urlsplit(variant).query)
        self.assertEqual((query['psch'],query['pkey'],query['playlistType']),
                         (['v2'],['OLzu7QlySkG2fVRn'],['standard']))
        media=('#EXTM3U\n#EXT-X-MAP:URI="x_init_111.mp4"\n#EXTINF:1,\n'
               '#EXT-X-MOUFLON:URI:https://media-hls.example.org/b-hls-24/111/x_1_Ykz+0DiX_111.mp4\n../media.mp4\n')
        segment=hls_playlist(media,variant)[1][0]
        self.assertEqual(segment[3],'https://media-hls.example.org/b-hls-24/111/x_1_source_111.mp4')

    def test_mouflon_v1_e_chave_ausente(self):
        media=('#EXTM3U\n#EXT-X-MOUFLON:PSCH:v1:OLzu7QlySkG2fVRn\n#EXTINF:1,\n'
               '#EXT-X-MOUFLON:FILE:HH6wpiIiD+LAqjhqYZkr/wd1Q61xzQ==\n../media.mp4\n')
        self.assertEqual(hls_playlist(media,ROOT+'index.m3u8')[1][0][3],ROOT+'x_1_source_111.mp4')
        with self.assertRaisesRegex(DirectUnavailable,'mouflon_key_missing'):
            hls_playlist(media.replace('OLzu7QlySkG2fVRn','unknown'),ROOT+'index.m3u8')

    def test_rejeita_preview_finito_criptografia_e_formatos_nao_implementados(self):
        for extra in ('#EXT-X-ENDLIST','#EXT-X-MOUFLON:abc','#EXT-X-KEY:METHOD=AES-128,URI="key"',
                      '#EXT-X-BYTERANGE:12','#EXT-X-GAP','#EXT-X-SKIP:SKIPPED-SEGMENTS=3'):
            with self.subTest(extra=extra),self.assertRaises(DirectUnavailable):
                hls_playlist(playlist()+extra,ROOT+'index.m3u8')
        for text in ('html','#EXTM3U\n#EXT-X-MAP:bad\n','#EXTM3U\n#EXTINF:bad\nx.mp4'):
            with self.subTest(text=text),self.assertRaises(DirectUnavailable):
                hls_playlist(text,ROOT+'index.m3u8')


class DirectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name);self.outputs=[];self.requests=[];self.stage=0;self.private=False
        self.owner=StripchatRecorder(Live('alvo','','',0,''),self.folder,part_limit=100,segment_bytes=35,
                                    stop_event=threading.Event())
        self.owner.live.livestream_id='live-1'
        self.owner.capture_allowed=lambda live_id:True
        self.owner.segment_callback=lambda path,meta:self.outputs.append((path.read_bytes(),meta))
        self.parts=0
        def base():
            self.parts+=1;folder=self.folder/str(self.parts);folder.mkdir();return folder/'capture'
        self.owner._recording_base=base
        self.session=DirectSession(self.owner,profile())
        self.real_client=httpx.AsyncClient

    def client(self,*args,**kwargs):
        return self.real_client(*args,**kwargs,transport=httpx.MockTransport(self.handler))

    def handler(self,request):
        self.requests.append(str(request.url));path=request.url.path
        if '/master/' in path:
            data='#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\n../index.m3u8\n'
        elif path.endswith('.m3u8'):
            data=playlist(1 if self.stage==0 else 2 if self.stage==1 else 3,
                          discontinuity=int(self.stage>=2),init='new-init.mp4' if self.stage>=2 else 'init.mp4')
        elif path.endswith('init.mp4'):data=INIT+(b'new' if 'new-init' in path else b'')
        else:data=MEDIA
        return httpx.Response(200,content=data)

    def access(self,*args):
        info=profile()
        if self.private:info.update(recordable=False,status='private')
        return info

    async def run_capture(self,pause):
        self.owner.pause=pause
        with patch('stripchat_recorder.httpx.AsyncClient',side_effect=self.client),\
             patch('stripchat_recorder.model_info',side_effect=self.access):
            await self.session.run()

    async def test_sem_player_sem_duplicatas_com_cortes_e_init_novo(self):
        async def pause(seconds):
            self.stage+=1
            if self.stage==3:self.owner.stop_event.set()
        await self.run_capture(pause)
        self.assertEqual([data for data,_ in self.outputs],[INIT+MEDIA*2,INIT+MEDIA,INIT+b'new'+MEDIA])
        self.assertTrue(all(meta['username']=='stripchat:alvo' for _,meta in self.outputs))
        self.assertEqual(sum('x_2_' in url for url in self.requests),1)
        self.assertTrue(all(len(data)<=100 for data,_ in self.outputs))
        self.assertFalse(self.owner.progress()['recording'])

    async def test_sem_config_no_html_busca_api_estatica(self):
        self.session.info['hls_urls']=[]
        handler=self.handler
        def api(request):
            if request.url.path=='/api/front/v3/config/static':
                self.requests.append(str(request.url))
                return httpx.Response(200,json={'static':{'hlsStreamHost':'example.org'}})
            return handler(request)
        self.handler=api
        async def pause(seconds):self.owner.stop_event.set()
        await self.run_capture(pause)
        self.assertTrue(any('/api/front/v3/config/static' in url for url in self.requests))
        self.assertEqual([data for data,_ in self.outputs],[INIT+MEDIA*2])

    async def test_master_edge_hls_midia_em_outro_cdn_e_diretorio(self):
        handler=self.handler
        def cdn(request):
            if '/master/' in request.url.path:
                return httpx.Response(200,content=('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\n'
                    'https://media-hls.example.org/b-hls-24/111/index.m3u8\n'))
            return handler(request)
        self.handler=cdn
        async def pause(seconds):self.owner.stop_event.set()
        await self.run_capture(pause)
        self.assertTrue(any('media-hls.example.org/b-hls-24/111/x_1_' in url for url in self.requests))
        self.assertEqual([data for data,_ in self.outputs],[INIT+MEDIA*2])

    async def test_stop_fecha_parte_sem_baixar_proximo_segmento(self):
        async def pause(seconds):
            self.owner.capture_allowed=lambda live_id:False
            self.owner.request_part()
        await self.run_capture(pause)
        self.assertEqual([data for data,_ in self.outputs],[INIT+MEDIA*2])
        self.assertFalse(any('x_3_' in url for url in self.requests))

    async def test_privada_ao_obter_vaga_nao_grava_dados_baixados(self):
        base=self.owner._recording_base
        def blocked_base():
            self.private=True
            return base()
        self.owner._recording_base=blocked_base
        await self.run_capture(AsyncMock())
        self.assertEqual([data for data,_ in self.outputs],[b''])
        self.assertEqual(self.owner.progress()['access'],'private')

    async def test_falha_de_rede_preserva_prefixo(self):
        handler=self.handler
        def failing(request):
            if 'x_2_' in request.url.path:raise httpx.ReadError('offline',request=request)
            return handler(request)
        self.handler=failing
        with self.assertRaises(DirectUnavailable):await self.run_capture(AsyncMock())
        self.assertEqual([data for data,_ in self.outputs],[INIT+MEDIA])

    async def test_lacuna_do_cdn_e_registrada_sem_finalizar_live_ainda_publica(self):
        handler=self.handler
        def gap(request):
            if self.stage and request.url.path.endswith('.m3u8'):
                return httpx.Response(200,content=playlist(9))
            return handler(request)
        self.handler=gap
        self.owner.segment_bytes=90
        async def pause(seconds):
            self.stage+=1
            self.assertEqual(self.outputs,[])
            if self.stage==2:self.owner.stop_event.set()
        with self.assertLogs('stripchat_recorder',level='WARNING') as logs:await self.run_capture(pause)
        self.assertEqual([data for data,_ in self.outputs],[INIT+MEDIA*4])
        self.assertIn('ausentes=6',logs.output[0])

    async def test_fim_de_um_perfil_nao_corta_nem_publica_outro(self):
        ended=asyncio.Event();other=[]
        owner=StripchatRecorder(Live('outro','','',0,''),self.folder,part_limit=1000,segment_bytes=900,
                               stop_event=threading.Event())
        owner.live.livestream_id='live-2';owner.capture_allowed=lambda live_id:True
        owner._recording_base=lambda:self.folder/'other'
        owner.segment_callback=lambda path,meta:other.append((path.read_bytes(),meta))
        info=profile();info.update(username='outro',id=222,stream_name='222',
                                  hls_urls=[ROOT.replace('/111/','/222/')+'master/222_auto.m3u8'])
        second=DirectSession(owner,info);stage=0
        async def first_pause(seconds):
            self.session.public=False;self.owner.access('offline',False)
        async def second_pause(seconds):
            nonlocal stage
            await ended.wait()
            self.assertTrue(owner.progress()['recording']);self.assertEqual(other,[])
            stage+=1
            if stage==2:owner.stop_event.set()
        callback=self.owner.segment_callback
        loop=asyncio.get_running_loop()
        def first_publish(path,meta):
            callback(path,meta)
            loop.call_soon_threadsafe(ended.set)
        self.owner.segment_callback=first_publish
        self.owner.pause=first_pause;owner.pause=second_pause
        def handler(request):
            if '/222/' not in request.url.path:return self.handler(request)
            data=playlist(1 if not stage else 3).replace('_111','_222') if request.url.path.endswith('.m3u8') else INIT if 'init' in request.url.path else MEDIA
            return httpx.Response(200,content=data)
        def client(*args,**kwargs):return self.real_client(*args,**kwargs,transport=httpx.MockTransport(handler))
        with patch('stripchat_recorder.httpx.AsyncClient',side_effect=client),\
             patch.object(self.session,'refresh_access',new=AsyncMock(return_value=True)),\
             patch.object(second,'refresh_access',new=AsyncMock(return_value=True)):
            await asyncio.wait_for(asyncio.gather(self.session.run(),second.run()),5)
        self.assertEqual(len(self.outputs),1);self.assertEqual(len(other),1)
        self.assertEqual(other[0][1]['username'],'stripchat:outro')
        self.assertEqual(other[0][1]['live_id'],'live-2')
        self.assertEqual(other[0][0],INIT+MEDIA*4)

    async def test_limita_respostas_e_rejeita_redirecionamentos_ou_outro_perfil(self):
        self.session.root=ROOT
        async with self.real_client(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=b'12345'))) as client:
            self.session.client=client
            with self.assertRaises(DirectUnavailable):await self.session.fetch(ROOT+'x',4)
            for url in ('https://evil.example/hls/111/x',ROOT.replace('/111/','/222/')+'x',
                        ROOT+'%2e%2e/222/x','file:///hls/111/x',ROOT+'cpa/video.mp4',
                        ROOT+'media.mp4','https://edge-hls.example.org/x_111_source_222.mp4'):
                with self.subTest(url=url),self.assertRaises(DirectUnavailable):await self.session.fetch(url)
        async with self.real_client(transport=httpx.MockTransport(lambda r:httpx.Response(302,headers={'location':ROOT+'x'}))) as client:
            self.session.client=client
            with self.assertRaises(httpx.HTTPStatusError):await self.session.fetch(ROOT+'redirect')
        async with self.real_client(transport=httpx.MockTransport(lambda r:httpx.Response(206,content=b'partial'))) as client:
            self.session.client=client
            with self.assertRaisesRegex(DirectUnavailable,'partial_response'):await self.session.fetch(ROOT+'partial')

    async def test_mpeg_ts_sem_recodificar_e_sem_init(self):
        packet=b'\x47'+b'\0'*187
        def ts(request):
            return httpx.Response(200,content=playlist(init='') if request.url.path.endswith('.m3u8') else packet)
        self.handler=ts;self.owner.segment_bytes=500;self.owner.part_limit=1000
        async def pause(seconds):self.owner.stop_event.set()
        await self.run_capture(pause)
        self.assertEqual([data for data,_ in self.outputs],[packet*2])
        self.assertEqual(self.outputs[0][1]['mime'],'video/mp2t')

    async def test_playlist_incompativel_nao_abre_navegador(self):
        self.owner.identity_callback=lambda uid:None
        self.owner.live_id_callback=lambda token,online:'live-1'
        async def pause(seconds):self.owner.stop_event.set()
        self.owner.pause=pause
        with patch('stripchat_recorder.model_info',return_value=profile()),\
             patch('stripchat_recorder.DirectSession.run',side_effect=DirectUnavailable('unsupported_playlist')),\
             patch('stripchat_recorder.Session.run',new=AsyncMock()) as browser:
            with self.assertLogs('stripchat_recorder',level='WARNING') as logs:await self.owner.monitor()
            browser.assert_not_awaited()
            self.assertIn('unsupported_playlist',logs.output[0])
