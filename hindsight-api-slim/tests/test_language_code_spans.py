"""Real-detector regressions for language-integrity code exemptions."""

import pytest
from hindsight_api.engine import language_integrity as guard

from tests.test_language_prevention import ENGLISH, SPANISH
from tests.test_language_prevention_review import check


@pytest.mark.parametrize(
    "output",
    [
        "`El equipo completó la revisión (incluyendo las pruebas) y preservará los datos originales para la próxima reunión.`",
        "`El equipo completó la revisión palabra(incluida) y preservará los datos originales para la próxima reunión.`",
        "```text\nconst status = 'ready';\nEl equipo completó la revisión y preservará los datos originales para la próxima reunión.\n```",
        "`L’équipe continue la vérification des résultats et prépare les documents pour la prochaine réunion.`",
        "`const status = 'ready'; El equipo completó la revisión y preservará los datos originales.`",
    ],
)
def test_foreign_prose_inside_code_delimiters_is_not_exempt(output):
    assert guard.enforcement_failures(check(ENGLISH, output), guard.LanguageIntegrityMode.REJECT)


@pytest.mark.parametrize(
    "output",
    [
        "`print('hello')`",
        "`const 标签 = '这是示例代码中的文本内容';`",
        "```python\ndef greeting(name):\n    return f'hello {name}'\n```",
    ],
)
def test_recognizable_code_spans_remain_exempt(output):
    assert not guard.enforcement_failures(check(ENGLISH, output), guard.LanguageIntegrityMode.REJECT)


def test_untagged_fence_keeps_foreign_first_body_line_for_rejection():
    foreign = "L’équipe continue la vérification des résultats et prépare les documents pour la prochaine réunion."
    output = f"```\n{foreign}\nprint('hello')\n```"

    assert foreign in guard._without_code(output)
    assert guard.enforcement_failures(check(ENGLISH, output), guard.LanguageIntegrityMode.REJECT)


@pytest.mark.parametrize(
    "wrapper",
    [
        "`{prose}`",
        "```{prose}```",
        "```{prose}\n```",
        "```{prose}\r\n```",
        "```{prose}\nprint('hello')\n```",
        "```\n{prose}\n```",
        "```text\n{prose}\n```",
        "```\r\n{prose}\r\n```",
    ],
)
def test_backtick_delimiters_preserve_novel_foreign_prose(wrapper):
    foreign = "L’équipe continue la vérification des résultats et prépare les documents pour la prochaine réunion."
    output = wrapper.format(prose=foreign)

    assert guard.enforcement_failures(check(ENGLISH, output), guard.LanguageIntegrityMode.REJECT)
    assert foreign in guard._without_code(output)


def test_single_line_fenced_genuine_code_remains_exempt():
    output = "```print('hello')```"

    assert not guard.enforcement_failures(check(ENGLISH, output), guard.LanguageIntegrityMode.REJECT)


@pytest.mark.parametrize(
    "output",
    [
        "```\nprint('hello')\n```",
        "```python\ndef greeting(name):\n    return f'hello {name}'\n```",
        "```\n\nprint('hello')\n```",
        "```\r\nprint('hello')\r\n```",
    ],
)
def test_fenced_code_with_optional_info_and_blank_or_crlf_lines_remains_exempt(output):
    assert not guard.enforcement_failures(check(ENGLISH, output), guard.LanguageIntegrityMode.REJECT)


@pytest.mark.parametrize(
    "output",
    [
        "`await client.fetch_records()`",
        "`report.save()`",
        "```python\nfor record in records:\n    report.save(record)\n```",
        '```json\n{"message": "这是示例代码中的文本内容", "ready": true}\n```',
        '```\n{\n  "message": "这是示例代码中的文本内容"\n}\n```',
    ],
    ids=["await-call", "method-call", "python-loop", "json-strings", "untagged-json"],
)
def test_syntax_validated_code_literals_are_not_terminal_rejects(output: str) -> None:
    result = check(ENGLISH, output)

    assert result.verdicts[0].status == "preserved"
    assert not guard.enforcement_failures(result, guard.LanguageIntegrityMode.REJECT)


