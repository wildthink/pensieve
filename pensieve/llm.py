"""Where summaries, topic names and insights get written. One setting, `ai`, picks the engine:

    builtin  Qwen 3.5 9B run by Pensieve itself with MLX (Apple Silicon; ~5 GB download on first use)
    apple    Apple Intelligence's on-device model (macOS 26+), through the app's pensieve-fm helper; no download
    server   any OpenAI-compatible server, local (LM Studio, Ollama…) or hosted, at llm_url / llm_model (+ llm_key)
    claude   the Claude Code CLI (`claude -p`): uses your Claude account; text leaves this Mac
    codex    the Codex CLI (`codex exec`): uses your OpenAI account; text leaves this Mac
    none     no AI: maps, search, activity, keyword topics and team commits still work
    auto     not chosen yet: use `server` if one is running, otherwise nothing (and the UI asks)
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import httpx

from pensieve import config, settings


OWN_PREFIX = "pensieve-ai-"


class Offline(Exception):
    """The chosen engine isn't available right now (not running, not chosen, still downloading)."""


BUILTIN_PORT = 8766
IDLE_SECONDS = 10 * 60   # unload the built-in model after this long unused (it holds ~6 GB of memory)
MIN_RAM_GB = 16
_builtin = {"proc": None, "status": "off", "detail": "", "lock": threading.Lock(), "last_used": 0.0,
            "download": None, "progress": None}


def ram_gb():
    try:
        return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / 2**30
    except (ValueError, OSError):
        return 0


def on_battery() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        return "Battery Power" in subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def paused() -> str | None:
    """Why background AI work should wait right now, or None."""
    if not settings.get("ai_on_battery") and on_battery():
        return "Paused on battery power"
    return None


def _apple_silicon():
    return sys.platform == "darwin" and platform.machine() == "arm64"


def _auth(key=None):
    key = settings.get("llm_key") if key is None else key
    return {"Authorization": f"Bearer {key}"} if key else {}


def _server_up(url, timeout=1.5, key=None):
    try:
        return httpx.get(f"{url.rstrip('/')}/models", timeout=timeout, headers=_auth(key)).status_code == 200
    except httpx.HTTPError:
        return False


def is_local(url) -> bool:
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "") in ("localhost", "127.0.0.1", "::1") or (urlparse(url).hostname or "").endswith(".local")


def server_models(url, key=None, timeout=4):
    """Check an OpenAI-compatible server and list its models: {ok, models, error}."""
    try:
        r = httpx.get(f"{url.rstrip('/')}/models", timeout=timeout, headers=_auth(key))
    except httpx.HTTPError as e:
        return dict(ok=False, models=[], error=f"Can't reach {url} ({type(e).__name__})")
    if r.status_code in (401, 403):
        return dict(ok=False, models=[], error="The server needs a valid API key")
    if r.status_code != 200:
        return dict(ok=False, models=[], error=f"{url}/models answered {r.status_code}")
    try:
        ids = [m["id"] for m in r.json().get("data", []) if isinstance(m, dict) and m.get("id")]
    except (ValueError, AttributeError):
        return dict(ok=False, models=[], error="That isn't an OpenAI-compatible server (no model list)")
    return dict(ok=True, models=sorted(ids), error=None)


