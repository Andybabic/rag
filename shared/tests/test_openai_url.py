"""OpenAI-compatible base-URL normalization."""

from shared.llm.providers.openai import normalize_openai_base_url


def test_host_only_gets_v1():
    assert (
        normalize_openai_base_url("https://nemotron.sparklab.media.ustp.at/")
        == "https://nemotron.sparklab.media.ustp.at/v1"
    )
    assert (
        normalize_openai_base_url("https://nemotron.sparklab.media.ustp.at")
        == "https://nemotron.sparklab.media.ustp.at/v1"
    )


def test_existing_v1_unchanged():
    assert (
        normalize_openai_base_url("https://api.openai.com/v1")
        == "https://api.openai.com/v1"
    )
    assert (
        normalize_openai_base_url("http://litellm:4000/v1/")
        == "http://litellm:4000/v1"
    )


def test_azure_style_path_unchanged():
    url = "https://my.openai.azure.com/openai/deployments/gpt"
    assert normalize_openai_base_url(url) == url


def test_empty():
    assert normalize_openai_base_url("") == ""
    assert normalize_openai_base_url("   ") == ""
