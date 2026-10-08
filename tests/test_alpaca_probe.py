"""probe-alpaca (docs/ALPACA.md "The probe"): the credentials loader pins the base URL to
Alpaca's trading hosts, the probe only sends GETs with the key headers, the key and the
secret never appear in its output, the account's id and number are never printed, and
an endpoint that answers with stocks is never reported as event contracts. No network,
no database: a fake transport answers by URL."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from host.exchange import alpaca_credentials as ac
from host.exchange import alpaca_probe, cli
from host.exchange.adapters.base import SourceError

KEY = "PKTESTKEY000000000000WXYZ"
SECRET = "s3cr3t-value-that-must-never-print-0123456789"
NOW = datetime(2026, 10, 8, 14, 0, 0, tzinfo=timezone.utc)
DATE = "Thu, 08 Oct 2026 14:00:02 GMT"
ACCOUNT = {"id": "acc-uuid-1", "account_number": "PA123456789", "status": "ACTIVE", "crypto_status": "ACTIVE",
           "currency": "USD", "cash": "100000", "buying_power": "200000", "pattern_day_trader": False,
           "trading_blocked": False, "echo": f"{KEY} {SECRET}"}


def env(**extra: str) -> dict[str, str]:
    return {ac.KEY_VAR: KEY, ac.SECRET_VAR: SECRET, **extra}


class FakeAlpaca:
    """Answers (status, headers, body) by the first route whose text is in the URL; the
    routes a test passes are tried before the defaults."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []
        self.routes: dict[str, Any] = dict(routes or {})
        defaults: dict[str, Any] = {
            "/v2/account": (200, json.dumps(ACCOUNT)),
            "/v2/clock": (200, json.dumps({"timestamp": "2026-10-08T10:00:00-04:00", "is_open": True})),
            "/v2/assets/AAPL": (200, json.dumps({"symbol": "AAPL", "class": "us_equity"})),
            "asset_class=crypto": (200, json.dumps([{"symbol": "BTC/USD", "class": "crypto"}, {"symbol": "ETH/USD", "class": "crypto"}])),
            "/v2/options/contracts": (200, json.dumps({"option_contracts": [{"symbol": "SPY261016C00500000"}]})),
            "asset_class=": (422, json.dumps({"message": "invalid asset_class"})),
            "data.alpaca.markets": (200, json.dumps({"quote": {"ap": 1.0}})),
        }
        for needle, answer in defaults.items():
            self.routes.setdefault(needle, answer)

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float):
        self.calls.append((method, url, headers, body))
        for needle, answer in self.routes.items():
            if needle in url:
                if isinstance(answer, Exception):
                    raise answer
                status, text = answer
                return status, {"Date": DATE}, text
        return 404, {}, '{"message": "not found"}'


def probe(fake: FakeAlpaca, creds: ac.AlpacaCredentials | None = None, **kw: Any) -> dict[str, Any]:
    return alpaca_probe.run(creds or ac.load(env()), http=fake, now=lambda: NOW, **kw)


# ------------------------------------------------------------------ credentials


def test_load_needs_both_values_and_defaults_to_paper() -> None:
    assert ac.load({}) is None
    assert ac.load({ac.KEY_VAR: KEY}) is None
    assert ac.load({ac.KEY_VAR: KEY, ac.SECRET_VAR: "  "}) is None
    creds = ac.load(env())
    assert creds.base_url == ac.PAPER_URL and creds.environment == "paper" and creds.key_hint == "WXYZ"


@pytest.mark.parametrize("url, base, environment", [
    ("https://paper-api.alpaca.markets/v2", ac.PAPER_URL, "paper"),
    ("https://paper-api.alpaca.markets/", ac.PAPER_URL, "paper"),
    ("https://API.alpaca.markets/v2/", ac.LIVE_URL, "live"),
])
def test_base_url_accepts_alpaca_hosts_with_or_without_v2(url: str, base: str, environment: str) -> None:
    creds = ac.load(env(**{ac.BASE_URL_VAR: url}))
    assert (creds.base_url, creds.environment) == (base, environment)


@pytest.mark.parametrize("url", [
    "http://paper-api.alpaca.markets", "https://evil.example/v2", "https://paper-api.alpaca.markets.evil.example",
    "https://user:pw@api.alpaca.markets", "https://api.alpaca.markets/v2?x=1", "https://api.alpaca.markets/other",
    "https://data.alpaca.markets", "https://api.alpaca.markets:8443",
])
def test_base_url_refuses_anything_else(url: str) -> None:
    with pytest.raises(ac.AlpacaConfigError) as err:
        ac.load(env(**{ac.BASE_URL_VAR: url}))
    assert SECRET not in str(err.value) and KEY not in str(err.value)


def test_repr_and_redact_hide_the_key_and_the_secret() -> None:
    creds = ac.load(env())
    assert SECRET not in repr(creds) and KEY not in repr(creds) and "WXYZ" in repr(creds)
    assert creds.redact(f"a {KEY} b {SECRET}") == "a ***WXYZ b ***WXYZ"


# ------------------------------------------------------------------ the probe


