"""Unit 07 acceptance tests: the --json envelope shape and exit-code values,
independent of any subcommand."""

import json

from tscribe.cli.output import ExitCode, emit, error, success


def test_success_envelope_shape():
    assert success({"a": 1}) == {"status": "success", "data": {"a": 1}}


def test_success_defaults_to_empty_data():
    assert success() == {"status": "success", "data": {}}


def test_error_envelope_shape():
    assert error("bad", ExitCode.USAGE) == {"status": "error", "code": 2, "message": "bad"}


def test_exit_codes_match_the_documented_contract():
    """docs/units/07-cli-surface-and-exit-codes.md's table, verbatim."""
    assert ExitCode.SUCCESS == 0
    assert ExitCode.USAGE == 2
    assert ExitCode.CONFIG_INVALID == 3
    assert ExitCode.BACKEND_UNREACHABLE == 4
    assert ExitCode.CONTRACT_VIOLATED == 5
    assert ExitCode.JOB_NOT_FOUND == 6
    assert ExitCode.INTERRUPTED == 7


def test_emit_json_success_goes_to_stdout(capsys):
    emit(success({"a": 1}), json_mode=True)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "success", "data": {"a": 1}}
    assert captured.err == ""


def test_emit_json_error_is_still_stdout_with_status_error(capsys):
    """--json callers parse stdout for the envelope regardless of status --
    `status: error` in the payload is the signal, not which stream it
    arrived on."""
    emit(error("bad", ExitCode.USAGE), json_mode=True)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "error", "code": 2, "message": "bad"}


def test_emit_human_success_prefers_provided_string(capsys):
    emit(success({"a": 1}), json_mode=False, human="hello world")
    assert capsys.readouterr().out.strip() == "hello world"


def test_emit_human_success_default_rendering(capsys):
    emit(success({"a": 1, "b": 2}), json_mode=False)
    out = capsys.readouterr().out
    assert "a: 1" in out
    assert "b: 2" in out


def test_emit_human_error_goes_to_stderr_not_stdout(capsys):
    emit(error("bad thing", ExitCode.USAGE), json_mode=False)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "bad thing" in captured.err
