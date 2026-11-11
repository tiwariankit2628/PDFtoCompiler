"""Helpers shared by question workflow tests."""


def response_body(response):
    """Consume streaming test-client responses through Django's close wrapper.

    Calling response.close() directly fires request_finished without the test
    client's connection guard and can close a file-backed SQLite connection
    in the surrounding TestCase transaction. Non-streaming responses are
    already closed by the client.
    """
    if response.streaming:
        return b"".join(response.streaming_content)
    return response.content