def test_probe_sends_only_gets_with_the_key_headers_to_alpaca_hosts() -> None:
    fake = FakeAlpaca()
    result = probe(fake)
    assert fake.calls and all(method == "GET" and body is None for method, _, _, body in fake.calls)
    hosts = {url.split("/")[2] for _, url, _, _ in fake.calls}
    assert hosts == {"paper-api.alpaca.markets", "data.alpaca.markets"}
    assert all(h["APCA-API-KEY-ID"] == KEY and h["APCA-API-SECRET-KEY"] == SECRET for _, _, h, _ in fake.calls)
    assert any("feed=iex" in url for _, url, _, _ in fake.calls), "stock quotes use the free IEX feed"
    assert result["verdict"]["keys"].startswith("ok (paper account")
    assert result["account"]["summary"]["buying_power"] == "200000"
    assert result["clock"]["clock_skew_ms"] == 2000
    assert result["crypto_assets"]["count"] == 2 and result["crypto_assets"]["sample"] == ["BTC/USD", "ETH/USD"]
    assert result["options_contracts"]["count"] == 1
    assert "not found" in result["verdict"]["event_contracts"]


def test_probe_output_never_carries_secrets_or_account_identity() -> None:
    fake = FakeAlpaca({"/v2/assets/AAPL": (200, f'{{"echo": "{KEY} {SECRET}"}}')})
    for raw in (False, True):
        text = json.dumps(probe(fake, raw=raw))
        assert KEY not in text and SECRET not in text
        assert "PA123456789" not in text and "acc-uuid-1" not in text


def test_rejected_keys_say_what_to_check() -> None:
    result = probe(FakeAlpaca({"/v2/account": (403, '{"message": "forbidden."}')}))
    assert result["verdict"]["keys"].startswith("rejected (403)") and "paper-api" in result["verdict"]["keys"]
    assert result["account"]["status"] == 403 and "forbidden" in result["account"]["payload"]


def test_transport_failures_are_recorded_per_check_and_redacted() -> None:
    result = probe(FakeAlpaca({"/v2/clock": SourceError(f"GET failed for {KEY}")}))
    assert result["clock"]["error"] == "GET failed for ***WXYZ" and result["account"]["status"] == 200


def test_event_contracts_found_under_a_guess_or_an_extra_asset_class() -> None:
    contracts = json.dumps([{"symbol": "KXNFLGAME-26OCT11-KC", "class": "event_contract"}])
    result = probe(FakeAlpaca({"asset_class=event_contract": (200, contracts)}))
    assert result["event_contract_guesses"]["event_contract"]["count"] == 1
    assert result["verdict"]["event_contracts"].startswith("found under event_contract")
    result = probe(FakeAlpaca({"asset_class=sports": (200, contracts)}), asset_classes=["sports"])
    assert result["verdict"]["event_contracts"].startswith("found under sports")


def test_an_endpoint_answering_with_stocks_is_not_event_contracts() -> None:
    stocks = json.dumps([{"symbol": "AAPL", "class": "us_equity"}, {"symbol": "MSFT", "class": "us_equity"}])
    result = probe(FakeAlpaca({"asset_class=event": (200, stocks)}))
    guess = result["event_contract_guesses"]["event"]
    assert guess["count"] == 0 and guess["known_class_records_ignored"] == 2
    assert "not found" in result["verdict"]["event_contracts"]


def test_extra_get_paths_go_to_the_right_host_and_unsafe_ones_are_refused() -> None:
    fake = FakeAlpaca({"/v2/events": (200, json.dumps({"events": [{"id": "e1"}]}))})
    result = probe(fake, gets=["/v2/events", "data:/v1beta1/events"])
    urls = [url for _, url, _, _ in fake.calls]
    assert "https://paper-api.alpaca.markets/v2/events" in urls and "https://data.alpaca.markets/v1beta1/events" in urls
    assert result["extra"]["/v2/events"]["count"] == 1
    assert "not found" in result["verdict"]["event_contracts"], "a --get answer never decides the verdict"
    for bad in ("https://evil.example/x", "v2/x", "//evil.example/x", "/v2/../x", "data:https://evil.example"):
        with pytest.raises(ValueError):
            probe(FakeAlpaca(), gets=[bad])


# ------------------------------------------------------------------ the CLI


def run_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], values: dict[str, str],
            fake: FakeAlpaca | None = None, *args: str) -> tuple[int, str, str]:
    for name in (ac.KEY_VAR, ac.SECRET_VAR, ac.BASE_URL_VAR):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(alpaca_probe, "urllib_http", fake or FakeAlpaca())
    code = cli.main(["probe-alpaca", *args])
    out, err = capsys.readouterr()
    return code, out, err


def test_cli_without_keys_or_with_a_bad_url_exits_1(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run_cli(monkeypatch, capsys, {})
    assert code == 1 and "no Alpaca keys" in err and ac.KEY_VAR in err
    code, _, err = run_cli(monkeypatch, capsys, env(**{ac.BASE_URL_VAR: "https://evil.example"}))
    assert code == 1 and "not an Alpaca trading host" in err and SECRET not in err


def test_cli_prints_the_probe_and_exits_by_the_account_answer(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run_cli(monkeypatch, capsys, env(), None, "--asset-class", "sports")
    assert code == 0 and json.loads(out)["verdict"]["keys"].startswith("ok") and "sports" in out
    assert KEY not in out and SECRET not in out
    code, out, _ = run_cli(monkeypatch, capsys, env(), FakeAlpaca({"/v2/account": (401, "{}")}))
    assert code == 1 and "rejected (401)" in out
    code, _, err = run_cli(monkeypatch, capsys, env(), None, "--get", "https://evil.example")
    assert code == 1 and "--get needs a path" in err
