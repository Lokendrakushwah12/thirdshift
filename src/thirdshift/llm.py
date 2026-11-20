"""Every model call in thirdshift goes through here. Any OpenAI-compatible endpoint works;
the default is a local Ollama."""

import json
import os
import urllib.request

BASE_URL = os.environ.get("THIRDSHIFT_BASE_URL", "http://localhost:11434/v1")
MODEL = os.environ.get("THIRDSHIFT_MODEL", "hf.co/Qwen/Qwen2.5-3B-Instruct-GGUF:Q4_K_M")
SMALL_MODEL = os.environ.get("THIRDSHIFT_SMALL_MODEL", "hf.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M")
API_KEY = os.environ.get("THIRDSHIFT_API_KEY", "none")  # Ollama ignores it; hosted providers need it

calls: list[dict] = []  # one entry per call: model, prompt and completion tokens


gateway = None  # set by use(); when present, every call without an explicit model goes through it


def use(installed) -> None:
    global gateway
    gateway = installed


def complete(system: str, user: str, max_tokens: int, schema: dict | None, model: str) -> tuple[str, int]:
    """One raw call to one model. Returns the reply text and the tokens it used."""
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,  # always capped: a small model can otherwise generate until it times out
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "reply", "schema": schema}}
    request = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        reply = json.load(response)
    usage = reply.get("usage", {})
    calls.append({"model": model, "prompt": usage.get("prompt_tokens", 0), "completion": usage.get("completion_tokens", 0)})
    return reply["choices"][0]["message"]["content"], usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)


def write(system: str, user: str, max_tokens: int = 700, schema: dict | None = None, model: str | None = None) -> str:
    """The model's reply as text. With a schema, the reply is constrained to JSON matching it."""
    if gateway is not None and model is None:
        return gateway.write(system, user, max_tokens, schema)
    return complete(system, user, max_tokens, schema, model or MODEL)[0]


def ask(system: str, user: str, schema: dict, max_tokens: int = 400) -> dict:
    """A JSON reply matching the schema. Raises ValueError if the reply was cut off or malformed."""
    text = write(system, user, max_tokens, schema)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise ValueError(f"the model's JSON reply was cut off or malformed: {text[-80:]!r}") from None