def options():
    """The engines a user can pick, with whether each is usable on this Mac."""
    s = settings.load()
    have_mlx = _apple_silicon() and ram_gb() >= MIN_RAM_GB
    if have_mlx:
        try:
            import mlx_lm  # noqa: F401
        except ImportError:
            have_mlx = False
    local = _local_copy(settings.get("builtin_model"))
    return [
        dict(id="builtin", label="Built-in: Qwen 3.5 9B", privacy="local", available=have_mlx,
             description="Pensieve runs the model itself, in the background at low priority. Nothing leaves this Mac.",
             note=("Uses the copy LM Studio already downloaded." if local else
                   "Downloads about 6 GB in the background; you can keep working.") if have_mlx
             else f"Needs an Apple Silicon Mac with {MIN_RAM_GB} GB of memory."),
        dict(id="apple", label="Apple Intelligence", privacy="local", available=(fm := apple_status())["available"],
             description="Apple's on-device model, built into macOS. Nothing leaves this Mac and there's nothing to "
                         "download. Writes summaries, topic names and team summaries; the cross-topic insights "
                         "(themes, open threads) need a larger model, so they stay off with this one.",
             note="Ready." if fm["available"] else fm.get("reason", "")),
        dict(id="server", label="Model server", privacy="local" if is_local(s["llm_url"]) else "cloud", available=True,
             url=s["llm_url"], model=s["llm_model"], key_set=bool(s["llm_key"]), up=(up := _server_up(s["llm_url"])),
             connections=[dict(name=c["name"], url=c["url"], model=c["model"], key_set=bool(c["key"]), local=is_local(c["url"]))
                          for c in s["llm_connections"]],
             description="Any model with an OpenAI-compatible API at an address: on this Mac (LM Studio, Ollama), on "
                         "another computer, or a cloud provider. "
                         + ("Nothing leaves this Mac." if is_local(s["llm_url"]) else "Session and code text is sent to that server."),
             note=f"Connected to {s['llm_url']}." if up else f"Can't reach {s['llm_url']} right now."),
        dict(id="claude", label="Claude Code", privacy="cloud", available=bool(shutil.which("claude") or _find("claude")),
             description="Uses your Claude Code login (claude -p). Session and code text is sent to Anthropic.",
             note="Installed." if (shutil.which("claude") or _find("claude")) else "Claude Code isn't installed."),
        dict(id="codex", label="Codex", privacy="cloud", available=bool(shutil.which("codex") or _find("codex")),
             description="Uses your Codex login (codex exec). Session and code text is sent to OpenAI.",
             note="Installed." if (shutil.which("codex") or _find("codex")) else "Codex isn't installed."),
        dict(id="none", label="Off", privacy="local", available=True,
             description="No AI. Maps, search, activity, keyword topics and team commits still work.", note=""),
    ]


def _find(name):
    for d in (os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.npm-global/bin")):
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    return None


def provider() -> str:
    """The engine actually in use ('auto' resolved)."""
    a = settings.get("ai")
    if a == "auto":
        return "server" if _server_up(settings.get("llm_url")) else "none"
    return a


def status() -> dict:
    a = settings.get("ai")
    p = provider()
    if p == "none":
        st, detail = "off", ("Not set up yet." if a == "auto" else "AI is off.")
    elif p == "server":
        ok = _server_up(settings.get("llm_url"))
        st, detail = ("ready", f"{settings.get('llm_model')} at {settings.get('llm_url')}") if ok else ("offline", f"Can't reach {settings.get('llm_url')}")
    elif p == "apple":
        fm = apple_status()
        st, detail = ("ready", "Apple's on-device model") if fm["available"] else ("error", fm.get("reason", ""))
    elif p == "builtin":
        st, detail = _builtin["status"], _builtin["detail"]
        if st == "off":
            st, detail = ("ready", "Loads when there's work, and unloads after 10 minutes idle.") if _have_weights() \
                else ("downloading", "Waiting to download…")
    else:
        exe = shutil.which(p) or _find(p)
        st, detail = ("ready", exe) if exe else ("error", f"{p} isn't installed")
    why = paused() if p != "none" else None
    return dict(setting=a, provider=p, configured=a != "auto", status=st, detail=detail, options=options(),
                insights=writes_insights(),
                enabled=p != "none", paused=why, progress=_builtin["progress"] if p == "builtin" else None,
                on_battery_setting=settings.get("ai_on_battery"))


# ---- builtin: an mlx_lm server we start and stop ------------------------------
def _local_copy(model: str):
    """An MLX copy LM Studio already downloaded (same repo name), so we don't fetch 5 GB again."""
    import glob
    for d in glob.glob(os.path.expanduser(f"~/.lmstudio/models/*/{model.split('/')[-1]}")):
        if os.path.exists(os.path.join(d, "config.json")):
            return d
    return None

def _have_weights():
    model = settings.get("builtin_model")
    if _local_copy(model):
        return True
    from huggingface_hub import try_to_load_from_cache
    return isinstance(try_to_load_from_cache(model, "config.json"), str) and \
        isinstance(try_to_load_from_cache(model, "model.safetensors.index.json"), str)


def start_download():
    """Fetch the built-in model's weights on a background thread, reporting progress. Safe to call repeatedly."""
    t = _builtin["download"]
    if (t and t.is_alive()) or _have_weights():
        return
    model = settings.get("builtin_model")

    def run():
        from huggingface_hub import HfApi, snapshot_download
        from huggingface_hub.constants import HF_HUB_CACHE
        os.environ.pop("HF_HUB_OFFLINE", None)
        try:
            info = HfApi().model_info(model, files_metadata=True)
            total = sum(x.size or 0 for x in info.siblings) or 1
            folder = os.path.join(HF_HUB_CACHE, "models--" + model.replace("/", "--"))
            done = threading.Event()

            def watch():  # progress = bytes on disk under the model's cache folder (blobs + in-flight .incomplete)
                while not done.wait(2):
                    n = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(folder) for f in fs
                            if not os.path.islink(os.path.join(d, f)))
                    _builtin.update(progress=min(0.99, n / total),
                                    detail=f"Downloading Qwen 3.5 9B in the background: {n / 1e9:.1f} of {total / 1e9:.1f} GB")
            threading.Thread(target=watch, daemon=True).start()
            _builtin.update(status="downloading", progress=0.0, detail="Starting the download…")
            snapshot_download(model)
            done.set()
            _builtin.update(status="off", progress=1.0, detail="")
        except Exception as e:
            _builtin.update(status="error", detail=f"Download failed: {e!r}"[:300])
    _builtin["download"] = threading.Thread(target=run, daemon=True, name="builtin-model-download")
    _builtin["download"].start()


