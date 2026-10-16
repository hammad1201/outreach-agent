"""Model calls. Two providers behind one method:

- "cli": the Claude Code command-line tool, billed to the Claude subscription it is logged into.
- "api": the Anthropic API, billed to API credits (ANTHROPIC_API_KEY).

Every call returns parsed JSON. Prompts carry untrusted text (scraped websites, prospect replies),
so calls run with no tools at all — except discovery, which gets web search and fetch only. An
instruction injected into a web page therefore has nothing it can act on beyond reading more pages.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from .config import Config, data_dir
from .db import DB, iso, utcnow

log = logging.getLogger("outreach.brain")


# Anthropic API prices, $ per million tokens: (input, output, cache read). Cache writes cost 1.25x input.
# Web search is $10 per 1,000 searches.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20), "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20), "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-fable-5-1": (10.0, 50.0, 0.25),
}
WEB_SEARCH_USD = 0.01


def call_cost(model: str, tokens_in: int, tokens_out: int, searches: int = 0,
              cache_write: int = 0, cache_read: int = 0) -> float:
    p_in, p_out, p_read = PRICES.get(model, PRICES["claude-opus-5-5"])
    return ((tokens_in + 1.25 * cache_write) * p_in + cache_read * p_read + tokens_out * p_out) / 1e6 \
        + searches * WEB_SEARCH_USD


USAGE_LINE = re.compile(r"Current (session|week \(all models\)):\s*(\d+)% used(?:\s*·\s*resets ([^(\n]+))?")


def parse_usage(text: str) -> dict:
    """Claude Code's /usage text → {"session": (23, "Oct 17 at 12:50am"), "week": (20, "Oct 20 at 2pm")}."""
    out = {}
    for which, pct, resets in USAGE_LINE.findall(text or ""):
        out["session" if which == "session" else "week"] = (int(pct), (resets or "").strip())
    return out


class BrainError(Exception):
    pass


class BudgetExhausted(BrainError):
    pass


class SpendLimitReached(BrainError):
    """The month's API budget is used up. Stops API calls until next month or until the budget is raised."""


def extract_json(text: str):
    """Pull the first JSON object or array out of model text (fenced or bare)."""
    text = (text or "").strip()
    if "```" in text:
        for chunk in text.split("```")[1::2]:
            chunk = chunk.removeprefix("json").strip()
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                continue
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                return decoder.raw_decode(text[i:])[0]
            except json.JSONDecodeError:
                continue
    raise BrainError(f"no JSON in model output: {text[:200]!r}")


def _cli_binary(cfg: Config) -> str:
    for candidate in (cfg.brain.cli_path, shutil.which("claude"),
                      str(Path.home() / ".local/bin/claude"),
                      str(Path.home() / ".local/node/bin/claude"),
                      str(Path.home() / ".claude/local/claude")):
        if candidate and Path(candidate).exists():
            return candidate
    raise BrainError("Claude Code CLI not found. Install it with "
                     "`npm install -g @anthropic-ai/claude-code`, then run `claude` once to log in.")


