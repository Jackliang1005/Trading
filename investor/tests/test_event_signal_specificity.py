from domain.services import event_service
from domain.services.report_style_service import event_summary_cn


def test_english_daily_open_digest_is_not_scored_as_one_event():
    raw = event_service.RawEvent(
        title="CNBC Daily Open: Iran, Oman in talks on Hormuz; SpaceX is loyal to Nvidia on AI",
        summary="Oil cooled while chip demand remained strong.",
    )

    event = event_service.analyze_event(raw, positions=[])

    assert event["is_multi_story_digest"] is True
    assert event["themes"] == []
    assert event["score"] == 0
    assert event["should_push"] is False


def test_english_macro_headlines_keep_the_specific_catalyst():
    assert event_summary_cn(
        "U.S. Treasury yields fall as oil prices plunge on Iran de-escalation hopes",
        ["Global Macro", "Energy Commodities", "Geopolitics"],
    ) == "伊朗局势缓和预期推动油价与美债收益率回落"
    assert event_summary_cn(
        "Russia sanctions bill clears Senate, targeting Russian oil purchasers",
        ["Energy Commodities", "Geopolitics"],
    ) == "美国参议院推进对俄罗斯的新制裁法案"
    assert event_summary_cn(
        "U.S. Treasury yields fall as traders monitor possible Iran war deal",
        ["Global Macro", "Geopolitics"],
    ) == "伊朗局势变化牵动美国国债收益率"
    assert event_summary_cn(
        "Oil rises as Iran's draft plan sees U.S. and Israel banned from Strait of Hormuz",
        ["Energy Commodities", "Geopolitics"],
    ) == "伊朗霍尔木兹海峡限制方案推升石油供应担忧"
