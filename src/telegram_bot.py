"""Private commands, persistent language selection and owner-bound controls."""
import logging
import uuid
from datetime import datetime
from html import escape
from telegram import Update,InlineKeyboardButton,InlineKeyboardMarkup,BotCommandScopeAllPrivateChats,BotCommandScopeChat
from telegram.error import BadRequest,Conflict,TelegramError
from telegram.ext import Application,CallbackQueryHandler,CommandHandler,ContextTypes,MessageHandler,TypeHandler,filters
from traffic_control import TrafficGuard
from config import Config
from monitor_service import MonitorRegistry
from platforms import PLATFORMS,split_profile
from i18n import tr,decimal,date_format,UserError

log=logging.getLogger(__name__)
BOT_COMMANDS=(('start','Start and show help'),('add','Add a profile'),('remove','Remove a profile'),
    ('stop','Send the current part and keep recording'),('monitor','View and manage your profiles'),
    ('stats','View your recording statistics'))
PLATFORM_ICONS={'superlive':'🔵','stripchat':'🔴','kick':'🟢','tiktok':'🟣'}
SUPERLIVE_ID_HELP='Send the username, profile URL, ID or sharing ID.\n\n⚠️ Live stream IDs are not accepted.'


async def configure_command_menu(bot,language='en',chat_id=None):
    try:
        scope=BotCommandScopeAllPrivateChats() if chat_id is None else BotCommandScopeChat(chat_id)
        await bot.set_my_commands([(name,tr(language,label)) for name,label in BOT_COMMANDS],scope=scope)
    except TelegramError as exc:
        log.warning('command menu unavailable error=%s',type(exc).__name__)


