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
from typing import Protocol

from crucible.ports.chat.types import Form, FormField


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
        style: str = "buttons",
    ) -> bool:
        """Post the choices (``style`` = "buttons" | "select"); the click returns
        later as a message. False if the conversation couldn't be resolved."""
        ...

    async def open_form(self, agent: str, runtime_session_id: str, form: Form) -> bool:
        """Post the button that opens ``form``; the submission returns later as a
        message. False if the conversation couldn't be resolved."""
        ...


def form_to_json(form: Form) -> str:
    return json.dumps(
        {
            "title": form.title,
            "intro": form.intro,
            "submit_label": form.submit_label,
            "fields": [
                {
                    "name": f.name,
                    "label": f.label,
                    "type": f.type,
                    "options": list(f.options),
                    "optional": f.optional,
                    "placeholder": f.placeholder,
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
        fields=tuple(
            FormField(
                name=f["name"],
                label=f["label"],
                type=f.get("type", "text"),
                options=tuple(f.get("options", ())),
                optional=f.get("optional", False),
                placeholder=f.get("placeholder", ""),
            )
            for f in d["fields"]
        ),
    )
