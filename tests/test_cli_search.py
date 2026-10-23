from splice_curator import cli
from splice_curator.catalog import Catalog
from splice_curator.client import AuthRejected, RateLimited
from splice_curator.config import Delay


def test_search_command_records_all_pages(tmp_path, fake_client, cli_env):
    code = cli.main(["--config", str(cli_env), "search", "--group", "kicks", "--pages", "2"])
    assert code == 0
    searches = [c for c in fake_client.calls if c[0] == "search"]
    assert len(searches) == 6
    assert {c[1] for c in searches} == {"house kick", "tech house kick", "deep house kick"}
    assert all(c[2] == "oneshot" for c in searches)
    rows = Catalog(tmp_path / "data" / "catalog.sqlite").rows_for_group("kicks")
    assert len(rows) == 12


def test_search_single_query(tmp_path, fake_client, cli_env):
    assert cli.main(["--config", str(cli_env), "search", "--group", "drum_loops",
                     "--pages", "1", "--query", "groovy top"]) == 0
    assert [c[1:3] for c in fake_client.calls] == [("groovy top", "loop")]


def test_search_unknown_group(cli_env, capsys):
    assert cli.main(["--config", str(cli_env), "search", "--group", "nope"]) == 1
    assert "unknown group" in capsys.readouterr().err


def test_search_stops_on_rate_limit(tmp_path, fake_client, cli_env, capsys):
    fake_client.search_error = RateLimited("search: HTTP 429; gave up after 4 tries")
    assert cli.main(["--config", str(cli_env), "search", "--group", "kicks"]) == 1
    assert len([c for c in fake_client.calls if c[0] == "search"]) == 1
    assert "429" in capsys.readouterr().err


def test_auth_command_prints_credits_not_token(capsys, fake_client, cli_env, token, monkeypatch):
    monkeypatch.setenv("SPLICE_TOKEN", token)
    assert cli.main(["--config", str(cli_env), "auth"]) == 0
    out = capsys.readouterr()
    assert "3360" in out.out
    assert "tester" in out.out
    assert token not in out.out + out.err


def test_auth_rejected_exit_code(fake_client, cli_env, capsys):
    fake_client.account_error = AuthRejected("Splice rejected the session; log in to splice.com again")
    assert cli.main(["--config", str(cli_env), "auth"]) == 2
    assert "log in to splice.com again" in capsys.readouterr().err


def test_missing_token_exit_code(cli_env, monkeypatch, capsys):
    monkeypatch.undo()  # real build_client
    monkeypatch.delenv("SPLICE_TOKEN", raising=False)
    assert cli.main(["--config", str(cli_env), "auth"]) == 2
    assert "SPLICE_TOKEN" in capsys.readouterr().err


def test_global_overrides(cli_env):
    config = cli.load_cli_config(cli.build_parser().parse_args(
        ["--config", str(cli_env), "--max-per-day", "50", "--delay", "purchase=10:20",
         "--delay", "search=1:2", "auth"]))
    assert config.max_purchases_per_day == 50
    assert config.delays["purchase"] == Delay(10, 20)
    assert config.delays["search"] == Delay(1, 2)


def test_bad_delay_flag(cli_env, capsys):
    assert cli.main(["--config", str(cli_env), "--delay", "purchase=oops", "auth"]) == 1