def sweep():
    """Unload the built-in model when it hasn't been used for IDLE_SECONDS (frees ~6 GB)."""
    p = _builtin["proc"]
    if p and p.poll() is None and time.time() - _builtin["last_used"] > IDLE_SECONDS:
        stop_builtin()


def _ensure_builtin():
    url = f"http://127.0.0.1:{BUILTIN_PORT}/v1"
    _builtin["last_used"] = time.time()
    if _server_up(url):
        _builtin.update(status="ready", detail=settings.get("builtin_model"))
        return url
    if not _have_weights():  # never block on a 6 GB download: fetch it in the background and try again later
        start_download()
        raise Offline(_builtin["detail"] or "Downloading the built-in model")
    with _builtin["lock"]:
        p = _builtin["proc"]
        if p is None or p.poll() is not None:
            model = settings.get("builtin_model")
            from huggingface_hub import try_to_load_from_cache
            local = _local_copy(model)
            _builtin.update(status="starting", detail="Loading Qwen 3.5 9B…")
            log = open(config.DATA_DIR / "builtin-llm.log", "a")
            argv = [sys.executable, "-m", "mlx_lm", "server", "--model", local or model, "--host", "127.0.0.1",
                    "--port", str(BUILTIN_PORT)]
            if os.path.exists("/usr/sbin/taskpolicy"):  # background QoS: yields to whatever you're doing
                argv = ["/usr/sbin/taskpolicy", "-b"] + argv
            _builtin["proc"] = subprocess.Popen(argv, stdout=log, stderr=log, start_new_session=True)
    for _ in range(600):  # loading at background priority can take a few minutes on a busy Mac
        if _server_up(url):
            _builtin.update(status="ready", detail=settings.get("builtin_model"))
            return url
        if _builtin["proc"].poll() is not None:
            _builtin.update(status="error", detail="The built-in model stopped; see ~/.pensieve/builtin-llm.log")
            raise Offline(_builtin["detail"])
        time.sleep(1)
    raise Offline("The built-in model is still loading")


def stop_builtin():
    p = _builtin["proc"]
    if p and p.poll() is None:
        p.terminate()
    _builtin.update(proc=None, status="off" if _builtin["status"] != "downloading" else "downloading",
                    detail="" if _builtin["status"] != "downloading" else _builtin["detail"])


# ---- completion ---------------------------------------------------------------
def _json_from(t: str):
    return json.loads(t[t.find("{"):t.rfind("}") + 1])


def _schema_hint(schema):
    return ("\n\nReply with only a JSON object (no prose, no code fences) that matches this JSON schema:\n"
            + json.dumps(schema))


def _strip_think(t):
    if "</think>" in t:
        t = t.split("</think>", 1)[1]
    return t.strip()