class Brain:
    def __init__(self, cfg: Config, db: DB):
        self.cfg = cfg
        self.db = db
        self.model = cfg.brain.model
        self._gate = asyncio.Semaphore(2)
        self._api = None
        self._usage_cache = (0.0, None)
        if cfg.brain.provider not in ("cli", "api"):
            raise BrainError(f"unknown brain provider {cfg.brain.provider!r} (use 'cli' or 'api')")

    @property
    def provider(self) -> str:
        """Switchable at runtime from Discord; falls back to config.toml."""
        return self.db.get("brain_provider") or self.cfg.brain.provider

    def set_provider(self, provider: str) -> None:
        if provider not in ("cli", "api"):
            raise BrainError("use 'api' or 'subscription'")
        if provider == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
            raise BrainError("ANTHROPIC_API_KEY isn't set in outreach/.env yet — add it, then restart once")
        self.db.set("brain_provider", provider)
        self.db.log("provider", provider)

    # ── budget ──

    def _budget_key(self) -> str:
        return f"brain_calls:{utcnow().date().isoformat()}"

    def calls_today(self) -> int:
        return int(self.db.get(self._budget_key(), "0") or 0)

    def budget_left(self) -> int:
        return max(0, self.cfg.brain.max_calls_per_day - self.calls_today())

    # ── usage reporting ──

    def api_spend(self, since: datetime) -> float:
        """What outreach itself has spent on API credits since `since`, from the logged token counts."""
        total = 0.0
        for row in self.db.all("SELECT detail FROM events WHERE kind='api_usage' AND ts >= ?", (iso(since),)):
            d = json.loads(row["detail"] or "{}")
            total += d["cost"] if "cost" in d else call_cost(self.model, d.get("in", 0), d.get("out", 0),
                                                              d.get("web_searches", 0) or 0)
        return total

    async def subscription_usage(self) -> dict | None:
        """The Claude plan's session and weekly usage, via Claude Code's own /usage command.
        That runs no model call and never touches the login token. Cached for 5 minutes."""
        fetched_at, cached = self._usage_cache
        if time.monotonic() - fetched_at < 300:
            return cached
        result = None
        try:
            workdir = data_dir() / "brain_cwd"
            workdir.mkdir(parents=True, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                _cli_binary(self.cfg), "-p", "/usage", "--output-format", "json", "--strict-mcp-config",
                "--setting-sources", "project", cwd=workdir,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            result = parse_usage(json.loads(out.decode() or "{}").get("result", "")) or None
        except (BrainError, asyncio.TimeoutError, json.JSONDecodeError, OSError):
            result = None
        self._usage_cache = (time.monotonic(), result)
        return result

    # ── public ──

    async def json(self, *, task: str, system: str, prompt: str, schema: dict,
                   web: bool = False, effort: str = "medium", timeout: float = 300):
        if self.budget_left() <= 0:
            raise BudgetExhausted(f"daily model-call budget of {self.cfg.brain.max_calls_per_day} used up")
        budget = self.cfg.brain.api_budget_usd
        if self.provider == "api" and budget:
            month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            spent = self.api_spend(month_start)
            if spent >= budget:
                raise SpendLimitReached(
                    f"outreach has used ${spent:.2f} of your ${budget:.0f} monthly API budget, so API calls are "
                    "stopped. Raise api_budget_usd in config.toml, or type `brain subscription`.")
        self.db.incr(self._budget_key())
        async with self._gate:
            if self.provider == "cli":
                result = await self._cli(system, prompt, schema, web, effort, timeout)
            else:
                result = await self._api_call(system, prompt, schema, web, effort, timeout)
        self.db.log("brain", {"task": task, "provider": self.provider, "web": web})
        return result

    # ── Claude Code CLI (subscription) ──

    async def _cli(self, system, prompt, schema, web, effort, timeout):
        cmd = [
            _cli_binary(self.cfg), "-p",
            "--output-format", "json",
            "--model", self.model,
            "--effort", effort,
            "--system-prompt", system,
            "--json-schema", json.dumps(schema),
            # Ignore the operator's own MCP servers, plugins and hooks: this call needs none of them.
            "--strict-mcp-config",
            "--setting-sources", "project",
            "--no-session-persistence",
        ]
        if web:
            cmd += ["--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch", "WebFetch"]
        else:
            cmd += ["--tools", ""]
        # An empty working directory, so no CLAUDE.md from any project is pulled into the prompt.
        workdir = data_dir() / "brain_cwd"
        workdir.mkdir(parents=True, exist_ok=True)
        environment = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=workdir, env=environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise BrainError(f"Claude Code timed out after {timeout:.0f}s")

        try:
            payload = json.loads(out.decode() or "{}")
        except json.JSONDecodeError:
            payload = {}
        if proc.returncode != 0 or payload.get("is_error"):
            detail = (payload.get("result") or err.decode() or out.decode()).strip()
            if "login" in detail.lower() or "not logged in" in detail.lower():
                raise BrainError("Claude Code is not logged in — run `claude` in a terminal once and log in.")
            raise BrainError(f"Claude Code failed: {detail[:300]}")
        structured = payload.get("structured_output")
        if structured is not None:
            return structured
        return extract_json(payload.get("result", ""))

    # ── Anthropic API (credits) ──

    async def _api_call(self, system, prompt, schema, web, effort, timeout):
        import anthropic

        if self._api is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise BrainError("brain provider is 'api' but ANTHROPIC_API_KEY is not set in .env")
            self._api = anthropic.AsyncAnthropic(timeout=timeout, max_retries=3)

        output_config = {"effort": effort}
        tools = None
        if web:
            # Web results arrive with citations, which can't be combined with an enforced output
            # format, so web calls ask for JSON in the prompt and are parsed leniently instead.
            tools = [
                {"type": "web_search_20260209", "name": "web_search", "max_uses": 20},
                {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 25},
            ]
        else:
            output_config["format"] = {"type": "json_schema", "schema": schema}

        messages = [{"role": "user", "content": prompt}]
        kwargs = dict(
            model=self.model, max_tokens=16000, system=system, messages=messages,
            output_config=output_config,
            # Web search re-reads everything gathered so far at every step; caching makes those
            # re-reads cost a fraction of the price (the API adds cache points after tool results).
            cache_control={"type": "ephemeral"},
            # Server-side fallback: if a safety classifier declines, the API reroutes in the same call.
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        if tools:
            kwargs["tools"] = tools

        totals = {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0, "web_searches": 0, "requests": 0}

        def add(resp):
            u = resp.usage
            totals["in"] += u.input_tokens or 0
            totals["out"] += u.output_tokens or 0
            totals["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
            totals["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
            totals["web_searches"] += getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0
            totals["requests"] += 1

        try:
            response = await self._api.beta.messages.create(**kwargs)
            add(response)
            # Server tools run a sampling loop that can pause; resume it by resending the turn.
            for _ in range(8):
                if response.stop_reason != "pause_turn":
                    break
                messages = [{"role": "user", "content": prompt},
                            {"role": "assistant", "content": response.content}]
                kwargs["messages"] = messages
                response = await self._api.beta.messages.create(**kwargs)
                add(response)
        except anthropic.AuthenticationError:
            raise BrainError("the Anthropic API key in .env was rejected")
        except anthropic.RateLimitError as e:
            raise BrainError(f"Anthropic API rate limit: {e.message}")
        except anthropic.BadRequestError as e:
            raise BrainError(f"Anthropic API rejected the request: {e.message}")
        except anthropic.APIStatusError as e:
            raise BrainError(f"Anthropic API error {e.status_code}: {e.message}")
        except anthropic.APIConnectionError:
            raise BrainError("could not reach the Anthropic API")

        if response.stop_reason == "refusal":
            raise BrainError("the model declined this request")
        if response.stop_reason == "max_tokens":
            raise BrainError("the model ran out of output tokens")

        # Every request of a resumed turn is billed, so log the sum, not just the last one.
        self.db.log("api_usage", {**totals, "model": self.model, "cost": round(call_cost(
            self.model, totals["in"], totals["out"], totals["web_searches"],
            totals["cache_write"], totals["cache_read"]), 5)})
        text = "".join(b.text for b in response.content if b.type == "text")
        if web:
            return extract_json(text)
        return json.loads(text)
