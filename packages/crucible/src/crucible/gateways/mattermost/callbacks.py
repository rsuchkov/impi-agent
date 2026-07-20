"""MattermostCallbackCodec: Mattermost's interactive-message callback wire-shape
<-> the neutral interaction callbacks/replies.

Mattermost posts a button/select click to the interact endpoint with the action's
``context`` (we set ``token``/``value`` at post time; a select's pick arrives as
``selected_option``) plus a ``trigger_id`` for opening modals; a dialog submission
posts ``state`` + ``submission``. Responses use MM's "update the message" /
ephemeral shapes. This is the ONLY place that shape lives.
"""

from crucible.interactions.callbacks import ActionCallback, DialogCallback


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
        )

    def parse_dialog(self, body: dict) -> DialogCallback:
        return DialogCallback(
            state=str(body.get("state") or ""),
            submission=body.get("submission") or {},
            cancelled=bool(body.get("cancelled")),
            user_id=str(body.get("user_id") or ""),
        )

    def reply_replace(self, text: str) -> dict:
        return {"update": {"message": text, "props": {"attachments": []}}}

    def reply_notice(self, text: str) -> dict:
        return {"ephemeral_text": text}

    def reply_none(self) -> dict:
        return {}
