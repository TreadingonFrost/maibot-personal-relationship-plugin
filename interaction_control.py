"""Exact, configurable private control phrases; no fuzzy substring stop detector.

Natural-language refusal must still be respected by the reply policy. These
phrases provide deterministic state control, not the sole way to withdraw.
"""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ControlPhrases:
    stop: str = '/关系暂停'
    resume: str = '/关系恢复'

    def __post_init__(self):
        for text in (self.stop, self.resume):
            if not isinstance(text,str) or not 2 <= len(text) <= 64 or text != text.strip():
                raise ValueError('control phrase must be 2..64 characters with no outer whitespace')
            if any(ord(c) < 32 for c in text) or any(c in text for c in '"\'“”‘’<>`'):
                raise ValueError('control phrase cannot contain control or quote markers')
            if text.startswith('/relation'):
                raise ValueError('control phrase conflicts with management command')
        if self.stop == self.resume:
            raise ValueError('stop and resume must differ')


def match_control(text, phrases, *, private, quoted=False):
    """Only entire original private message, not quote/reply/forward snippets."""
    if not private or quoted or not isinstance(text,str): return None
    # Exact includes whitespace: no punctuation stripping or substring matching.
    if text == phrases.stop: return 'stop'
    if text == phrases.resume: return 'resume'
    return None


class InteractionState:
    """Per-session state; only explicit resume clears pause. Config reload retains it."""
    def __init__(self):
        self.paused = set()
        self.previews = {}
        self._preview_messages = {}

    @staticmethod
    def key(identity):
        return (identity.platform,identity.session_id,identity.user_id)

    def control(self, identity, action):
        key = self.key(identity)
        if action == 'stop':
            self.paused.add(key); self.previews.pop(key,None)
            self._preview_messages = {k:v for k,v in self._preview_messages.items() if k[0] != key}
        elif action == 'resume': self.paused.discard(key)
        else: raise ValueError('unknown control')

    def set_preview(self, identity, level, *, admin_uid=''):
        if (not isinstance(admin_uid, str) or re.fullmatch(r'[1-9][0-9]{4,19}', admin_uid) is None
                or identity.platform != 'qq' or identity.user_id != admin_uid or not identity.private):
            raise PermissionError('preview requires the configured private administrator')
        if type(level) is not int or not -2 <= level <= 5: raise ValueError('bad preview level')
        if self.key(identity) in self.paused: raise ValueError('resume before preview')
        self.previews[self.key(identity)] = level
        self._preview_messages = {k:v for k,v in self._preview_messages.items() if k[0] != self.key(identity)}

    def preview_level(self, identity, message_id):
        """One target message, stable across retries; never another session/user."""
        key = self.key(identity)
        if key in self.paused: return None
        anchor_key = (key, message_id)
        if anchor_key in self._preview_messages:
            return self._preview_messages[anchor_key]
        if key not in self.previews: return None
        level = self.previews.pop(key)
        self._preview_messages[anchor_key] = level
        # Bounded retry cache; old anchors eventually expire, never affect new ones.
        while len(self._preview_messages) > 128:
            self._preview_messages.pop(next(iter(self._preview_messages)))
        return level

    def clear_previews(self):
        self.previews.clear(); self._preview_messages.clear()