@pytest.mark.parametrize(
    "output",
    [
        "`report.save(); " + SPANISH + "`",
        "```python\nfor record in records:\n    report.save(record)\n" + SPANISH + "\n```",
        '```json\n{"message": "这是示例代码中的文本内容"}\n' + SPANISH + "\n```",
        "```python\nreport.save()\n" + SPANISH + "\n```",
        '```python\nreport.save()\n"' + SPANISH + '"\n```',
        '```json\n"' + SPANISH + '"\n```',
        '`report.save(); "' + SPANISH + '"`',
        "```\n说明:团队已经完成了全部检查并且保存了所有原始文件\n备注:下次会议将继续讨论这些结果和后续安排\n```",
        "`团队已经完成了全部检查并且保存了所有原始文件以便下次会议使用(全部)`",
        "`チームは確認を完了し元のファイルを保存しました(次回の会議用)`",
    ],
    ids=[
        "call-prefix-prose",
        "loop-prose",
        "json-prose",
        "call-prose",
        "bare-string-after-call",
        "json-scalar-prose",
        "call-and-string-same-line",
        "chinese-annotated-prose",
        "chinese-call-prose",
        "japanese-call-prose",
    ],
)
def test_code_syntax_or_tag_does_not_exempt_adjacent_foreign_prose(output: str) -> None:
    result = check(ENGLISH, output)

    assert guard.enforcement_failures(result, guard.LanguageIntegrityMode.REJECT)


@pytest.mark.parametrize(
    "code",
    [
        "说明:int",
        "description:团队已经完成了全部检查",
        "团队已经完成了全部检查(全部)",
        "report.团队已经完成了全部检查()",
        "report.save(团队已经完成了全部检查=True)",
        "def save(团队已经完成了全部检查): pass",
        "import 团队已经完成了全部检查",
        "global 团队已经完成了全部检查",
        "from 团队已经完成了全部检查 import save",
        "import report as 团队已经完成了全部检查",
        "def 团队已经完成了全部检查(): pass",
        "class 团队已经完成了全部检查: pass",
        "match report:\n    case {**团队已经完成了全部检查}: pass",
        "match report:\n    case Report(团队已经完成了全部检查=True): pass",
    ],
    ids=[
        "annotated-target",
        "annotation",
        "call-name",
        "attribute",
        "keyword",
        "argument",
        "import",
        "global",
        "import-module",
        "import-alias",
        "function-name",
        "class-name",
        "pattern-rest",
        "pattern-attribute",
    ],
)
def test_python_unicode_identifiers_are_not_syntax_exemption_authority(code: str) -> None:
    assert not guard._is_syntax_code(code)


@pytest.mark.parametrize("wrapper", ["`{code}`", "```python\n{code}\n```"], ids=["inline", "python-fence"])
def test_foreign_string_call_arguments_are_deliberately_exempt_data(wrapper: str) -> None:
    output = wrapper.format(code=f'note("{SPANISH}")')
    result = check(ENGLISH, output)

    assert result.verdicts[0].status == "preserved"
    assert not guard.enforcement_failures(result, guard.LanguageIntegrityMode.REJECT)


@pytest.mark.parametrize(
    "output",
    [
        f"`report.save()  # {SPANISH}`",
        f"```python\nreport.save()\n# {SPANISH}\n```",
        f"`x = 1  # {SPANISH}`",
    ],
    ids=["call-inline-comment", "python-fence-comment-line", "assignment-inline-comment"],
)
def test_foreign_python_comments_remain_language_checked(output: str) -> None:
    result = check(ENGLISH, output)

    assert SPANISH in guard._without_code(output)
    assert result.verdicts[0].status == "mismatch"
    assert guard.enforcement_failures(result, guard.LanguageIntegrityMode.REJECT)


@pytest.mark.parametrize("wrapper", ["`{code}`", "```python\n{code}\n```"], ids=["inline", "python-fence"])
def test_english_python_comment_passes_without_exposing_string_data(wrapper: str) -> None:
    comment = "The team completed the review and preserved the original files for the next meeting."
    output = wrapper.format(code=f'note("{SPANISH} # literal data")  # {comment}')
    result = check(ENGLISH, output)

    assert comment in guard._without_code(output)
    assert SPANISH not in guard._without_code(output)
    assert not guard.enforcement_failures(result, guard.LanguageIntegrityMode.REJECT)


def test_fenced_mixed_code_and_foreign_prose_is_not_exempt():
    output = "```python\nprint('hello')\nL’équipe continue la vérification des résultats et prépare les documents.\n```"

    assert guard.enforcement_failures(check(ENGLISH, output), guard.LanguageIntegrityMode.REJECT)
