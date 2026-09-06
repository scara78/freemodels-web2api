"""Minimal OpenAI SDK example for a locally running adapter."""

from openai import OpenAI


client = OpenAI(
    base_url="http://127.0.0.1:8082/v1",
    api_key="local-test",
)

stream = client.chat.completions.create(
    model="gpt-5.6-terra",
    messages=[{"role": "user", "content": "Say hello in one short sentence."}],
    stream=True,
)

for chunk in stream:
    text = chunk.choices[0].delta.content
    if text:
        print(text, end="", flush=True)
print()
