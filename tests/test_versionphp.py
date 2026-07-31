import textwrap

from camp.versionphp import parse_dependencies


def php(body: str) -> str:
    return "<?php\ndefined('MOODLE_INTERNAL') || die();\n" + textwrap.dedent(body)


def test_short_array_syntax():
    text = php("""\
        $plugin->component = 'mod_example';
        $plugin->dependencies = [
            'mod_forum' => 2024042200,
            'local_helper' => 2023111300,
        ];
        """)
    assert parse_dependencies(text) == {
        "mod_forum": 2024042200,
        "local_helper": 2023111300,
    }


def test_legacy_array_syntax():
    text = php("""\
        $plugin->dependencies = array(
            'mod_quiz' => 2022041900,
        );
        """)
    assert parse_dependencies(text) == {"mod_quiz": 2022041900}


def test_single_line_declaration():
    text = php("$plugin->dependencies = ['mod_book' => 2021051700];\n")
    assert parse_dependencies(text) == {"mod_book": 2021051700}


def test_any_version_constant_and_string():
    text = php("""\
        $plugin->dependencies = [
            'mod_forum' => ANY_VERSION,
            'mod_wiki' => 'any',
            'mod_glossary' => "any",
        ];
        """)
    assert parse_dependencies(text) == {
        "mod_forum": "any", "mod_wiki": "any", "mod_glossary": "any",
    }


def test_module_style_declaration():
    text = php("$module->dependencies = array('mod_data' => 2020061500);\n")
    assert parse_dependencies(text) == {"mod_data": 2020061500}


def test_comments_and_trailing_comma():
    text = php("""\
        $plugin->dependencies = [
            // needs the parent activity
            'mod_quiz' => 2023042400,  /* pinned for question API */
            # legacy hash comment
            'qbehaviour_deferredfeedback' => ANY_VERSION,
        ];
        """)
    assert parse_dependencies(text) == {
        "mod_quiz": 2023042400,
        "qbehaviour_deferredfeedback": "any",
    }


def test_no_declaration():
    assert parse_dependencies(php("$plugin->version = 2026011500;\n")) == {}


def test_empty_array():
    assert parse_dependencies(php("$plugin->dependencies = [];\n")) == {}
    assert parse_dependencies(php("$plugin->dependencies = array();\n")) == {}


def test_invalid_key_dropped():
    text = php("""\
        $plugin->dependencies = [
            'NotFrankenstyle' => 2023042400,
            'mod_ok' => 2023042400,
        ];
        """)
    assert parse_dependencies(text) == {"mod_ok": 2023042400}


def test_computed_value_dropped():
    text = php("""\
        $plugin->dependencies = [
            'mod_weird' => $someversion,
            'mod_ok' => 2023042400,
        ];
        """)
    assert parse_dependencies(text) == {"mod_ok": 2023042400}


def test_pathological_body_parses_empty_not_wrong():
    # Nested structure the regex cannot read: the non-greedy block match
    # stops at the inner closer and the malformed remainder yields no pairs
    # it can misread as different components' versions.
    text = php("""\
        $plugin->dependencies = [
            'mod_a' => [2023042400],
        ];
        """)
    assert parse_dependencies(text).get("mod_a") is None


def test_dependency_declared_after_other_fields():
    text = php("""\
        $plugin->version = 2026011500;
        $plugin->dependencies = ['tool_camp' => 2025070100];
        $plugin->release = '1.2.3';
        """)
    assert parse_dependencies(text) == {"tool_camp": 2025070100}
