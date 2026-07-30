"""hermes-severance-v1 T2 — the retired-platform SET, single source of truth.

Extracted from ``tests/gateway/test_retired_platforms.py`` so the retirement
pin and the platform↔toolset parity guard
(``tests/hermes_cli/test_tools_config.py::TestPlatformToolsetConsistency``)
key on ONE constant. A second copy would drift; this module is the sole
definition (SPEC v2 rider-1, two-directional parity ruling).

Underscore-prefixed → pytest does not collect it as a test module; importing
``RETIRED`` / ``KEPT`` from here adds zero collected nodes.

Every platform adapter except telegram, api_server, and tty/local (LOCAL) was
excised at the T2 adapter severance. The ``Platform`` enum members are RETAINED
as inert vocabulary (~318 kept-code guards still reference them) but no adapter
exists any more, so ``_create_adapter`` returns ``None`` for each retired
member. Keep this in sync with the enum note in ``gateway/config.py``.
"""
from gateway.config import Platform

# The retired set — enumerated explicitly by ruling.
RETIRED = [
    Platform.DISCORD,
    Platform.WHATSAPP,
    Platform.SLACK,
    Platform.SIGNAL,
    Platform.HOMEASSISTANT,
    Platform.EMAIL,
    Platform.SMS,
    Platform.DINGTALK,
    Platform.FEISHU,
    Platform.WECOM,
    Platform.WECOM_CALLBACK,
    Platform.WEIXIN,
    Platform.MATTERMOST,
    Platform.MATRIX,
    Platform.WEBHOOK,
    Platform.MSGRAPH_WEBHOOK,
    Platform.BLUEBUBBLES,
    Platform.QQBOT,
    Platform.YUANBAO,
]

# The kept adapters (adapter code survives severance).
KEPT = {Platform.LOCAL, Platform.TELEGRAM, Platform.API_SERVER}
