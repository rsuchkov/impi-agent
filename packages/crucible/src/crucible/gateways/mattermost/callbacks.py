"""MattermostCallbackCodec: Mattermost's interactive-message callback wire-shape
<-> the neutral interaction callbacks/replies.

Mattermost posts a button/select click to the interact endpoint with the action's
``context`` (we set ``token``/``value`` at post time; a select's pick arrives as
``selected_option``) plus a ``trigger_id`` for opening modals; a dialog submission
posts ``state`` + ``submission``. Responses use MM's "update the message" /
ephemeral shapes. This is the ONLY place that shape lives.
"""

from crucible.interactions.callbacks import (
    ActionCallback,
    CommandCallback,
    DialogCallback,
)


class MattermostCallbackCodec:
    def parse_action(self, body: dict) -> ActionCallback:
        context = body.get("context") or {}
        return ActionCallback(
            token=str(context.get("token") or ""),
            # Buttons carry the value in context.value (set at post time); a select's
            # picked value arrives as context.selected_option (MM adds it).
            value=str(context.get("value") or context.get("selected_option") or ""),
            form_token=str(context.get("form") or ""),
            trigger=str(body.get("trigger_id") or ""),
            user_id=str(body.get("user_id") or ""),
            # Set by the engine at post time: MM echoes the whole context back, so
            # a picker's own context says what kind of id came back.
            pick=str(context.get("pick") or ""),
            # A screen's click carries its own routing in the same context, and MM
            # names the post it came from — that's the message to redraw.
            screen=str(context.get("screen") or ""),
            state=str(context.get("state") or ""),
            post_id=str(body.get("post_id") or ""),
            approval=str(context.get("approval") or ""),
        )

    def parse_dialog(self, body: dict) -> DialogCallback:
        return DialogCallback(
            state=str(body.get("state") or ""),
            submission=_normalize(body.get("submission") or {}),
            cancelled=bool(body.get("cancelled")),
            user_id=str(body.get("user_id") or ""),
        )

    def parse_command(self, body: dict) -> CommandCallback:
        # MM posts a slash command as a form; a command typed inside a thread
        # carries that thread's root_id (verified against a live server).
        return CommandCallback(
            command=str(body.get("command") or ""),
            text=str(body.get("text") or ""),
            channel_id=str(body.get("channel_id") or ""),
            root_id=str(body.get("root_id") or ""),
            user_id=str(body.get("user_id") or ""),
            user_name=str(body.get("user_name") or ""),
            token=str(body.get("token") or ""),
            response_url=str(body.get("response_url") or ""),
        )

    def reply_replace(self, text: str) -> dict:
        return {"update": {"message": text, "props": {"attachments": []}}}

    def reply_notice(self, text: str) -> dict:
        return {"ephemeral_text": text}

    def reply_none(self) -> dict:
        return {}

    def reply_ack(self, text: str) -> dict:
        # Answers the command POST itself: visible only to the invoker, and
        # replaced by nothing — the real answer is delivered by the agent later.
        return {"response_type": "ephemeral", "text": text}


def _normalize(submission: dict) -> dict[str, str]:
    """Mattermost's per-element value shapes -> the neutral string-per-field the
    dispatcher renders: a checkbox arrives as a real boolean, a multiselect as
    one comma-separated string, an untouched optional as null."""
    out: dict[str, str] = {}
    for name, value in submission.items():
        if isinstance(value, bool):
            out[name] = "yes" if value else "no"
        elif value is None:
            out[name] = ""
        elif isinstance(value, list):  # defensive: some builds send a real list
            out[name] = ", ".join(str(v) for v in value)
        else:
            out[name] = str(value)
    return out