def _openai(url, model, msgs, max_tokens, temperature, schema, structured=True, headers=None):
    body = {"model": model, "messages": msgs, "max_tokens": max_tokens, "temperature": temperature,
            "reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False}}
    if schema and structured:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "strict": True, "schema": schema}}
    try:
        r = httpx.post(f"{url.rstrip('/')}/chat/completions", timeout=600, json=body, headers=headers or {})
    except httpx.ConnectError as e:
        raise Offline(f"No model server at {url}") from e
    r.raise_for_status()
    return _strip_think(r.json()["choices"][0]["message"].get("content") or "")


def _cli(argv_fn, prompt, system, timeout=600):
    """Run an agent CLI once, non-interactively, with no tools, and return its final text."""
    full = (system + "\n\n" if system else "") + prompt
    # an empty working dir: the agent has nothing to read or edit; the prefix lets the indexer skip the session the
    # CLI records for this call (it isn't the user's work)
    with tempfile.TemporaryDirectory(prefix=OWN_PREFIX) as tmp:
        argv, out_file = argv_fn(full, tmp)
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=tmp, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            raise RuntimeError(f"{argv[0]} failed: {(r.stderr or r.stdout).strip()[:300]}")
        if out_file and os.path.exists(out_file):
            return open(out_file).read().strip()
        return r.stdout.strip()


def _claude_argv(text, tmp):
    exe = shutil.which("claude") or _find("claude")
    if not exe:
        raise Offline("Claude Code isn't installed")
    # The prompt is indexed text (sessions, documents), which anyone could have planted instructions in, so the call
    # gets no tools at all: no built-in ones (--tools ""), none of the user's MCP servers (an empty --mcp-config,
    # strictly) and no skills. A list of disallowed tools misses every MCP tool.
    return [exe, "-p", text, "--model", settings.get("agent_model") or "haiku", "--output-format", "text",
            "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers": {}}', "--disable-slash-commands",
            "--max-turns", "1"], None


# Codex features that give the model a tool (shell, MCP apps and plugins, browser, computer use, sub-agents...).
# The read-only sandbox still lets a shell read any file, so it's off too.
CODEX_OFF = ("shell_tool", "unified_exec", "code_mode_host", "apps", "plugins", "browser_use", "computer_use",
             "in_app_browser", "tool_suggest", "skill_mcp_dependency_install", "multi_agent", "view_image",
             "image_generation", "goals", "hooks")


def _codex_argv(text, tmp):
    exe = shutil.which("codex") or _find("codex")
    if not exe:
        raise Offline("Codex isn't installed")
    out = os.path.join(tmp, "last.txt")
    # --ignore-user-config drops the MCP servers in ~/.codex/config.toml (sign-in still works); no tools, as for claude
    off = [a for f in CODEX_OFF for a in ("--disable", f)]
    return [exe, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--sandbox", "read-only", *off, "-c", 'web_search="disabled"', "-c", "include_apply_patch_tool=false",
            "--output-last-message", out, text], out


# ---- apple: Apple Intelligence's on-device model, through the Mac app's pensieve-fm helper -----------------------
APPLE_CONTEXT = 4096   # tokens the on-device model sees at once: instructions, prompt and reply together
APPLE_MAX_REPLY = 1500  # leaves room for a useful prompt; the long JSON replies (insights) fit in this
_apple_cache = {"at": 0.0, "status": None}


def writes_insights() -> bool:
    """Can the engine in use write the cross-topic insights? Not the on-device model: their JSON reply is long, and
    its guided output stops early, at random, often missing whole sections (even with the prompt cut short)."""
    return provider() != "apple"


def apple_helper():
    """pensieve-fm in the Mac app: where the app said (PENSIEVE_FM, set when it starts the backend), else an
    installed copy of the app."""
    for p in (os.environ.get("PENSIEVE_FM"), "/Applications/Pensieve.app/Contents/MacOS/pensieve-fm",
              os.path.expanduser("~/Applications/Pensieve.app/Contents/MacOS/pensieve-fm")):
        if p and os.access(p, os.X_OK):
            return p
    return None


