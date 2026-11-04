"""Mock-only automatic scoring tests: no Host, network or paid model calls."""
from copy import deepcopy
from pathlib import Path
import asyncio
import importlib
import json
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "auto_scoring_test_package"
package = types.ModuleType(PACKAGE); package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
scoring = importlib.import_module(PACKAGE + ".auto_scoring")


def msg(mid="m3", uid="100", text="谢谢你一直耐心帮我", timestamp=3, group=None):
    return {"platform": "qq", "account_id": "9000", "scope": "main", "session_id": "s1",
            "message_id": mid, "timestamp": str(timestamp), "is_notify": False, "is_command": False,
            "message_info": {"user_info": {"user_id": uid}, "group_info": group},
            "raw_message": [{"type": "text", "data": text}], "processed_plain_text": "DO NOT EVALUATE THIS"}


def output(kind="meaningful_support", delta=1, evidence=None):
    if evidence is None:
        evidence = [{"message_id": "m3", "quote": "谢谢"}]
    return json.dumps({"kind": kind, "delta": delta, "evidence": evidence}, ensure_ascii=False)


class ScorerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = scoring.RelationStore(Path(self.temp.name) / "test.db", cooldown_seconds=0)
        self.messages = {"m3": msg()}
        self.response = output()
        self.calls = []
        self.lookups = []
        async def lookup(mid, *, chat_id):
            self.lookups.append((mid, chat_id))
            return deepcopy(self.messages.get(mid))
        async def llm(messages):
            self.calls.append(messages)
            return self.response
        self.scorer = scoring.AsyncScorer(self.store, scoring.Binding("qq", "9000", "main"), lookup=lookup, llm=llm)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def run_score(self, prior=()):
        return await self.scorer.score(session_id="s1", message_id="m3", prior_message_ids=prior)

    def score_value(self):
        subject = scoring.Subject("qq", "100")
        return self.store.projection(subject, audience="self_private", viewer=subject)["score"]

    def negative_window(self, third=False, group=None):
        self.messages = {
            "m1": msg("m1", text="我要骚扰你", timestamp=1, group=group),
            "b1": msg("b1", "9000", "停止骚扰", timestamp=2, group=group),
            "m3": msg("m3", text="继续骚扰你", timestamp=3, group=group)}
        evidence = [{"message_id": "m1", "quote": "我要骚扰你"},
                    {"message_id": "m3", "quote": "继续骚扰你"}]
        prior = ("m1", "b1")
        if third:
            self.messages["m2"] = msg("m2", text="我会骚扰你", timestamp=2.5, group=group)
            prior = ("m1", "b1", "m2")
            evidence.append({"message_id": "m2", "quote": "我会骚扰你"})
        self.response = output("repeated_boundary_violation", -3, evidence)
        return prior

    async def test_public_self_claims_cannot_prove_shared_activity(self):
        for kind in ('shared_activity','mutual_humor'):
            window=[scoring._Evidence('m3','user','我们刚才一起玩得真好',3.,'800')]
            result=scoring.AsyncScorer._validate({'kind':kind,'delta':1,'evidence':[{'message_id':'m3','quote':'一起玩'}]},window)
            self.assertEqual(result,('message',0.))

    async def test_positive_real_subject(self):
        result = await self.run_score()
        self.assertEqual(result.reason, "recorded")
        self.assertTrue(result.event.accepted)
        self.assertEqual(self.score_value(), 1)
        content = self.calls[0][1]["content"]
        self.assertNotIn("DO NOT EVALUATE", content)
        self.assertNotIn("user_id", content)

    async def test_persistent_duplicate_does_not_award_twice(self):
        await self.run_score()
        # Recreate scorer to prove idempotency is in persistent core, not memory.
        self.scorer = scoring.AsyncScorer(self.store, self.scorer.binding,
                                         lookup=self.scorer.lookup, llm=self.scorer.llm)
        result = await self.run_score()
        self.assertEqual(result.event.reason, "duplicate")
        self.assertEqual(self.score_value(), 1)

    async def test_none_zero_consumed(self):
        self.response = output("none", 0, [])
        result = await self.run_score()
        self.assertEqual(result.event.reason, "not_meaningful")
        self.assertEqual(self.score_value(), 0)

    async def test_wrong_anchor_identity_no_model(self):
        for key in ("session_id", "account_id", "platform", "scope", "message_id"):
            original = msg(); original[key] = "wrong"
            self.messages["m3"] = original
            self.assertEqual((await self.run_score()).reason, "invalid_anchor")
        self.assertFalse(self.calls)

    async def test_missing_lookup_no_model(self):
        self.messages = {}
        self.assertEqual((await self.run_score()).reason, "invalid_anchor")
        self.assertFalse(self.calls)

    async def test_cross_user_window_no_model(self):
        self.messages["other"] = msg("other", "200", timestamp=1)
        self.assertEqual((await self.run_score(("other",))).reason, "failed_closed")
        self.assertFalse(self.calls)

    async def test_quotes_forwards_and_media_no_model(self):
        for component in ({"type": "reply", "data": "我要骚扰你"},
                          {"type": "forward", "data": []}, {"type": "image", "data": "x"}):
            self.messages["m3"] = msg(); self.messages["m3"]["raw_message"] = [component]
            self.assertEqual((await self.run_score()).reason, "failed_closed")
        self.messages["m3"] = msg(); self.messages["m3"]["reply_to"] = "quote"
        self.assertEqual((await self.run_score()).reason, "failed_closed")
        self.assertFalse(self.calls)

    async def test_model_serious_tag_single_message_cannot_deduct(self):
        self.messages["m3"] = msg(text="我要骚扰你")
        self.response = output("repeated_boundary_violation", -1, [{"message_id": "m3", "quote": "我要骚扰你"}])
        result = await self.run_score()
        self.assertEqual(result.event.reason, "not_meaningful")
        self.assertEqual(self.score_value(), 0)

    async def test_two_negative_messages_without_bot_stop_zero(self):
        prior = self.negative_window()
        self.messages["b1"]["raw_message"] = [{"type": "text", "data": "哈哈"}]
        result = await self.run_score(prior)
        self.assertEqual(result.event.reason, "not_meaningful")

    async def test_small_negative_two_source_messages(self):
        prior = self.negative_window()
        result = await self.run_score(prior)
        self.assertTrue(result.event.accepted)
        self.assertEqual(self.score_value(), -1)

    async def test_medium_negative_three_source_messages(self):
        prior = self.negative_window(third=True)
        result = await self.run_score(prior)
        self.assertTrue(result.event.accepted)
        self.assertEqual(self.score_value(), -3)

    async def test_group_negative_never_assumes_stop_target(self):
        prior = self.negative_window(group={"group_id": "800"})
        result = await self.run_score(prior)
        self.assertEqual(result.event.reason, "not_meaningful")

    async def test_large_and_targeted_negative_disabled(self):
        prior = self.negative_window(third=True)
        for kind, delta in (("repeated_boundary_violation", -4), ("targeted_harassment", -5)):
            self.response = output(kind, delta, [{"message_id": "m3", "quote": "继续骚扰你"}])
            self.assertEqual((await self.run_score(prior)).reason, "failed_closed")
        self.assertEqual(self.score_value(), 0)

    async def test_false_quotes_rejected(self):
        self.response = output(evidence=[{"message_id": "m3", "quote": "fabricated support"}])
        self.assertEqual((await self.run_score()).reason, "failed_closed")
        self.assertEqual(self.score_value(), 0)

    async def test_stop_or_refusal_always_zero(self):
        for text in ("不要这样了", "我不愿", "停止", "拒绝亲密"):
            self.messages["m3"] = msg(text=text)
            self.response = output("meaningful_support", 1, [{"message_id": "m3", "quote": text}])
            await self.run_score()
        self.assertEqual(self.score_value(), 0)

    async def test_teasing_cannot_negative_even_with_model_label(self):
        prior = self.negative_window()
        self.messages["m1"]["raw_message"] = [{"type": "text", "data": "笨蛋哈哈"}]
        self.messages["m3"]["raw_message"] = [{"type": "text", "data": "你又发癫了"}]
        self.response = output("repeated_boundary_violation", -1, [
            {"message_id": "m1", "quote": "笨蛋"}, {"message_id": "m3", "quote": "发癫"}])
        self.assertEqual((await self.run_score(prior)).event.reason, "not_meaningful")

    async def test_fresh_lookup_text_change_aborts(self):
        async def llm(messages):
            self.messages["m3"]["raw_message"] = [{"type": "text", "data": "changed"}]
            return self.response
        self.scorer.llm = llm
        self.assertEqual((await self.run_score()).reason, "stale_evidence")
        self.assertEqual(self.score_value(), 0)

    async def test_fresh_lookup_cross_user_aborts(self):
        async def llm(messages):
            self.messages["m3"]["message_info"]["user_info"]["user_id"] = "200"
            return self.response
        self.scorer.llm = llm
        self.assertEqual((await self.run_score()).reason, "failed_closed")
        self.assertEqual(self.score_value(), 0)

    async def test_malformed_json_duplicate_keys_and_nan(self):
        for response in ('{"kind":"none","delta":0,"evidence":[],"kind":"none"}',
                         '{"kind":"meaningful_support","delta":NaN,"evidence":[]}',
                         '{"kind":"none","delta":true,"evidence":[]}', '```json {} ```'):
            self.response = response
            self.assertEqual((await self.run_score()).reason, "failed_closed")

    async def test_window_limits_and_out_of_order(self):
        self.assertEqual((await self.run_score(tuple(str(x) for x in range(8)))).reason, "invalid_window")
        self.assertEqual((await self.run_score(("m3",))).reason, "invalid_window")
        self.messages["old"] = msg("old", timestamp=-1000)
        self.assertEqual((await self.run_score(("old",))).reason, "invalid_window")
        self.messages["old"] = msg("old", timestamp=4)
        self.assertEqual((await self.run_score(("old",))).reason, "invalid_window")

    async def test_commit_guard_rejects_changed_epoch(self):
        self.scorer.commit_guard = lambda: False
        self.assertEqual((await self.run_score()).reason, "commit_guard_rejected")
        self.assertEqual(self.score_value(), 0)

    async def test_quoted_threat_is_not_negative_evidence(self):
        prior = self.negative_window()
        self.messages["m1"]["raw_message"] = [{"type": "text", "data": '他说“我要骚扰你”'}]
        self.assertEqual((await self.run_score(prior)).event.reason, "not_meaningful")

    async def test_play_or_teasing_positive_label_still_zero(self):
        self.messages["m3"] = msg(text="笨蛋哈哈")
        self.response = output("mutual_humor", 1, [{"message_id": "m3", "quote": "哈哈"}])
        self.assertEqual((await self.run_score()).event.reason, "not_meaningful")

    async def test_repeated_cancellation_waits_until_commit_thread_finishes(self):
        from threading import Event
        from unittest.mock import patch
        entered, release, finished = asyncio.Event(), Event(), Event()
        loop = asyncio.get_running_loop()
        original = self.store.record_event
        def blocked(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            release.wait(timeout=5)
            try:
                return original(*args, **kwargs)
            finally:
                finished.set()
        with patch.object(self.store, "record_event", blocked):
            task = asyncio.create_task(self.run_score())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertFalse(finished.is_set())
                self.assertTrue(self.scorer._lock.locked())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(finished.is_set())
            self.assertFalse(self.scorer._lock.locked())

    async def test_model_failure_no_score(self):
        async def fail(messages):
            raise RuntimeError("do not log body")
        self.scorer.llm = fail
        self.assertEqual((await self.run_score()).reason, "failed_closed")
        self.assertEqual(self.score_value(), 0)

    async def test_sdk_adapter_exact_contract(self):
        class FakeLLM:
            async def generate(inner, **kwargs):
                self.assertEqual(kwargs["task_name"], "relationship_scoring")
                self.assertEqual(kwargs["temperature"], 0)
                self.assertNotIn("tools", kwargs)
                return {"success": True, "response": self.response}
        generate = scoring.sdk_llm_adapter(FakeLLM(), task_name="relationship_scoring")
        self.assertEqual(await generate([]), self.response)
        with self.assertRaises(ValueError):
            scoring.sdk_llm_adapter(FakeLLM(), task_name="")


if __name__ == "__main__":
    unittest.main()
