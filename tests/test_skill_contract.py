"""Static contract checks for the Agent Skill shell around the Python CLI."""

from pathlib import Path

ROOT = Path(__file__).parents[1]
SKILLS = [
    ROOT / "skills" / "seo-writer" / "SKILL.md",
    ROOT / "skills" / "seo-writer-onboarding" / "SKILL.md",
]


def test_skill_shells_call_the_cli_with_json_and_isolated_data_dir() -> None:
    for path in SKILLS:
        text = path.read_text(encoding="utf-8")
        assert "seo-writer" in text
        assert "--data-dir" in text
        assert "--workspace" in text
        assert "--json" in text
        assert "stdout" in text
        assert "stderr" in text
        assert "exit" in text.lower()


def test_skill_shell_declares_local_only_credential_boundaries() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in SKILLS).lower()
    assert "temporary" in combined
    assert "synthetic" in combined
    assert "real" in combined and "oauth" in combined
    assert "provider credentials" in combined


def test_production_skill_requires_user_configured_real_research() -> None:
    onboarding = (ROOT / "skills" / "seo-writer-onboarding" / "SKILL.md").read_text(encoding="utf-8")
    production = (ROOT / "skills" / "seo-writer" / "SKILL.md").read_text(encoding="utf-8")
    policy = (ROOT / "examples" / "brand-packs" / "policy-real.template.yaml").read_text(encoding="utf-8")
    combined = f"{onboarding}\n{production}".lower()
    assert "providers configure --name dataforseo" in combined
    assert "providers configure --name reddit" in combined
    assert "providers status" in combined
    assert "never silently falls back to mock data" in combined
    assert "name: dataforseo" in policy
    assert "name: reddit" in policy
    assert "name: http" in policy
