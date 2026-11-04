"""Opt-in offline automatic evaluation orchestration, not a registered Host hook.

Injected evaluator/lookup are trusted infrastructure callables. No provider,
network access, background timer or production enable flag exists here.
"""
import asyncio

try:
    from .evaluation import EvaluationSubject, build_request
    from .evaluation_binding import Receipt, apply_bound_result
    from .host_adapter import trusted_message
    from .relation_core import Subject
except ImportError:
    from evaluation import EvaluationSubject, build_request
    from evaluation_binding import Receipt, apply_bound_result
    from host_adapter import trusted_message
    from relation_core import Subject


class OfflineEvaluationPipeline:
    def __init__(self, store, binding, evaluator, lookup):
        self.store, self.binding = store, binding
        self.evaluator, self.lookup = evaluator, lookup
        self._inflight = set()

    async def evaluate(self, message):
        """Use only this author's text; relookup original evidence after await.

        Concurrent repeated submissions for the same original message do not
        incur repeated evaluator calls. Store remains the persistent dedup and
        rate-limit boundary. No shared transcript or last-sender cache is used.
        """
        identity = trusted_message(message, self.binding, for_hook=True)
        if identity is None:
            raise ValueError('untrusted evaluation message')
        key = (identity.session_id, identity.message_id, identity.user_id)
        if key in self._inflight:
            return None
        self._inflight.add(key)
        try:
            subject = Subject(identity.platform, identity.user_id)
            state = await asyncio.to_thread(self.store.projection, subject, 'self_private', subject)
            bound = EvaluationSubject(identity.platform, self.binding.account_id,
                                      identity.user_id, identity.session_id,
                                      identity.message_id, self.binding.scope)
            receipt = Receipt(bound, state['version'])
            request = build_request(bound, message.get('processed_plain_text'))
            # Only model messages cross the evaluator boundary, not internal IDs.
            raw = await self.evaluator(request['messages'])
            fresh = await self.lookup(bound.message_id, chat_id=bound.session_id,
                                      include_binary_data=False)
            return await asyncio.to_thread(apply_bound_result, self.store, receipt,
                                           fresh, self.binding, raw)
        finally:
            self._inflight.discard(key)