class Commands:
    def __init__(self,registry:MonitorRegistry):self.registry=registry

    @staticmethod
    def private(update):
        return bool(update.effective_user and update.effective_message and update.effective_chat
                    and update.effective_chat.type=='private')

    def language(self,user_id):return self.registry.store.language(user_id)

    def t(self,user_id,message,**values):return tr(self.language(user_id),message,**values)

    def error(self,user_id,exc):
        return exc.translated(self.language(user_id)) if isinstance(exc,UserError) else self.t(
            user_id,'The command could not be completed. Please try again shortly.')

    @staticmethod
    def clear_pending(context):
        for key in ('pending_command','pending_platform','platform_choice'):context.user_data.pop(key,None)

    async def start(self,update,context):
        if not self.private(update):return
        uid,chat=update.effective_user.id,update.effective_chat.id
        self.registry.user(uid,chat);self.clear_pending(context)
        await self.send_start(update,True)

    async def send_start(self,update,choose_language):
        uid=update.effective_user.id
        def t(message,**values):return self.t(uid,message,**values)
        paragraphs=[t('👋 <b>Welcome!</b>'),
            t('Your favorite live streams, recorded and delivered right here on Telegram.'),
            ' · '.join(f'{PLATFORM_ICONS[key]} {label}' for key,label in PLATFORMS.items()),
            t('Add a profile and I will record its live streams automatically. Even while the profile is offline, I keep monitoring for its next stream. You can pause or resume monitoring whenever you want.'),
            t('🧭 <b>Commands</b>\n/add — add a profile to follow\n/monitor — view your profiles and manage monitoring\n/stop — receive the current part and keep recording\n/remove — remove a profile from your list\n/stats — view your recording statistics\n/start — see this welcome message and choose your language'),
            t('🚀 Start with /add: choose a platform and send the profile you want to follow.')]
        markup=None
        if choose_language:
            paragraphs.append(t('Choose your language:'))
            markup=InlineKeyboardMarkup([[InlineKeyboardButton('🇺🇸 Continue in English',callback_data=f'language:{uid}:en'),
                InlineKeyboardButton('🇧🇷 Continuar em Português',callback_data=f'language:{uid}:pt')]])
        await update.effective_message.reply_text('\n\n'.join(paragraphs),parse_mode='HTML',reply_markup=markup)

    async def choose_language(self,update,context):
        query=update.callback_query
        if not self.private(update):
            await query.answer(self.t(update.effective_user.id,'Use these controls in a private chat.'),show_alert=True);return
        _,owner,language=query.data.split(':')
        uid=update.effective_user.id
        if int(owner)!=uid:
            await query.answer(self.t(uid,'This panel belongs to another user.'),show_alert=True);return
        self.registry.user(uid,update.effective_chat.id)
        self.registry.store.set_language(uid,language);self.clear_pending(context)
        await query.answer(self.t(uid,'Language saved.'))
        try:await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest:pass
        await configure_command_menu(context.bot,language,update.effective_chat.id)
        await self.send_start(update,False)

    @staticmethod
    def duration(seconds):
        seconds=max(0,int(seconds))
        return f'{seconds//3600:02d}:{seconds%3600//60:02d}:{seconds%60:02d}'

    def stats_panel(self,user_id,chat_id):
        rows=self.registry.details(user_id,chat_id);data=self.registry.store.statistics(user_id)
        language=self.language(user_id)
        def t(message,**values):return tr(language,message,**values)
        def gb(size):return decimal(language,size/1e9)+' GB'
        groups=[t('📈 <b>Your statistics</b>'), '\n'.join([
            t('<b>Recorded time:</b> {value}',value=self.duration(data['seconds'])),
            t('<b>Recorded volume:</b> {value}',value=gb(data['bytes'])),
            t('<b>Recorded lives:</b> {value}',value=data['lives']),
            t('<b>Profiles with recordings:</b> {value}',value=data['profiles'])]), '\n'.join([
            t('<b>Videos received:</b> {value}',value=str(data['received'])+' · '+gb(data['sent_bytes'])),
            t('<b>Received time:</b> {value}',value=self.duration(data['sent_seconds'])),
            t('<b>Longest video received:</b> {value}',value=self.duration(data['longest'])),
            t('<b>Queued deliveries:</b> {value}',value=data['pending']),
            t('<b>Failed/unconfirmed deliveries:</b> {value}',value=data['failures'])]),'\n'.join([
            t('<b>Registered profiles:</b> {value}',value=data['registered']),
            t('<b>Monitoring enabled:</b> {value}',value=data['enabled']),
            t('<b>Paused:</b> {value}',value=data['registered']-data['enabled']),
            t('<b>Recording now:</b> {value}',value=sum(row['recording'] for row in rows))])]
        top='\n'.join(f'{rank}. {escape(self.registry.store.profile_label(profile))} — {self.duration(seconds)}'
            for rank,(profile,seconds) in enumerate(data['top'],1)) if data['top'] else t('Your history starts with your first completed recording.')
        groups.append(t('<b>Most recorded by time</b>')+'\n'+top)
        if data['first'] is not None:
            groups.append(t('<b>First recording:</b> {date}',date=datetime.fromtimestamp(data['first'],self.registry.zone).strftime(date_format(language)))+
                '\n'+t('<b>Latest recording:</b> {date}',date=datetime.fromtimestamp(data['last'],self.registry.zone).strftime(date_format(language))))
        groups.append(t('Totals include completed recordings; the current part is included after processing.'))
        if data['estimated']:groups.append(t('Duration estimated for {count} older recording(s).',count=data['estimated']))
        groups.append(t('🕒 Updated at {time}',time=datetime.now(self.registry.zone).strftime('%H:%M:%S')))
        return '\n\n'.join(groups),InlineKeyboardMarkup([[InlineKeyboardButton(t('🔄 Refresh statistics'),callback_data=f'stats:{user_id}')]])

    async def stats(self,update,context):
        if not self.private(update):return
        uid=update.effective_user.id
        if context.args:
            await update.effective_message.reply_text(self.t(uid,'Send only /stats to view your history.'));return
        self.clear_pending(context)
        text,markup=self.stats_panel(uid,update.effective_chat.id)
        await update.effective_message.reply_text(text,reply_markup=markup,parse_mode='HTML')

    async def refresh_summary(self,update,context):
        query=update.callback_query;uid=update.effective_user.id
        if not self.private(update):
            await query.answer(self.t(uid,'Use these controls in a private chat.'),show_alert=True);return
        panel,owner=query.data.split(':',1)
        if int(owner)!=uid:
            await query.answer(self.t(uid,'This panel belongs to another user.'),show_alert=True);return
        if panel in ('limits','limite'):
            await query.answer(self.t(uid,'This panel has expired. Use /monitor.'),show_alert=True)
            return
        text,markup=self.stats_panel(uid,update.effective_chat.id)
        await query.answer(self.t(uid,'Statistics refreshed.'))
        try:await query.edit_message_text(text,reply_markup=markup,parse_mode='HTML')
        except BadRequest as exc:
            if 'message is not modified' not in str(exc).casefold():raise

    async def target(self,update,context,command):
        context.user_data['pending_command']=(update.effective_chat.id,command)
        if command!='add':
            context.user_data.pop('pending_platform',None);context.user_data.pop('platform_choice',None)
        if len(context.args or [])==1:return context.args[0]
        uid=update.effective_user.id
        if command=='add':
            nonce=uuid.uuid4().hex;context.user_data['platform_choice']=nonce
            context.user_data.pop('pending_platform',None)
            buttons=[InlineKeyboardButton(f'{PLATFORM_ICONS[key]} {label}',callback_data=f'platform:{nonce}:{key}') for key,label in PLATFORMS.items()]
            await update.effective_message.reply_text(self.t(uid,'📺 Which platform would you like to monitor?'),
                reply_markup=InlineKeyboardMarkup([buttons[:2],buttons[2:]]))
            return None
        question={'remove':'➖ Which profile would you like to remove?',
                  'stop':'📤 Which profile should send the current part and keep recording?'}[command]
        prompt='Send the username, profile URL or the ID shown in /monitor.\n\nTo cancel, use /start or /monitor.'
        await update.effective_message.reply_text(self.t(uid,question)+'\n'+self.t(uid,prompt))
        return None

    async def platform_choice(self,update,context):
        query=update.callback_query;uid=update.effective_user.id
        if not self.private(update):
            await query.answer(self.t(uid,'Use these controls in a private chat.'),show_alert=True);return
        _,nonce,platform=query.data.split(':',2)
        if (context.user_data.get('pending_command')!=(update.effective_chat.id,'add')
                or context.user_data.get('platform_choice')!=nonce or platform not in PLATFORMS):
            await query.answer(self.t(uid,'This button expired. Send /add again.'),show_alert=True);return
        context.user_data.pop('platform_choice',None);context.user_data['pending_platform']=platform
        await query.answer()
        prompt=SUPERLIVE_ID_HELP if platform=='superlive' else 'Send the username or profile URL.'
        await query.edit_message_text(self.t(uid,'👤 Which profile would you like to monitor on {platform}?',platform=PLATFORMS[platform])+
            '\n'+self.t(uid,prompt)+'\n\n'+self.t(uid,'To cancel, use /start or /monitor.'))

    async def parameter(self,update,context):
        if not self.private(update):return
        pending=context.user_data.get('pending_command')
        if not pending or pending[0]!=update.effective_chat.id:return
        if pending[1]=='add' and 'platform_choice' in context.user_data:
            await update.effective_message.reply_text(self.t(update.effective_user.id,'Choose a platform using the buttons above first.'));return
        context.args=[update.effective_message.text.strip()]
        await getattr(self,pending[1])(update,context)

    async def add(self,update,context):
        if not self.private(update):return
        uid=update.effective_user.id
        if update.effective_message.text.startswith('/'):context.user_data.pop('pending_platform',None)
        target=await self.target(update,context,'add')
        if target is None:return
        try:
            target,user=await self.registry.resolve_add_target(target,context.user_data.get('pending_platform'))
            username,added=self.registry.add(uid,update.effective_chat.id,target,context.user_data.get('pending_platform'))
            if user:self.registry.store.update_superlive_profile(username,user)
        except ValueError as exc:
            await update.effective_message.reply_text('⚠️ '+self.error(uid,exc));return
        self.clear_pending(context)
        text=('✅ {profile} added.\nContinuous monitoring enabled. You will receive recordings automatically.' if added else
              'ℹ️ {profile} is already on your list.\nUse /monitor to pause or resume monitoring.')
        await update.effective_message.reply_text(self.t(uid,text,profile=self.registry.store.profile_label(username)))

    async def remove(self,update,context):
        if not self.private(update):return
        uid=update.effective_user.id;target=await self.target(update,context,'remove')
        if target is None:return
        try:username=self.registry.remove(uid,update.effective_chat.id,target)
        except ValueError as exc:
            await update.effective_message.reply_text('⚠️ '+self.error(uid,exc));return
        self.clear_pending(context)
        await update.effective_message.reply_text(self.t(uid,'✅ Your subscription to {profile} was removed.',profile=self.registry.store.profile_label(username)))

    async def stop(self,update,context):
        if not self.private(update):return
        uid=update.effective_user.id;target=await self.target(update,context,'stop')
        if target is None:return
        try:username=self.registry.send_part(uid,update.effective_chat.id,target)
        except ValueError as exc:
            await update.effective_message.reply_text('⚠️ '+self.error(uid,exc));return
        self.clear_pending(context)
        text=self.t(uid,'📤 Sending the current part of {profile}.',profile=self.registry.store.profile_label(username))+'\n'
        text+=self.t(uid,'The current part will be sent if it has at least 10 seconds. Recording and monitoring continue.')
        await update.effective_message.reply_text(text)

    def monitor_pages(self,user_id,chat_id):
        rows=self.registry.details(user_id,chat_id)
        def t(message,**values):return self.t(user_id,message,**values)
        header=t('📡 <b>Monitoring</b>')+'\n\n'+'\n'.join([
            t('🟢 Live: {count}',count=sum(r['online'] is True for r in rows)),
            t('⚫ Offline: {count}',count=sum(r['online'] is False for r in rows)),
            t('👁 Monitoring: {active}/{total}',active=sum(r['enabled'] for r in rows),
              total=len(rows) if self.registry.is_superuser(user_id) else max(len(rows),self.registry.config.max_monitors_per_user)),
            t('🎥 Active recordings: {count}',count=sum(r['recording'] for r in rows))])
        unknown=sum(r['online'] is None for r in rows)
        if unknown:header+='\n'+t('❔ Unknown status: {count}',count=unknown)
        groups=[[]];length=0
        for row in rows:
            if length+len(row['text'])>3000 and groups[-1]:groups.append([]);length=0
            groups[-1].append(row);length+=len(row['text'])+2
        pages=[]
        for index,group in enumerate(groups):
            sections=[escape(r['text']).replace(escape(r['label']),f"<b>{escape(r['label'])}</b>",1) for r in group]
            if index==len(groups)-1 and not self.registry.is_superuser(user_id):
                sections.extend(t('⚪ Empty slot') for _ in range(max(0,self.registry.config.max_monitors_per_user-len(rows))))
            body='\n\n'.join(sections) or t('ℹ️ Your list is empty. Use /add to add a profile.')
            text=header+'\n\n'+body+'\n\n'+t('🕒 Updated at {time}',time=datetime.now(self.registry.zone).strftime('%H:%M'))
            if len(groups)>1:text+='\n'+t('Page {page}/{total}',page=index+1,total=len(groups))
            keyboard=[]
            for row in group:
                resume=not row['enabled'] or row['stopped'];action='resume' if resume else 'pause'
                buttons=[InlineKeyboardButton(t('▶️ Resume {profile}' if resume else '⏸️ Pause {profile}',profile=row['label']),callback_data=action+':'+row['generation'])]
                buttons.append(InlineKeyboardButton(t('🗑 Remove'),callback_data='remove:'+row['generation']))
                keyboard.append(buttons)
            keyboard.append([InlineKeyboardButton(t('🔄 Refresh status'),callback_data=f'refresh:{index}')])
            pages.append((text,InlineKeyboardMarkup(keyboard),{r['generation'] for r in group}))
        return pages

    async def monitor(self,update,context):
        if not self.private(update):return
        self.clear_pending(context)
        if context.args:
            await update.effective_message.reply_text(self.t(update.effective_user.id,'Send only /monitor to view your profiles.'));return
        for text,markup,_ in self.monitor_pages(update.effective_user.id,update.effective_chat.id):
            await update.effective_message.reply_text(text,reply_markup=markup,parse_mode='HTML')

    async def controls(self,update,context):
        query=update.callback_query;uid=update.effective_user.id
        if not self.private(update):
            await query.answer(self.t(uid,'Use these controls in a private chat.'),show_alert=True);return
        action,target=query.data.split(':',1);generation=None
        try:
            if action=='refresh':page=int(target);answer='Status refreshed.'
            else:
                sub=self.registry.store.subscription(uid,generation=target)
                if sub is None:raise UserError('This button expired or the profile is not on your list. Use /monitor.')
                generation=sub['generation'];platform,handle=split_profile(sub['username']);profile=platform+':'+handle
                if action in ('send','stop'):
                    self.registry.send_part(uid,update.effective_chat.id,profile)
                    answer='The current part will be sent if it has at least 10 seconds. Recording and monitoring continue.'
                elif action=='remove':
                    self.registry.remove(uid,update.effective_chat.id,profile)
                    self.clear_pending(context)
                    answer='Subscription removed.'
                else:
                    self.registry.set_enabled(uid,update.effective_chat.id,profile,action=='resume')
                    answer='Monitoring resumed.' if action=='resume' else 'Monitoring paused. Completed parts remain queued.'
                page=0
            pages=self.monitor_pages(uid,update.effective_chat.id)
            if generation:page=next((i for i,p in enumerate(pages) if generation in p[2]),0)
            text,markup,_=pages[min(page,len(pages)-1)]
        except ValueError as exc:
            await query.answer('⚠️ '+self.error(uid,exc),show_alert=True);return
        await query.answer(self.t(uid,answer))
        try:await query.edit_message_text(text,reply_markup=markup,parse_mode='HTML')
        except BadRequest as exc:
            if 'message is not modified' not in str(exc).casefold():log.error('panel update failed user_id=%s',uid)


