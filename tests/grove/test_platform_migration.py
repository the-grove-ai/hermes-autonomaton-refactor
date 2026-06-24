from grove.capability_registry import load_capabilities

def test_discord_capability_platform_is_discord_only():
    caps = load_capabilities()
    discord_cap = caps.get("discord")
    assert discord_cap is not None, "discord capability record missing"
    assert discord_cap.platform == ["discord"], (
        f"Expected platform=['discord'], got {discord_cap.platform!r}"
    )

def test_discord_admin_capability_platform_is_discord_only():
    caps = load_capabilities()
    discord_admin_cap = caps.get("discord_admin")
    assert discord_admin_cap is not None, "discord_admin capability record missing"
    assert discord_admin_cap.platform == ["discord"], (
        f"Expected platform=['discord'], got {discord_admin_cap.platform!r}"
    )
