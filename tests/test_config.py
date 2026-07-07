from app.core.config import settings


def test_defaults_present():
    assert settings.APP_NAME == "nbdpsy-mcp"
    assert settings.PUBLISH_CONCURRENCY >= 1
    assert settings.retry_delays == [120, 600, 1800]