async def on_error(update,context):
    error=context.error
    if isinstance(error,Conflict):
        reason='webhook reactivated externally' if 'webhook' in str(error).casefold() else 'another receiver is using this token'
        log.critical('Conflict: %s; shutting down.',reason)
        context.application.bot_data['fatal_error']='Conflict: '+reason+'.'
        context.application.bot_data['stop_event'].set();return
    log.error('Telegram error=%s user_id=%s',type(error).__name__,update.effective_user.id if isinstance(update,Update) and update.effective_user else None)
    if isinstance(update,Update) and Commands.private(update):
        registry=context.application.bot_data.get('monitors')
        language=registry.store.language(update.effective_user.id) if registry else 'en'
        try:await update.effective_message.reply_text('⚠️ '+tr(language,'The command could not be completed. Please try again shortly.'))
        except TelegramError:pass


def build_application(config:Config,registry=None):
    builder=(Application.builder().token(config.token).connection_pool_size(8)
        .connect_timeout(15).read_timeout(30).write_timeout(30).pool_timeout(15))
    if config.api_mode=='local':builder=builder.base_url(config.api_url+'/bot').base_file_url(config.api_url+'/file/bot').local_mode(True)
    application=builder.build()
    if registry is not None:install_commands(application,registry)
    return application


def install_commands(application,registry):
    application.bot_data['monitors']=registry;commands=Commands(registry)
    application.add_handler(TypeHandler(Update,TrafficGuard(registry.store).check),group=-1)
    for name,_ in BOT_COMMANDS:
        application.add_handler(CommandHandler(name,getattr(commands,name),filters=filters.ChatType.PRIVATE))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE&filters.TEXT&~filters.COMMAND,commands.parameter))
    application.add_handler(CallbackQueryHandler(commands.choose_language,pattern=r'^language:[1-9]\d{0,18}:(?:en|pt)$'))
    application.add_handler(CallbackQueryHandler(commands.platform_choice,pattern=r'^platform:[0-9a-f]{32}:(superlive|stripchat|kick|tiktok)$'))
    application.add_handler(CallbackQueryHandler(commands.refresh_summary,pattern=r'^(?:limits|limite|stats|estatisticas):[1-9]\d{0,18}$'))
    application.add_handler(CallbackQueryHandler(commands.controls,pattern=r'^(?:(?:pause|resume|send|stop|remove):[0-9a-f]{32}|refresh:\d{1,6})$'))
    application.add_error_handler(on_error)
