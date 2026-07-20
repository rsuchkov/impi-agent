"""FormService port + Form (de)serialization.

A form is a modal collected in one submit. Because a modal dialog opens only from
a click's short-lived trigger, the flow is two-step: post a "fill in" button now,
open the modal when it's clicked, feed the submission back as a new message. The
(de)serialization stashes the spec between those steps (the store holds an opaque
JSON string; only this layer knows the shape).
"""

import json
from typing import Protocol

from crucible.ports.chat.types import Form, FormField


class FormService(Protocol):
    async def open(self, agent: str, runtime_session_id: str, form: Form) -> bool:
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
