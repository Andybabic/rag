from config import Settings


def test_defaults():
    s = Settings()
    assert s.USE_MINERU is False
    assert s.MINERU_API_URL == "http://mineru:8000"
    assert s.BATCH_PARSE_WORKERS == 2
    assert s.BATCH_CHUNK_WORKERS == 4
    assert s.MAX_RETRIES == 3
    assert s.LOG_LEVEL == "INFO"
