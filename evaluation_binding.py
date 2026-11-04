"""Offline result-binding boundary; never calls a model or enables automation.

The caller supplies a fresh Host lookup AFTER the awaited evaluator. A receipt
is immutable trusted-side state, not fields returned by the model. This helper
is not an RPC endpoint and must not be exposed as an LLM Tool.
"""
from dataclasses import dataclass

try:
    from .evaluation import EvaluationSubject, parse_suggestion
    from .host_adapter import Binding, trusted_message
    from .relation_core import RelationStore, Subject
except ImportError:
    from evaluation import EvaluationSubject, parse_suggestion
    from host_adapter import Binding, trusted_message
    from relation_core import RelationStore, Subject


@dataclass(frozen=True)
class Receipt:
    subject: EvaluationSubject
    relationship_version: int

    def __post_init__(self):
        if not isinstance(self.subject, EvaluationSubject):
            raise ValueError('invalid bound subject')
        if type(self.relationship_version) is not int or self.relationship_version < 0:
            raise ValueError('invalid relationship version')


def apply_bound_result(store: RelationStore, receipt: Receipt, fresh_message: dict,
                       binding: Binding, raw_result: str):
    """Revalidate exact identity and route, then atomically apply bounded result.

    The store MUST belong to binding's database namespace. Return None for an
    abstention. Identity/schema/version errors raise without changing any row.
    A fresh lookup cannot be substituted by a cached 'last sender' object.
    """
    if not isinstance(receipt, Receipt):
        raise ValueError('invalid receipt')
    expected = receipt.subject
    if (binding.platform, binding.account_id, binding.scope) != (
            expected.platform, expected.account_id, expected.scope):
        raise ValueError('evaluation binding changed')
    message = trusted_message(fresh_message, binding,
                              session_id=expected.session_id,
                              message_id=expected.message_id, for_hook=True)
    if message is None or message.user_id != expected.user_id:
        raise ValueError('evaluation evidence mismatch')
    # The helper may not be pointed at another bot's similarly shaped database.
    from pathlib import Path
    if Path(store.db_path).name != binding.database_name():
        raise ValueError('evaluation store namespace mismatch')
    suggestion = parse_suggestion(raw_result)
    if suggestion.kind == 'none' or suggestion.delta == 0:
        return None
    return store.record_event(
        Subject(expected.platform, expected.user_id), expected.message_id,
        suggestion.kind, suggestion.delta, source_user_id=expected.user_id,
        expected_version=receipt.relationship_version)
