import copy

from app.agent.llm import LLMReply, ToolCall
from app.channels.sender import SendError


def text_reply(text: str) -> LLMReply:
    return LLMReply(text=text, tool_calls=[], content_blocks=[{"type": "text", "text": text}], stop_reason="end_turn",
                    input_tokens=10, output_tokens=5)


def tool_reply(name: str, args: dict, call_id: str = "toolu_1") -> LLMReply:
    return LLMReply(
        text="",
        tool_calls=[ToolCall(call_id, name, args)],
        content_blocks=[{"type": "tool_use", "id": call_id, "name": name, "input": args}],
        stop_reason="tool_use",
        input_tokens=10,
        output_tokens=5,
    )


class FakeLLM:
    """Rejoue des réponses scriptées et mémorise chaque appel (système, outils, messages)."""

    def __init__(self, *replies, repeat_last: bool = False, error: Exception | None = None, on_first_call=None):
        self.replies = list(replies)
        self.repeat_last = repeat_last
        self.error = error
        self.calls: list[dict] = []
        self.on_first_call = on_first_call  # simule un message client arrivant PENDANT le traitement

    def create(self, *, model, system, tools, messages, max_tokens):
        self.calls.append(copy.deepcopy(dict(model=model, system=system, tools=tools, messages=messages)))
        if len(self.calls) == 1 and self.on_first_call:
            self.on_first_call()
        if self.error:
            raise self.error
        if len(self.replies) > 1 or not self.repeat_last:
            return self.replies.pop(0)
        return self.replies[0]


class FakeSender:
    def __init__(self, fail_times: int = 0, permanent: bool = False, profile_name: str | None = None, profile_error: bool = False):
        self.profile_name = profile_name
        self.profile_error = profile_error
        self.sent: list[tuple[str, str]] = []
        self.fail_times = fail_times
        self.permanent = permanent

    def send_text(self, recipient_id, text):
        if self.fail_times:
            self.fail_times -= 1
            raise SendError("échec simulé", permanent=self.permanent)
        self.sent.append((recipient_id, text))
        return f"mid_out_{len(self.sent)}"

    def get_profile(self, recipient_id):
        if self.profile_error:
            raise SendError("profil indisponible", permanent=False)
        return {"name": self.profile_name} if self.profile_name else None

    def factory(self):
        return lambda channel_type, token: self


class FakeEmail:
    def __init__(self, fail_times: int = 0):
        self.sent: list[dict] = []
        self.fail_times = fail_times

    def send(self, to, subject, body):
        if self.fail_times:
            self.fail_times -= 1
            from app.notify.email import EmailError
            raise EmailError("échec simulé")
        self.sent.append({"to": list(to), "subject": subject, "body": body})
