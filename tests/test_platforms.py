"""Identidades, inscrições e sessões independentes, incluindo bancos anteriores."""
import asyncio
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from platforms import profile_key,split_profile
from test_telegram import RegistryFixture,BlockingRecorder,until


class PlatformTests(RegistryFixture):
    async def test_mesmo_nome_compartilha_apenas_na_mesma_plataforma_e_preserva_estado(self):
        with patch('monitor_service.StripchatRecorder',BlockingRecorder):
            self.registry.add(1,10,'@Alvo','superlive')
            self.registry.add(1,10,'@Alvo','stripchat')
            self.registry.add(2,20,'alvo','stripchat')
            self.assertFalse(self.registry.add(2,20,'ALVO','stripchat')[1])
            await until(lambda:BlockingRecorder.active==2)
            self.assertEqual(set(self.registry.monitors),{'alvo','stripchat:alvo'})
            self.assertEqual(self.store.recipients('alvo'),{1:10})
            self.assertEqual(self.store.recipients('stripchat:alvo'),{1:10,2:20})
            with self.assertRaisesRegex(ValueError,'multiple platforms'):
                self.registry.stop(1,10,'alvo')
            self.registry.stop(1,10,'stripchat:alvo')
            self.assertEqual(self.store.recipients('stripchat:alvo','123456'),{2:20})
            self.assertEqual(self.store.recipients('alvo','123456'),{1:10})
            rows=self.registry.details(1,10)
            self.assertEqual({r['platform'] for r in rows},{'superlive','stripchat'})
            self.assertIn('alvo · StripChat',rows[1]['text'])
            self.registry.set_enabled(1,10,'superlive:alvo',False)
            await until(lambda:BlockingRecorder.active==1)
            await self.registry.close()
            from monitor_service import MonitorRegistry
            self.registry=MonitorRegistry(self.config,self.store,self.delivery)
            await self.registry.start()
            await until(lambda:BlockingRecorder.active==1)
            self.assertFalse(self.store.subscription(1,'alvo')['enabled'])
            self.assertEqual(self.store.subscription(1,'stripchat:alvo')['stopped_live_id'],'123456')
            self.registry.remove(1,10,'stripchat:alvo')
            self.assertEqual(self.store.recipients('stripchat:alvo'),{2:20})
            self.assertEqual(self.store.subscriptions(1),['alvo'])

    async def test_urls_identidade_fixa_e_quota_contam_as_duas_plataformas(self):
        for bad in ('https://evilstripchat.com/alvo','https://stripchat.com.evil.org/alvo',
                    'https://user:secret@stripchat.com/alvo','https://stripchat.com/a/b'):
            with self.assertRaises(ValueError):profile_key(bad,'stripchat')
        with self.assertRaises(ValueError):profile_key('stripchat:alvo','superlive')
        self.assertEqual(profile_key('https://pt.stripchat.com/Alvo?ignored=1'),'stripchat:alvo')
        self.assertEqual(split_profile('antigo'),('superlive','antigo'))
        with patch('monitor_service.StripchatRecorder',BlockingRecorder):
            for platform,name in (('superlive','alvo'),('stripchat','alvo'),('stripchat','outro')):
                self.registry.add(1,10,name,platform)
            with self.assertRaisesRegex(ValueError,'Profile limit of 3'):self.registry.add(1,10,'extra','stripchat')
            self.store.fix_identity('alvo','111');self.store.fix_identity('stripchat:alvo','222')
            with self.assertRaises(ValueError):self.store.fix_identity('stripchat:alvo','111')

    async def test_sessao_stripchat_persiste_e_muda_somente_apos_offline_ou_novo_fluxo(self):
        self.store.add(1,10,'stripchat:alvo')
        original=self.store.observed_live('stripchat:alvo','111',True)
        self.assertEqual(self.store.observed_live('stripchat:alvo','111',True),original)
        self.store.stop_live(1,'stripchat:alvo',original,'')
        self.assertFalse(self.store.recipients('stripchat:alvo',original))
        self.store.observed_live('stripchat:alvo','111',False)
        fresh=self.store.observed_live('stripchat:alvo','111',True)
        self.assertNotEqual(fresh,original)
        self.assertEqual(self.store.recipients('stripchat:alvo',fresh),{1:10})
        self.assertNotEqual(self.store.observed_live('stripchat:alvo','new-stream',True),fresh)
