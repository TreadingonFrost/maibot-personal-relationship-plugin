"""SDK 2.21.0 relationship settings, private management APIs and scoring.

Automatic scoring is opt-in and evaluates anchored messages through the Host LLM
capability. No send capability or model-accessible management tools are registered.
"""
from typing import Literal
from datetime import datetime, timezone
from uuid import uuid4
import asyncio
import json
import re

from maibot_sdk import API, Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
import time

from .host_adapter import Binding, parse_command, projection_prompt, trusted_command, trusted_message
from .relation_core import RelationStore, Subject
from .interaction_control import ControlPhrases, InteractionState, match_control
from .style_config import StyleConfig, get_style
from pydantic import model_validator


def _host_message_result(result):
    """SDK 2.21 normalizes message.get_by_id to its direct message dict.

    Errors and unknown shapes are rejected; no identity is manufactured here.
    """
    return result if isinstance(result,dict) and 'message_id' in result else None


async def _io(function, *args, **kwargs):
    """Keep lifecycle lock owned until a cancelled DB operation actually ends."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Each new cancel() must be caught without ever cancelling the worker.
            cancelled = True
        except BaseException:
            # Consume the worker outcome below, after it has actually completed.
            break
    if cancelled:
        # Retrieve any exception to avoid unhandled-task warnings; cancellation
        # still wins, but only after the thread can no longer touch the database.
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


class PluginSection(PluginConfigBase):
    """仅控制关系语气插件，不控制整个人格或整Bot。"""
    __ui_label__ = '插件开关'
    __ui_order__ = 0
    config_version: str = Field(default="3.0.0", json_schema_extra={'label':'配置版本','disabled':True})
    enabled: bool = Field(default=False, json_schema_extra={'label':'启用关系插件','hint':'先配置机器人路由；是否自动计分由模式选择'})


class AccountSection(PluginConfigBase):
    """必须与宿主实际连接一致；管理员QQ不是机器人账号。"""
    __ui_label__ = '机器人绑定（不是管理员账号）'
    __ui_order__ = 1
    platform: Literal["qq"] = Field(default="qq", json_schema_extra={'label':'平台','disabled':True})
    account_id: str = Field(default="", json_schema_extra={'label':'机器人账号','placeholder':'填写实际机器人QQ，不能填管理员QQ'})
    scope: str = Field(default="", json_schema_extra={'label':'连接域 scope','hint':'与宿主消息的scope完全一致，不是群号'})


class Admin(PluginConfigBase):
    """Compatibility field; must match the single configured administrator."""
    platform: Literal["qq"] = "qq"
    user_id: str = Field(pattern=r'^[1-9][0-9]{4,19}$')


class ControlConfig(PluginConfigBase):
    """纯文字完整口令；非模糊关键词，暂停不扣关系分。"""
    __ui_label__ = '暂停与恢复口令'
    __ui_order__ = 2
    stop_phrase: str = Field(default='/关系暂停', description='完整私聊消息精确匹配；引用/复述/子串不触发', json_schema_extra={'label':'暂停口令','max_length':64})
    resume_phrase: str = Field(default='/关系恢复', description='独立恢复口令，不以继续聊天自动恢复', json_schema_extra={'label':'恢复口令','max_length':64})

    @model_validator(mode='after')
    def validate_phrases(self):
        ControlPhrases(self.stop_phrase, self.resume_phrase)
        return self


class DampingConfig(PluginConfigBase):
    """仅作用于可信负向证据；不自动评估消息，不处理严重骚扰豁免。"""
    __ui_label__ = '负向扣分阻尼'
    __ui_order__ = 3
    familiar_factor: float = Field(default=.9, gt=0, le=1, allow_inf_nan=False, json_schema_extra={'label':'1级扣分倍率'})
    friend_factor: float = Field(default=.8, gt=0, le=1, allow_inf_nan=False, json_schema_extra={'label':'2级扣分倍率'})
    kind_factor: float = Field(default=.7, gt=0, le=1, allow_inf_nan=False, json_schema_extra={'label':'3级扣分倍率'})
    intimate_factor: float = Field(default=.6, gt=0, le=1, allow_inf_nan=False, json_schema_extra={'label':'4级扣分倍率'})
    admin_factor: float = Field(default=.6, gt=0, le=1, allow_inf_nan=False, json_schema_extra={'label':'5级信号扣分倍率（不降管理员等级）'})
    daily_threshold: int = Field(default=3, strict=True, ge=1, le=100, json_schema_extra={'label':'单日负向次数宽限'})
    daily_decay: float = Field(default=.5, gt=0, le=1, allow_inf_nan=False, json_schema_extra={'label':'超限后逐次倍率'})
    min_daily_factor: float = Field(default=.1, gt=0, le=1, allow_inf_nan=False, json_schema_extra={'label':'单日倍率下限'})


class ScoringConfig(PluginConfigBase):
    """只评价实际回复对象；模型调用有上限，普通发癫/互损默认不扣。"""
    __ui_label__ = '自动评分'
    __ui_order__ = 4
    task_name: Literal['utils','planner','replyer'] = Field(default='utils', json_schema_extra={'label':'评分模型任务','hint':'复用现有任务，产生模型调用费用'})
    cooldown_seconds: int = Field(default=120, ge=60, le=3600, json_schema_extra={'label':'每对象评价间隔（秒）'})
    daily_calls: int = Field(default=60, ge=1, le=500, json_schema_extra={'label':'每日模型调用上限（运行内存）'})
    webui_operator_enabled: bool = Field(default=True, json_schema_extra={'label':'允许已登录WebUI管理员调分'})


class GroupConfig(PluginConfigBase):
    """群聊的关系表现开关；群私共用每级额外设定。"""
    __ui_label__ = '群聊关系表现'
    __ui_order__ = 5
    allow_high_levels: bool = Field(default=False, strict=True, json_schema_extra={'label':'群聊允许3/4/5级生效','hint':'开启后显示真实有效等级的亲近表现；关闭时群聊仍最多2级'})


class RelationshipConfig(StyleConfig):
    plugin: PluginSection = Field(default_factory=PluginSection)
    account: AccountSection = Field(default_factory=AccountSection)
    admin_uid: str = Field(default="", pattern=r'^(?:[1-9][0-9]{4,19})?$', json_schema_extra={'label':'唯一管理员QQ','hint':'启用前填写精确数字QQ；默认空白不授予任何人权限'})
    admins: list[Admin] = Field(default_factory=list, description="旧结构兼容项，只能为空或与admin_uid完全相同的唯一账号")
    # Literal prevents an accidental configuration flag from promising automation.
    evaluation_mode: Literal['disabled','automatic'] = Field(default='disabled', json_schema_extra={'label':'自动评分模式'})
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    group: GroupConfig = Field(default_factory=GroupConfig)
    controls: ControlConfig = Field(default_factory=ControlConfig)
    damping: DampingConfig = Field(default_factory=DampingConfig)

    @model_validator(mode='after')
    def validate_administrator(self):
        if self.plugin.enabled and not self.admin_uid:
            raise ValueError('启用关系插件前必须配置唯一管理员QQ')
        if len(self.admins) > 1 or any(admin.user_id != self.admin_uid for admin in self.admins):
            raise ValueError('admins只能为空或与admin_uid一致的唯一QQ')
        return self


class WhaleRelationshipPlugin(MaiBotPlugin):
    config_model = RelationshipConfig

    def __init__(self) -> None:
        super().__init__()
        self._lock = asyncio.Lock()
        self._store: RelationStore | None = None
        self._binding: Binding | None = None
        self._admins: tuple[tuple[str, str], ...] = ()
        self._interaction = InteractionState()
        self._interaction_namespace = None
        self._scoring_task = None
        self._scoring_queue = None
        self._scoring_epoch = 0
        self._score_last = {}
        self._score_calls = (0,0)
        self._score_seen = set()
        self._relation_webui = None

    def _clear_locked(self) -> None:
        self._scoring_epoch += 1
        if self._scoring_task is not None:
            self._scoring_task.cancel()
        self._scoring_task = None
        self._scoring_queue = None
        self._store = None
        self._binding = None
        self._admins = ()
        self._relation_webui = None

    async def _stop_scoring_locked(self):
        task=self._scoring_task
        self._scoring_epoch += 1
        cancelled=False
        if task is not None:
            task.cancel()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling(): cancelled=True
                    if task.done(): break
            if not task.cancelled(): task.exception()
        self._scoring_task=None;self._scoring_queue=None
        if cancelled: raise asyncio.CancelledError

    async def _load_locked(self) -> None:
        await self._stop_scoring_locked()
        self._clear_locked()
        cfg = self.config
        if not cfg.plugin.enabled:
            return
        binding = Binding(cfg.account.platform, cfg.account.account_id, cfg.account.scope)
        if not binding.valid():
            return
        namespace = (binding.platform, binding.account_id, binding.scope, cfg.admin_uid)
        if self._interaction_namespace != namespace:
            self._interaction = InteractionState()
            self._interaction_namespace = namespace
        admins = (("qq", cfg.admin_uid),)
        path = self.ctx.paths.data_dir / binding.database_name()
        # Keep all SQLite/file operations off the event loop.
        await _io(path.parent.mkdir, parents=True, exist_ok=True)
        d = cfg.damping
        store = await _io(RelationStore, path,
                          admin_subjects=[Subject(p, u) for p, u in admins],
                          negative_relation_factors=(1.,1.,1.,d.familiar_factor,d.friend_factor,d.kind_factor,d.intimate_factor,d.admin_factor),
                          negative_daily_threshold=d.daily_threshold,
                          negative_daily_decay=d.daily_decay,
                          negative_min_factor=d.min_daily_factor)
        self._binding, self._admins, self._store = binding, admins, store
        from .admin_webui import AdminWebUIBackend
        self._relation_webui = await _io(AdminWebUIBackend,store,
            operator_enabled=cfg.scoring.webui_operator_enabled)

    async def on_load(self) -> None:
        async with self._lock:
            await self._load_locked()

    async def on_unload(self) -> None:
        # Cancel and settle evaluation/commit before releasing the old namespace.
        async with self._lock:
            await self._stop_scoring_locked()
            self._clear_locked()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == "self":
            # Configuration and binding transition form one linearized operation;
            # an unload queued behind this update always leaves us unloaded.
            async with self._lock:
                await self._stop_scoring_locked()
                self._clear_locked()
                self._interaction.clear_previews()
                self.set_plugin_config(config_data)
                await self._load_locked()

    @API('relation_webui_query', public=False, version='1')
    async def relation_webui_query(self, target_qq: str = ''):
        async with self._lock:
            if self._relation_webui is None: raise ValueError('插件未启用或未绑定')
            return await _io(self._relation_webui.query,target_qq)

    @API('relation_webui_set_score', public=False, version='1')
    async def relation_webui_set_score(self, target_qq: str, score: float, expected_version: int):
        async with self._lock:
            if self._relation_webui is None: raise ValueError('插件未启用或未绑定')
            return await _io(self._relation_webui.set_score,target_qq,score,expected_version)

    @API('relation_webui_grant', public=False, version='1')
    async def relation_webui_grant(self, target_qq: str, level: int, expected_version: int):
        async with self._lock:
            if self._relation_webui is None: raise ValueError('插件未启用或未绑定')
            return await _io(self._relation_webui.grant,target_qq,level,expected_version)

    @HookHandler('maisaka.replyer.after_response', name='relationship_auto_score')
    async def after_response_score(self, **kwargs):
        sid, mid = kwargs.get('session_id'), kwargs.get('reply_message_id')
        if not isinstance(kwargs.get('response'),str) or not kwargs['response'].strip():
            return {'action':'continue'}
        if not isinstance(sid,str) or not sid or not isinstance(mid,str) or not mid:
            return {'action':'continue'}
        async with self._lock:
            if self._store is None or self.config.evaluation_mode != 'automatic':
                return {'action':'continue'}
            if self._scoring_queue is None:
                self._scoring_queue = asyncio.Queue(maxsize=32)
                self._scoring_task = asyncio.create_task(self._score_worker(self._scoring_epoch))
            if (sid,mid) not in self._score_seen and not self._scoring_queue.full():
                self._score_seen.add((sid,mid))
                if len(self._score_seen)>2048:
                    self._score_seen={(sid,mid)}
                self._scoring_queue.put_nowait((sid,mid))
        return {'action':'continue'}

    async def _score_worker(self, epoch):
        queue = self._scoring_queue
        while epoch == self._scoring_epoch:
            sid, mid = await queue.get()
            try:
                await self._score_one(epoch,sid,mid)
            except asyncio.CancelledError:
                raise
            except Exception:
                # No message bodies, score evidence or provider errors in logs.
                pass
            finally:
                queue.task_done()

    async def _score_one(self, epoch, sid, mid):
        from .auto_scoring import AsyncScorer
        async with self._lock:
            if epoch != self._scoring_epoch or self._store is None:
                return
            store,binding,cfg = self._store,self._binding,self.config.scoring
        anchor = _host_message_result(await self.ctx.message.get_by_id(mid,chat_id=sid,include_binary_data=False))
        identity = trusted_message(anchor,binding,session_id=sid,message_id=mid,for_hook=True)
        if identity is None or self._interaction.key(identity) in self._interaction.paused:
            return
        now=time.time(); key=(binding.database_name(),identity.user_id)
        day=int(now//86400); old_day,count=self._score_calls
        if old_day != day: count=0
        if count>=cfg.daily_calls or now-self._score_last.get(key,0)<cfg.cooldown_seconds:
            return
        self._score_calls=(day,count+1);self._score_last[key]=now
        recent=await self.ctx.message.get_recent(sid,limit=8)
        prior=[]
        # Group bot turns may have addressed somebody else. Score only the
        # current authored message there, never infer shared history by adjacency.
        if identity.private and isinstance(recent,list):
            for item in recent:
                if not isinstance(item,dict) or item.get('message_id')==mid: continue
                try:
                    if not 0 < float(anchor.get('timestamp',0))-float(item.get('timestamp',0)) <=600: continue
                except (TypeError,ValueError): continue
                info=item.get('message_info',{}); user=info.get('user_info',{}) if isinstance(info,dict) else {}
                if user.get('user_id') not in (identity.user_id,binding.account_id): continue
                if not isinstance(item.get('message_id'),str): continue
                if not isinstance(item.get('raw_message'),list) or item.get('reply_to') is not None: continue
                if any(not isinstance(c,dict) or c.get('type')!='text' for c in item['raw_message']): continue
                prior.append((float(item['timestamp']),item['message_id']))
            prior=[mid for _,mid in sorted(prior)]
        async def lookup(message_id,**kw):
            return _host_message_result(await self.ctx.message.get_by_id(message_id,chat_id=sid,include_binary_data=False))
        async def llm(messages):
            if epoch!=self._scoring_epoch: raise asyncio.CancelledError
            result=await self.ctx.llm.generate(prompt=messages,task_name=cfg.task_name,temperature=0,max_tokens=512)
            if not isinstance(result,dict) or result.get('success') is not True:
                raise ValueError('evaluation failed')
            return result.get('response','')
        scorer=AsyncScorer(store=store,binding=binding,lookup=lookup,llm=llm,
                           commit_guard=lambda: epoch==self._scoring_epoch and self._interaction.key(identity) not in self._interaction.paused)
        await asyncio.wait_for(scorer.score(session_id=sid,message_id=mid,prior_message_ids=tuple(prior[-7:])),timeout=45)

    @Command("relation", description="私聊关系授权与本人状态（不支持昵称目标）",
             pattern=r"^/relation(?:\s|$)", intercept_message_level=1)
    async def relation_command(self, **kwargs):
        async with self._lock:
            if self._store is None or self._binding is None:
                return False, "关系插件未启用或未绑定账号。", True
            trusted = trusted_command(kwargs, self._binding)
            if trusted is None:
                return False, "拒绝：需要身份一致的私聊命令。", True
            identity, text = trusted
            if self._store.is_admin(Subject(identity.platform, identity.user_id)):
                match = re.fullmatch(r'/relation setscore ([1-9][0-9]{0,19}) (-?(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,2})?)', text)
                if match:
                    target, value = match.groups()
                    try:
                        state = await _io(self._store.admin_set_score,
                            Subject(identity.platform,target), float(value),
                            authenticated_admin=Subject(identity.platform,identity.user_id))
                        return True, f'已设置该QQ分数为{state.score:g}；有效等级{state.effective_level}。手动3/4授权与管理员5不随分数改变。', True
                    except (ValueError,PermissionError):
                        return False, '设分失败：普通对象仅接受−200到200，最多两位小数；管理员固定200，不能改分。', True
                if text.startswith('/relation preview '):
                    try:
                        level_text = text.removeprefix('/relation preview ')
                        if level_text not in tuple(str(n) for n in range(-2, 6)):
                            raise ValueError('invalid preview')
                        self._interaction.set_preview(identity, int(level_text), admin_uid=self.config.admin_uid)
                        return True, '已设置下一条本人私聊回复的临时语气；不修改关系数据。', True
                    except ValueError:
                        return False, '调试失败：请先恢复互动，并使用−2到5的整数。', True
                if text == '/relation reset_styles':
                    data = self.config.model_dump()
                    defaults = StyleConfig().model_dump()
                    for key in defaults:
                        data[key] = defaults[key]
                    # Host config persistence is deliberately not claimed: this is
                    # a session reset; WebUI save persists editable defaults.
                    self.set_plugin_config(data)
                    self._interaction.clear_previews()
                    return True, '当前运行配置已恢复默认语气；持久恢复请在WebUI按默认模板修改并保存。', True
            parsed = parse_command(text, identity, admin_uid=self.config.admin_uid)
            if parsed is None:
                return False, "拒绝：无权限或命令格式无效。", True
            actor = Subject(identity.platform, identity.user_id)
            subject = Subject(identity.platform, parsed.target_id)
            try:
                if parsed.action == "status":
                    state = await _io(self._store.projection, subject,
                                                   audience="self_private", viewer=actor)
                    # This endpoint can ONLY render the caller's own state.
                    return True, f"你当前的关系阶段：{state['effective_level']}。自动评价未启用。", True
                await _io(self._store.grant, subject, parsed.level,
                                        authenticated_admin=actor)
                return True, "已更新指定数字QQ的私聊关系授权。", True
            except Exception:
                # Do not log exception strings: external data may be embedded there.
                return False, "关系操作失败，未返回关系资料。", True

    def _control_locked(self, message, identity):
        # Processed text can come from voice/JSON cards/forward placeholders.
        # Only one original text segment may control pause/resume state.
        raw = message.get('raw_message')
        if (not isinstance(raw, list) or len(raw) != 1
                or not isinstance(raw[0], dict) or raw[0].get('type') != 'text'
                or not isinstance(raw[0].get('data'), str)
                or raw[0]['data'] != message.get('processed_plain_text')):
            return None
        action = match_control(
            raw[0]['data'],
            ControlPhrases(self.config.controls.stop_phrase, self.config.controls.resume_phrase),
            private=identity.private,
            quoted=bool(message.get('reply_to')) or bool(message.get('is_forward')),
        )
        if action:
            self._interaction.control(identity, action)
        return action

    @HookHandler('chat.receive.after_process', name='relationship_control')
    async def receive_control(self, message=None, **kwargs):
        # No replies or model calls; the real serialized message supplies identity.
        async with self._lock:
            if self._store is None or self._binding is None:
                return None
            identity = trusted_message(message, self._binding)
            if identity is None or message.get('is_notify') is not False:
                return None
            self._control_locked(message, identity)
        return None

    @HookHandler("maisaka.planner.before_request", name="relationship_planner_guidance")
    async def planner_before_request(self, **kwargs):
        """Advisory per-sender relationship guidance; never an authorization gate.

        Planner has no authoritative selected target identity. Recent Host rows
        authenticate only their OWN sender and route, not a quoted person or the
        current selected_history. The appended text requires exact mid+QQ target
        matching; the model following that instruction is not code enforcement.
        """
        result = {"action": "continue", "modified_kwargs": dict(kwargs)}
        items, sid = kwargs.get("items"), kwargs.get("session_id")
        if (not isinstance(items, list) or not isinstance(sid, str) or not sid
                or type(kwargs.get("item_schema_version")) is not int
                or kwargs["item_schema_version"] != 1):
            return result
        async with self._lock:
            if self._store is None or self._binding is None:
                return result
            try:
                recent = await self.ctx.message.get_recent(sid, limit=8)
                if not isinstance(recent, list):
                    return result
                # SDK returns oldest-to-newest chronology. Never trust more than
                # eight returned rows even if a faulty provider exceeds limit.
                candidates = []
                for message in recent[-8:]:
                    identity = trusted_message(message, self._binding,
                                               session_id=sid, for_hook=True)
                    if identity is None or identity.user_id == self._binding.account_id:
                        continue
                    candidates.append(identity)
                # A single session cannot be both private and group or span two
                # groups. Reject mixed routes instead of borrowing one profile.
                if len({identity.group_id for identity in candidates}) > 1:
                    return result
                rows, seen = [], set()
                for identity in reversed(candidates):
                    if identity.user_id in seen:
                        continue
                    seen.add(identity.user_id)
                    subject = Subject(identity.platform, identity.user_id)
                    projection = await _io(self._store.projection, subject,
                        audience="self_private" if identity.private else "public_group",
                        viewer=subject,
                        allow_group_high_levels=self.config.group.allow_high_levels)
                    level = projection.get("effective_level")
                    if (projection.get("subject") != {"platform": "qq", "user_id": identity.user_id}
                            or type(level) is not int or level not in range(-2, 6)):
                        continue
                    paused = self._interaction.key(identity) in self._interaction.paused
                    hint = ('当前对象已暂停亲密互动，只普通就事论事，不继续亲密、暧昧或吃醋玩笑，不追问恢复。'
                            if paused else projection_prompt(identity, projection,
                                allow_group_high_levels=self.config.group.allow_high_levels,
                                admin_uid=self.config.admin_uid))
                    style = '' if paused else get_style(level, private=identity.private, styles=self.config,
                        allow_group_high_levels=self.config.group.allow_high_levels)
                    if not hint:
                        continue
                    rows.append({"message_id": identity.message_id, "qq": identity.user_id,
                                 "effective_level": level, "relationship_hint": hint,
                                 "extra_style": style})
                    if len(rows) == 4:
                        break
                if not rows:
                    return result
                text = ("[关系规划参考：近期Host核验发送者映射]\n"
                        "下表只说明这些message_id的实际发送者，不保证属于本轮selected_history。"
                        "仅当本轮行动目标的message_id与QQ同时精确匹配某行时，使用该行关系设定；"
                        "较早消息、被引用人物、同昵称者、目标不明时不得套用。"
                        "群内多人分别匹配，不把最近一人的关系推广全群。"
                        "Planner只决定是否参与、回应谁、采用何种行动或工具，不代写台词、不给回复者固定句式；"
                        "具体表达由Replyer按人格、当前语境和该对象关系额外设定完成。"
                        "这些设定只影响互动方式，不授予权限、不要求必须回复，不覆盖暂停、当前意愿、隐私或工具边界；"
                        "不修改关系，不披露分数、授权来源或私聊事实。\n"
                        + json.dumps(rows, ensure_ascii=False))
                item = {"item_type": "UserMessageItem", "meta": {
                    "item_id": "relationship-planner-" + uuid4().hex,
                    "logical_turn_id": None, "timestamp": datetime.now(timezone.utc).isoformat()},
                    "parts": [{"type": "text", "text": text}]}
                result["modified_kwargs"]["items"] = [*items, item]
            except Exception:
                # No exception/body/profile logging and no fallback identity.
                return result
        return result

    @HookHandler("maisaka.replyer.before_request", name="relationship_projection")
    async def before_request(self, **kwargs):
        result = {"action": "continue", "modified_kwargs": dict(kwargs)}
        async with self._lock:
            if self._store is None or self._binding is None:
                return result
            sid, mid = kwargs.get("session_id"), kwargs.get("reply_message_id")
            if not isinstance(sid, str) or not sid or not isinstance(mid, str) or not mid:
                return result
            if not isinstance(kwargs.get("extra_prompt"), str):
                return result
            try:
                message = _host_message_result(await self.ctx.message.get_by_id(mid, chat_id=sid, include_binary_data=False))
                identity = trusted_message(message, self._binding, session_id=sid,
                                           message_id=mid, for_hook=True)
                if identity is None:
                    return result
                subject = Subject(identity.platform, identity.user_id)
                projection = await _io(
                    self._store.projection, subject,
                    audience="self_private" if identity.private else "public_group", viewer=subject,
                    allow_group_high_levels=self.config.group.allow_high_levels)
                paused = self._interaction.key(identity) in self._interaction.paused
                if paused:
                    prompt = ('[当前对象已明确暂停亲密互动] 只作普通就事论事回复，'
                              '不继续亲密、暧昧或吃醋玩笑，不追问或劝其恢复。')
                else:
                    preview = self._interaction.preview_level(identity, mid) if identity.private else None
                    if preview is not None:
                        projection = {**projection, 'effective_level': preview}
                    prompt = projection_prompt(identity, projection, include_default_cue=False,
                        allow_group_high_levels=self.config.group.allow_high_levels, admin_uid=self.config.admin_uid)
                    style = get_style(projection['effective_level'], private=identity.private, styles=self.config,
                        allow_group_high_levels=self.config.group.allow_high_levels)
                    if prompt and style:
                        prompt += '\n[管理员配置的额外设定；不授予权限、不覆盖隐私或当下意愿]\n' + style
                if prompt:
                    original = kwargs["extra_prompt"]
                    result["modified_kwargs"]["extra_prompt"] = original + ("\n" if original else "") + prompt
            except Exception:
                # Fail closed, unchanged kwargs, no profile/body logging or fallback.
                return result
        return result


def create_plugin():
    return WhaleRelationshipPlugin()