def apple_status() -> dict:
    """{available, reason}: whether the on-device model can run now. Cached for a minute (the UI asks often)."""
    if _apple_cache["status"] and time.time() - _apple_cache["at"] < 60:
        return _apple_cache["status"]
    exe = apple_helper() if sys.platform == "darwin" else None
    if not exe:
        st = dict(available=False, reason="Needs the Pensieve Mac app on macOS 26 or later.")
    else:
        try:
            r = subprocess.run([exe, "status"], capture_output=True, text=True, timeout=20)
            st = json.loads(r.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            st = dict(available=False, reason=f"pensieve-fm didn't answer ({type(e).__name__})")
    _apple_cache.update(at=time.time(), status=st)
    return st


def _complete(schema, v) -> bool:
    """Does `v` have everything the schema requires (keys, at least minItems)? The on-device model's guided
    output can stop early, without an error, when the prompt and the schema leave it too little room."""
    t = schema.get("type")
    if t == "object":
        return isinstance(v, dict) and all(k in v for k in schema.get("required", [])) and \
            all(_complete(sub, v[k]) for k, sub in schema.get("properties", {}).items() if k in v)
    if t == "array":
        return isinstance(v, list) and len(v) >= schema.get("minItems", 0) and \
            all(_complete(schema.get("items", {}), x) for x in v)
    return True


def _apple(prompt, system, max_tokens, temperature, schema):
    """One reply from the on-device model. With `schema`, its output is guided to the schema (parsed JSON back).
    The prompt is cut to fit the small context window, from the end (as the other prompts are cut), so the
    instructions and the first part of the text are what the model sees; cut further if it still doesn't fit, or if
    a JSON reply comes back incomplete. The schema is in the model's context too, so it takes from the budget."""
    exe = apple_helper()
    if not exe:
        raise Offline("Apple Intelligence needs the Pensieve Mac app on macOS 26 or later")
    max_tokens = min(max_tokens, APPLE_MAX_REPLY)
    used = (len(system) + (len(json.dumps(schema)) if schema else 0)) / 3 + 200
    chars = max(1000, int((APPLE_CONTEXT - max_tokens - used) * 3))  # ~3 characters a token (code, prose)
    for _ in range(3):
        req = dict(system=system, prompt=prompt[:chars], max_tokens=max_tokens, temperature=temperature, schema=schema)
        r = subprocess.run([exe, "respond"], input=json.dumps(req), capture_output=True, text=True, timeout=600)
        if r.returncode == 0:
            text = json.loads(r.stdout)["text"]
            if not schema:
                return text.strip()
            d = json.loads(text)
            if _complete(schema, d):
                return d
            chars = int(chars * 0.75)
            continue
        why = (r.stderr or r.stdout).strip()[:300]
        if r.returncode == 2:
            _apple_cache["status"] = None
            raise Offline(why)
        if r.returncode != 3:  # 4: guardrails declined this text; 1: anything else
            raise RuntimeError(f"Apple Intelligence: {why}")
        chars = int(chars * 0.6)
    raise RuntimeError("Apple Intelligence: the text doesn't fit the on-device model (its replies came back incomplete)")


def complete(prompt: str, system: str = "", max_tokens: int = 200, temperature: float = 0.2, schema: dict | None = None):
    """One-shot completion with the chosen engine. With `schema`, returns the parsed JSON object.
    Raises Offline when no engine is available, so callers can skip AI work quietly."""
    p = provider()
    if p == "none":
        raise Offline("AI is off")
    if p == "apple":
        return _apple(prompt, system, max_tokens, temperature, schema)
    if p in ("claude", "codex"):
        text = _cli(_claude_argv if p == "claude" else _codex_argv, prompt + (_schema_hint(schema) if schema else ""), system)
        return _json_from(text) if schema else _strip_think(text)
    msgs = ([{"role": "system", "content": system}] if system else [])
    if p == "builtin":
        url, model, structured = _ensure_builtin(), settings.get("builtin_model"), False
    else:
        url, model, structured = settings.get("llm_url"), settings.get("llm_model"), True
    content = prompt + (_schema_hint(schema) if schema and not structured else "")
    t = _openai(url, model, msgs + [{"role": "user", "content": content}], max_tokens, temperature, schema, structured,
                headers=_auth() if p == "server" else None)
    return _json_from(t) if schema else t
