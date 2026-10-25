"""InteractionService port + Form (de)serialization.

What a tool calls to run an interactive round-trip: ask the user with buttons or a
dropdown, or open a modal form. The platform-posting verbs live on ChatClient;
this port is the higher-level, store-backed orchestration — resolve the
conversation, register the pending interaction/form, post, and match the callback
later. Keeps the tool layer free of the store/posting concretes.

A form is collected in one modal submit. Because a modal opens only from a click's
short-lived trigger, the flow is two-step: post a "fill in" button now, open the
modal when it's clicked, feed the submission back as a new message. The
(de)serialization stashes the spec between those steps (the store holds an opaque
JSON string; only this layer knows the shape).
"""

import json
from collections.abc import Sequence
from typing import Any, Protocol

from crucible.ports.chat.types import (
    ACTION_BUTTON,
    Action,
    ApprovalCard,
    Choice,
    Form,
    FormField,
    PreviewRow,
)

# How ``ask`` renders the question. The first two carry options of the caller's
# own; the pickers are filled by the platform with its people / channels.
ASK_BUTTONS = "buttons"
ASK_SELECT = "select"
ASK_USERS = "users"
ASK_CHANNELS = "channels"
ASK_STYLES = (ASK_BUTTONS, ASK_SELECT, ASK_USERS, ASK_CHANNELS)


class InteractionService(Protocol):
    """``runtime_session_id`` ties the interaction back to the conversation the
    calling turn runs inside (the tool forwards it opaquely); the service resolves
    where to post and registers the pending interaction/form."""

    async def ask(
        self,
        agent: str,
        runtime_session_id: str,
        prompt: str,
        options: list[str],
        *,
        style: str = ASK_BUTTONS,
    ) -> bool:
        """Post the choices (``style`` ∈ ``ASK_STYLES``); the click returns later
        as a message. False if the conversation couldn't be resolved."""
        ...

    async def open_form(self, agent: str, runtime_session_id: str, form: Form) -> bool:
        """Post the button that opens ``form``; the submission returns later as a
        message. False if the conversation couldn't be resolved."""
        ...

    async def open_screen(
        self, agent: str, runtime_session_id: str, name: str, *, user_id: str = "",
        data: dict[str, Any] | None = None,
    ) -> bool:
        """Post one of the engine's own screens here, opened at ``data`` (none =
        its first page). Unlike the two above, the clicks never come back as a
        turn — the engine answers them and rewrites the message. False if no
        screen answers to ``name``, or it refuses to appear here."""
        ...

    def screen_names(self) -> tuple[str, ...]:
        """Which names ``open_screen`` accepts in this deployment."""
        ...


def form_to_json(form: Form) -> str:
    return json.dumps(
        {
            "title": form.title,
            "intro": form.intro,
            "submit_label": form.submit_label,
            "open_label": form.open_label,
            "fields": [
                {
                    "name": f.name,
                    "label": f.label,
                    "type": f.type,
                    "options": list(f.options),
                    "optional": f.optional,
                    "placeholder": f.placeholder,
                    "help_text": f.help_text,
                }
                for f in form.fields
            ],
        }
    )


def form_from_json(spec: str) -> Form:
    d = json.loads(spec)
    return Form(
        title=d["title"],
        intro=d.get("intro", ""),
        submit_label=d.get("submit_label", "Submit"),
        open_label=d.get("open_label", ""),
        fields=tuple(
            FormField(
                name=f["name"],
                label=f["label"],
                type=f.get("type", "text"),
                options=tuple(f.get("options", ())),
                optional=f.get("optional", False),
                placeholder=f.get("placeholder", ""),
                help_text=f.get("help_text", ""),
            )
            for f in d["fields"]
        ),
    )


def actions_to_json(actions: Sequence[Action]) -> str:
    """A card's controls as the store keeps them: every field, the context too
    (it carries the token a click answers)."""
    return json.dumps([
        {
            "id": a.id, "label": a.label, "value": a.value, "style": a.style, "context": dict(a.context),
            "kind": a.kind, "options": [{"label": c.label, "value": c.value} for c in a.options],
        }
        for a in actions
    ])


def actions_from_json(spec: str) -> tuple[Action, ...]:
    return tuple(
        Action(
            id=a["id"], label=a["label"], value=a.get("value", ""), style=a.get("style", ""),
            context=dict(a.get("context") or {}), kind=a.get("kind", ACTION_BUTTON),
            options=tuple(Choice(label=c["label"], value=c["value"]) for c in a.get("options", ())),
        )
        for a in json.loads(spec)
    )


def approval_card_to_json(card: ApprovalCard) -> str:
    """A card as the store keeps it, whole: what a gateway redraws it from."""
    return json.dumps({
        "agent": card.agent, "headline": card.headline, "icon": card.icon, "title": card.title,
        "tool": card.tool,
        "fields": [
            {"label": r.label, "value": r.value, "before": r.before, "passage": r.passage} for r in card.fields
        ],
        "block_label": card.block_label, "block": card.block, "danger": card.danger,
        "expires_at": card.expires_at, "verdict": card.verdict, "grant_s": card.grant_s,
    })


def approval_card_from_json(spec: str) -> ApprovalCard:
    d = json.loads(spec)
    return ApprovalCard(
        agent=d["agent"], headline=d["headline"], icon=d.get("icon", "⚙️"), title=d.get("title", ""),
        tool=d.get("tool", ""),
        fields=tuple(
            PreviewRow(label=f["label"], value=f["value"], before=f.get("before", ""), passage=f.get("passage", False))
            for f in d.get("fields", ())
        ),
        block_label=d.get("block_label", ""), block=d.get("block", ""), danger=d.get("danger", False),
        expires_at=d.get("expires_at", ""), verdict=d.get("verdict", ""), grant_s=d.get("grant_s", 0),
    )
