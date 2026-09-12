"""Real-detector regressions for language-integrity code exemptions."""

import pytest

from hindsight_api.engine import language_integrity as guard
from tests.test_language_prevention import ENGLISH
from tests.test_language_prevention_review import check


@pytest.mark.parametrize(
    "output",
    [
        "`El equipo completó la revisión (incluyendo las pruebas) y preservará los datos originales para la próxima reunión.`",
        "`El equipo completó la revisión palabra(incluida) y preservará los datos originales para la próxima reunión.`",
        "```text\nconst status = 'ready';\nEl equipo completó la revisión y preservará los datos originales para la próxima reunión.\n```",
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
