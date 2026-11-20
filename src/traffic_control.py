"""Bound command bursts before profile lookups and Telegram replies."""
import time
from telegram import Update
from telegram.ext import ApplicationHandlerStop
from i18n import tr


class TrafficGuard:
    def __init__(self,store,clock=time.monotonic):self.store,self.clock,self.users,self.notified=store,clock,{},{}

    def allowed(self,user_id):
        now=self.clock();tokens,previous=self.users.get(user_id,(20,now))
        tokens=min(20,tokens+max(0,now-previous)*2)
        allowed=tokens>=1
        self.users[user_id]=(tokens-1 if allowed else tokens,now)
        if len(self.users)>10000:
            self.users={uid:value for uid,value in self.users.items() if now-value[1]<600}
            self.notified={uid:stamp for uid,stamp in self.notified.items() if uid in self.users}
        return allowed

    async def check(self,update,context):
        if not isinstance(update,Update) or not update.effective_user:return
        if not self.allowed(update.effective_user.id):
            uid=update.effective_user.id;now=self.clock()
            if now-self.notified.get(uid,-1e9)>=10:
                self.notified[uid]=now
                text=tr(self.store.language(uid),'Too many requests. Please wait a few seconds.')
                if update.callback_query:await update.callback_query.answer(text)
                elif update.effective_chat and update.effective_chat.type=='private':await update.effective_message.reply_text(text)
            raise ApplicationHandlerStop
